"""Tactics and the risk lock copied into a new paper ledger.

Risk-lock fields are stored in the ledger at creation. A later process that
loads the same ledger keeps those values. Editing the config does not reset
the loss floor, widen the per-market budget, or shrink cancel latency.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path

from paper_split_maker.decimal_utils import D


class ConfigError(ValueError):
    pass


ALLOWED_SERIES = frozenset(
    {"btc-updown-5m", "btc-updown-15m", "btc-updown-1h"}
)


_FORBIDDEN_PARTS = (
    "private",
    "secret",
    "passphrase",
    "mnemonic",
    "seed_phrase",
    "api_key",
    "apikey",
    "signature",
    "live_trading",
    "enable_live",
)


@dataclass
class Config:
    initial_capital: Decimal = Decimal("1000")
    max_loss_fraction: Decimal = Decimal("0.03")
    per_market_budget: Decimal = Decimal("40")
    clip_size: Decimal = Decimal("5")
    min_order_size: Decimal = Decimal("5")
    order_size_quantum: Decimal = Decimal("0.01")
    default_tick: Decimal = Decimal("0.01")
    min_price: Decimal = Decimal("0.01")
    max_price: Decimal = Decimal("0.99")
    min_edge: Decimal = Decimal("0.01")
    skew_edge_per_share: Decimal = Decimal("0.002")
    skew_size_per_share: Decimal = Decimal("1")
    max_spread: Decimal = Decimal("0.10")
    stale_after_ms: int = 3000
    clock_skew_ms: int = 5000
    quote_cooldown_ms: int = 500
    stop_quoting_before_expiry_ms: int = 20000
    ack_latency_ms: int = 250
    cancel_latency_ms: int = 500
    fill_mode: str = "queue"
    trade_through_fills: bool = False
    extra_queue_shares: Decimal = Decimal("0")
    improve_queue_shares: Decimal = Decimal("5")
    maker_fee_rate: Decimal = Decimal("0")
    risk_fee_rate: Decimal = Decimal("0.07")
    published_taker_fee_rate: Decimal = Decimal("0.07")
    rebate_share: Decimal = Decimal("0.20")
    operational_reserve: Decimal = Decimal("0.05")
    global_rounding_reserve: Decimal = Decimal("0.01")
    freeze_band: Decimal = Decimal("0")
    max_open_markets: int = 2
    split_clip: Decimal = Decimal("20")
    min_pair_inventory: Decimal = Decimal("10")
    auto_split: bool = True
    merge_when_idle: bool = True
    paper_duration_seconds: int = 20
    paper_max_markets: int = 1
    poll_interval_seconds: float = 2.0
    market_slugs: tuple[str, ...] = ()
    # Live is off unless this is true AND the CLI flag AND the risk confirmation.
    live: bool = False
    # Recurring predict.fun series. 5m, 15m, and 1h are the BTC up/down books.
    series_slug: str = "btc-updown-5m"
    # small_pair discovers these series. The default is 5m and 15m together.
    small_pair_series: tuple[str, ...] = ("btc-updown-5m", "btc-updown-15m")
    # 0 disables the per-session cap. The aggregate floor still applies.
    session_max_loss: Decimal = Decimal("0")
    # 0 cancels on any price change. 1 cancels only after a full tick.
    requote_min_ticks: int = 0
    requote_min_interval_ms: int = 0
    # A resting sell is left alone for this long, and afterwards only if the
    # target moved more than max(2 ticks, requote_vol_k * short-window mid stdev).
    min_order_rest_seconds: int = 20
    requote_vol_k: Decimal = Decimal("1")
    requote_vol_window_ms: int = 30_000
    # Inventory drift at or below this does not cancel a resting sell.
    requote_size_threshold: Decimal = Decimal("1")
    # After the open, give posts this long before unwinding a session whose
    # sells are failing. Floor-blocked quotes are left until the normal close.
    sell_ready_timeout_seconds: int = 30
    # A sell that pairs already-sold opposite shares must clear 1 + this - avg.
    pair_min_edge: Decimal = Decimal("0.01")
    # One side may be sold this far ahead of the other, as a fraction of the split.
    max_unpaired_fraction: Decimal = Decimal("0.5")
    # Live books are stale only after this long with no message, not on a quiet book.
    stale_feed_seconds: int = 10
    # Split the next session this many seconds before it opens.
    # Quoting of the current session stops on stop_quoting_before_expiry_ms,
    # not on this lead. 300s is one full window, which is their pre-open split.
    split_lead_seconds: int = 300
    # Stop new sells after both sides together have sold this fraction of the
    # split. 0 leaves it off. Their train-set 90th percentile is about 0.35.
    session_sell_fraction: Decimal = Decimal("0")
    # When set, a resting rung is cancelled only if the new price is at least
    # one tick away or the bid has reached the order.
    queue_priority: bool = False
    # Hard stop for a live run. The duration flag does not cut a session off.
    max_runtime_seconds: int = 2400
    # Give up on a redeem after this many attempts and leave it for redeem-pending.
    redeem_max_attempts: int = 5
    # single keeps one maker sell per side. ladder posts a few post-only
    # rungs around the mid and skews size toward the side that is falling.
    # dual_bid buys both outcomes during the open round and holds them.
    # small_pair is the paper 0.96 pair on BTC 5m and 15m. It is paper-only.
    strategy: str = "single"
    # dual_bid quotes this many seconds after the round opens, and pulls
    # every bid at the stop. 8s and 275s sit in the 5-10s and 270-285s windows.
    dual_bid_start_seconds: int = 8
    dual_bid_stop_seconds: int = 275
    dual_bid_levels: int = 6
    dual_bid_clip: Decimal = Decimal("5")
    dual_bid_max_clip: Decimal = Decimal("24")
    dual_bid_requote_ms: int = 1500
    target_pair_cost: Decimal = Decimal("0.97")
    pair_cost_stop: Decimal = Decimal("1")
    min_share_balance: Decimal = Decimal("0.85")
    min_bid_price: Decimal = Decimal("0.10")
    round_budget: Decimal = Decimal("25")
    # Off unless --binance-lean or this flag. The book-mid quote is unchanged.
    small_pair_binance: bool = False
    # Kept so older configs still load. The lean no longer recentres on fair value.
    small_pair_fair_gap: Decimal = Decimal("0.03")
    # After this many seconds, stop a side whose fair value is below small_pair_loser_fair.
    small_pair_loser_fair: Decimal = Decimal("0.45")
    small_pair_loser_after_seconds: int = 30
    # Drop a bid that is more than this far above that side's fair value.
    # The wider gap is used when the Binance print is older than small_pair_stale_seconds.
    small_pair_adverse_gap: Decimal = Decimal("0.02")
    small_pair_adverse_gap_stale: Decimal = Decimal("0.03")
    small_pair_stale_seconds: int = 2
    # Per-second log-return volatility floor. About 56% annualized.
    small_pair_sigma_floor: Decimal = Decimal("0.0001")
    # Realized vol window. 900 seconds is 15 minutes. The multiplier cannot exceed 1.
    small_pair_vol_lookback_seconds: int = 900
    small_pair_sigma_multiplier: Decimal = Decimal("1")
    # P >= favorite_fair may hold this many excess shares. P < 0.5 is capped lower.
    small_pair_favorite_excess: Decimal = Decimal("12")
    small_pair_underdog_excess: Decimal = Decimal("4")
    small_pair_favorite_fair: Decimal = Decimal("0.55")
    # |mid - 0.5| below this band uses the nearer pair sum, unless the mode halves the lot.
    small_pair_neutral_band: Decimal = Decimal("0.10")
    small_pair_neutral_target: Decimal = Decimal("0.94")
    small_pair_neutral_mode: str = "sum"
    # Binance mode waits this long after the open, and pulls P < 0.5 from this second.
    small_pair_start_seconds: int = 10
    small_pair_late_loser_seconds: int = 240
    # Per-session spend is min(round budget, cap, equity * fraction) when the lean is on.
    session_budget_cap: Decimal = Decimal("25")
    session_budget_fraction: Decimal = Decimal("0.25")
    session_budget_min: Decimal = Decimal("1")
    # binance keeps the current filter. chainlink or combined opt into the blend.
    price_source: str = "binance"
    # |binance - chainlink| / chainlink above this widens the adverse gap.
    small_pair_price_disagree: Decimal = Decimal("0.001")
    small_pair_disagree_gap: Decimal = Decimal("0.04")
    # The on-chain aggregator is fresh for this long after updatedAt.
    chainlink_stale_seconds: int = 3600
    chainlink_poll_seconds: int = 5
    ladder_levels: int = 3
    ladder_spacing: Decimal = Decimal("0.01")
    # Join the touch on a side once its mid is this far below the other.
    skew_mid_gap: Decimal = Decimal("0.04")
    # In this many seconds before the stop, the winner rests at or above
    # winner_min_price and the loser joins the bid. Still post-only.
    late_window_seconds: int = 60
    winner_min_price: Decimal = Decimal("0.90")
    # After one sell leg fills, keep checking the other leg for this long.
    # A bid at or above 1 - fill_price is taken immediately; this is the
    # backstop that repeats that check and then allows a capped loss.
    leg_unwind_seconds: int = 5
    split_confirm_timeout_ms: int = 30_000
    kill_switch_file: str = ""
    gas_limit: int = 500_000
    max_fee_gwei: Decimal = Decimal("0")
    priority_fee_gwei: Decimal = Decimal("0")

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ConfigError("initial_capital must be positive")
        if self.max_loss_fraction < 0 or self.max_loss_fraction >= 1:
            raise ConfigError("max_loss_fraction must be in [0, 1)")
        if self.per_market_budget <= 0:
            raise ConfigError("per_market_budget must be positive")
        if self.per_market_budget > self.initial_capital:
            raise ConfigError("per_market_budget cannot exceed initial_capital")
        if self.clip_size <= 0 or self.min_order_size <= 0:
            raise ConfigError("sizes must be positive")
        if self.clip_size < self.min_order_size:
            raise ConfigError("clip_size must be at least min_order_size")
        if self.order_size_quantum <= 0 or self.default_tick <= 0:
            raise ConfigError("quantums must be positive")
        if (
            self.min_price <= 0
            or self.max_price >= 1
            or self.min_price > self.max_price
        ):
            raise ConfigError("price bounds must sit inside (0, 1)")
        if (
            self.min_edge < 0
            or self.skew_edge_per_share < 0
            or self.skew_size_per_share < 0
        ):
            raise ConfigError("edge and skew must be non-negative")
        if self.max_spread <= 0:
            raise ConfigError("max_spread must be positive")
        for name in (
            "stale_after_ms",
            "clock_skew_ms",
            "quote_cooldown_ms",
            "stop_quoting_before_expiry_ms",
            "ack_latency_ms",
            "cancel_latency_ms",
        ):
            if int(getattr(self, name)) < 0:
                raise ConfigError(f"{name} must be non-negative")
        if self.fill_mode not in ("queue", "none"):
            raise ConfigError("fill_mode must be 'queue' or 'none'")
        if self.maker_fee_rate < 0 or self.risk_fee_rate < 0:
            raise ConfigError("fee rates must be non-negative")
        if self.risk_fee_rate < self.maker_fee_rate:
            raise ConfigError("risk_fee_rate must be at least maker_fee_rate")
        if self.published_taker_fee_rate < 0 or self.rebate_share < 0:
            raise ConfigError("rebate inputs must be non-negative")
        if self.rebate_share > 1:
            raise ConfigError("rebate_share must be at most 1")
        if self.operational_reserve < 0 or self.global_rounding_reserve < 0:
            raise ConfigError("reserves must be non-negative")
        if self.freeze_band < 0:
            raise ConfigError("freeze_band must be non-negative")
        if self.max_open_markets < 1:
            raise ConfigError("max_open_markets must be at least 1")
        if self.split_clip <= 0 or self.min_pair_inventory < 0:
            raise ConfigError("split settings are invalid")
        if self.session_max_loss < 0:
            raise ConfigError("session_max_loss must be non-negative")
        if self.requote_min_ticks < 0 or self.requote_min_interval_ms < 0:
            raise ConfigError("requote limits must be non-negative")
        if (
            self.min_order_rest_seconds < 0
            or self.sell_ready_timeout_seconds < 0
        ):
            raise ConfigError("rest and close windows must be non-negative")
        if self.requote_vol_k < 0 or self.requote_size_threshold < 0:
            raise ConfigError("requote volatility settings must be non-negative")
        if self.pair_min_edge < 0:
            raise ConfigError("pair_min_edge must be non-negative")
        if self.max_unpaired_fraction < 0 or self.max_unpaired_fraction > 1:
            raise ConfigError("max_unpaired_fraction must be in [0, 1]")
        if self.stale_feed_seconds < 1:
            raise ConfigError("stale_feed_seconds must be at least 1")
        if self.split_lead_seconds < 0:
            raise ConfigError("split_lead_seconds must be non-negative")
        if self.session_sell_fraction < 0 or self.session_sell_fraction > 1:
            raise ConfigError("session_sell_fraction must be in [0, 1]")
        if self.max_runtime_seconds <= 0:
            raise ConfigError("max_runtime_seconds must be positive")
        if self.redeem_max_attempts < 1:
            raise ConfigError("redeem_max_attempts must be at least 1")
        if self.strategy not in ("single", "ladder", "dual_bid", "small_pair"):
            raise ConfigError("strategy must be single, ladder, dual_bid, or small_pair")
        if self.dual_bid_start_seconds < 0 or self.dual_bid_stop_seconds <= self.dual_bid_start_seconds:
            raise ConfigError("dual_bid quote window is invalid")
        if self.dual_bid_levels < 1 or self.dual_bid_levels > 20:
            raise ConfigError("dual_bid_levels must be from 1 to 20")
        if self.dual_bid_clip <= 0 or self.dual_bid_max_clip < self.dual_bid_clip:
            raise ConfigError("dual_bid clip sizes are invalid")
        if self.dual_bid_max_clip > 100:
            raise ConfigError("dual_bid_max_clip must be at most 100")
        if self.dual_bid_requote_ms < 0:
            raise ConfigError("dual_bid_requote_ms must be non-negative")
        if self.target_pair_cost <= 0 or self.target_pair_cost > 1:
            raise ConfigError("target_pair_cost must be in (0, 1]")
        if self.pair_cost_stop < self.target_pair_cost or self.pair_cost_stop > Decimal("2"):
            raise ConfigError("pair_cost_stop must sit between the target and 2")
        if self.min_share_balance <= 0 or self.min_share_balance > 1:
            raise ConfigError("min_share_balance must be in (0, 1]")
        if self.min_bid_price <= 0 or self.min_bid_price >= 1:
            raise ConfigError("min_bid_price must sit inside (0, 1)")
        if self.round_budget <= 0:
            raise ConfigError("round_budget must be positive")
        if self.series_slug not in ALLOWED_SERIES:
            raise ConfigError(
                "series_slug must be btc-updown-5m, btc-updown-15m, or btc-updown-1h"
            )
        if not self.small_pair_series:
            raise ConfigError("small_pair_series must name at least one series")
        unknown = [item for item in self.small_pair_series if item not in ALLOWED_SERIES]
        if unknown:
            raise ConfigError(
                "small_pair_series must be btc-updown-5m, btc-updown-15m, or btc-updown-1h"
            )
        if self.small_pair_fair_gap < 0 or self.small_pair_fair_gap >= 1:
            raise ConfigError("small_pair_fair_gap must be in [0, 1)")
        if self.small_pair_loser_fair <= 0 or self.small_pair_loser_fair > Decimal("0.5"):
            raise ConfigError("small_pair_loser_fair must sit inside (0, 0.5]")
        if self.small_pair_loser_after_seconds < 0:
            raise ConfigError("small_pair_loser_after_seconds must be non-negative")
        if self.small_pair_adverse_gap <= 0 or self.small_pair_adverse_gap >= 1:
            raise ConfigError("small_pair_adverse_gap must sit inside (0, 1)")
        if self.small_pair_adverse_gap_stale < self.small_pair_adverse_gap:
            raise ConfigError("small_pair_adverse_gap_stale must be at least the fresh gap")
        if self.small_pair_adverse_gap_stale >= 1:
            raise ConfigError("small_pair_adverse_gap_stale must be below 1")
        if self.small_pair_stale_seconds < 0:
            raise ConfigError("small_pair_stale_seconds must be non-negative")
        if self.small_pair_sigma_floor <= 0:
            raise ConfigError("small_pair_sigma_floor must be positive")
        if self.small_pair_vol_lookback_seconds < 60:
            raise ConfigError("small_pair_vol_lookback_seconds must be at least 60")
        if self.small_pair_sigma_multiplier <= 0 or self.small_pair_sigma_multiplier > 1:
            raise ConfigError("small_pair_sigma_multiplier must sit inside (0, 1]")
        if self.small_pair_underdog_excess < 2 or self.small_pair_underdog_excess > 4:
            raise ConfigError("small_pair_underdog_excess must sit inside [2, 4]")
        if self.small_pair_favorite_excess < self.small_pair_underdog_excess:
            raise ConfigError("small_pair_favorite_excess must be at least the underdog cap")
        if self.small_pair_favorite_fair <= Decimal("0.5") or self.small_pair_favorite_fair >= 1:
            raise ConfigError("small_pair_favorite_fair must sit inside (0.5, 1)")
        if self.small_pair_neutral_band <= 0 or self.small_pair_neutral_band >= Decimal("0.5"):
            raise ConfigError("small_pair_neutral_band must sit inside (0, 0.5)")
        if self.small_pair_neutral_target <= 0 or self.small_pair_neutral_target > 1:
            raise ConfigError("small_pair_neutral_target must sit inside (0, 1]")
        if self.small_pair_neutral_mode not in {"sum", "half_lot"}:
            raise ConfigError("small_pair_neutral_mode must be sum or half_lot")
        if self.small_pair_start_seconds < 0:
            raise ConfigError("small_pair_start_seconds must be non-negative")
        if self.small_pair_late_loser_seconds < 0:
            raise ConfigError("small_pair_late_loser_seconds must be non-negative")
        if self.session_budget_cap <= 0:
            raise ConfigError("session_budget_cap must be positive")
        if self.session_budget_fraction <= 0 or self.session_budget_fraction > 1:
            raise ConfigError("session_budget_fraction must sit inside (0, 1]")
        if self.session_budget_min <= 0:
            raise ConfigError("session_budget_min must be positive")
        if self.session_budget_min > self.session_budget_cap:
            raise ConfigError("session_budget_min cannot exceed session_budget_cap")
        if self.price_source not in {"binance", "chainlink", "combined"}:
            raise ConfigError("price_source must be binance, chainlink, or combined")
        if self.small_pair_price_disagree <= 0 or self.small_pair_price_disagree >= 1:
            raise ConfigError("small_pair_price_disagree must sit inside (0, 1)")
        if self.small_pair_disagree_gap < self.small_pair_adverse_gap_stale:
            raise ConfigError("small_pair_disagree_gap must be at least the stale gap")
        if self.small_pair_disagree_gap >= 1:
            raise ConfigError("small_pair_disagree_gap must be below 1")
        if self.chainlink_stale_seconds < 1:
            raise ConfigError("chainlink_stale_seconds must be at least 1")
        if self.chainlink_poll_seconds < 1:
            raise ConfigError("chainlink_poll_seconds must be at least 1")
        if self.ladder_levels < 1:
            raise ConfigError("ladder_levels must be at least 1")
        if self.ladder_spacing <= 0:
            raise ConfigError("ladder_spacing must be positive")
        if self.skew_mid_gap < 0:
            raise ConfigError("skew_mid_gap must be non-negative")
        if self.late_window_seconds < 0:
            raise ConfigError("late_window_seconds must be non-negative")
        if self.winner_min_price <= 0 or self.winner_min_price >= 1:
            raise ConfigError("winner_min_price must sit inside (0, 1)")
        if self.leg_unwind_seconds < 0:
            raise ConfigError("leg_unwind_seconds must be non-negative")
        if self.requote_vol_window_ms < 0:
            raise ConfigError("requote_vol_window_ms must be non-negative")
        if self.split_confirm_timeout_ms < 0:
            raise ConfigError("timing settings must be non-negative")
        if self.gas_limit < 0 or self.max_fee_gwei < 0 or self.priority_fee_gwei < 0:
            raise ConfigError("gas settings must be non-negative")
        if self.paper_duration_seconds <= 0:
            raise ConfigError("paper_duration_seconds must be positive")
        if self.paper_max_markets < 1:
            raise ConfigError("paper_max_markets must be at least 1")
        if self.poll_interval_seconds <= 0:
            raise ConfigError("poll_interval_seconds must be positive")
        loss_budget = q_loss_budget(self.initial_capital, self.max_loss_fraction)
        if loss_budget < 0 or loss_budget >= self.initial_capital:
            raise ConfigError("loss budget must be inside the bankroll")

    def loss_budget(self) -> Decimal:
        return q_loss_budget(self.initial_capital, self.max_loss_fraction)

    def loss_floor(self) -> Decimal:
        return self.initial_capital - self.loss_budget()


def q_loss_budget(capital: Decimal, fraction: Decimal) -> Decimal:
    from paper_split_maker.decimal_utils import floor_cash

    return floor_cash(capital * fraction)


def _forbidden(key: str) -> bool:
    lowered = key.strip().lower().replace("-", "_")
    if lowered in {"sign", "signing"}:
        return True
    return any(part in lowered for part in _FORBIDDEN_PARTS)


_DECIMAL_FIELDS = {
    "initial_capital",
    "max_loss_fraction",
    "per_market_budget",
    "clip_size",
    "min_order_size",
    "order_size_quantum",
    "default_tick",
    "min_price",
    "max_price",
    "min_edge",
    "skew_edge_per_share",
    "skew_size_per_share",
    "max_spread",
    "extra_queue_shares",
    "improve_queue_shares",
    "maker_fee_rate",
    "risk_fee_rate",
    "published_taker_fee_rate",
    "rebate_share",
    "operational_reserve",
    "global_rounding_reserve",
    "freeze_band",
    "split_clip",
    "min_pair_inventory",
    "session_max_loss",
    "requote_vol_k",
    "requote_size_threshold",
    "pair_min_edge",
    "session_sell_fraction",
    "max_unpaired_fraction",
    "ladder_spacing",
    "skew_mid_gap",
    "winner_min_price",
    "max_fee_gwei",
    "priority_fee_gwei",
    "dual_bid_clip",
    "dual_bid_max_clip",
    "target_pair_cost",
    "pair_cost_stop",
    "min_share_balance",
    "min_bid_price",
    "round_budget",
    "small_pair_fair_gap",
    "small_pair_loser_fair",
    "small_pair_adverse_gap",
    "small_pair_adverse_gap_stale",
    "small_pair_sigma_floor",
    "small_pair_sigma_multiplier",
    "small_pair_favorite_excess",
    "small_pair_underdog_excess",
    "small_pair_favorite_fair",
    "small_pair_neutral_band",
    "small_pair_neutral_target",
    "session_budget_cap",
    "session_budget_fraction",
    "session_budget_min",
    "small_pair_price_disagree",
    "small_pair_disagree_gap",
}
_INT_FIELDS = {
    "stale_after_ms",
    "clock_skew_ms",
    "quote_cooldown_ms",
    "stop_quoting_before_expiry_ms",
    "ack_latency_ms",
    "cancel_latency_ms",
    "max_open_markets",
    "paper_duration_seconds",
    "paper_max_markets",
    "requote_min_ticks",
    "requote_min_interval_ms",
    "min_order_rest_seconds",
    "requote_vol_window_ms",
    "sell_ready_timeout_seconds",
    "stale_feed_seconds",
    "split_lead_seconds",
    "max_runtime_seconds",
    "redeem_max_attempts",
    "leg_unwind_seconds",
    "ladder_levels",
    "late_window_seconds",
    "split_confirm_timeout_ms",
    "gas_limit",
    "dual_bid_start_seconds",
    "dual_bid_stop_seconds",
    "dual_bid_levels",
    "dual_bid_requote_ms",
    "small_pair_loser_after_seconds",
    "small_pair_stale_seconds",
    "small_pair_vol_lookback_seconds",
    "small_pair_start_seconds",
    "small_pair_late_loser_seconds",
    "chainlink_stale_seconds",
    "chainlink_poll_seconds",
}
_BOOL_FIELDS = {
    "trade_through_fills",
    "queue_priority",
    "auto_split",
    "merge_when_idle",
    "live",
    "small_pair_binance",
}


def config_from_mapping(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be an object")
    allowed = {item.name for item in fields(Config)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    for key in data:
        if _forbidden(str(key)):
            raise ConfigError(f"forbidden config key: {key}")
    kwargs = {}
    for key, value in data.items():
        if key in _DECIMAL_FIELDS:
            try:
                kwargs[key] = D(value)
            except (ValueError, ArithmeticError) as exc:
                raise ConfigError(f"{key} is not a decimal") from exc
        elif key in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ConfigError(f"{key} is not an integer")
            kwargs[key] = int(value)
        elif key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ConfigError(f"{key} is not a boolean")
            kwargs[key] = value
        elif key == "fill_mode":
            kwargs[key] = str(value)
        elif key == "strategy":
            kwargs[key] = str(value)
        elif key == "series_slug":
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("series_slug must be a string")
            kwargs[key] = value.strip()
        elif key == "poll_interval_seconds":
            kwargs[key] = float(value)
        elif key == "market_slugs":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ConfigError("market_slugs must be a list of strings")
            kwargs[key] = tuple(value)
        elif key == "small_pair_series":
            kwargs[key] = parse_series_list(value)
        elif key == "price_source":
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("price_source must be a string")
            kwargs[key] = value.strip()
        elif key == "small_pair_neutral_mode":
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("small_pair_neutral_mode must be a string")
            kwargs[key] = value.strip()
        elif key == "kill_switch_file":
            if not isinstance(value, str):
                raise ConfigError("kill_switch_file must be a string")
            kwargs[key] = value
        else:
            raise ConfigError(f"unhandled config key: {key}")
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg


def parse_series_list(value) -> tuple[str, ...]:
    """Accept a JSON list or a comma-separated CLI string. Order is kept."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ConfigError("small_pair_series must be a list of series slugs")
    chosen: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigError("small_pair_series must be a list of series slugs")
        name = item.strip()
        if not name or name in chosen:
            continue
        chosen.append(name)
    if not chosen:
        raise ConfigError("small_pair_series must name at least one series")
    return tuple(chosen)


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return config_from_mapping(raw)


def config_to_public_dict(config: Config) -> dict:
    out = {}
    for key, value in asdict(config).items():
        if isinstance(value, Decimal):
            out[key] = format(value, "f")
        elif isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out
