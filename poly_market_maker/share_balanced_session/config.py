"""Config for the share-balanced session. Maker-only cannot be turned off."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D

EXPLICIT = "explicit_allocation"


@dataclass(frozen=True)
class SessionConfig:
    condition_id: str
    slug: str
    up_token_id: str
    down_token_id: str
    allocated_up: Decimal
    allocated_down: Decimal
    cost_basis_usd: Decimal
    capital_usd: Decimal
    cost_basis_source: str
    tick_size: Decimal
    min_order_size: Decimal
    size_quantum: Decimal
    price_floor: Decimal
    price_ceiling: Decimal
    improve_ticks: int
    hysteresis_ticks: int
    min_replace_seconds: Decimal
    seed_shares: Decimal
    max_share_skew: Decimal
    risk_budget_usd: Decimal
    take_profit_usd: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    rebate_share: Decimal
    stale_after_seconds: Decimal
    session_start_ts: Decimal
    session_end_ts: Decimal
    cutoff_seconds: Decimal
    state_path: str
    maker_only: bool = True

    def token_id(self, outcome: str) -> str:
        if outcome == "up":
            return self.up_token_id
        if outcome == "down":
            return self.down_token_id
        raise KeyError(outcome)


def load_config(path: str | Path) -> SessionConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    if raw.get("mode") != "share_balanced_session":
        raise ValueError("mode must be share_balanced_session")
    _reject_taker(raw)
    snapshot = _load_snapshot(raw["baseline_snapshot"])
    config = _build(raw)
    _same_baseline(config, snapshot)
    return config


def _reject_taker(raw: dict) -> None:
    if raw.get("maker_only") is False or raw.get("allow_taker") is True:
        raise ValueError("maker_only cannot be disabled and taker orders are rejected")
    for key in ("order_type", "time_in_force"):
        value = str(raw.get(key, "post_only")).lower()
        if value not in ("post_only", "maker"):
            raise ValueError("order style must stay post_only")


def _load_snapshot(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("cost_basis_source") != EXPLICIT:
        raise ValueError("baseline snapshot must declare explicit_allocation")
    if "cost_basis_usd" not in data:
        raise ValueError("baseline snapshot is missing cost_basis_usd")
    return data


def _txt(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(key + " must be a non-empty string")
    return value


def _dec(raw: dict, key: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(key + " must be a decimal string")
    return D(value)


def _int(raw: dict, key: str) -> int:
    value = raw.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(key + " must be a non-negative integer")
    return value


def _build(raw: dict) -> SessionConfig:
    return SessionConfig(
        condition_id=_txt(raw, "condition_id"),
        slug=_txt(raw, "slug"),
        up_token_id=_txt(raw, "up_token_id"),
        down_token_id=_txt(raw, "down_token_id"),
        allocated_up=_dec(raw, "allocated_up"),
        allocated_down=_dec(raw, "allocated_down"),
        cost_basis_usd=_dec(raw, "cost_basis_usd"),
        capital_usd=_dec(raw, "capital_usd"),
        cost_basis_source=_txt(raw, "cost_basis_source"),
        tick_size=_dec(raw, "tick_size"),
        min_order_size=_dec(raw, "min_order_size"),
        size_quantum=_dec(raw, "size_quantum"),
        price_floor=_dec(raw, "price_floor"),
        price_ceiling=_dec(raw, "price_ceiling"),
        improve_ticks=_int(raw, "improve_ticks"),
        hysteresis_ticks=_int(raw, "hysteresis_ticks"),
        min_replace_seconds=_dec(raw, "min_replace_seconds"),
        seed_shares=_dec(raw, "seed_shares"),
        max_share_skew=_dec(raw, "max_share_skew"),
        risk_budget_usd=_dec(raw, "risk_budget_usd"),
        take_profit_usd=_dec(raw, "take_profit_usd"),
        maker_fee_rate=_dec(raw, "maker_fee_rate"),
        taker_fee_rate=_dec(raw, "taker_fee_rate"),
        rebate_share=_dec(raw, "rebate_share"),
        stale_after_seconds=_dec(raw, "stale_after_seconds"),
        session_start_ts=_dec(raw, "session_start_ts"),
        session_end_ts=_dec(raw, "session_end_ts"),
        cutoff_seconds=_dec(raw, "cutoff_seconds"),
        state_path=_txt(raw, "state_path"),
        maker_only=True,
    )


def _same_baseline(config: SessionConfig, snapshot: dict) -> None:
    if config.cost_basis_source != EXPLICIT:
        raise ValueError(
            "cost basis must be explicit_allocation, not a wallet or deposit"
        )
    _match_text(snapshot, "condition_id", config.condition_id)
    _match_text(snapshot, "up_token_id", config.up_token_id)
    _match_text(snapshot, "down_token_id", config.down_token_id)
    _match_text(snapshot, "cost_basis_source", config.cost_basis_source)
    _match_decimal(snapshot, "allocated_up", config.allocated_up)
    _match_decimal(snapshot, "allocated_down", config.allocated_down)
    _match_decimal(snapshot, "cost_basis_usd", config.cost_basis_usd)
    _match_decimal(snapshot, "capital_usd", config.capital_usd)
    if config.allocated_up <= 0 or config.allocated_down <= 0:
        raise ValueError("allocated shares must be positive")
    if config.cost_basis_usd <= 0 or config.capital_usd <= 0:
        raise ValueError("cost basis and capital must be positive")
    if config.up_token_id == config.down_token_id:
        raise ValueError("token ids must differ")
    if config.session_end_ts <= config.session_start_ts:
        raise ValueError("session end must be after start")


def _match_text(snapshot: dict, key: str, expected: str) -> None:
    found = snapshot.get(key)
    if found != expected:
        raise ValueError("baseline snapshot mismatches " + key)


def _match_decimal(snapshot: dict, key: str, expected: Decimal) -> None:
    found = snapshot.get(key)
    if not isinstance(found, str) or D(found) != expected:
        raise ValueError("baseline snapshot mismatches " + key)
