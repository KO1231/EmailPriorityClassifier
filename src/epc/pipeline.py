"""The run: list, fetch, classify, plan, dispatch.

Concurrency is threads, not processes. The work is HTTP wait from end to end, so
processes bought nothing and cost a great deal: the classifier had to be
picklable, which is why the API clients were module globals; every run paid to
spawn workers; and every child opened the same rotating log file.

Rate limiting paces individual requests rather than sleeping between blocks, so
one slow response no longer stalls every worker.

**Every classification is isolated from the others.** Whatever a worker raises
costs that thread and nothing else. An exception left to escape a future used to
end the loop while the executor went on paying for every queued request, and
skipped the flush — so one malformed model reply threw away every label the run
had already decided.
"""

import threading
import time
import traceback
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal

from epc.actions.model import ThreadMutation
from epc.actions.planner import plan_carry_forward, plan_mutation
from epc.classify.base import Classifier, Usage
from epc.classify.budget import build_payload
from epc.dispatch.applier import ApplyReport
from epc.dispatch.sink import DirectSink, JsonlSink, MutationSink
from epc.errors import ClassificationError, GmailError, RejectedByProviderError, UnusableResponseError
from epc.gmail.client import GmailClient
from epc.gmail.mime import parse_thread
from epc.gmail.models import EmailThread, ThreadRef
from epc.gmail.query import build_search_query
from epc.logging import get_logger
from epc.priority import Priority
from epc.ratelimit import RateLimiter
from epc.report import HistorySink, NullHistorySink, record_for
from epc.settings import Settings
from epc.shutdown import ShutdownRequestedError
from epc.state import NullStateStore, RunState, StateStore

logger = get_logger(__name__)

# Failures in a row, in completion order, after which no new classification is
# started. Past this point the cause is almost never the threads: a revoked key,
# a model that no longer exists, a bug. Each further request costs money or
# quota and teaches nothing the first ten did not.
MAX_CONSECUTIVE_FAILURES = 10

# The summary names skipped threads so they can be looked up, up to a point.
_SKIPPED_IDS_SHOWN = 10


@dataclass
class RunSummary:
    """What a run did. Printed at the end and used to pick an exit code."""

    listed: int = 0
    already_labelled: int = 0
    # Of those, threads whose label was put on replies that arrived after it.
    labels_carried_forward: int = 0
    # Left out because they failed on earlier runs for reasons of their own.
    # Not failures of this run: see `epc.state` for when they come back.
    skipped_known_failures: int = 0
    skipped_thread_ids: list[str] = field(default_factory=list)
    fetch_failed: int = 0
    # Fetched, but the message could not be turned into something to classify.
    # Kept apart from fetch failures: those are Gmail's, and pass; these belong
    # to the mail itself, and will happen again on the next run.
    parse_failed: int = 0
    classified: int = 0
    classify_failed: int = 0
    # Threads left for the next run because a stop was requested. Not failures:
    # nothing went wrong, and still lacking a label, they come back next run.
    abandoned: int = 0
    interrupted: bool = False
    # Set when too many classifications failed in a row and the rest were not
    # started. A failure, unlike an interruption: something is wrong.
    halted: bool = False
    suspicious: int = 0
    by_priority: dict[Priority, int] = field(default_factory=lambda: dict.fromkeys(Priority, 0))
    usage: Usage = field(default_factory=Usage)
    apply: ApplyReport = field(default_factory=ApplyReport)
    # The failure records could not be written. Nothing is lost this run, but
    # the next one will retry what it should have skipped — and if it keeps
    # happening, someone should hear about it.
    state_not_saved: bool = False
    elapsed_seconds: float = 0.0

    @property
    def had_failures(self) -> bool:
        return bool(
            self.fetch_failed
            or self.parse_failed
            or self.classify_failed
            or self.apply.had_failures
            or self.state_not_saved
        )

    def render(self) -> str:
        shown = ", ".join(self.skipped_thread_ids[:_SKIPPED_IDS_SHOWN])
        if len(self.skipped_thread_ids) > _SKIPPED_IDS_SHOWN:
            shown += f" +{len(self.skipped_thread_ids) - _SKIPPED_IDS_SHOWN}"
        lines = [
            "=== Summary ===",
            f"  listed            {self.listed}",
            f"  already labelled  {self.already_labelled}"
            f"  (skipped, no LLM call; label carried to new replies: {self.labels_carried_forward})",
            *(
                [f"  known failures    {self.skipped_known_failures}  (skipped: {shown})"]
                if self.skipped_known_failures
                else []
            ),
            f"  classified        {self.classified}"
            f"  (failed: {self.classify_failed}, unfetchable: {self.fetch_failed}, unparseable: {self.parse_failed})",
            "    " + "  ".join(f"{p.value}: {self.by_priority[p]}" for p in Priority),
            f"  suspicious        {self.suspicious}",
            *([f"  not started       {self.abandoned}  ({self._stop_reason})"] if self.abandoned else []),
            f"  tokens            in {self.usage.input_tokens:,} / out {self.usage.output_tokens:,}",
            f"  applied           {self.apply.applied}"
            f"  (no-op: {self.apply.skipped_noop}, failed: {self.apply.failed})",
            f"  gmail write calls {self.apply.api_calls}",
            f"  elapsed           {self.elapsed_seconds:.1f}s",
            *(["  ! run state could not be saved"] if self.state_not_saved else []),
        ]
        lines.extend(f"  ! {failure}" for failure in self.apply.failures)
        return "\n".join(lines)

    @property
    def _stop_reason(self) -> str:
        return "too many consecutive failures" if self.halted else "stop requested"


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
        state_store: StateStore | None = None,
        history: HistorySink | None = None,
        shutdown: threading.Event | None = None,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        classifier_version: str = "",
        dry_run: bool = False,
    ) -> None:
        self._settings = settings
        self._client = client
        self._classifier = classifier
        self._sink = sink
        self._priority_label_ids = priority_label_ids
        self._limiter = RateLimiter(settings.llm.requests_per_min)
        self._priority_label_id_set = set(priority_label_ids.values())
        self._state_store = state_store or NullStateStore()
        self._history = history or NullHistorySink()
        self._shutdown = shutdown or threading.Event()
        # Kept apart from `shutdown`: that one means "asked to stop", and this
        # one means "stopping because something is broken".
        self._halt = threading.Event()
        self._max_consecutive_failures = max_consecutive_failures
        # What failure records are valid for; a change retries every one.
        self._classifier_version = classifier_version
        # A dry run reads the records, so it lists what a real run would, and
        # writes none — it must not change what the next real run does.
        self._dry_run = dry_run

        # One run's outcome, for the failure records.
        self._history_ids: dict[str, str] = {}
        self._failed_threads: dict[str, str] = {}
        self._succeeded: set[str] = set()
        self._carried_forward: list[ThreadMutation] = []
        self._dispatched_via: Literal["direct", "sqs", "jsonl"] = (
            "jsonl" if isinstance(sink, JsonlSink) else "direct" if isinstance(sink, DirectSink) else "sqs"
        )

    def search_query(self) -> str:
        return build_search_query(
            base=self._settings.gmail.query,
            extra=self._settings.gmail.extra_query,
            exclude_labels=list(self._settings.labels.by_priority.values()),
        )

    def run(self) -> RunSummary:
        started = time.monotonic()
        summary = RunSummary()

        state = self._state_store.load().for_run(
            mailbox=self._client.mailbox_address(),
            classifier=self._classifier_version,
        )
        threads = self._hydrate(self._threads_to_consider(state, summary), summary)

        try:
            # Inside the `try`, so the sink that takes them is always closed.
            for mutation in self._carried_forward:
                self._sink.emit(mutation)
            self._classify_all(threads, summary)
        finally:
            # Reached on every path out — a stop request, a halt, an exception
            # from the sink itself. Whatever was classified is already paid for.
            summary.interrupted = self._shutdown.is_set()
            summary.halted = self._halt.is_set()
            try:
                summary.apply = self._sink.close()
            finally:
                self._history.close()
            summary.elapsed_seconds = time.monotonic() - started

        self._save_state(state, summary)
        return summary

    def _classify_all(self, threads: list[EmailThread], summary: RunSummary) -> None:
        pool = ThreadPoolExecutor(max_workers=self._settings.llm.concurrency, thread_name_prefix="epc")
        consecutive_failures = 0
        try:
            futures = {pool.submit(self._classify, thread): thread for thread in threads}
            for future in as_completed(futures):
                thread = futures[future]
                try:
                    mutation, usage = future.result()
                except ShutdownRequestedError:
                    # Never started. Left for the next run, and not counted as
                    # a failure: the failures that caused a halt are counted
                    # already, and a stop request is not one.
                    summary.abandoned += 1
                    continue
                except (RejectedByProviderError, UnusableResponseError) as exc:
                    # About this thread: remembered, so it can stop being retried.
                    summary.classify_failed += 1
                    self._failed_threads[thread.thread_id] = self._history_ids.get(thread.thread_id, "")
                    logger.warning("classification failed", thread_id=thread.thread_id, error=str(exc))
                    consecutive_failures = self._note_failure(consecutive_failures)
                    continue
                except ClassificationError as exc:
                    summary.classify_failed += 1
                    logger.warning("classification failed", thread_id=thread.thread_id, error=str(exc))
                    consecutive_failures = self._note_failure(consecutive_failures)
                    continue
                except Exception as exc:
                    # A bug, or a library raising something undocumented. Still
                    # one thread. The message is left out on purpose: a pydantic
                    # error quotes its input, and the input here is mail or
                    # model output. The type and the stack are what debugging
                    # needs, and neither carries content.
                    summary.classify_failed += 1
                    logger.error(
                        "unexpected error while classifying",
                        thread_id=thread.thread_id,
                        error_type=type(exc).__name__,
                        # A list of frames, so the per-string length cap in
                        # redaction does not cut the stack to its first line.
                        frames=[frame.rstrip() for frame in traceback.format_tb(exc.__traceback__)],
                    )
                    consecutive_failures = self._note_failure(consecutive_failures)
                    continue

                consecutive_failures = 0
                self._succeeded.add(thread.thread_id)
                summary.classified += 1
                summary.by_priority[mutation.priority] += 1
                summary.usage = summary.usage + usage
                if mutation.suspicious:
                    summary.suspicious += 1

                self._sink.emit(mutation)
                self._history.write(
                    record_for(
                        mutation,
                        subject=thread.subject,
                        sender_domain=thread.latest.sender_domain if thread.latest else "",
                        message_count=len(thread.messages),
                        usage=usage,
                        dispatched_via=self._dispatched_via,
                        include_reason=self._settings.observability.history_include_reason,
                    )
                )
        finally:
            # On a normal exit every future is done and this cancels nothing.
            # On the way out through an exception it is what stops the executor
            # working through — and paying for — the rest of the queue.
            pool.shutdown(wait=True, cancel_futures=True)

    def _note_failure(self, consecutive_failures: int) -> int:
        consecutive_failures += 1
        if consecutive_failures >= self._max_consecutive_failures and not self._halt.is_set():
            logger.error("classifications failing in a row; starting no more this run", failures=consecutive_failures)
            self._halt.set()
        return consecutive_failures

    def _raise_if_stopping(self, thread: EmailThread) -> None:
        if self._shutdown.is_set() or self._halt.is_set():
            raise ShutdownRequestedError(thread.thread_id)

    def _threads_to_consider(self, state: RunState, summary: RunSummary) -> Iterator[ThreadRef]:
        """The query's threads, less those that have earned a skip.

        Skipped threads do not use up `max_threads`: a handful of permanently
        failing threads must not crowd out new mail. The listing asks for that
        many extra to make room.
        """
        limit = self._settings.gmail.max_threads
        taken = 0
        for ref in self._client.list_threads(self.search_query(), limit=limit + len(state.failures)):
            if state.should_skip(ref.id, ref.history_id):
                summary.skipped_known_failures += 1
                summary.skipped_thread_ids.append(ref.id)
                continue
            if taken >= limit:
                return
            taken += 1
            yield ref

    def _hydrate(self, refs: Iterable[ThreadRef], summary: RunSummary) -> list[EmailThread]:
        """Fetch and parse the threads that still need a priority."""
        threads: list[EmailThread] = []
        for ref in refs:
            summary.listed += 1
            self._history_ids[ref.id] = ref.history_id
            try:
                raw = self._client.get_thread(ref.id)
            except GmailError as exc:
                summary.fetch_failed += 1
                logger.warning("could not fetch thread", thread_id=ref.id, error=str(exc))
                continue

            try:
                thread = parse_thread(raw)
            except Exception as exc:
                # Anyone can send mail, so anything the parser trips over is
                # something an outsider can put in every run's path. Named
                # decoding failures are handled where they occur; this is the
                # backstop for the ones nobody has thought of yet. It is the
                # mail's own doing, so it is remembered like one.
                summary.parse_failed += 1
                self._failed_threads[ref.id] = ref.history_id
                logger.warning("could not parse thread", thread_id=ref.id, error_type=type(exc).__name__)
                continue
            # Second half of idempotency: a label can appear between the search
            # and the fetch. Such a thread is counted and left alone — never
            # re-planned, because the user may have set that label by hand and
            # re-planning would undo the correction on every run.
            if thread.label_ids & self._priority_label_id_set:
                summary.already_labelled += 1
                carried = plan_carry_forward(
                    thread_id=thread.thread_id,
                    message_label_ids={message.message_id: set(message.label_ids) for message in thread.messages},
                    priority_label_ids=self._priority_label_ids,
                )
                if carried is not None:
                    # So the thread stops matching the search, instead of being
                    # fetched and skipped like this on every run from now on.
                    summary.labels_carried_forward += 1
                    self._carried_forward.append(carried)
                continue
            threads.append(thread)
        return threads

    def _save_state(self, state: RunState, summary: RunSummary) -> None:
        if self._dry_run:
            return
        updated = state.after_run(failed=self._failed_threads, succeeded=self._succeeded)
        try:
            self._state_store.save(updated)
        except Exception as exc:
            summary.state_not_saved = True
            logger.error("could not save run state", error_type=type(exc).__name__)

    def _classify(self, thread: EmailThread) -> tuple[ThreadMutation, Usage]:
        # Checked before the expensive part rather than after: a queued task
        # that has not started should cost nothing once a stop is requested.
        # Checked again after the rate limiter, which can hold a task for a
        # while — long enough for a halt to have happened in the meantime.
        self._raise_if_stopping(thread)
        payload = build_payload(thread, self._settings.llm.budget)
        self._limiter.acquire()
        self._raise_if_stopping(thread)
        try:
            result = self._classifier.classify(payload)
        except UnusableResponseError:
            # Once more before it counts. A model that returns something
            # unusable for a thread once may well not do it twice, and a
            # thread is only ever skipped for failing again.
            self._limiter.acquire()
            self._raise_if_stopping(thread)
            result = self._classifier.classify(payload)

        response = self._settings.security.on_suspected_injection
        suspicious = payload.injection.suspicious and response != "ignore"
        mutation = plan_mutation(
            thread_id=thread.thread_id,
            message_ids=thread.message_ids,
            thread_label_ids=thread.label_ids,
            priority=result.classification.priority,
            priority_label_ids=self._priority_label_ids,
            rules=self._settings.actions.rules,
            move_targets=set(self._settings.actions.move_targets),
            suspicious=suspicious,
            # `flag` records the signal and takes nothing away; only
            # `downgrade_and_flag` withholds the star and the move.
            withhold_privileged_when_suspicious=response == "downgrade_and_flag",
            allow_destructive=self._settings.actions.allow_destructive,
            confidence=result.classification.confidence,
            reason=result.classification.reason,
            signals=result.classification.signals,
            backend=result.backend,
            model=result.model,
            prompt_version=result.prompt_version,
        )
        return mutation, result.usage
