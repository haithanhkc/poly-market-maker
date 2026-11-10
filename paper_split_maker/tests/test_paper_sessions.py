"""Offline runs of the shipped configs across three sessions. No network."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import load_config
from paper_split_maker.engine import Engine
from paper_split_maker.risk import worst_case_equity
from tests.helpers import book_event, run

ROOT = Path(__file__).resolve().parents[1]


def _play(engine, sessions: int) -> list[dict]:
    logs: list[dict] = []
    for index in range(sessions):
        start = index * 400_000
        end = start + 300_000
        market_id = f"s{index}"
        logs.extend(
            run(
                engine,
                [
                    {
                        "type": "market_open",
                        "event_id": f"open-{market_id}",
                        "ts": start,
                        "market_id": market_id,
                        "condition_id": f"0x{market_id}",
                        "slug": f"btc-updown-5m-{1_800_000_000 + index * 300}",
                        "question": "Synthetic BTC up or down",
                        "up_token_id": f"{market_id}-up",
                        "down_token_id": f"{market_id}-down",
                        "up_label": "Up",
                        "down_label": "Down",
                        "up_index": 0,
                        "down_index": 1,
                        "end_ts": end,
                        "tick_size": "0.01",
                        "min_order_size": "5",
                        "synthetic": True,
                        "accepting_orders": True,
                    },
                    book_event(market_id, "up", "0.48", "0.52", ts=start + 1_000),
                    book_event(market_id, "down", "0.48", "0.52", ts=start + 1_000),
                    {
                        "type": "heartbeat",
                        "event_id": f"hb-{market_id}",
                        "ts": start + 1_000,
                        "market_id": market_id,
                    },
                    {
                        "type": "heartbeat",
                        "event_id": f"live-{market_id}",
                        "ts": start + 3_000,
                        "market_id": market_id,
                    },
                ],
            )
        )
        orders = [
            order
            for order in engine.state.orders.values()
            if order.market_id == market_id and order.status in {"queued", "live"}
        ]
        if orders:
            order = orders[0]
            logs.extend(
                run(
                    engine,
                    [
                        {
                            "type": "adverse_fill",
                            "event_id": f"fill-{market_id}",
                            "ts": start + 6_000,
                            "order_id": order.order_id,
                            "size": str(order.size),
                            "label": "synthetic",
                        }
                    ],
                )
            )
        logs.extend(
            run(
                engine,
                [
                    {
                        "type": "heartbeat",
                        "event_id": f"stop-{market_id}",
                        "ts": end - 1_000,
                        "market_id": market_id,
                    },
                    {
                        "type": "cancel",
                        "event_id": f"flat-{market_id}",
                        "ts": end - 900,
                        "market_id": market_id,
                    },
                    {
                        "type": "heartbeat",
                        "event_id": f"merge-{market_id}",
                        "ts": end - 800,
                        "market_id": market_id,
                    },
                    {
                        "type": "resolution",
                        "event_id": f"res-{market_id}",
                        "ts": end,
                        "market_id": market_id,
                        "source": "fixture",
                        "label": "synthetic-fixture",
                        "winner": "up",
                    },
                ],
            )
        )
        assert worst_case_equity(engine.state) >= engine.state.loss_floor
    return logs


def test_paper_config_runs_three_sessions(tmp_path, capsys):
    config = load_config(ROOT / "config.paper.json")
    engine = Engine.create(config, tmp_path / "state.json", tmp_path / "logs")
    engine.mode = "paper"
    logs = _play(engine, 3)
    assert engine.state.markets.keys() >= {"s0", "s1", "s2"}
    for market_id in ("s0", "s1", "s2"):
        assert engine.state.markets[market_id].gross_split == Decimal("25")
    text = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "Traceback" not in text
    assert "Traceback" not in capsys.readouterr().err
    kinds = {row["kind"] for row in logs}
    assert "market_open" in kinds
    assert "split" in kinds or any(
        market.gross_split > 0 for market in engine.state.markets.values()
    )


def test_live_ladder_config_runs_three_sessions_offline(tmp_path, capsys):
    config = load_config(ROOT / "config.live.json")
    # Same numbers as the live file. Cancels complete in this offline ledger
    # so a fill can be observed; the process still does not touch the network.
    config.live = False
    config.auto_split = False
    config.ack_latency_ms = 0
    config.cancel_latency_ms = 0
    config.quote_cooldown_ms = 0
    config.min_order_rest_seconds = 0
    config.requote_min_interval_ms = 0
    config.fill_mode = "queue"
    config.improve_queue_shares = Decimal("0")
    config.stale_after_ms = 1_000_000
    engine2 = Engine.create(config, tmp_path / "state.json", tmp_path / "logs")
    engine2.mode = "live"
    logs: list[dict] = []
    for index in range(3):
        start = index * 400_000
        end = start + 300_000
        market_id = f"s{index}"
        logs.extend(
            run(
                engine2,
                [
                    {
                        "type": "market_open",
                        "event_id": f"open-{market_id}",
                        "ts": start,
                        "market_id": market_id,
                        "condition_id": f"0x{market_id}",
                        "slug": f"btc-updown-5m-{1_800_000_000 + index * 300}",
                        "question": "Synthetic BTC up or down",
                        "up_token_id": f"{market_id}-up",
                        "down_token_id": f"{market_id}-down",
                        "up_label": "Up",
                        "down_label": "Down",
                        "up_index": 0,
                        "down_index": 1,
                        "end_ts": end,
                        "tick_size": "0.01",
                        "min_order_size": "5",
                        "synthetic": True,
                        "accepting_orders": True,
                    },
                    {
                        "type": "split",
                        "event_id": f"split-{market_id}",
                        "ts": start + 10,
                        "market_id": market_id,
                        "amount": "25",
                    },
                    book_event(market_id, "up", "0.48", "0.52", ts=start + 20),
                    book_event(market_id, "down", "0.48", "0.52", ts=start + 20),
                    {
                        "type": "heartbeat",
                        "event_id": f"hb-{market_id}",
                        "ts": start + 30,
                        "market_id": market_id,
                    },
                ],
            )
        )
        posted = [
            order
            for order in engine2.state.orders.values()
            if order.market_id == market_id and order.status in {"queued", "live"}
        ]
        assert posted, market_id
        assert {order.outcome for order in posted} == {"up", "down"}
        down = next(order for order in posted if order.outcome == "down")
        logs.extend(
            run(
                engine2,
                [
                    {
                        "type": "adverse_fill",
                        "event_id": f"fill-{market_id}",
                        "ts": start + 40,
                        "order_id": down.order_id,
                        "size": "5",
                        "label": "synthetic",
                    },
                    book_event(
                        market_id,
                        "up",
                        "0.55",
                        "0.57",
                        ts=start + 50,
                        digest=f"up-bid-{market_id}",
                    ),
                    {
                        "type": "heartbeat",
                        "event_id": f"unwind-{market_id}",
                        "ts": start + 60,
                        "market_id": market_id,
                    },
                    book_event(
                        market_id,
                        "up",
                        "0.94",
                        "0.96",
                        ts=end - 40_000,
                        digest=f"up-late-{market_id}",
                    ),
                    book_event(
                        market_id,
                        "down",
                        "0.03",
                        "0.05",
                        ts=end - 40_000,
                        digest=f"down-late-{market_id}",
                    ),
                    {
                        "type": "heartbeat",
                        "event_id": f"late-{market_id}",
                        "ts": end - 40_000,
                        "market_id": market_id,
                    },
                    {
                        "type": "heartbeat",
                        "event_id": f"stop-{market_id}",
                        "ts": end - 1_000,
                        "market_id": market_id,
                    },
                    {
                        "type": "resolution",
                        "event_id": f"res-{market_id}",
                        "ts": end,
                        "market_id": market_id,
                        "source": "fixture",
                        "label": "synthetic-fixture",
                        "winner": "up",
                    },
                ],
            )
        )
        assert worst_case_equity(engine2.state) >= engine2.state.loss_floor
    assert engine2.state.markets["s2"].sold_down_shares >= Decimal("5")
    text = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "Traceback" not in text
    assert "Traceback" not in capsys.readouterr().err
    assert any(row["kind"] == "leg_unwind" for row in logs)
