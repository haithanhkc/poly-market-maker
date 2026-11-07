"""Shared constructors for offline engine tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import Config
from paper_split_maker.engine import Engine

DEFAULTS = dict(
    initial_capital=Decimal("1000"),
    max_loss_fraction=Decimal("0.03"),
    per_market_budget=Decimal("40"),
    clip_size=Decimal("5"),
    min_order_size=Decimal("5"),
    order_size_quantum=Decimal("0.01"),
    min_edge=Decimal("0.01"),
    skew_edge_per_share=Decimal("0"),
    skew_size_per_share=Decimal("1"),
    max_spread=Decimal("0.10"),
    stale_after_ms=100_000,
    quote_cooldown_ms=0,
    stop_quoting_before_expiry_ms=1_000,
    ack_latency_ms=0,
    cancel_latency_ms=0,
    fill_mode="queue",
    trade_through_fills=False,
    extra_queue_shares=Decimal("0"),
    improve_queue_shares=Decimal("0"),
    maker_fee_rate=Decimal("0"),
    risk_fee_rate=Decimal("0"),
    published_taker_fee_rate=Decimal("0"),
    rebate_share=Decimal("0"),
    operational_reserve=Decimal("0"),
    global_rounding_reserve=Decimal("0"),
    freeze_band=Decimal("0"),
    max_open_markets=2,
    split_clip=Decimal("20"),
    min_pair_inventory=Decimal("10"),
    auto_split=True,
    merge_when_idle=False,
    paper_duration_seconds=20,
    paper_max_markets=1,
    poll_interval_seconds=2.0,
)


def make_engine(tmp_path: Path, **overrides) -> Engine:
    values = dict(DEFAULTS)
    values.update(overrides)
    config = Config(**values)
    config.validate()
    return Engine.create(config, tmp_path / "state.json", tmp_path / "logs")


def run(engine: Engine, events: list[dict]) -> list[dict]:
    logs = []
    for event in events:
        logs.extend(engine.handle_event(event))
    return logs


def market_event(market_id: str = "m1", end_ts: int = 1_000_000, **overrides) -> dict:
    event = {
        "type": "market_open",
        "event_id": f"open-{market_id}",
        "ts": 0,
        "market_id": market_id,
        "condition_id": f"0x{market_id}",
        "slug": f"synthetic-{market_id}",
        "question": "Synthetic BTC up or down",
        "up_token_id": f"{market_id}-up",
        "down_token_id": f"{market_id}-down",
        "up_label": "Up",
        "down_label": "Down",
        "up_index": 0,
        "down_index": 1,
        "end_ts": end_ts,
        "tick_size": "0.01",
        "min_order_size": "5",
        "synthetic": True,
        "accepting_orders": True,
    }
    event.update(overrides)
    return event


def book_event(
    market_id: str,
    outcome: str,
    bid: str,
    ask: str,
    *,
    ts: int,
    bid_size: str = "100",
    ask_size: str = "10",
    digest: str | None = None,
) -> dict:
    tag = digest or f"{outcome}-{bid}-{ask}-{ts}-{ask_size}"
    return {
        "type": "book",
        "event_id": f"book-{market_id}-{tag}",
        "ts": ts,
        "market_id": market_id,
        "outcome": outcome,
        "bids": [[bid, bid_size]],
        "asks": [[ask, ask_size]],
        "hash": tag,
    }


def both_books(market_id: str, bid: str, ask: str, ts: int, ask_size: str = "10") -> list[dict]:
    return [
        book_event(market_id, "up", bid, ask, ts=ts, ask_size=ask_size, digest=f"up-{ts}-{ask}-{ask_size}"),
        book_event(market_id, "down", bid, ask, ts=ts, ask_size=ask_size, digest=f"down-{ts}-{ask}-{ask_size}"),
    ]
