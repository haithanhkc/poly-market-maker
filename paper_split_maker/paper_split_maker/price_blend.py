"""Stream estimate from Binance and the public Chainlink BTC/USD aggregator.

predict.fun settles on the Chainlink BTC/USDT Data Stream, which is the
Binance top-of-book mid. The on-chain BTC/USD print sits a fairly steady
USD/USDT amount below that. ``combined`` removes that offset, then adds the
Binance move since the Chainlink print:

    spot = chainlink + basis + (binance_now - binance_at_chainlink)

``basis`` is the rolling median of ``binance - chainlink`` measured when
Chainlink updates. A steady offset is not a disagreement. The quote still
bids both Up and Down. Fair value only sets the price and the lot.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass(frozen=True)
class PriceView:
    spot: Decimal | None
    binance: Decimal | None
    chainlink: Decimal | None
    binance_age_s: float | None
    chainlink_age_s: float | None
    price_gap: Decimal | None
    disagree: bool
    widen: bool
    spot_source: str
    basis: Decimal | None = None
    residual: Decimal | None = None


class BasisTracker:
    """Rolling median of the contemporaneous Binance-minus-Chainlink gap."""

    def __init__(self, keep_ms: int = 900_000):
        self.keep_ms = int(keep_ms)
        self._points: list[tuple[int, Decimal]] = []

    def add(self, ts_ms: int, gap: Decimal) -> None:
        stamp = int(ts_ms)
        self._points.append((stamp, gap))
        cutoff = stamp - self.keep_ms
        self._points = [item for item in self._points if item[0] >= cutoff]

    def median(self) -> Decimal | None:
        if not self._points:
            return None
        values = sorted((gap for _ts, gap in self._points), key=Decimal)
        count = len(values)
        mid = count // 2
        if count % 2 == 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2

    def __len__(self) -> int:
        return len(self._points)


def combine_prices(
    *,
    mode: str,
    binance: Decimal | None,
    chainlink: Decimal | None,
    binance_at_chainlink: Decimal | None,
    binance_age_s: float | None,
    chainlink_age_s: float | None,
    binance_stale_s: float,
    chainlink_stale_s: float,
    disagree_fraction: Decimal,
    basis: Decimal | None = None,
) -> PriceView:
    """Spot used for fair value. Widen only on a stale feed or a real residual."""
    chosen = mode if mode in {"binance", "chainlink", "combined"} else "binance"
    gap = None
    if binance is not None and chainlink is not None:
        gap = binance - chainlink
    binance_stale = _stale(binance, binance_age_s, binance_stale_s)
    chainlink_stale = _stale(chainlink, chainlink_age_s, chainlink_stale_s)
    if chosen == "binance":
        return PriceView(
            binance,
            binance,
            chainlink,
            binance_age_s,
            chainlink_age_s,
            gap,
            False,
            False,
            "binance" if binance is not None else "",
        )
    if chosen == "chainlink":
        return PriceView(
            chainlink,
            binance,
            chainlink,
            binance_age_s,
            chainlink_age_s,
            gap,
            False,
            chainlink_stale,
            "chainlink" if chainlink is not None else "",
        )
    spot, used, residual, disagree = _debiased_spot(
        binance,
        chainlink,
        binance_at_chainlink,
        basis,
        disagree_fraction,
    )
    widen = disagree or binance_stale or chainlink_stale
    return PriceView(
        spot,
        binance,
        chainlink,
        binance_age_s,
        chainlink_age_s,
        gap,
        disagree,
        widen,
        used,
        basis,
        residual,
    )


def _debiased_spot(binance, chainlink, binance_at_chainlink, basis, disagree_fraction):
    """Chainlink level in stream units, plus the Binance move since that print."""
    if chainlink is None and binance is None:
        return None, "", None, False
    if chainlink is None:
        return binance, "binance", None, False
    if binance is None or binance_at_chainlink is None:
        level = chainlink if basis is None else chainlink + basis
        return level, "chainlink", None, False
    offset = basis
    if offset is None:
        offset = binance_at_chainlink - chainlink
    spot = chainlink + offset + (binance - binance_at_chainlink)
    contemporaneous = binance_at_chainlink - chainlink
    residual = contemporaneous - offset
    disagree = False
    if chainlink > 0 and disagree_fraction > 0:
        disagree = abs(residual) / chainlink > disagree_fraction
    return spot, "combined", residual, disagree


def _stale(price, age_s, limit: float) -> bool:
    if price is None or age_s is None:
        return True
    return float(age_s) > float(limit)


def fair_side_targets(
    fair_up: Decimal,
    pair_target: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    """Bid targets for both sides. Their sum is at most ``pair_target``.

    Each target is ``fair_side - edge``. The underdog takes a little more of
    the edge, because that is the bid toxic flow is more likely to hit. The
    two edges still add up to ``1 - pair_target``.
    """
    fair_down = Decimal("1") - fair_up
    edge_total = Decimal("1") - pair_target
    if edge_total < 0:
        edge_total = Decimal("0")
    base = edge_total / 2
    tilt = (fair_up - Decimal("0.5")) * base
    cap = base / 2
    if tilt > cap:
        tilt = cap
    elif tilt < -cap:
        tilt = -cap
    up = _floor_tick(fair_up - (base - tilt), tick)
    down = _floor_tick(fair_down - (base + tilt), tick)
    while up > 0 and down > 0 and up + down > pair_target:
        if up >= down:
            up -= tick
        else:
            down -= tick
    return up, down


def _floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    units = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return units * tick
