"""Atomic JSON ledger writes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from paper_split_maker.models import State


def save_state(path: Path, state: State) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Windows raises PermissionError (WinError 32) when another process still
    # has state.json open. A few short retries cover that. A lasting lock is
    # reported by the caller; it must not kill the decision loop.
    last: PermissionError | None = None
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    if last is not None:
        raise last


def load_state(path: Path) -> State:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return State.from_dict(data)
