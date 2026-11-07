"""Durable session state. Cost basis is required and never inferred."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D

ACTIVE = {
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
    price: Decimal
    original_size: Decimal
    filled_size: Decimal = Decimal("0")
    reserved_size: Decimal = Decimal("0")
    status: str = "intent"
    post_only: bool = True

    def __post_init__(self) -> None:
        if self.post_only is not True:
            raise ValueError("orders must be post_only")

    @property
    def remaining(self) -> Decimal:
        return self.original_size - self.filled_size

    def to_json(self) -> dict:
        return {
            "local_id": self.local_id,
            "exchange_id": self.exchange_id,
            "outcome": self.outcome,
            "price": str(self.price),
            "original_size": str(self.original_size),
            "filled_size": str(self.filled_size),
            "reserved_size": str(self.reserved_size),
            "status": self.status,
            "post_only": True,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "TrackedOrder":
        if raw.get("post_only") is not True:
            raise ValueError("state order is not post_only")
        return cls(
            local_id=raw["local_id"],
            exchange_id=raw.get("exchange_id"),
            outcome=raw["outcome"],
            price=D(raw["price"]),
            original_size=D(raw["original_size"]),
            filled_size=D(raw["filled_size"]),
            reserved_size=D(raw["reserved_size"]),
            status=raw["status"],
            post_only=True,
        )


@dataclass
class SessionState:
    version: int = 1
    mode: str = "share_balanced_session"
    cost_basis_source: str = "explicit_allocation"
    cost_basis_usd: Decimal = Decimal("0")
    capital_usd: Decimal = Decimal("0")
    condition_id: str = ""
    on_hand: dict[str, Decimal] = field(
        default_factory=lambda: {"up": Decimal("0"), "down": Decimal("0")}
    )
    gross_proceeds: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    fees_accrued: Decimal = Decimal("0")
    rebate_unpaid_estimate: Decimal = Decimal("0")
    seen_fill_ids: list[str] = field(default_factory=list)
    orders: list[TrackedOrder] = field(default_factory=list)
    phase: str = "quoting"
    halt_reason: str | None = None
    next_local: int = 1
    last_user_ts: Decimal | None = None
    last_replace_ts: dict[str, Decimal] = field(default_factory=dict)
    confirmed_cancel_ids: list[str] = field(default_factory=list)
    skip_reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "mode": self.mode,
            "cost_basis_source": self.cost_basis_source,
            "cost_basis_usd": str(self.cost_basis_usd),
            "capital_usd": str(self.capital_usd),
            "condition_id": self.condition_id,
            "on_hand": {key: str(value) for key, value in self.on_hand.items()},
            "gross_proceeds": str(self.gross_proceeds),
            "fees_paid": str(self.fees_paid),
            "fees_accrued": str(self.fees_accrued),
            "rebate_unpaid_estimate": str(self.rebate_unpaid_estimate),
            "seen_fill_ids": list(self.seen_fill_ids),
            "orders": [order.to_json() for order in self.orders],
            "phase": self.phase,
            "halt_reason": self.halt_reason,
            "next_local": self.next_local,
            "last_user_ts": None
            if self.last_user_ts is None
            else str(self.last_user_ts),
            "last_replace_ts": {
                key: str(value) for key, value in self.last_replace_ts.items()
            },
            "confirmed_cancel_ids": list(self.confirmed_cancel_ids),
            "skip_reasons": list(self.skip_reasons),
        }

    @classmethod
    def from_json(cls, raw: dict) -> "SessionState":
        if raw.get("version") != 1 or raw.get("mode") != "share_balanced_session":
            raise ValueError("unsupported session state")
        if raw.get("cost_basis_source") != "explicit_allocation":
            raise ValueError("state has no explicit cost basis")
        if raw.get("cost_basis_usd") in (None, ""):
            raise ValueError("state is missing cost basis")
        state = cls(
            cost_basis_source=raw["cost_basis_source"],
            cost_basis_usd=D(raw["cost_basis_usd"]),
            capital_usd=D(raw["capital_usd"]),
            condition_id=raw["condition_id"],
            on_hand={key: D(value) for key, value in raw["on_hand"].items()},
            gross_proceeds=D(raw["gross_proceeds"]),
            fees_paid=D(raw["fees_paid"]),
            fees_accrued=D(raw["fees_accrued"]),
            rebate_unpaid_estimate=D(raw["rebate_unpaid_estimate"]),
            seen_fill_ids=list(raw["seen_fill_ids"]),
            orders=[TrackedOrder.from_json(item) for item in raw["orders"]],
            phase=raw["phase"],
            halt_reason=raw.get("halt_reason"),
            next_local=int(raw["next_local"]),
            confirmed_cancel_ids=list(raw.get("confirmed_cancel_ids") or []),
            skip_reasons=list(raw.get("skip_reasons") or []),
        )
        if raw.get("last_user_ts") is not None:
            state.last_user_ts = D(raw["last_user_ts"])
        state.last_replace_ts = {
            key: D(value) for key, value in (raw.get("last_replace_ts") or {}).items()
        }
        return state


def save_state(state: SessionState, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, target)


def load_state(path: str | Path) -> SessionState:
    return SessionState.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
