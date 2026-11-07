"""In-memory broker. A crossing post-only sell is rejected and not retried."""

from __future__ import annotations

from decimal import Decimal

from poly_market_maker.share_balanced_session.book import Book
from poly_market_maker.share_balanced_session.submit import (
    CancelResult,
    MakerOnlyViolation,
    SubmitResult,
)


class PaperBroker:
    def __init__(self):
        self._seq = 0
        self.orders: dict[str, dict] = {}
        self.books: dict[str, Book] = {}
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[list[str]] = []
        self.next_submit: str | None = None
        self.next_cancel: str | None = None
        self.before_submit = None

    def set_book(self, token_id: str, book: Book) -> None:
        self.books[token_id] = book

    def submit_sell(
        self, token_id: str, price: Decimal, size: Decimal, post_only: bool
    ) -> SubmitResult:
        if post_only is not True:
            raise MakerOnlyViolation("sell submissions require post_only")
        self.submit_calls.append(
            {"token_id": token_id, "price": price, "size": size, "post_only": True}
        )
        if self.before_submit is not None:
            self.before_submit()
        mode = self.next_submit
        self.next_submit = None
        if mode == "raise":
            raise RuntimeError("paper submit uncertainty")
        if mode == "unknown":
            return SubmitResult("unknown", None, "unknown_submit")
        if self._crosses(token_id, price):
            return SubmitResult("rejected", None, "post_only_would_cross")
        if mode == "reject":
            return SubmitResult("rejected", None, "rejected")
        self._seq += 1
        exchange_id = "paper-%04d" % self._seq
        self.orders[exchange_id] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "open": True,
            "foreign": False,
            "post_only": True,
        }
        if mode == "unknown_with_id":
            return SubmitResult("unknown", exchange_id, "unknown_submit")
        return SubmitResult("accepted", exchange_id, "resting")

    def cancel(self, order_ids: list[str]) -> CancelResult:
        self.cancel_calls.append(list(order_ids))
        mode = self.next_cancel
        self.next_cancel = None
        if mode == "unknown":
            return CancelResult("unknown")
        if mode == "raise":
            raise RuntimeError("paper cancel uncertainty")
        canceled = []
        failed = {}
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
            "open": True,
            "foreign": True,
            "post_only": True,
        }

    def _crosses(self, token_id: str, price: Decimal) -> bool:
        book = self.books.get(token_id)
        if book is None or book.best_bid is None:
            return False
        return price <= book.best_bid
