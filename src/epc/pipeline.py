"""The run: list, fetch, classify, plan, dispatch.

Concurrency is threads, not processes. The work is HTTP wait from end to end, so
processes bought nothing and cost a great deal: the classifier had to be
picklable, which is why the API clients were module globals; every run paid to
spawn workers; and every child opened the same rotating log file.

Rate limiting paces individual requests rather than sleeping between blocks, so
one slow response no longer stalls every worker.
"""

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from epc.actions.model import ThreadMutation
from epc.actions.planner import plan_mutation
from epc.classify.base import Classifier, Usage
from epc.classify.budget import build_payload
from epc.dispatch.applier import ApplyReport
from epc.dispatch.sink import MutationSink
from epc.errors import ClassificationError, GmailError
from epc.gmail.client import GmailClient
from epc.gmail.mime import parse_thread
from epc.gmail.models import EmailThread
from epc.gmail.query import build_search_query
from epc.priority import Priority
from epc.ratelimit import RateLimiter
from epc.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """What a run did. Printed at the end and used to pick an exit code."""

    listed: int = 0
    already_labelled: int = 0
    fetch_failed: int = 0
    classified: int = 0
    classify_failed: int = 0
    suspicious: int = 0
    by_priority: dict[Priority, int] = field(default_factory=lambda: dict.fromkeys(Priority, 0))
    usage: Usage = field(default_factory=Usage)
    apply: ApplyReport = field(default_factory=ApplyReport)

    @property
    def had_failures(self) -> bool:
        return bool(self.fetch_failed or self.classify_failed or self.apply.had_failures)

    def render(self) -> str:
        lines = [
            "=== Summary ===",
            f"  listed            {self.listed}",
            f"  already labelled  {self.already_labelled}  (skipped, no LLM call)",
            f"  classified        {self.classified}"
            f"  (failed: {self.classify_failed}, unfetchable: {self.fetch_failed})",
            "    " + "  ".join(f"{p.value}: {self.by_priority[p]}" for p in Priority),
            f"  suspicious        {self.suspicious}",
            f"  tokens            in {self.usage.input_tokens:,} / out {self.usage.output_tokens:,}",
            f"  applied           {self.apply.applied}"
            f"  (no-op: {self.apply.skipped_noop}, failed: {self.apply.failed})",
            f"  gmail write calls {self.apply.api_calls}",
        ]
        lines.extend(f"  ! {failure}" for failure in self.apply.failures)
        return "\n".join(lines)


class Pipeline:
    """One batch: everything from a search query to a closed sink."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: GmailClient,
        classifier: Classifier,
        sink: MutationSink,
        priority_label_ids: dict[Priority, str],
    ) -> None:
        self._settings = settings
        self._client = client
        self._classifier = classifier
        self._sink = sink
        self._priority_label_ids = priority_label_ids
        self._limiter = RateLimiter(settings.llm.requests_per_min)
        self._priority_label_id_set = set(priority_label_ids.values())

    def search_query(self) -> str:
        return build_search_query(
            base=self._settings.gmail.query,
            extra=self._settings.gmail.extra_query,
            exclude_labels=list(self._settings.labels.by_priority.values()),
        )

    def run(self) -> RunSummary:
        summary = RunSummary()
        threads = self._hydrate(summary)

        concurrency = self._settings.llm.concurrency
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="epc") as pool:
            futures = {pool.submit(self._classify, thread): thread for thread in threads}
            for future in as_completed(futures):
                thread = futures[future]
                try:
                    mutation, usage = future.result()
                except ClassificationError as exc:
                    # One thread, not the run. This is the guarantee the old
                    # single-process path silently did not provide.
                    summary.classify_failed += 1
                    logger.warning("classification failed for %s: %s", thread.thread_id, exc)
                    continue

                summary.classified += 1
                summary.by_priority[mutation.priority] += 1
                summary.usage = summary.usage + usage
                if mutation.suspicious:
                    summary.suspicious += 1
                self._sink.emit(mutation)

        summary.apply = self._sink.close()
        return summary

    def _hydrate(self, summary: RunSummary) -> list[EmailThread]:
        """Fetch and parse the threads that still need a priority."""
        threads: list[EmailThread] = []
        for thread_id in self._list_thread_ids():
            summary.listed += 1
            try:
                raw = self._client.get_thread(thread_id)
            except GmailError as exc:
                summary.fetch_failed += 1
                logger.warning("could not fetch thread %s: %s", thread_id, exc)
                continue

            thread = parse_thread(raw)
            # Second half of idempotency: a label can appear between the search
            # and the fetch. Such a thread is counted and left alone — never
            # re-planned, because the user may have set that label by hand and
            # re-planning would undo the correction on every run.
            if thread.label_ids & self._priority_label_id_set:
                summary.already_labelled += 1
                continue
            threads.append(thread)
        return threads

    def _list_thread_ids(self) -> Iterator[str]:
        return self._client.list_thread_ids(self.search_query(), limit=self._settings.gmail.max_threads)

    def _classify(self, thread: EmailThread) -> tuple[ThreadMutation, Usage]:
        payload = build_payload(thread, self._settings.llm.budget)
        self._limiter.acquire()
        result = self._classifier.classify(payload)

        suspicious = payload.injection.suspicious and (self._settings.security.on_suspected_injection != "ignore")
        mutation = plan_mutation(
            thread_id=thread.thread_id,
            message_ids=thread.message_ids,
            thread_label_ids=thread.label_ids,
            priority=result.classification.priority,
            priority_label_ids=self._priority_label_ids,
            rules=self._settings.actions.rules,
            move_targets=set(self._settings.actions.move_targets),
            suspicious=suspicious,
            allow_destructive=self._settings.actions.allow_destructive,
            confidence=result.classification.confidence,
            reason=result.classification.reason,
            signals=result.classification.signals,
            backend=result.backend,
            model=result.model,
            prompt_version=result.prompt_version,
        )
        return mutation, result.usage
