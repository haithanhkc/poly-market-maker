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
    # 1 keeps the original one-order-per-side behavior. Live configs raise it.
    slices_per_side: int = 1
    slice_inside_tick: bool = False
    # >= 1 disables the pair floor. The live example sets 0.01.
    max_pair_loss: Decimal = Decimal("1")
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
    # Seconds before the pre-close cancel during which asks step toward the bid.
    close_improve_seconds: int = 90
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
    split_lead_seconds: int = 45
    # Hard stop for a live run. The duration flag does not cut a session off.
    max_runtime_seconds: int = 2400
    # Give up on a redeem after this many attempts and leave it for redeem-pending.
    redeem_max_attempts: int = 5
    # split_sell quotes both outcomes after a split. buy_pair_arb buys both
    # asks only when they sum to 1 - pair_min_edge, then merges.
    entry_mode: str = "split_sell"
    # After one sell leg fills, keep checking the other leg for this long.
    # A bid at or above 1 - fill_price is taken immediately; this is the
    # backstop that repeats that check and then allows a capped loss.
    leg_unwind_seconds: int = 5
    # Lift 1 - P so a taker fee at risk_fee_rate still nets a breakeven pair.
    leg_fee_buffer: bool = False
    # Near expiry, sell below 1 - P rather than carry the leftover to zero.
    allow_loss_unwind: bool = False
    # Most USDC the naked size may give up below the breakeven floor.
    max_unwind_loss: Decimal = Decimal("0")
    # The loss unwind is allowed only in this many seconds before expiry.
    unwind_loss_before_expiry_seconds: int = 30
    # 0 disables the pause. Otherwise pull quotes when the mid jumps.
    vol_pause_move: Decimal = Decimal("0")
    vol_pause_window_ms: int = 0
    vol_pause_hold_ms: int = 0
    btc_pause_move: Decimal = Decimal("0")
    pre_split_lead_ms: int = 45_000
    split_confirm_timeout_ms: int = 30_000
    taker_repair: bool = False
    taker_repair_max_usdc: Decimal = Decimal("0")
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
        if self.slices_per_side < 1:
            raise ConfigError("slices_per_side must be at least 1")
        if self.max_pair_loss < 0:
            raise ConfigError("max_pair_loss must be non-negative")
        if self.session_max_loss < 0:
            raise ConfigError("session_max_loss must be non-negative")
        if self.requote_min_ticks < 0 or self.requote_min_interval_ms < 0:
            raise ConfigError("requote limits must be non-negative")
        if (
            self.min_order_rest_seconds < 0
            or self.close_improve_seconds < 0
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
        if self.max_runtime_seconds <= 0:
            raise ConfigError("max_runtime_seconds must be positive")
        if self.redeem_max_attempts < 1:
            raise ConfigError("redeem_max_attempts must be at least 1")
        if self.entry_mode not in ("split_sell", "buy_pair_arb"):
            raise ConfigError("entry_mode must be split_sell or buy_pair_arb")
        if self.leg_unwind_seconds < 0:
            raise ConfigError("leg_unwind_seconds must be non-negative")
        if self.max_unwind_loss < 0:
            raise ConfigError("max_unwind_loss must be non-negative")
        if self.unwind_loss_before_expiry_seconds < 0:
            raise ConfigError("unwind_loss_before_expiry_seconds must be non-negative")
        if self.requote_vol_window_ms < 0:
            raise ConfigError("requote_vol_window_ms must be non-negative")
        if self.vol_pause_move < 0 or self.btc_pause_move < 0:
            raise ConfigError("pause thresholds must be non-negative")
        if (
            self.vol_pause_window_ms < 0
            or self.vol_pause_hold_ms < 0
            or self.pre_split_lead_ms < 0
            or self.split_confirm_timeout_ms < 0
        ):
            raise ConfigError("timing settings must be non-negative")
        if self.taker_repair_max_usdc < 0:
            raise ConfigError("taker_repair_max_usdc must be non-negative")
        if self.gas_limit < 0 or self.max_fee_gwei < 0 or self.priority_fee_gwei < 0:
            raise ConfigError("gas settings must be non-negative")
        if self.taker_repair and self.taker_repair_max_usdc <= 0:
            raise ConfigError("taker repair needs a positive USDC cap")
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
    "max_pair_loss",
    "session_max_loss",
    "vol_pause_move",
    "btc_pause_move",
    "requote_vol_k",
    "requote_size_threshold",
    "pair_min_edge",
    "max_unpaired_fraction",
    "max_unwind_loss",
    "taker_repair_max_usdc",
    "max_fee_gwei",
    "priority_fee_gwei",
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
    "slices_per_side",
    "requote_min_ticks",
    "requote_min_interval_ms",
    "min_order_rest_seconds",
    "requote_vol_window_ms",
    "close_improve_seconds",
    "sell_ready_timeout_seconds",
    "stale_feed_seconds",
    "split_lead_seconds",
    "max_runtime_seconds",
    "redeem_max_attempts",
    "leg_unwind_seconds",
    "unwind_loss_before_expiry_seconds",
    "vol_pause_window_ms",
    "vol_pause_hold_ms",
    "pre_split_lead_ms",
    "split_confirm_timeout_ms",
    "gas_limit",
}
_BOOL_FIELDS = {
    "trade_through_fills",
    "auto_split",
    "merge_when_idle",
    "live",
    "slice_inside_tick",
    "taker_repair",
    "leg_fee_buffer",
    "allow_loss_unwind",
}


def _apply_legacy_keys(data: dict) -> dict:
    """Map run14-style names onto the current fields. Seconds win."""
    data = dict(data)
    if "split_lead_seconds" not in data and "pre_split_lead_ms" in data:
        raw_ms = data["pre_split_lead_ms"]
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, (int, str)):
            raise ConfigError("pre_split_lead_ms is not an integer")
        data["split_lead_seconds"] = int(raw_ms) // 1000
    return data


def config_from_mapping(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be an object")
    data = _apply_legacy_keys(data)
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
        elif key == "entry_mode":
            kwargs[key] = str(value)
        elif key == "poll_interval_seconds":
            kwargs[key] = float(value)
        elif key == "market_slugs":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ConfigError("market_slugs must be a list of strings")
            kwargs[key] = tuple(value)
        elif key == "kill_switch_file":
            if not isinstance(value, str):
                raise ConfigError("kill_switch_file must be a string")
            kwargs[key] = value
        else:
            raise ConfigError(f"unhandled config key: {key}")
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg


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
