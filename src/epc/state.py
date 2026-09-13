"""What a run remembers between invocations.

Only one thing matters: the Gmail `historyId` reached last time. With it, the
next run asks Gmail "what changed since then" instead of re-listing the inbox —
which is the difference between a scheduled run costing a few API calls and it
costing a full scan every hour.

The store is a protocol because where this lives differs by deployment: a file
locally, a parameter or an object in the cloud. It holds no secret, so it needs
no special handling — just somewhere durable.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class RunState(BaseModel):
    """The checkpoint, plus enough context to tell whether it is stale."""

    # None means "never run", which is a full scan rather than an error.
    history_id: str | None = None
    last_run_at: datetime | None = None
    # Which mailbox this checkpoint belongs to. A checkpoint from a different
    # account is meaningless and must not be used.
    mailbox: str | None = None

    def advanced_to(self, history_id: str, *, mailbox: str | None = None) -> RunState:
        return RunState(
            history_id=history_id,
            last_run_at=datetime.now(UTC),
            mailbox=mailbox or self.mailbox,
        )

    def usable_for(self, mailbox: str) -> bool:
        """Whether this checkpoint may be used for `mailbox`."""
        return bool(self.history_id) and (self.mailbox in (None, mailbox))


@runtime_checkable
class StateStore(Protocol):
    def load(self) -> RunState: ...

    def save(self, state: RunState) -> None: ...


class LocalFileStateStore:
    """Checkpoint in a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> RunState:
        """A missing or unreadable checkpoint means "start over", never a crash.

        The cost of getting this wrong is one full scan; the cost of raising is
        a run that does nothing at all.
        """
        if not self._path.is_file():
            return RunState()
        try:
            return RunState.model_validate_json(self._path.read_text(encoding="utf-8"))
        except ValueError, json.JSONDecodeError, OSError:
            return RunState()

    def save(self, state: RunState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename: an interrupted save leaves the previous checkpoint
        # intact rather than a truncated file that reads as "never run".
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self._path)


class NullStateStore:
    """Remember nothing. Every run is a full scan."""

    def load(self) -> RunState:
        return RunState()

    def save(self, state: RunState) -> None:  # noqa: ARG002 - protocol shape
        return None
