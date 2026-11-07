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


def market_contribution(
    state: State, market: Market, extra: list[Order] | None = None
) -> Decimal:
    if market.status == "redeemed":
        return ZERO
    up_terms = ZERO
    down_terms = ZERO
    for order in working_orders(state, market.market_id, extra):
        term = sell_worst_term(order.remaining, order.price, state.risk_fee_rate)
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
    worst = worst_case_equity(state)
    if worst < state.loss_floor:
        raise InvariantError(
            f"worst-case equity {worst} is below the locked floor {state.loss_floor}"
        )
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
