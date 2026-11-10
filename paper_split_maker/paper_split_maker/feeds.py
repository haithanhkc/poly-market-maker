"""Public market websocket and authenticated user websocket message shapes.

The hosts are the official CLOB subscription endpoints. This module builds
messages and reads fills. It does not open a socket and it does not log the
user-channel credential object.
"""

from __future__ import annotations

from decimal import Decimal

MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
BINANCE_BTC_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
CONFIRMED_TRADE = {"MATCHED", "MINED", "CONFIRMED"}


def market_subscribe(asset_ids: list[str]) -> dict:
    return {
        "assets_ids": [str(item) for item in asset_ids],
        "type": "market",
        "custom_feature_enabled": True,
    }


def user_subscribe(
    api_key: str, secret: str, passphrase: str, condition_ids: list[str]
) -> dict:
    return {
        "auth": {
            "apiKey": api_key,
            "secret": secret,
            "passphrase": passphrase,
        },
        "markets": [str(item) for item in condition_ids],
        "type": "user",
    }


def describe_user_subscribe(funder: str, condition_count: int) -> str:
    """Text safe to print. It names the funder and never the API secret."""
    return f"user channel funder={funder} markets={condition_count}"


def binance_mid(payload: dict) -> Decimal | None:
    """Mid of a Binance bookTicker. Used only as a volatility input."""
    try:
        bid = Decimal(str(payload["b"]))
        ask = Decimal(str(payload["a"]))
    except (KeyError, ValueError, ArithmeticError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


def user_fill_deltas(payload: dict, matched_so_far: dict[str, Decimal]) -> list[dict]:
    """Fills for our orders. Trade ids dedupe MATCHED and later CONFIRMED.

    An order event with a larger size_matched emits only the increase. The
    synthetic trade id includes the new matched total, so a replay is a duplicate.
    """
    if not isinstance(payload, dict):
        return []
    kind = str(payload.get("event_type") or "")
    if kind == "trade":
        return _trade_fills(payload)
    if kind == "order":
        return _order_fill(payload, matched_so_far)
    return []


def _trade_fills(payload: dict) -> list[dict]:
    status = str(payload.get("status") or "")
    if status not in CONFIRMED_TRADE:
        return []
    trade_id = str(payload.get("id") or "")
    if not trade_id:
        return []
    rows = []
    makers = payload.get("maker_orders") or []
    if isinstance(makers, list):
        for maker in makers:
            if not isinstance(maker, dict):
                continue
            order_id = str(maker.get("order_id") or "")
            if not order_id:
                continue
            rows.append(
                {
                    "exchange_order_id": order_id,
                    "trade_id": trade_id,
                    "size": str(maker.get("matched_amount") or "0"),
                    "price": str(maker.get("price") or payload.get("price") or "0"),
                    "role": "maker",
                }
            )
    taker_id = str(payload.get("taker_order_id") or "")
    if taker_id and str(payload.get("trader_side") or "") == "TAKER":
        rows.append(
            {
                "exchange_order_id": taker_id,
                "trade_id": trade_id,
                "size": str(payload.get("size") or "0"),
                "price": str(payload.get("price") or "0"),
                "role": "taker",
            }
        )
    return rows


def _order_fill(payload: dict, matched_so_far: dict[str, Decimal]) -> list[dict]:
    order_id = str(payload.get("id") or "")
    if not order_id:
        return []
    try:
        matched = Decimal(str(payload.get("size_matched") or "0"))
    except (ValueError, ArithmeticError):
        return []
    previous = matched_so_far.get(order_id, Decimal("0"))
    if matched <= previous:
        matched_so_far[order_id] = previous
        return []
    delta = matched - previous
    matched_so_far[order_id] = matched
    return [
        {
            "exchange_order_id": order_id,
            "trade_id": f"order:{order_id}:matched:{matched}",
            "size": str(delta),
            "price": str(payload.get("price") or "0"),
            "role": "maker",
        }
    ]
