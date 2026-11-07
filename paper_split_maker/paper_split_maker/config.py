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
    if lowered in {"live", "sign", "signing"}:
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
}
_BOOL_FIELDS = {
    "trade_through_fills",
    "auto_split",
    "merge_when_idle",
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
        elif key == "poll_interval_seconds":
            kwargs[key] = float(value)
        elif key == "market_slugs":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ConfigError("market_slugs must be a list of strings")
            kwargs[key] = tuple(value)
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
