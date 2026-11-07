"""Settlement floor, separate marks, and informational rebate.

The settlement lower bound is:

    gross proceeds + min(remaining up, remaining down) - cost - fees

Paired shares are counted once, at one dollar. Unpaired shares are counted at
zero. Unpaid maker rebates are not cash. Maker fee rate on crypto markets is
documented as zero; taker fee math exists only so a hypothetical bid-walk
estimate can show its own fee. This package never submits a taker order.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from poly_market_maker.sell_equal.quant import D

_FEE_QUANTUM = Decimal("0.00001")


def money(value: Decimal) -> Decimal:
    return value.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_UP)


def curve_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Official curve: C * rate * p * (1 - p), rounded to 5 decimal places."""

    if shares <= 0 or rate <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    raw = shares * rate * price * (Decimal("1") - price)
    rounded = money(raw)
    if Decimal("0") < rounded < _FEE_QUANTUM:
        return Decimal("0")
    return rounded


def settlement_lower_bound(
    gross_proceeds: Decimal,
    remaining_up: Decimal,
    remaining_down: Decimal,
    cost_basis: Decimal,
    fees: Decimal,
) -> Decimal:
    paired = min(remaining_up, remaining_down)
    return gross_proceeds + paired - cost_basis - fees


def unpaid_rebate_estimate(
    shares: Decimal, price: Decimal, taker_rate: Decimal, rebate_share: Decimal
) -> Decimal:
    """Daily pool estimate. Not cash and not a risk credit."""

    if rebate_share <= 0:
        return Decimal("0")
    return money(curve_fee(shares, price, taker_rate) * rebate_share)


def midpoint_mark(
    gross_proceeds: Decimal,
    remaining_up: Decimal,
    remaining_down: Decimal,
    mid_up: Decimal | None,
    mid_down: Decimal | None,
    cost_basis: Decimal,
    fees: Decimal,
) -> Decimal | None:
    if mid_up is None or mid_down is None:
        return None
    held = remaining_up * mid_up + remaining_down * mid_down
    return gross_proceeds + held - cost_basis - fees


def bid_walk_cash(
    levels: list[tuple[Decimal, Decimal]], shares: Decimal, taker_rate: Decimal
) -> tuple[Decimal, Decimal]:
    """Walk displayed bids. Not an order. Unsold size is left unsold."""

    left = shares
    cash = Decimal("0")
    fees = Decimal("0")
    for price, size in levels:
        if left <= 0:
            break
        take = min(left, size)
        cash += take * price
        fees += curve_fee(take, price, taker_rate)
        left -= take
    return cash, fees


def as_decimal(value: Decimal | int | str) -> Decimal:
    return D(value)
