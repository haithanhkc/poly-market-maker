"""Map public Gamma, CLOB, Data API, and market-channel payloads into events.

Resolution is accepted only from the Data API resolution row or a
`market_resolved` frame whose winning asset and label agree. A displayed
price of 0.99 is not a resolution. Payloads that do not match the documented
binary Up/Down shape are refused.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from paper_split_maker.decimal_utils import D, on_grid

FULL_PAYOUT = 1_000_000


class ParseError(ValueError):
    pass


def _loads_maybe(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return value


def _parse_time_ms(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        number = int(Decimal(str(value)))
    else:
        text = str(value).strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            number = int(text)
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
    if number > 10**14:
        return number
    if number > 10**12:
        return number
    if number > 10**9:
        return number * 1000
    return number * 1000


def _levels(raw) -> Optional[list[tuple[str, str]]]:
    if not isinstance(raw, list):
        return None
    levels = []
    for item in raw:
        if isinstance(item, dict):
            price = item.get("price")
            size = item.get("size")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            return None
        try:
            D(price)
            D(size)
        except (ValueError, ArithmeticError):
            return None
        levels.append(
            (
                str(price) if not isinstance(price, str) else price,
                str(size) if not isinstance(size, str) else size,
            )
        )
    return levels


def extract_token_ids(market: dict) -> Optional[list[str]]:
    version = str(market.get("version") or "").strip().lower()
    if version == "v2":
        raw = market.get("positionIds")
        if isinstance(raw, str):
            raw = _loads_maybe(raw)
        if not isinstance(raw, list):
            return None
        return [str(item) for item in raw]
    if version in {"v1", ""}:
        raw = market.get("clobTokenIds")
        if isinstance(raw, str):
            raw = _loads_maybe(raw)
        if not raw and version == "":
            raw = market.get("positionIds")
            if isinstance(raw, str):
                raw = _loads_maybe(raw)
        if not isinstance(raw, list):
            return None
        return [str(item) for item in raw]
    return None


def gamma_market_event(
    payload: dict, *, market_id: Optional[str] = None, synthetic: bool = False
) -> Optional[dict]:
    """Return a market_open event, or None when the payload is not a usable Up/Down market."""
    if not isinstance(payload, dict):
        return None
    outcomes = _loads_maybe(payload.get("outcomes"))
    token_ids = extract_token_ids(payload)
    if not isinstance(outcomes, list) or token_ids is None:
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None
    labels = [str(item).strip() for item in outcomes]
    folded = [label.casefold() for label in labels]
    if set(folded) != {"up", "down"}:
        return None
    if len(set(token_ids)) != 2 or any(not token for token in token_ids):
        return None
    up_index = folded.index("up")
    down_index = folded.index("down")
    condition_id = payload.get("conditionId") or payload.get("condition_id")
    if not condition_id:
        return None
    end_raw = payload.get("endDate") or payload.get("end_date")
    end_ts = _parse_time_ms(end_raw)
    if end_ts is None:
        return None
    slug = str(payload.get("slug") or "")
    question = str(payload.get("question") or slug)
    tick_raw = (
        payload.get("orderPriceMinTickSize") or payload.get("tick_size") or "0.01"
    )
    min_size_raw = payload.get("orderMinSize") or payload.get("min_order_size") or "5"
    try:
        tick = D(tick_raw)
        min_size = D(min_size_raw)
    except (ValueError, ArithmeticError):
        return None
    if tick <= 0 or min_size <= 0:
        return None
    closed = bool(payload.get("closed"))
    accepting = payload.get("acceptingOrders")
    if accepting is None:
        accepting = payload.get("accepting_orders")
    if accepting is None:
        accepting = not closed
    ident = market_id or str(payload.get("id") or condition_id)
    return {
        "type": "market_open",
        "market_id": ident,
        "condition_id": str(condition_id),
        "slug": slug,
        "question": question,
        "up_token_id": token_ids[up_index],
        "down_token_id": token_ids[down_index],
        "up_label": labels[up_index],
        "down_label": labels[down_index],
        "up_index": up_index,
        "down_index": down_index,
        "end_ts": end_ts,
        "tick_size": format(tick, "f"),
        "min_order_size": format(min_size, "f"),
        "accepting_orders": bool(accepting) and not closed,
        "synthetic": synthetic,
        "closed": closed,
    }


def clob_book_event(
    market_id: str, outcome: str, payload: dict, *, event_id: Optional[str] = None
) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    bids = _levels(payload.get("bids"))
    asks = _levels(payload.get("asks"))
    if bids is None or asks is None:
        return None
    ts = _parse_time_ms(payload.get("timestamp"))
    if ts is None:
        return None
    tick = payload.get("tick_size")
    min_size = payload.get("min_order_size")
    event = {
        "type": "book",
        "market_id": market_id,
        "outcome": outcome,
        "ts": ts,
        "bids": bids,
        "asks": asks,
        "hash": str(payload.get("hash") or ""),
    }
    if event_id:
        event["event_id"] = event_id
    if tick is not None:
        event["tick_size"] = str(tick)
    if min_size is not None:
        event["min_order_size"] = str(min_size)
    asset = payload.get("asset_id")
    if asset is not None:
        event["asset_id"] = str(asset)
    return event


def data_api_trade_events(market: dict, payload: dict) -> list[dict]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if rows is None and isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list):
        return []
    events = []
    up_token = str(market["up_token_id"])
    down_token = str(market["down_token_id"])
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = str(
            row.get("token_id") or row.get("asset_id") or row.get("asset") or ""
        )
        if token == up_token:
            outcome = "up"
        elif token == down_token:
            outcome = "down"
        else:
            continue
        trade_id = row.get("id") or row.get("trade_id")
        if not trade_id:
            trade_id = "|".join(
                [
                    str(
                        row.get("transaction_hash") or row.get("transactionHash") or ""
                    ),
                    token,
                    str(row.get("price")),
                    str(row.get("size")),
                    str(row.get("timestamp") or row.get("match_time") or ""),
                    str(row.get("side") or ""),
                ]
            )
        ts = _parse_time_ms(row.get("timestamp") or row.get("match_time"))
        if ts is None:
            continue
        side = str(row.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        try:
            D(row.get("price"))
            D(row.get("size"))
        except (ValueError, ArithmeticError):
            continue
        events.append(
            {
                "type": "trade",
                "market_id": market["market_id"],
                "outcome": outcome,
                "ts": ts,
                "price": str(row.get("price")),
                "size": str(row.get("size")),
                "taker_side": side,
                "trade_id": str(trade_id),
                "event_id": f"trade:{trade_id}",
            }
        )
    return events


def data_api_resolution_event(market: dict, payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    condition = str(market["condition_id"]).lower()
    chosen = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_condition = str(
            row.get("condition_id") or row.get("conditionId") or ""
        ).lower()
        if row_condition and row_condition != condition:
            continue
        chosen = row
        break
    if chosen is None:
        return None
    status = str(chosen.get("status") or "").lower()
    if status != "resolved":
        return None
    market_type = chosen.get("market_type") or chosen.get("marketType")
    if market_type and str(market_type).upper() != "BINARY":
        return None
    payouts = chosen.get("payouts")
    if not isinstance(payouts, list) or len(payouts) != 2:
        return None
    try:
        payout_values = [int(item) for item in payouts]
    except (TypeError, ValueError):
        return None
    if sorted(payout_values) != [0, FULL_PAYOUT]:
        return None
    return {
        "type": "resolution",
        "source": "data-api-v2",
        "market_id": market["market_id"],
        "status": "resolved",
        "payouts": payout_values,
        "market_type": "BINARY",
        "event_id": f"resolution:{market['condition_id']}:{payout_values[0]}:{payout_values[1]}",
    }


def events_from_ws_message(
    markets_by_asset: dict[str, tuple[str, str]], message: dict
) -> list[dict]:
    """Normalize one public market-channel message. Unknown assets are ignored."""
    if not isinstance(message, dict):
        return []
    kind = message.get("event_type") or message.get("type")
    if kind == "book":
        asset = str(message.get("asset_id") or "")
        located = markets_by_asset.get(asset)
        if not located:
            return []
        market_id, outcome = located
        event = clob_book_event(
            market_id,
            outcome,
            message,
            event_id=f"ws-book:{asset}:{message.get('hash')}:{message.get('timestamp')}",
        )
        return [event] if event else []
    if kind == "last_trade_price":
        asset = str(message.get("asset_id") or "")
        located = markets_by_asset.get(asset)
        if not located:
            return []
        market_id, outcome = located
        ts = _parse_time_ms(message.get("timestamp"))
        if ts is None:
            return []
        side = str(message.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return []
        trade_key = "|".join(
            [
                str(message.get("transaction_hash") or ""),
                asset,
                str(message.get("price")),
                str(message.get("size")),
                str(message.get("timestamp")),
                side,
            ]
        )
        try:
            D(message.get("price"))
            D(message.get("size"))
        except (ValueError, ArithmeticError):
            return []
        return [
            {
                "type": "trade",
                "market_id": market_id,
                "outcome": outcome,
                "ts": ts,
                "price": str(message.get("price")),
                "size": str(message.get("size")),
                "taker_side": side,
                "trade_id": trade_key,
                "event_id": f"trade:{trade_key}",
            }
        ]
    if kind == "market_resolved":
        asset = str(message.get("winning_asset_id") or "")
        located = markets_by_asset.get(asset)
        if not located:
            return []
        market_id, _outcome = located
        frame_id = str(message.get("id") or message.get("timestamp") or "")
        return [
            {
                "type": "resolution",
                "source": "clob-market-resolved",
                "market_id": market_id,
                "winning_asset_id": asset,
                "winning_outcome": message.get("winning_outcome"),
                "condition_id": message.get("market"),
                "event_id": f"ws-resolved:{frame_id}:{asset}",
            }
        ]
    return []


def price_on_tick(price: Decimal, tick: Decimal) -> bool:
    return on_grid(price, tick)
