"""Check that a configured market is one active binary Up/Down condition.

Public reads only. ``end_date_iso`` from the legacy ``/markets`` payload is
not consulted: for the verified 5-minute market that field was midnight while
Gamma ``endDate`` was the real window end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from poly_market_maker.sell_equal.config import SellConfig
from poly_market_maker.sell_equal.quant import D


def _as_list(value):
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON list")
        return parsed
    if isinstance(value, list):
        return value
    raise ValueError("expected a list")


def _parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def verify_public_market(
    config: SellConfig,
    clob_market: dict,
    gamma_market: dict,
    up_book: dict,
    down_book: dict,
    now: datetime,
) -> list[str]:
    errors = []
    if clob_market.get("c") != config.condition_id:
        errors.append("clob condition id does not match config")
    if str(clob_market.get("v") or "") not in ("v1", "v2"):
        errors.append("clob protocol version is missing or unsupported")
    tokens = clob_market.get("t")
    if not isinstance(tokens, list) or len(tokens) != 2:
        errors.append("market is not a two-outcome binary")
        return errors
    by_outcome = {}
    for token in tokens:
        if not isinstance(token, dict):
            errors.append("token row is not an object")
            continue
        by_outcome[str(token.get("o"))] = str(token.get("t"))
    if by_outcome.get(config.up_outcome) != config.up_token_id:
        errors.append("up token does not match the CLOB market")
    if by_outcome.get(config.down_outcome) != config.down_token_id:
        errors.append("down token does not match the CLOB market")
    if clob_market.get("ao") is not True:
        errors.append("CLOB market is not accepting orders")
    try:
        tick = D(str(clob_market.get("mts")))
        minimum = D(str(clob_market.get("mos")))
    except Exception:
        errors.append("tick or minimum size missing")
        tick = None
        minimum = None
    if tick is not None and tick != config.tick_size:
        errors.append("config tick does not match the CLOB tick")
    if minimum is not None and minimum != config.min_order_size:
        errors.append("config minimum size does not match the CLOB minimum")
    errors.extend(_gamma_errors(config, gamma_market, now))
    errors.extend(_book_errors(config, up_book, config.up_token_id, "up", now))
    errors.extend(_book_errors(config, down_book, config.down_token_id, "down", now))
    return errors


def _gamma_errors(config: SellConfig, gamma_market: dict, now: datetime) -> list[str]:
    errors = []
    if gamma_market.get("conditionId") != config.condition_id:
        errors.append("gamma condition id does not match config")
    if gamma_market.get("slug") != config.slug:
        errors.append("gamma slug does not match config")
    if gamma_market.get("closed") is True:
        errors.append("gamma market is closed")
    if gamma_market.get("acceptingOrders") is not True:
        errors.append("gamma market is not accepting orders")
    try:
        outcomes = [str(item) for item in _as_list(gamma_market.get("outcomes"))]
        token_ids = [str(item) for item in _as_list(gamma_market.get("clobTokenIds"))]
    except Exception:
        return errors + ["gamma outcomes or token ids are unreadable"]
    if outcomes != [config.up_outcome, config.down_outcome]:
        errors.append("gamma outcomes are not the configured Up/Down pair")
    if token_ids != [config.up_token_id, config.down_token_id]:
        errors.append("gamma token ids do not match config")
    end_raw = gamma_market.get("endDate")
    if not isinstance(end_raw, str):
        errors.append("gamma endDate is missing")
    else:
        try:
            end = _parse_time(end_raw)
        except Exception:
            errors.append("gamma endDate is invalid")
        else:
            if now >= end:
                errors.append("market endDate has passed")
    return errors


def _book_errors(
    config: SellConfig, book: dict, token_id: str, label: str, now: datetime
) -> list[str]:
    errors = []
    if book.get("market") != config.condition_id:
        errors.append(f"{label} book is for a different condition")
    if str(book.get("asset_id")) != token_id:
        errors.append(f"{label} book asset id does not match")
    try:
        if D(str(book.get("tick_size"))) != config.tick_size:
            errors.append(f"{label} book tick does not match config")
        if D(str(book.get("min_order_size"))) != config.min_order_size:
            errors.append(f"{label} book minimum size does not match config")
    except Exception:
        errors.append(f"{label} book constraints are unreadable")
    raw_ts = book.get("timestamp")
    try:
        millis = Decimal(str(raw_ts))
        stamp = datetime.fromtimestamp(float(millis / Decimal("1000")), tz=timezone.utc)
    except Exception:
        errors.append(f"{label} book timestamp is unreadable")
        return errors
    age = (now - stamp).total_seconds()
    if age < 0 or Decimal(str(age)) > config.stale_after_seconds:
        errors.append(f"{label} book is stale")
    return errors
