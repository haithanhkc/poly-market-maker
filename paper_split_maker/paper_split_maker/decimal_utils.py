"""Decimal helpers. Money never passes through binary floats."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR

CASH_Q = Decimal("0.000001")
FEE_Q = Decimal("0.00001")
ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: object) -> Decimal:
    """Parse a decimal from a string, int, Decimal, or a short JSON float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a decimal: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty decimal")
        return Decimal(text)
    raise ValueError(f"not a decimal: {value!r}")


def qdown(value: Decimal, quantum: Decimal) -> Decimal:
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    units = (value / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum


def qfloor(value: Decimal, quantum: Decimal) -> Decimal:
    """Quantize toward -infinity so a worst-case figure is never rounded up."""
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    units = (value / quantum).to_integral_value(rounding=ROUND_FLOOR)
    return units * quantum


def qceil(value: Decimal, quantum: Decimal) -> Decimal:
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    if value <= 0:
        return ZERO
    units = (value / quantum).to_integral_value(rounding=ROUND_CEILING)
    return units * quantum


def floor_cash(value: Decimal) -> Decimal:
    return qdown(value, CASH_Q)


def ceil_tick(price: Decimal, tick: Decimal) -> Decimal:
    return qceil(price, tick)


def on_grid(value: Decimal, quantum: Decimal) -> bool:
    return qdown(value, quantum) == value


def money(value: Decimal) -> str:
    return format(value, "f")
