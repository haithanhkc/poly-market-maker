"""Decimal price, size, and gross-value helpers.

Pending and filled sale value in this package is gross: price times shares.
Fees are not subtracted. Rounding never increases a share size.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN


def D(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not accepted; use a decimal string")
    return Decimal(value)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def on_tick(price: Decimal, tick: Decimal) -> bool:
    if price <= 0 or tick <= 0:
        return False
    return price % tick == 0


def price_in_bounds(
    price: Decimal, tick: Decimal, floor: Decimal, ceiling: Decimal
) -> bool:
    if not on_tick(price, tick):
        return False
    if price < floor or price > ceiling:
        return False
    if price < tick:
        return False
    if price > (Decimal("1") - tick):
        return False
    return True


def shares_for_gross(
    gross: Decimal,
    price: Decimal,
    quantum: Decimal,
    min_size: Decimal,
    available: Decimal,
) -> Decimal | None:
    """Largest quantum-sized share count whose gross is still <= ``gross``.

    Returns None when the result is below the minimum size, above the
    available inventory, or would require rounding shares up.
    """

    if gross <= 0 or price <= 0 or quantum <= 0:
        return None
    size = floor_to_step(gross / price, quantum)
    size = min(size, floor_to_step(available, quantum))
    if size < min_size:
        return None
    while size >= min_size and size * price > gross:
        size -= quantum
    if size < min_size or size * price > gross:
        return None
    return size


def gross(shares: Decimal, price: Decimal) -> Decimal:
    return shares * price
