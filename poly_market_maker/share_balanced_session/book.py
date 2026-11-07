"""L2 view used only to choose a resting maker price.

Joining the ask is the default. Improving toward the bid is off unless
improve_ticks is set, and the price must stay strictly above the best bid.
A trade that merely touches our price does not imply a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP

from poly_market_maker.sell_equal.quant import D, price_in_bounds


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass
class Book:
    ts: Decimal
    bids: list[Level]
    asks: list[Level]
    tick_size: Decimal
    min_order_size: Decimal

    @property
    def best_bid(self) -> Decimal | None:
        if not self.bids:
            return None
        return max(level.price for level in self.bids)

    @property
    def best_ask(self) -> Decimal | None:
        if not self.asks:
            return None
        return min(level.price for level in self.asks)


def parse_book(payload: dict, tick: Decimal, minimum: Decimal) -> Book:
    return Book(
        ts=D(payload["ts"]),
        bids=_levels(payload.get("bids") or []),
        asks=_levels(payload.get("asks") or []),
        tick_size=tick,
        min_order_size=minimum,
    )


def _levels(rows: list) -> list[Level]:
    levels = []
    for row in rows:
        price = D(row[0]) if not isinstance(row, dict) else D(row["price"])
        size = D(row[1]) if not isinstance(row, dict) else D(row["size"])
        if size > 0 and price > 0:
            levels.append(Level(price, size))
    return levels


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_UP)
    return units * step


def maker_sell_price(book: Book, improve_ticks: int, floor: Decimal, ceiling: Decimal):
    """Return (price, reason). Price is None when no maker quote is valid."""

    ask = book.best_ask
    bid = book.best_bid
    if ask is None:
        return None, "no_ask_to_join"
    if bid is not None and ask <= bid:
        return None, "crossed_book"
    price = ask
    if improve_ticks > 0 and bid is not None:
        improved = ask - book.tick_size * improve_ticks
        above_bid = bid + book.tick_size
        if improved < above_bid:
            improved = above_bid
        price = improved
    price = ceil_to_step(price, book.tick_size)
    if bid is not None and price <= bid:
        return None, "would_cross"
    if not price_in_bounds(price, book.tick_size, floor, ceiling):
        return None, "price_bounds"
    return price, "ok"


def bids_high_to_low(book: Book) -> list[tuple[Decimal, Decimal]]:
    return [
        (level.price, level.size)
        for level in sorted(book.bids, key=lambda item: item.price, reverse=True)
    ]
