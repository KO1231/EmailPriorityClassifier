"""Applying mutations to Gmail, in as few calls as possible."""

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from epc.actions.model import ThreadMutation
from epc.errors import GmailError
from epc.gmail.client import BATCH_MODIFY_LIMIT, GmailClient


class ApplyReport(BaseModel):
    """What an apply pass did. Reported, never inferred from silence."""

    applied: int = 0
    skipped_noop: int = 0
    failed: int = 0
    api_calls: int = 0
    failures: list[str] = Field(default_factory=list)

    @property
    def had_failures(self) -> bool:
        return self.failed > 0


class MutationApplier:
    """Groups mutations by the change they request, then writes them."""

    def __init__(self, client: GmailClient, *, batch_size: int = BATCH_MODIFY_LIMIT) -> None:
        self._client = client
        self._batch_size = min(batch_size, BATCH_MODIFY_LIMIT)

    def apply(self, mutations: Iterable[ThreadMutation]) -> ApplyReport:
        """Apply every mutation, batching those that ask for the same change.

        A failed batch costs that batch, not the run: the remaining groups are
        still attempted, and the report says which threads were affected.
        """
        report = ApplyReport()
        grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[ThreadMutation]] = {}

        for mutation in mutations:
            if mutation.is_noop:
                # The thread already has exactly the labels asked for.
                report.skipped_noop += 1
                continue
            grouped.setdefault(mutation.label_signature, []).append(mutation)

        for (add, remove), group in grouped.items():
            for chunk in _chunks(group, self._batch_size):
                message_ids = [mid for mutation in chunk for mid in mutation.message_ids]
                if not message_ids:
                    report.skipped_noop += len(chunk)
                    continue
                try:
                    for id_chunk in _chunked_ids(message_ids, self._batch_size):
                        self._client.batch_modify(id_chunk, add_label_ids=add, remove_label_ids=remove)
                        report.api_calls += 1
                except GmailError as exc:
                    report.failed += len(chunk)
                    report.failures.append(f"{len(chunk)} thread(s) add={list(add)} remove={list(remove)}: {exc}")
                else:
                    report.applied += len(chunk)

        return report


def _chunks(items: Sequence[ThreadMutation], size: int) -> list[Sequence[ThreadMutation]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _chunked_ids(ids: Sequence[str], size: int) -> list[Sequence[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]
