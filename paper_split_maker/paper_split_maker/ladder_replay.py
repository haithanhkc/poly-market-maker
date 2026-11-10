"""Replay maker sells against a tape of prints.

The book is the last print on that outcome, with a one-tick bid under it.
An order fills only when a later buy print trades at its price. The reconstructed
ask shows 0.01 share, so the order is almost first in line. That overstates
fills. Reaction time is one print. Rebates are not cash.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import Config
from paper_split_maker.engine import Engine


def replay_session(
    config: Config,
    out_dir: Path,
    prints: list[dict],
    winner: str,
    *,
    split: str = "25",
    end_ms: int = 300_000,
    lead_seconds: int = 0,
) -> dict:
    """``prints`` are ``{sec, outcome, price, size}`` with outcome up/down.

    ``winner`` is ``up`` or ``down``. ``lead_seconds`` quotes that long before
    the open, at a 0.50 book, and keeps pre-open prints on their own clock.
    Returns realized pnl and sell stats.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lead = max(0, int(lead_seconds))
    end_ms = end_ms + lead * 1000
    engine = Engine.create(config, out_dir / "state.json", out_dir / "logs")
    engine.handle_event(
        {
            "type": "market_open",
            "event_id": "open",
            "ts": 0,
            "market_id": "m1",
            "condition_id": "0xm1",
            "slug": "synthetic-m1",
            "question": "Synthetic BTC up or down",
            "up_token_id": "m1-up",
            "down_token_id": "m1-down",
            "up_label": "Up",
            "down_label": "Down",
            "up_index": 0,
            "down_index": 1,
            "end_ts": end_ms,
            "tick_size": "0.01",
            "min_order_size": "5",
            "synthetic": True,
            "accepting_orders": True,
        }
    )
    engine.handle_event(
        {
            "type": "split",
            "event_id": "split",
            "ts": 1,
            "market_id": "m1",
            "amount": split,
        }
    )
    last = {"up": Decimal("0.50"), "down": Decimal("0.50")}
    sold = {"up": Decimal("0"), "down": Decimal("0")}
    if lead:
        # Rest at the pre-open 0.50 book before the first of their prints.
        _book(engine, last, 2, 0)
        engine.handle_event(
            {
                "type": "heartbeat",
                "event_id": "preopen",
                "ts": 2,
                "market_id": "m1",
            }
        )
    for index, item in enumerate(prints):
        sec = int(item["sec"])
        if lead == 0 and sec < 0:
            sec = 0
        ts = (sec + lead) * 1000
        if ts < 0:
            ts = 0
        if ts >= end_ms:
            ts = end_ms - 1
        outcome = str(item["outcome"])
        price = Decimal(str(item["price"])).quantize(Decimal("0.01"))
        if price <= 0 or price >= 1:
            continue
        _book(engine, last, ts, index)
        engine.handle_event(
            {
                "type": "heartbeat",
                "event_id": f"h-{index}",
                "ts": ts,
                "market_id": "m1",
            }
        )
        trade_ts = ts + 1
        if trade_ts >= end_ms:
            continue
        before = _sold(engine)
        engine.handle_event(
            {
                "type": "trade",
                "event_id": f"t-{index}",
                "ts": trade_ts,
                "trade_ts": trade_ts,
                "market_id": "m1",
                "outcome": outcome,
                "price": format(price, "f"),
                "size": str(item["size"]),
                "taker_side": "BUY",
                "trade_id": f"tape-{index}",
            }
        )
        after = _sold(engine)
        last[outcome] = price
        other = "down" if outcome == "up" else "up"
        complement = (Decimal("1") - price).quantize(Decimal("0.01"))
        if complement <= 0:
            complement = Decimal("0.01")
        if complement >= 1:
            complement = Decimal("0.99")
        last[other] = complement
        sold["up"] += after["up"] - before["up"]
        sold["down"] += after["down"] - before["down"]
    flat_ts = end_ms - 1
    _book(engine, last, flat_ts, 90000)
    engine.handle_event(
        {
            "type": "heartbeat",
            "event_id": "flat",
            "ts": flat_ts,
            "market_id": "m1",
        }
    )
    engine.handle_event(
        {
            "type": "resolution",
            "event_id": "resolve",
            "ts": end_ms + 1,
            "market_id": "m1",
            "source": "fixture",
            "label": "synthetic-fixture",
            "winner": winner,
        }
    )
    state = engine.state
    market = state.markets["m1"]
    return {
        "realized_pnl": state.realized_pnl,
        "sold_up": sold["up"],
        "sold_down": sold["down"],
        "sell_usdc": market.sold_up_proceeds + market.sold_down_proceeds,
        "cash": state.cash,
    }


def _sold(engine: Engine) -> dict:
    market = engine.state.markets["m1"]
    return {"up": market.sold_up_shares, "down": market.sold_down_shares}


def _book(engine: Engine, last: dict, ts: int, index: int) -> None:
    for outcome, mid in last.items():
        bid = mid - Decimal("0.01")
        ask = mid
        if bid <= 0:
            bid = Decimal("0.01")
            ask = Decimal("0.02")
        if ask >= 1:
            ask = Decimal("0.99")
            bid = Decimal("0.98")
        engine.handle_event(
            {
                "type": "book",
                "event_id": f"b-{index}-{outcome}-{ts}",
                "ts": ts,
                "market_id": "m1",
                "outcome": outcome,
                "bids": [[format(bid, "f"), "0.01"]],
                "asks": [[format(ask, "f"), "0.01"]],
                "hash": f"{outcome}-{mid}-{ts}-{index}",
            }
        )
