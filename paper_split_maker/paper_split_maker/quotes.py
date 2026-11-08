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


def pair_min_price(market: Market, outcome: str, config: Config):
    """Minimum offer for the unfilled side of shares already sold.

    A max_pair_loss of 1 or more turns the floor off. Fees already taken out
    of sale proceeds lower the average price and raise this floor.
    """
    if config.max_pair_loss >= 1:
        return None
    if outcome == "down":
        shares = market.sold_up_shares
        proceeds = market.sold_up_proceeds
        other = market.sold_down_shares
    else:
        shares = market.sold_down_shares
        proceeds = market.sold_down_proceeds
        other = market.sold_up_shares
    if shares <= 0 or shares <= other:
        return None
    average = proceeds / shares
    floor = Decimal("1") - average - config.max_pair_loss
    if floor <= 0:
        return None
    return floor


def ladder_prices(
    base: Decimal,
    tick: Decimal,
    count: int,
    bid: Decimal | None,
    *,
    inside: bool,
) -> list[Decimal]:
    """Maker prices: optional one tick inside the spread, then the base and above."""
    prices: list[Decimal] = []
    if inside and bid is not None and count > 1:
        inside_px = bid + tick
        if bid < inside_px < base and inside_px < 1:
            prices.append(inside_px)
    price = base
    while len(prices) < count and price < 1:
        if price not in prices and price > (bid or Decimal("0")):
            prices.append(price)
        price += tick
    return prices


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
    floor = pair_min_price(market, outcome, config)
    if floor is not None and price < floor:
        price = ceil_tick(floor, market.tick_size)
    if price < config.min_price or price > config.max_price or price >= 1:
        return _no("pair_floor" if floor is not None else "no_edge", outcome)
    if price <= bid:
        return _no(
            "pair_floor" if floor is not None and price <= bid else "would_cross",
            outcome,
        )
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
