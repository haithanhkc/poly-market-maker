"""Two-sided maker-sell quotes from each outcome's own book.

Down is not priced as one minus Up. A missing edge, a wide book, or a quote
that would cross the bid produces no order. Skew shrinks and reprices the
sell that would add to an existing imbalance; it does not authorize a sale at
an arbitrary price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from paper_split_maker.config import Config
from paper_split_maker.decimal_utils import ZERO, ceil_tick, qdown
from paper_split_maker.models import Market
from paper_split_maker.risk import reserved
from paper_split_maker.models import State


@dataclass(frozen=True)
class Proposal:
    outcome: str
    price: Decimal
    size: Decimal
    fair: Decimal
    required_edge: Decimal
    repair: bool
    reason: str = "ok"


def _no(reason: str, outcome: str) -> Proposal:
    return Proposal(
        outcome=outcome,
        price=ZERO,
        size=ZERO,
        fair=ZERO,
        required_edge=ZERO,
        repair=False,
        reason=reason,
    )


def propose(state: State, market: Market, outcome: str, config: Config) -> Proposal:
    book = market.book(outcome)
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None:
        return _no("uncertain_book", outcome)
    if ask <= bid:
        return _no("uncertain_book", outcome)
    if ask - bid > config.max_spread:
        return _no("uncertain_book", outcome)
    self_inv = market.inventory(outcome)
    other_inv = market.down if outcome == "up" else market.up
    adverse = other_inv - self_inv
    if adverse < 0:
        adverse = ZERO
    required_edge = config.min_edge + config.skew_edge_per_share * adverse
    fair = (bid + ask) / 2
    raw = ask if ask >= fair + required_edge else fair + required_edge
    price = ceil_tick(raw, market.tick_size)
    if price < config.min_price or price > config.max_price or price >= 1:
        return _no("no_edge", outcome)
    if price <= bid:
        return _no("would_cross", outcome)
    repair = self_inv > other_inv
    if adverse > 0:
        size = config.clip_size - config.skew_size_per_share * adverse
    else:
        size = config.clip_size
    free = self_inv - reserved(state, market, outcome)
    if size > free:
        size = free
    size = qdown(size, config.order_size_quantum)
    minimum = (
        market.min_order_size
        if market.min_order_size > config.min_order_size
        else config.min_order_size
    )
    if size < minimum:
        return Proposal(
            outcome=outcome,
            price=price,
            size=ZERO,
            fair=fair,
            required_edge=required_edge,
            repair=repair,
            reason="below_min_size" if free > 0 else "insufficient_inventory",
        )
    return Proposal(
        outcome=outcome,
        price=price,
        size=size,
        fair=fair,
        required_edge=required_edge,
        repair=repair,
    )
