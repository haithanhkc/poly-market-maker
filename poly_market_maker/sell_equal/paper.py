"""Deterministic in-memory broker. No network and no credentials."""

from __future__ import annotations

from decimal import Decimal

from poly_market_maker.sell_equal.engine import CancelResult, SubmitResult


class PaperBroker:
    def __init__(self):
        self._seq = 0
        self.orders: dict[str, dict] = {}
        self.submit_calls: list[tuple[str, Decimal, Decimal]] = []
        self.cancel_calls: list[list[str]] = []
        self.next_submit: str | None = None
        self.next_cancel: str | None = None

    def submit_sell(self, token_id: str, price: Decimal, size: Decimal) -> SubmitResult:
        self.submit_calls.append((token_id, price, size))
        mode = self.next_submit
        self.next_submit = None
        if mode == "raise":
            raise RuntimeError("paper submit uncertainty")
        if mode == "reject":
            return SubmitResult("rejected")
        if mode == "unknown":
            return SubmitResult("unknown", None)
        exchange_id = self._add(token_id, price, size)
        if mode == "unknown_with_id":
            return SubmitResult("unknown", exchange_id)
        return SubmitResult("accepted", exchange_id)

    def cancel(self, order_ids: list[str]) -> CancelResult:
        self.cancel_calls.append(list(order_ids))
        mode = self.next_cancel
        self.next_cancel = None
        if mode == "raise":
            raise RuntimeError("paper cancel uncertainty")
        if mode == "unknown":
            return CancelResult("unknown")
        canceled = []
        failed: dict[str, str] = {}
        for order_id in order_ids:
            row = self.orders.get(order_id)
            if row is None or not row["open"]:
                failed[order_id] = "not_open"
                continue
            row["open"] = False
            canceled.append(order_id)
        return CancelResult("known", frozenset(canceled), failed)

    def inject_foreign(self, order_id: str) -> None:
        self.orders[order_id] = {
            "token_id": "foreign",
            "price": Decimal("0.50"),
            "size": Decimal("1"),
            "matched": Decimal("0"),
            "open": True,
            "foreign": True,
        }

    def open_ids(self) -> set[str]:
        return {key for key, row in self.orders.items() if row["open"]}

    def matched_map(self) -> dict[str, Decimal]:
        return {key: row["matched"] for key, row in self.orders.items()}

    def _add(self, token_id: str, price: Decimal, size: Decimal) -> str:
        self._seq += 1
        order_id = "paper-%04d" % self._seq
        self.orders[order_id] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "matched": Decimal("0"),
            "open": True,
            "foreign": False,
        }
        return order_id
