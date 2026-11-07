"""Durable bot state. Decimal values are stored as strings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D

ACTIVE_STATUSES = {
    "intent",
    "open",
    "unknown_submit",
    "cancel_requested",
    "unknown_cancel",
}


@dataclass
class TrackedOrder:
    local_id: str
    exchange_id: str | None
    outcome: str
    role: str
    price: Decimal
    original_size: Decimal
    filled_size: Decimal = Decimal("0")
    trade_accounted: Decimal = Decimal("0")
    reserved_size: Decimal = Decimal("0")
    status: str = "intent"

    @property
    def remaining(self) -> Decimal:
        return self.original_size - self.filled_size

    def to_json(self) -> dict:
        return {
            "local_id": self.local_id,
            "exchange_id": self.exchange_id,
            "outcome": self.outcome,
            "role": self.role,
            "price": str(self.price),
            "original_size": str(self.original_size),
            "filled_size": str(self.filled_size),
            "trade_accounted": str(self.trade_accounted),
            "reserved_size": str(self.reserved_size),
            "status": self.status,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "TrackedOrder":
        return cls(
            local_id=raw["local_id"],
            exchange_id=raw.get("exchange_id"),
            outcome=raw["outcome"],
            role=raw["role"],
            price=D(raw["price"]),
            original_size=D(raw["original_size"]),
            filled_size=D(raw["filled_size"]),
            trade_accounted=D(raw["trade_accounted"]),
            reserved_size=D(raw["reserved_size"]),
            status=raw["status"],
        )


@dataclass
class BotState:
    version: int = 1
    value_basis: str = "gross"
    on_hand: dict[str, Decimal] = field(
        default_factory=lambda: {"up": Decimal("0"), "down": Decimal("0")}
    )
    start_on_hand: dict[str, Decimal] = field(
        default_factory=lambda: {"up": Decimal("0"), "down": Decimal("0")}
    )
    gross_sold: dict[str, Decimal] = field(
        default_factory=lambda: {"up": Decimal("0"), "down": Decimal("0")}
    )
    seen_fill_ids: list[str] = field(default_factory=list)
    orders: list[TrackedOrder] = field(default_factory=list)
    halt_reason: str | None = None
    seeds_placed: bool = False
    next_local: int = 1
    last_market_ts: Decimal | None = None
    last_user_ts: Decimal | None = None
    confirmed_cancel_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "value_basis": self.value_basis,
            "on_hand": {k: str(v) for k, v in self.on_hand.items()},
            "start_on_hand": {k: str(v) for k, v in self.start_on_hand.items()},
            "gross_sold": {k: str(v) for k, v in self.gross_sold.items()},
            "seen_fill_ids": list(self.seen_fill_ids),
            "orders": [order.to_json() for order in self.orders],
            "halt_reason": self.halt_reason,
            "seeds_placed": self.seeds_placed,
            "next_local": self.next_local,
            "last_market_ts": None
            if self.last_market_ts is None
            else str(self.last_market_ts),
            "last_user_ts": None
            if self.last_user_ts is None
            else str(self.last_user_ts),
            "confirmed_cancel_ids": list(self.confirmed_cancel_ids),
        }

    @classmethod
    def from_json(cls, raw: dict) -> "BotState":
        if raw.get("version") != 1:
            raise ValueError("unsupported state version")
        if raw.get("value_basis") != "gross":
            raise ValueError("state value basis must stay gross")
        state = cls(
            on_hand={k: D(v) for k, v in raw["on_hand"].items()},
            start_on_hand={k: D(v) for k, v in raw["start_on_hand"].items()},
            gross_sold={k: D(v) for k, v in raw["gross_sold"].items()},
            seen_fill_ids=list(raw["seen_fill_ids"]),
            orders=[TrackedOrder.from_json(item) for item in raw["orders"]],
            halt_reason=raw.get("halt_reason"),
            seeds_placed=bool(raw["seeds_placed"]),
            next_local=int(raw["next_local"]),
            confirmed_cancel_ids=list(raw.get("confirmed_cancel_ids") or []),
        )
        if raw.get("last_market_ts") is not None:
            state.last_market_ts = D(raw["last_market_ts"])
        if raw.get("last_user_ts") is not None:
            state.last_user_ts = D(raw["last_user_ts"])
        return state


def save_state(state: BotState, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_json(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def load_state(path: str | Path) -> BotState:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return BotState.from_json(raw)
