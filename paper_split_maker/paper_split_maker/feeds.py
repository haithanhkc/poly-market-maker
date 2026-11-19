"""predict.fun websocket message shapes.

Mainnet is ``wss://ws.predict.fun/ws``. Testnet is
``wss://ws-testnet.predict.fun/ws``. This module builds subscribe frames and
reads fills. It does not open a socket and it does not log a JWT or a key.
"""

from __future__ import annotations

from decimal import Decimal

from paper_split_maker.chain_data import WEI, wei_to_shares

MARKET_WS = "wss://ws.predict.fun/ws"
TESTNET_WS = "wss://ws-testnet.predict.fun/ws"
BINANCE_BTC_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"


def market_ws(network: str = "mainnet") -> str:
    if str(network).lower() == "testnet":
        return TESTNET_WS
    return MARKET_WS


def market_subscribe(market_ids: list[str], *, request_id: int = 1) -> dict:
    """One order-book subscription. The server reads only the first topic."""
    first = str(market_ids[0]) if market_ids else ""
    return {
        "method": "subscribe",
        "requestId": int(request_id),
        "params": [f"predictOrderbook/{first}"] if first else [],
    }


def market_subscriptions(market_ids: list[str]) -> list[dict]:
    return [
        market_subscribe([item], request_id=index + 1)
        for index, item in enumerate(market_ids)
        if str(item)
    ]


def user_subscribe(jwt: str, *, request_id: int = 1) -> dict:
    return {
        "method": "subscribe",
        "requestId": int(request_id),
        "params": [f"predictWalletEvents/{jwt}"],
    }


def describe_user_subscribe(funder: str, condition_count: int) -> str:
    """Text safe to print. It names the funder and never the JWT or API key."""
    return f"wallet events funder={funder} markets={condition_count}"


def heartbeat_reply(frame: dict) -> dict | None:
    """Echo the server heartbeat. A missing or different echo closes the socket."""
    if not isinstance(frame, dict) or frame.get("topic") != "heartbeat":
        return None
    return {"method": "heartbeat", "data": frame.get("data")}


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
    """Fills for our orders.

    A predict.fun ``orderTransactionSuccess`` event is one settlement. An
    order snapshot with a larger matched size emits only the increase.
    """
    if not isinstance(payload, dict):
        return []
    event = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    kind = str(event.get("type") or payload.get("event_type") or "")
    if kind == "orderTransactionSuccess":
        return _wallet_fill(event)
    if kind == "order":
        return _order_fill(payload, matched_so_far)
    return []


def _wallet_fill(event: dict) -> list[dict]:
    fill = event.get("fill") if isinstance(event.get("fill"), dict) else {}
    order_id = str(event.get("orderHash") or event.get("orderId") or "")
    trade_id = str(event.get("settlementId") or event.get("orderId") or "")
    if not order_id or not trade_id:
        return []
    try:
        size = _amount_shares(fill.get("executedSizeWei"))
        price = _amount_shares(fill.get("executedPriceWei"))
    except (ValueError, ArithmeticError):
        return []
    if size <= 0:
        return []
    return [
        {
            "exchange_order_id": order_id,
            "trade_id": trade_id,
            "size": format(size, "f"),
            "price": format(price, "f"),
            "role": "maker" if event.get("isMaker") else "taker",
        }
    ]


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


def _amount_shares(value) -> Decimal:
    """Wei when the integer is large, otherwise a human share amount."""
    if value is None or value == "":
        return Decimal("0")
    number = Decimal(str(value))
    if number >= Decimal(WEI) / Decimal(1000):
        return wei_to_shares(int(number))
    return number
