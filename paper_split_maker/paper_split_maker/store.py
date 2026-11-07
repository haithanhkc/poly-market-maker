"""Atomic JSON ledger writes."""

from __future__ import annotations

import json
import os
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
    os.replace(temporary, path)


def load_state(path: Path) -> State:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return State.from_dict(data)
