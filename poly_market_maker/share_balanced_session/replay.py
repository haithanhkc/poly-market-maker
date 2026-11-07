"""Deterministic paper replay. Touching a price does not create a fill."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.quant import D
from poly_market_maker.share_balanced_session.book import parse_book
from poly_market_maker.share_balanced_session.config import SessionConfig, load_config
from poly_market_maker.share_balanced_session.engine import Fill, ShareSession
from poly_market_maker.share_balanced_session.paper import PaperBroker


class Clock:
    def __init__(self, start: str):
        self.now = D(start)

    def __call__(self) -> Decimal:
        return self.now


def run_replay(config: SessionConfig, steps: list[dict]):
    clock = Clock(str(config.session_start_ts))
    broker = PaperBroker()
    session = ShareSession(config, broker, clock)
    for step in steps:
        _apply(session, broker, clock, step)
    return session, broker


def load_script(path: str | Path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_config(raw["config"]), raw["steps"]


def _apply(
    session: ShareSession, broker: PaperBroker, clock: Clock, step: dict
) -> None:
    op = step["op"]
    if op == "at":
        clock.now = D(step["ts"])
        return
    if op == "book":
        book = parse_book(step, session.config.tick_size, session.config.min_order_size)
        session.observe_book(step["outcome"], book)
        return
    if op == "user":
        clock.now = D(step["ts"])
        session.observe_user(D(step["ts"]))
        return
    if op == "tick":
        session.tick()
        return
    if op == "fill":
        _explicit_fill(session, step)
        return
    if op == "sweep":
        _sweep(session, step)
        return
    raise ValueError("unknown replay op " + op)


def _explicit_fill(session: ShareSession, step: dict) -> None:
    price = D(step["price"])
    matches = [
        order
        for order in session.state.orders
        if order.outcome == step["outcome"]
        and order.price == price
        and order.exchange_id
        and order.status == "open"
    ]
    if len(matches) != 1:
        raise RuntimeError("fill did not match one open maker order")
    session.apply_fills(
        [
            Fill(
                step["fill_id"],
                matches[0].exchange_id,
                D(step["shares"]),
                D(step.get("fill_price", step["price"])),
                step.get("liquidity", "maker"),
            )
        ]
    )


def _sweep(session: ShareSession, step: dict) -> None:
    """Fill only when the print is strictly through the resting maker price."""

    trade_price = D(step["trade_price"])
    left = D(step["shares"])
    fills = []
    for order in session.open_orders():
        if order.outcome != step["outcome"] or trade_price <= order.price or left <= 0:
            continue
        take = min(left, order.remaining)
        fills.append(
            Fill(
                "sweep-%s-%s" % (step["trade_id"], order.exchange_id),
                order.exchange_id,
                take,
                order.price,
            )
        )
        left -= take
    if fills:
        session.apply_fills(fills)
