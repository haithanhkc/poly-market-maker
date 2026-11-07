"""Secret-free configuration. Numeric fields must be decimal strings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D


@dataclass(frozen=True)
class SellConfig:
    mode: str
    condition_id: str
    slug: str
    up_token_id: str
    down_token_id: str
    up_outcome: str
    down_outcome: str
    tick_size: Decimal
    min_order_size: Decimal
    size_quantum: Decimal
    price_floor: Decimal
    price_ceiling: Decimal
    max_reprice_ticks: int
    seed_price_min: Decimal
    seed_price_max: Decimal
    seed_price_step: Decimal
    seed_shares: Decimal
    hedge_price_up: Decimal
    hedge_price_down: Decimal
    inventory_up: Decimal
    inventory_down: Decimal
    mark_up: Decimal
    mark_down: Decimal
    max_outstanding_notional: Decimal
    max_gross_skew: Decimal
    max_session_loss: Decimal
    value_dust: Decimal
    stale_after_seconds: Decimal
    market_end_ts: Decimal
    share_decimals: int
    require_user_feed: bool
    state_path: str

    def token_id(self, outcome: str) -> str:
        if outcome == "up":
            return self.up_token_id
        if outcome == "down":
            return self.down_token_id
        raise KeyError(outcome)

    def hedge_price(self, outcome: str) -> Decimal:
        if outcome == "up":
            return self.hedge_price_up
        if outcome == "down":
            return self.hedge_price_down
        raise KeyError(outcome)

    def inventory(self, outcome: str) -> Decimal:
        if outcome == "up":
            return self.inventory_up
        return self.inventory_down

    def mark(self, outcome: str) -> Decimal:
        if outcome == "up":
            return self.mark_up
        return self.mark_down


def _req(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_dec(raw: dict, key: str, default: str) -> Decimal:
    if key not in raw:
        return D(default)
    return _dec(raw, key)


def _dec(raw: dict, key: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a decimal string, not {type(value).__name__}")
    try:
        parsed = D(value)
    except Exception as exc:
        raise ValueError(f"{key} is not a decimal") from exc
    return parsed


def _nested_dec(raw: dict, group: str, key: str) -> Decimal:
    block = raw.get(group)
    if not isinstance(block, dict):
        raise ValueError(f"{group} must be an object")
    value = block.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{group}.{key} must be a decimal string")
    return D(value)


def load_config(path: str | Path) -> SellConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    mode = _req(data, "mode")
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    ticks = data.get("max_reprice_ticks", 0)
    if not isinstance(ticks, int) or isinstance(ticks, bool) or ticks < 0:
        raise ValueError("max_reprice_ticks must be a non-negative integer")
    decimals = data.get("share_decimals", 6)
    if not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
        raise ValueError("share_decimals must be a non-negative integer")
    require_user = data.get("require_user_feed", mode == "live")
    if not isinstance(require_user, bool):
        raise ValueError("require_user_feed must be a boolean")
    config = SellConfig(
        mode=mode,
        condition_id=_req(data, "condition_id"),
        slug=_req(data, "slug"),
        up_token_id=_req(data, "up_token_id"),
        down_token_id=_req(data, "down_token_id"),
        up_outcome=_req(data, "up_outcome"),
        down_outcome=_req(data, "down_outcome"),
        tick_size=_dec(data, "tick_size"),
        min_order_size=_dec(data, "min_order_size"),
        size_quantum=_dec(data, "size_quantum"),
        price_floor=_dec(data, "price_floor"),
        price_ceiling=_dec(data, "price_ceiling"),
        max_reprice_ticks=ticks,
        seed_price_min=_dec(data, "seed_price_min"),
        seed_price_max=_dec(data, "seed_price_max"),
        seed_price_step=_dec(data, "seed_price_step"),
        seed_shares=_dec(data, "seed_shares"),
        hedge_price_up=_nested_dec(data, "hedge_price", "up"),
        hedge_price_down=_nested_dec(data, "hedge_price", "down"),
        inventory_up=_nested_dec(data, "inventory", "up"),
        inventory_down=_nested_dec(data, "inventory", "down"),
        mark_up=_nested_dec(data, "mark_price", "up"),
        mark_down=_nested_dec(data, "mark_price", "down"),
        max_outstanding_notional=_dec(data, "max_outstanding_notional"),
        max_gross_skew=_dec(data, "max_gross_skew"),
        max_session_loss=_dec(data, "max_session_loss"),
        value_dust=_optional_dec(data, "value_dust", "0.01"),
        stale_after_seconds=_dec(data, "stale_after_seconds"),
        market_end_ts=_dec(data, "market_end_ts"),
        share_decimals=decimals,
        require_user_feed=require_user,
        state_path=_req(data, "state_path"),
    )
    _validate(config)
    return config


def _validate(config: SellConfig) -> None:
    if config.up_token_id == config.down_token_id:
        raise ValueError("up and down token ids must differ")
    if config.seed_price_step <= 0 or config.tick_size <= 0:
        raise ValueError("steps must be positive")
    if config.seed_price_min > config.seed_price_max:
        raise ValueError("seed price range is inverted")
    if config.min_order_size <= 0 or config.seed_shares <= 0:
        raise ValueError("sizes must be positive")
    if config.inventory_up < 0 or config.inventory_down < 0:
        raise ValueError("inventory cannot be negative")
