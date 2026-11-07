"""Deterministic paper replay. No credentials and no network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from poly_market_maker.sell_equal.config import SellConfig, load_config
from poly_market_maker.sell_equal.engine import Fill, MarketView, SellEqualEngine
from poly_market_maker.sell_equal.paper import PaperBroker
from poly_market_maker.sell_equal.quant import D


class Clock:
    def __init__(self, start: str = "0"):
        self.now = D(start)

    def __call__(self) -> Decimal:
        return self.now


def run_replay(config: SellConfig, steps: list[dict], clock: Clock | None = None):
    clock = clock or Clock("0")
    broker = PaperBroker()
    engine = SellEqualEngine(config, broker, clock)
    expectations = []
    for step in steps:
        expectations.extend(_apply_step(engine, broker, clock, step))
    return engine, broker, expectations


def _apply_step(engine, broker, clock: Clock, step: dict):
    op = step["op"]
    if op == "at":
        clock.now = D(step["ts"])
        return []
    if op == "market":
        clock.now = D(step["ts"])
        engine.observe_market(
            MarketView(
                ts=D(step["ts"]),
                accepting=bool(step.get("accepting", True)),
                ended=bool(step.get("ended", False)),
                tick_size=engine.config.tick_size,
                min_order_size=engine.config.min_order_size,
            )
        )
        return []
    if op == "user":
        clock.now = D(step["ts"])
        engine.observe_user(D(step["ts"]))
        return []
    if op == "tick":
        engine.tick()
        return []
    if op == "fill":
        _fill(engine, step)
        return []
    if op == "fills":
        engine.apply_fills([_make_fill(engine, item) for item in step["items"]])
        return []
    if op == "shutdown":
        engine.shutdown()
        return []
    if op == "restart":
        resumed = SellEqualEngine.resume(
            engine.config, broker, clock, engine.config.state_path
        )
        engine.state = resumed.state
        engine._seen = resumed._seen
        engine.market = resumed.market
        return []
    if op == "expect":
        _expect(engine, step)
        return [step]
    raise ValueError("unknown replay op " + op)


def _fill(engine, step: dict) -> None:
    engine.apply_fills([_make_fill(engine, step)])


def _make_fill(engine, step: dict) -> Fill:
    outcome = step["outcome"]
    price = D(step["price"])
    matches = [
        order
        for order in engine.state.orders
        if order.outcome == outcome
        and order.price == price
        and order.exchange_id
        and order.status in ("open", "cancel_requested", "unknown_cancel")
    ]
    if len(matches) != 1:
        raise RuntimeError("replay fill did not match one open order")
    return Fill(
        fill_id=step["fill_id"],
        exchange_order_id=matches[0].exchange_id,
        shares=D(step["shares"]),
        price=price if "fill_price" not in step else D(step["fill_price"]),
    )


def _expect(engine, step: dict) -> None:
    if "gross_up" in step:
        _eq(engine.state.gross_sold["up"], step["gross_up"], "gross_up")
    if "gross_down" in step:
        _eq(engine.state.gross_sold["down"], step["gross_down"], "gross_down")
    if "hedge_size" in step:
        hedges = [
            order
            for order in engine.open_orders()
            if order.role == "hedge" and order.outcome == step["hedge_outcome"]
        ]
        if len(hedges) != 1:
            raise AssertionError("expected one open hedge, found %s" % len(hedges))
        _eq(hedges[0].price, step["hedge_price"], "hedge_price")
        _eq(hedges[0].original_size, step["hedge_size"], "hedge_size")
    if "hedge_count" in step:
        count = len([order for order in engine.open_orders() if order.role == "hedge"])
        if count != int(step["hedge_count"]):
            raise AssertionError("hedge_count %s != %s" % (count, step["hedge_count"]))
    if step.get("halt"):
        if engine.state.halt_reason != step["halt"]:
            raise AssertionError(
                "halt %s != %s" % (engine.state.halt_reason, step["halt"])
            )


def _eq(actual: Decimal, expected: str, label: str) -> None:
    if actual != D(expected):
        raise AssertionError("%s %s != %s" % (label, actual, expected))


def load_script(path: str | Path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    config = load_config(raw["config"])
    return config, raw["steps"]
