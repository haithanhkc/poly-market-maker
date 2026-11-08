"""Worst-case and best-case terminal equity.

Worst case for an unresolved binary market is cash already in the ledger plus
the worse of the two settlement branches. Each branch assumes every working
sell of the winning outcome fills and every sell of the losing outcome does
not. Partial fills are the open remainder. Fees in this formula are a
conservative reserve (rounded up), not the maker fee booked in cash.

Maker rebates are not an input to this module.
"""

from __future__ import annotations

from decimal import Decimal

from paper_split_maker.decimal_utils import FEE_Q, ONE, ZERO, floor_cash, qceil, qfloor
from paper_split_maker.models import WORKING_ORDER_STATUSES, Market, Order, State


class InvariantError(RuntimeError):
    pass


def conservative_fee(size: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    if size <= 0 or rate <= 0:
        return ZERO
    if price <= 0 or price >= 1:
        raw = size * rate
    else:
        raw = size * rate * price * (ONE - price)
    return qceil(raw, FEE_Q)


def sell_worst_term(size: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Cash-and-token change if `size` winning tokens are sold at `price`."""
    if size <= 0:
        return ZERO
    gross = floor_cash(size * price)
    return gross - conservative_fee(size, price, fee_rate) - size


def net_maker_proceeds(
    size: Decimal, price: Decimal, maker_fee_rate: Decimal
) -> Decimal:
    gross = floor_cash(size * price)
    return gross - conservative_fee(size, price, maker_fee_rate)


def working_orders(
    state: State, market_id: str, extra: list[Order] | None = None
) -> list[Order]:
    orders = [
        order
        for order in state.orders.values()
        if order.market_id == market_id and order.status in WORKING_ORDER_STATUSES
    ]
    if extra:
        orders.extend(order for order in extra if order.market_id == market_id)
    return orders


def reserved(state: State, market: Market, outcome: str) -> Decimal:
    total = ZERO
    for order in working_orders(state, market.market_id):
        if order.outcome == outcome:
            total += order.remaining
    return total


# Defaults of pair_min_edge and max_spread. A sell is a planned pair when its
# price plus the other side's posted ask, or the ask on a quotable book, clears
# the pair edge. A wide book is not a quote.
_PAIR_PLAN_EDGE = Decimal("0.01")
_PLAN_MAX_SPREAD = Decimal("0.10")


def _planned_opposite_price(state: State, market: Market, order: Order) -> Decimal:
    """Price we are posting or would post on the other side.

    A book wider than the quote spread is not a plan. Only a real posted ask
    counts then, so a one-tick bid on a broken book cannot waive the loss.
    """
    other = "down" if order.outcome == "up" else "up"
    book = market.book(other)
    bid = book.best_bid()
    ask = book.best_ask()
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    quotable = (
        bid is not None
        and ask is not None
        and ask > bid
        and ask - bid <= _PLAN_MAX_SPREAD
    )
    planned = (bid + tick) if quotable else ZERO
    if quotable and ask is not None and ask > planned:
        planned = ask
    for existing in working_orders(state, market.market_id):
        if (
            existing.order_id != order.order_id
            and existing.outcome == other
            and existing.price > planned
        ):
            planned = existing.price
    return planned


def _pair_planned(state: State, market: Market, order: Order) -> bool:
    if order.price <= 0:
        return False
    planned = _planned_opposite_price(state, market, order)
    return order.price + planned >= Decimal("1") + _PAIR_PLAN_EDGE


def market_contribution(
    state: State, market: Market, extra: list[Order] | None = None
) -> Decimal:
    if market.status == "redeemed":
        return ZERO
    up_terms = ZERO
    down_terms = ZERO
    orders = working_orders(state, market.market_id, extra)
    for order in orders:
        # The other side's planned ask covers this price, so resolution still
        # pays about 1 for the pair. Do not value those shares as a total loss.
        size = ZERO if _pair_planned(state, market, order) else order.remaining
        term = sell_worst_term(size, order.price, state.risk_fee_rate)
        if order.outcome == "up":
            up_terms += term
        elif order.outcome == "down":
            down_terms += term
        else:
            raise InvariantError(f"bad outcome {order.outcome}")
    value_up = market.up + up_terms
    value_down = market.down + down_terms
    known = (
        market.status == "resolved"
        and market.winner in ("up", "down")
        and not market.resolution_conflict
    )
    if known and market.winner == "up":
        value = value_up
    elif known and market.winner == "down":
        value = value_down
    else:
        value = value_up if value_up <= value_down else value_down
    if market.reserve_active:
        value -= market.operational_reserve_amount
    return value


def session_loss(state: State, market: Market) -> Decimal:
    """Loss already booked in one market, ignoring orders that have not filled.

    A fresh split is zero because the two sides still pair. After a sale, the
    paired remainder is still worth one USDC per share and the unpaired
    remainder is worth nothing if its side loses. Unfilled quotes stay under
    the account floor; they do not spend this per-session budget.
    """
    if market.status == "redeemed":
        return -market.net_cash_flow if market.net_cash_flow < 0 else ZERO
    paired = market.up if market.up < market.down else market.down
    terminal = market.net_cash_flow + paired
    if terminal >= 0:
        return ZERO
    return -terminal


def worst_case_equity(
    state: State, extra: list[Order] | None = None, cash_delta: Decimal = ZERO
) -> Decimal:
    total = state.cash + cash_delta - state.global_rounding_reserve
    for market in state.markets.values():
        total += market_contribution(state, market, extra if extra else None)
    return qfloor(total, Decimal("0.000001"))


def best_case_equity(state: State) -> Decimal:
    """Upper payoff inside the model. Rebates and the risk-fee reserve are excluded."""
    total = state.cash
    for market in state.markets.values():
        if market.status == "redeemed":
            continue
        up_proceeds = ZERO
        down_proceeds = ZERO
        for order in working_orders(state, market.market_id):
            proceeds = net_maker_proceeds(
                order.remaining, order.price, state.maker_fee_rate
            )
            if order.outcome == "up":
                up_proceeds += proceeds
            else:
                down_proceeds += proceeds
        best_up = market.up + down_proceeds
        best_down = market.down + up_proceeds
        known = (
            market.status == "resolved"
            and market.winner in ("up", "down")
            and not market.resolution_conflict
        )
        if known and market.winner == "up":
            total += best_up
        elif known and market.winner == "down":
            total += best_down
        else:
            total += best_up if best_up >= best_down else best_down
    return total


def assert_ledger_invariants(state: State) -> None:
    # A fill can leave worst-case equity under the floor. That blocks new
    # splits and buys. It must not raise inside a tick, or the bot freezes
    # and cannot sell, cancel, requote, merge, or redeem what it already holds.
    if state.cash < 0:
        raise InvariantError("cash is negative")
    if state.loss_floor > state.initial_capital:
        raise InvariantError("floor exceeds initial capital")
    for market in state.markets.values():
        if market.up < 0 or market.down < 0:
            raise InvariantError(f"{market.market_id} inventory is negative")
        if market.gross_split < 0:
            raise InvariantError("gross split is negative")
        up_reserved = reserved(state, market, "up")
        down_reserved = reserved(state, market, "down")
        if up_reserved > market.up or down_reserved > market.down:
            raise InvariantError(
                f"{market.market_id} reserved inventory exceeds holdings"
            )
        if market.up_token_id == market.down_token_id:
            raise InvariantError("outcome tokens are not distinct")
    for order in state.orders.values():
        if order.remaining < 0 or order.size < 0 or order.queue_ahead < 0:
            raise InvariantError(f"{order.order_id} has a negative size")
        if order.remaining > order.size + Decimal("0.000000001"):
            raise InvariantError(f"{order.order_id} remaining exceeds original size")
        if order.status in WORKING_ORDER_STATUSES and order.remaining <= 0:
            raise InvariantError(f"{order.order_id} is working with no remainder")
        market = state.markets.get(order.market_id)
        if market is None:
            raise InvariantError(f"{order.order_id} has no market")
        if order.outcome not in ("up", "down"):
            raise InvariantError("order outcome is not up or down")
