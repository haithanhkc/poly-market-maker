"""One BTC estimate from Binance and the public Chainlink aggregator.

``combined`` keeps the Chainlink print as the level and adds the Binance move
since that print. The round open stays the predict.fun price-to-beat when the
payload has one, otherwise the Chainlink print. A disagreement, or a stale
feed, asks the quote to use the wider adverse gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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
) -> PriceView:
    """Spot used for fair value, plus whether the adverse gap should widen."""
    chosen = mode if mode in {"binance", "chainlink", "combined"} else "binance"
    gap = None
    if binance is not None and chainlink is not None and chainlink > 0:
        gap = binance - chainlink
    disagree = False
    if gap is not None and chainlink is not None and chainlink > 0:
        disagree = abs(gap) / chainlink > disagree_fraction
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
    spot, used = _combined_spot(binance, chainlink, binance_at_chainlink)
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
    )


def _combined_spot(binance, chainlink, binance_at_chainlink):
    if chainlink is not None and binance is not None and binance_at_chainlink is not None:
        return chainlink + (binance - binance_at_chainlink), "combined"
    if chainlink is not None:
        return chainlink, "chainlink"
    if binance is not None:
        return binance, "binance"
    return None, ""


def _stale(price, age_s, limit: float) -> bool:
    if price is None or age_s is None:
        return True
    return float(age_s) > float(limit)
