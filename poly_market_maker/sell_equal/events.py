"""Parse authenticated user-channel messages.

The trade ``side`` is the taker's side. A maker SELL is taken from
``maker_orders``, not from the top-level taker size. Gross value stays
price times shares; ``fee_rate_bps`` is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from poly_market_maker.sell_equal.engine import Fill
from poly_market_maker.sell_equal.quant import D

_COUNTABLE = {"MATCHED", "MINED", "CONFIRMED"}
_IGNORED = {"RETRYING"}


@dataclass(frozen=True)
class UserBatch:
    fills: tuple[Fill, ...]
    failed_ids: tuple[str, ...]
    size_matched: tuple[tuple[str, Decimal], ...]


def parse_user_payload(payload, own_order_ids: set[str]) -> UserBatch:
    messages = payload if isinstance(payload, list) else [payload]
    fills = []
    failed = []
    matched = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        event_type = message.get("event_type")
        if event_type == "trade":
            _consume_trade(message, own_order_ids, fills, failed)
        elif event_type == "order":
            _consume_order(message, own_order_ids, matched)
    return UserBatch(tuple(fills), tuple(failed), tuple(matched))


def _consume_trade(
    message: dict, own_order_ids: set[str], fills: list, failed: list
) -> None:
    trade_id = str(message.get("id") or "")
    status = str(message.get("status") or "").upper()
    if status.startswith("TRADE_STATUS_"):
        status = status[len("TRADE_STATUS_") :]
    if not trade_id:
        return
    own_legs = _own_sell_legs(message, own_order_ids, trade_id)
    if not own_legs:
        return
    if status == "FAILED":
        failed.extend(leg[0] for leg in own_legs)
        return
    if status in _IGNORED or status not in _COUNTABLE:
        return
    for fill_id, order_id, shares, price in own_legs:
        fills.append(
            Fill(
                fill_id=fill_id,
                exchange_order_id=order_id,
                shares=shares,
                price=price,
            )
        )


def _own_sell_legs(message: dict, own_order_ids: set[str], trade_id: str):
    legs = []
    trader_side = str(message.get("trader_side") or "").upper()
    taker_id = str(message.get("taker_order_id") or "")
    if trader_side == "TAKER" and taker_id in own_order_ids:
        if str(message.get("side") or "").upper() == "SELL":
            legs.append(
                (
                    trade_id,
                    taker_id,
                    D(str(message["size"])),
                    D(str(message["price"])),
                )
            )
    if trader_side == "MAKER" or trader_side == "":
        for index, maker in enumerate(message.get("maker_orders") or []):
            if not isinstance(maker, dict):
                continue
            order_id = str(maker.get("order_id") or "")
            side = str(maker.get("side") or "").upper()
            if order_id not in own_order_ids or side != "SELL":
                continue
            legs.append(
                (
                    "%s:maker:%s:%s" % (trade_id, order_id, index),
                    order_id,
                    D(str(maker["matched_amount"])),
                    D(str(maker["price"])),
                )
            )
    return legs


def _consume_order(message: dict, own_order_ids: set[str], matched: list) -> None:
    if str(message.get("side") or "").upper() != "SELL":
        return
    if str(message.get("type") or "").upper() != "UPDATE":
        return
    order_id = str(message.get("id") or "")
    if order_id not in own_order_ids:
        return
    matched.append((order_id, D(str(message["size_matched"]))))
