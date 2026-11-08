"""Decisions the live loop asks before it touches the exchange.

The gateway is injected. Dry-run uses a gateway that records plans and has no
signing method. This module does not read credentials and does not open sockets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


class Gateway(Protocol):
    def place_post_only_sells(self, orders: list[dict]) -> list[dict]:
        ...

    def cancel(self, order_ids: list[str]) -> None:
        ...

    def cancel_all(self) -> None:
        ...

    def heartbeat(self) -> None:
        ...

    def split(self, plan: dict) -> dict:
        ...

    def merge(self, plan: dict) -> dict:
        ...

    def redeem(self, plan: dict) -> dict:
        ...


@dataclass
class RecordingGateway:
    """Test double. ``signed`` stays false. Nothing is transmitted."""

    calls: list[tuple] = field(default_factory=list)
    signed: bool = False
    dry_run: bool = True
    open_order_ids: list[str] = field(default_factory=list)

    def place_post_only_sells(self, orders: list[dict]) -> list[dict]:
        self.calls.append(("place", orders))
        made = []
        for index, order in enumerate(orders):
            ident = f"ex-{len(self.open_order_ids) + index + 1}"
            made.append(
                {"order_id": ident, "status": "would_send" if False else "accepted"}
            )
            self.open_order_ids.append(ident)
        return made

    def cancel(self, order_ids: list[str]) -> None:
        self.calls.append(("cancel", list(order_ids)))
        self.open_order_ids = [
            item for item in self.open_order_ids if item not in set(order_ids)
        ]

    def cancel_all(self) -> None:
        self.calls.append(("cancel_all",))
        self.open_order_ids.clear()

    def heartbeat(self) -> None:
        self.calls.append(("heartbeat",))

    def split(self, plan: dict) -> dict:
        self.calls.append(("split", plan))
        return {"status": "submitted", "confirmed": False}

    def merge(self, plan: dict) -> dict:
        self.calls.append(("merge", plan))
        return {"status": "submitted"}

    def redeem(self, plan: dict) -> dict:
        self.calls.append(("redeem", plan))
        return {"status": "submitted"}


def should_pre_split(
    now_ms: int,
    open_ms: int,
    lead_ms: int,
    already_pending_or_confirmed: bool,
    risk_ok: bool,
) -> bool:
    if not risk_ok or already_pending_or_confirmed or lead_ms < 0:
        return False
    return open_ms - lead_ms <= now_ms < open_ms


def may_quote_unconfirmed(confirmed: bool) -> bool:
    return confirmed


@dataclass
class LatencyLog:
    rows: list[dict] = field(default_factory=list)

    def record(self, kind: str, received_ms: int, ack_ms: int) -> None:
        self.rows.append(
            {
                "kind": kind,
                "received_ms": received_ms,
                "ack_ms": ack_ms,
                "latency_ms": ack_ms - received_ms,
            }
        )


class LiveController:
    def __init__(self, engine, gateway: Gateway, *, dry_run: bool):
        self.engine = engine
        self.gateway = gateway
        self.dry_run = dry_run
        self.quoting = True
        self.latency = LatencyLog()

    def on_disconnect(self, ts: int) -> None:
        self.quoting = False
        self.gateway.cancel_all()
        self.engine.handle_event(
            {
                "type": "feed",
                "status": "down",
                "ts": ts,
                "event_id": f"feed-down-{ts}",
            }
        )

    def on_kill(self, ts: int, reason: str = "kill") -> None:
        self.quoting = False
        self.gateway.cancel_all()
        self.engine.handle_event(
            {"type": "kill", "ts": ts, "reason": reason, "event_id": f"kill-{ts}"}
        )

    def on_reconcile(self, remote_ids: list[str], ts: int) -> None:
        self.engine.handle_event(
            {
                "type": "reconcile",
                "open_order_ids": list(remote_ids),
                "ts": ts,
                "event_id": f"reconcile-{ts}-{len(remote_ids)}",
            }
        )
        if self.engine.state.mismatch_stop:
            self.quoting = False
            self.gateway.cancel_all()

    def plan_split(
        self, market_id: str, amount: Decimal, version: str, condition_id: str
    ) -> dict:
        from paper_split_maker.chain_data import (
            encode_split,
            position_target,
            shares_to_base_units,
        )

        base = shares_to_base_units(str(amount))
        plan = {
            "action": "split",
            "market_id": market_id,
            "amount": str(amount),
            "target": position_target(version, False),
            "data": encode_split(version, condition_id, base),
            "dry_run": self.dry_run,
        }
        self.gateway.split(plan)
        return plan
