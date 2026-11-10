"""Offline tests for the live gates, pair floor, slices, and session stop."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from paper_split_maker.chain_data import (
    CTF_ADAPTER,
    SEL_APPROVE,
    V2_ROUTER,
    encode_approve,
    encode_split,
    position_target,
)
from paper_split_maker.chain_send import (
    approval_needed,
    official_position_call,
    relayer_body,
)
from paper_split_maker.cli import main
from paper_split_maker.config import Config, load_config
from paper_split_maker.credentials import load_credentials
from paper_split_maker.engine import Engine
from paper_split_maker.risk import reserved, worst_case_equity
from paper_split_maker.feeds import (
    describe_user_subscribe,
    market_subscribe,
    user_fill_deltas,
    user_subscribe,
)
from paper_split_maker.live_gate import live_gates
from paper_split_maker.live_runner import (
    apply_market_frame,
    quoting_allowed,
    split_plan,
)
from paper_split_maker.live_session import (
    LiveController,
    RecordingGateway,
    should_pre_split,
)
from tests.helpers import both_books, make_engine, market_event, run

ROOT = Path(__file__).resolve().parents[1]
SECRET = "super-secret-key-value"


def test_live_requires_all_three_gates(tmp_path):
    assert live_gates(config_live=True, cli_live=False, cli_yes=True) == (
        False,
        "paper",
    )
    assert live_gates(config_live=False, cli_live=True, cli_yes=True) == (
        False,
        "config_live_false",
    )
    assert live_gates(config_live=True, cli_live=True, cli_yes=False, is_tty=False) == (
        False,
        "confirmation_missing",
    )
    assert live_gates(config_live=True, cli_live=True, cli_yes=True)[0] is True
    config = ROOT / "config.paper.json"
    out = tmp_path / "out"
    code = main(
        [
            "--config",
            str(config),
            "--out",
            str(out),
            "--max-markets",
            "1",
            "--live",
            "--yes-i-understand-live-risk",
        ]
    )
    assert code == 2


def test_credentials_are_not_in_public_text():
    creds = load_credentials(
        {
            "POLY_PRIVATE_KEY": SECRET,
            "POLY_FUNDER": "0xabc",
            "POLY_SIGNATURE_TYPE": "2",
            "POLY_API_SECRET": "api-secret-value",
        }
    )
    public = repr(creds) + json.dumps(creds.public_view())
    public += describe_user_subscribe(creds.funder, 1)
    subscribe = user_subscribe("key", "api-secret-value", "pass", ["0xcondition"])
    assert SECRET not in public
    assert "api-secret-value" not in public
    assert "api-secret-value" not in describe_user_subscribe(creds.funder, 1)
    assert creds.redact(f"failed {SECRET}") == "failed [redacted]"
    assert subscribe["auth"]["secret"] == "api-secret-value"
    assert market_subscribe(["1"])["type"] == "market"


def test_pre_split_waits_for_confirmation(tmp_path):
    open_ms = 10_000
    assert should_pre_split(4_000, open_ms, 5_000, False, True) is False
    assert should_pre_split(6_000, open_ms, 5_000, False, True) is True
    assert should_pre_split(10_000, open_ms, 5_000, False, True) is False
    assert should_pre_split(6_000, open_ms, 5_000, True, True) is False
    assert should_pre_split(6_000, open_ms, 5_000, False, False) is False
    assert quoting_allowed(False) is False
    engine = make_engine(
        tmp_path,
        auto_split=False,
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
        clip_size=Decimal("5"),
    )
    run(
        engine,
        [
            market_event(),
            {
                "type": "split_pending",
                "event_id": "p",
                "ts": 1,
                "market_id": "m1",
                "amount": "5",
            },
            *both_books("m1", "0.40", "0.50", 2),
            {"type": "heartbeat", "event_id": "h", "ts": 3},
        ],
    )
    market = engine.state.markets["m1"]
    assert market.up == 0
    assert market.pending_split == Decimal("5")
    assert engine.state.orders == {}
    run(
        engine,
        [
            {
                "type": "split_abandoned",
                "event_id": "a",
                "ts": 4,
                "market_id": "m1",
                "amount": "5",
            }
        ],
    )
    assert market.pending_split == 0
    assert market.up == 0


def test_confirmed_split_can_be_quoted(tmp_path):
    engine = make_engine(
        tmp_path,
        auto_split=False,
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
        clip_size=Decimal("5"),
    )
    run(
        engine,
        [
            market_event(),
            {
                "type": "split_pending",
                "event_id": "p",
                "ts": 1,
                "market_id": "m1",
                "amount": "5",
            },
            {
                "type": "split_confirmed",
                "event_id": "c",
                "ts": 2,
                "market_id": "m1",
                "amount": "5",
            },
            *both_books("m1", "0.40", "0.50", 3),
            {"type": "heartbeat", "event_id": "h1", "ts": 4},
            {"type": "heartbeat", "event_id": "h2", "ts": 5},
        ],
    )
    assert engine.state.markets["m1"].up == Decimal("5")
    assert engine.state.orders


def test_pair_floor_and_session_cap(tmp_path):
    engine = make_engine(tmp_path, clip_size=Decimal("5"))
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    up = next(order for order in engine.state.orders.values() if order.outcome == "up")
    run(
        engine,
        [
            {
                "type": "adverse_fill",
                "event_id": "fill",
                "ts": 4,
                "order_id": up.order_id,
                "size": str(up.size),
                "label": "synthetic",
            },
            *both_books("m1", "0.40", "0.42", 5),
            {"type": "heartbeat", "event_id": "h3", "ts": 6},
            {"type": "heartbeat", "event_id": "h4", "ts": 7},
        ],
    )
    down = [
        order
        for order in engine.state.orders.values()
        if order.outcome == "down" and order.status in {"queued", "live"}
    ]
    assert down
    assert min(order.price for order in down) >= Decimal("0.49")

    capped = make_engine(
        tmp_path / "cap",
        session_max_loss=Decimal("1"),
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
    )
    run(
        capped,
        [
            market_event(),
            *both_books("m1", "0.30", "0.40", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    assert capped.state.markets["m1"].session_stopped is False
    live = next(
        order for order in capped.state.orders.values() if order.status == "live"
    )
    run(
        capped,
        [
            {
                "type": "adverse_fill",
                "event_id": "cheap",
                "ts": 4,
                "order_id": live.order_id,
                "size": str(live.size),
                "label": "synthetic",
            },
            {"type": "heartbeat", "event_id": "h3", "ts": 5},
        ],
    )
    assert capped.state.markets["m1"].session_stopped is True
    assert not [
        order
        for order in capped.state.orders.values()
        if order.status in {"queued", "live"}
    ]


def test_reconcile_mismatch_stops_quoting(tmp_path):
    engine = make_engine(tmp_path)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    live_ids = [
        order.order_id
        for order in engine.state.orders.values()
        if order.status == "live"
    ]
    assert live_ids
    logs = run(
        engine,
        [
            {
                "type": "reconcile",
                "event_id": "r1",
                "ts": 4,
                "open_order_ids": live_ids,
            }
        ],
    )
    assert any(row["reason"] == "ok" for row in logs)
    logs = run(
        engine,
        [
            {
                "type": "reconcile",
                "event_id": "r2",
                "ts": 5,
                "open_order_ids": [],
            }
        ],
    )
    # A brief lag on the open-orders endpoint is tolerated.
    assert engine.state.mismatch_stop is False
    assert any(row["reason"] == "pending" for row in logs)
    logs = run(
        engine,
        [
            {
                "type": "reconcile",
                "event_id": "r3",
                "ts": 5 + 15000,
                "open_order_ids": [],
            }
        ],
    )
    # A persistent mismatch stops only the affected market, not the run.
    assert engine.state.mismatch_stop is False
    assert engine.state.markets["m1"].session_stopped is True
    assert any(row["reason"] == "mismatch" for row in logs)
    before = len(engine.state.orders)
    run(
        engine,
        [
            *both_books("m1", "0.40", "0.50", 6),
            {"type": "heartbeat", "event_id": "h3", "ts": 7},
        ],
    )
    assert len(engine.state.orders) == before


def test_disconnect_and_kill_switch_cancel(tmp_path):
    engine = make_engine(tmp_path, kill_switch_file=str(tmp_path / "kill.switch"))
    gateway = RecordingGateway(dry_run=True)
    controller = LiveController(engine, gateway, dry_run=True)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    controller.on_disconnect(4)
    assert ("cancel_all",) in gateway.calls
    assert gateway.signed is False
    assert engine.state.feed_up is False
    assert controller.quoting is False

    other = make_engine(tmp_path / "kill", kill_switch_file=str(tmp_path / "STOP"))
    Path(tmp_path / "STOP").write_text("stop", encoding="utf-8")
    run(
        other,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h", "ts": 2},
        ],
    )
    assert other.state.killed is True


def test_user_fills_during_cancel_and_replays(tmp_path):
    engine = make_engine(tmp_path, cancel_latency_ms=5_000, clip_size=Decimal("5"))
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    order = next(item for item in engine.state.orders.values() if item.status == "live")
    run(
        engine,
        [
            {
                "type": "cancel",
                "event_id": "c",
                "ts": 4,
                "order_id": order.order_id,
            }
        ],
    )
    assert order.status == "cancel_requested"
    run(
        engine,
        [
            {
                "type": "user_fill",
                "event_id": "f2",
                "ts": 6,
                "order_id": order.order_id,
                "trade_id": "trade-b",
                "size": "1",
            },
            {
                "type": "user_fill",
                "event_id": "f1",
                "ts": 5,
                "order_id": order.order_id,
                "trade_id": "trade-a",
                "size": "1",
            },
            {
                "type": "user_fill",
                "event_id": "dup",
                "ts": 7,
                "order_id": order.order_id,
                "trade_id": "trade-a",
                "size": "1",
            },
            {
                "type": "user_fill",
                "event_id": "big",
                "ts": 8,
                "order_id": order.order_id,
                "trade_id": "trade-big",
                "size": "100",
            },
        ],
    )
    assert order.remaining == order.size - Decimal("2")
    assert "trade-a" in engine.state.seen_trade_ids
    assert "trade-big" in engine.state.seen_trade_ids


def test_restart_cannot_loosen_the_session_cap(tmp_path):
    engine = make_engine(tmp_path, session_max_loss=Decimal("1"))
    looser = Config(**{**engine.config.__dict__})
    looser.session_max_loss = Decimal("5")
    resumed = Engine.load(looser, tmp_path / "state.json", tmp_path / "logs")
    assert resumed.state.session_max_loss == Decimal("1")
    tighter = Config(**{**engine.config.__dict__})
    tighter.session_max_loss = Decimal("0.25")
    resumed = Engine.load(tighter, tmp_path / "state.json", tmp_path / "logs")
    assert resumed.state.session_max_loss == Decimal("0.25")


def test_calldata_is_labeled_and_the_secret_stays_out():
    engine_config = load_config(ROOT / "config.live.json")
    assert engine_config.live is True
    plan = split_plan("v1", "0x" + "11" * 32, "5")
    assert plan["target"] == CTF_ADAPTER
    assert plan["data"].startswith("0x72ce4275")
    assert plan["confirmed"] is False
    assert position_target("v2", False) == V2_ROUTER
    assert encode_split("v2", "0x" + "11" * 32, 5_000_000).startswith("0x82d3b9f1")
    approve = encode_approve(CTF_ADAPTER, 10)
    assert approve.startswith("0x" + SEL_APPROVE)
    assert approval_needed(0, 10) is True
    assert approval_needed(10, 10) is False
    body = relayer_body(
        plan,
        load_credentials(
            {
                "POLY_PRIVATE_KEY": SECRET,
                "POLY_FUNDER": "0xabc",
                "POLY_SIGNATURE_TYPE": "1",
            }
        ),
    )
    assert SECRET not in json.dumps(body)
    payload = {
        "event_type": "trade",
        "status": "MATCHED",
        "id": "trade-1",
        "maker_orders": [{"order_id": "ex-1", "matched_amount": "2", "price": "0.50"}],
    }
    fills = user_fill_deltas(payload, {})
    assert fills[0]["trade_id"] == "trade-1"
    assert fills[0]["size"] == "2"


def _quote_rows(engine):
    return sorted(
        (order.outcome, order.price, order.size)
        for order in engine.state.orders.values()
        if order.status in {"queued", "live"}
    )


def test_live_config_quotes_both_legs_and_holds_the_slot(tmp_path):
    shipped = load_config(ROOT / "config.live.json")
    assert shipped.live is True
    assert shipped.strategy == "ladder"
    assert shipped.initial_capital == Decimal("59")
    assert shipped.loss_floor() == Decimal("47.20")
    assert shipped.loss_budget() == Decimal("11.80")
    assert shipped.split_clip == Decimal("25")
    assert shipped.session_max_loss == Decimal("6")
    assert shipped.ladder_levels == 3
    assert shipped.max_open_markets == 1
    assert shipped.kill_switch_file == "kill.switch"
    assert shipped.leg_unwind_seconds == 5
    assert shipped.split_lead_seconds == 8
    shared = dict(
        initial_capital=Decimal("200"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("8"),
        clip_size=Decimal("5"),
        min_edge=Decimal("0.01"),
        split_clip=Decimal("5"),
        min_pair_inventory=Decimal("5"),
        quote_cooldown_ms=500,
        stop_quoting_before_expiry_ms=20_000,
        cancel_latency_ms=500,
        max_open_markets=1,
        requote_min_ticks=1,
        requote_min_interval_ms=500,
        min_order_rest_seconds=15,
        pair_min_edge=Decimal("0"),
    )
    live = Config(
        live=True,
        auto_split=False,
        fill_mode="none",
        kill_switch_file="kill.switch",
        **shared,
    )
    old = Config(**shared)
    assert live.loss_floor() == Decimal("194")
    assert old.loss_floor() == Decimal("194")

    def opened(config, path):
        engine = Engine.create(config, path / "state.json", path / "logs")
        run(
            engine,
            [
                market_event(end_ts=1_000_000),
                {
                    "type": "split",
                    "event_id": "split-5",
                    "ts": 1,
                    "market_id": "m1",
                    "amount": "5",
                },
                *both_books("m1", "0.40", "0.50", 1_000),
                {"type": "heartbeat", "event_id": "h1", "ts": 1_000},
                {"type": "heartbeat", "event_id": "h2", "ts": 1_001},
            ],
        )
        return engine

    live_engine = opened(live, tmp_path / "live")
    old_engine = opened(old, tmp_path / "old")
    assert _quote_rows(live_engine) == _quote_rows(old_engine)
    assert _quote_rows(live_engine) == [
        ("down", Decimal("0.50"), Decimal("5")),
        ("up", Decimal("0.50"), Decimal("5")),
    ]
    market = live_engine.state.markets["m1"]
    assert reserved(live_engine.state, market, "up") == Decimal("5")
    assert worst_case_equity(live_engine.state) >= live_engine.state.loss_floor
    rejected = run(
        live_engine,
        [
            {
                "type": "merge",
                "event_id": "merge-reserved",
                "ts": 1_100,
                "market_id": "m1",
                "amount": "5",
            }
        ],
    )
    assert any(row["reason"] == "reserved_inventory" for row in rejected)
    assert market.up == Decimal("5")
    blocked = run(
        live_engine,
        [market_event(market_id="m2", end_ts=1_000_000, ts=1_200)],
    )
    assert any(row["reason"] == "max_open_markets" for row in blocked)
    assert "m2" not in live_engine.state.markets

    apply_market_frame(
        live_engine,
        {
            "event_type": "book",
            "asset_id": "m1-up",
            "bids": [{"price": "0.41", "size": "100"}],
            "asks": [{"price": "0.51", "size": "10"}],
            "timestamp": 2,
            "hash": "up-51",
        },
        2_000,
    )
    apply_market_frame(
        live_engine,
        {
            "event_type": "best_bid_ask",
            "asset_id": "m1-down",
            "best_bid": "0.41",
            "best_ask": "0.51",
            "timestamp": 2,
        },
        2_000,
        seq=1,
    )
    run(
        live_engine,
        [
            {"type": "heartbeat", "event_id": "rq1", "ts": 2_000},
            {"type": "heartbeat", "event_id": "rq2", "ts": 2_600},
        ],
    )
    working = [
        order
        for order in live_engine.state.orders.values()
        if order.status in {"queued", "live"}
    ]
    # A one-tick move does not reprice before the 15 s minimum rest.
    assert {order.price for order in working} == {Decimal("0.50")}
    assert worst_case_equity(live_engine.state) >= live_engine.state.loss_floor
    assert live_engine.chain_outbox == []


class _Handle:
    def __init__(self, action, condition_id, amount):
        self.action = action
        self.condition_id = condition_id
        self.amount = amount

    def wait(self):
        return {"transaction_hash": f"0x{self.action}", "transaction_id": "tid"}


class _Official:
    def __init__(self):
        self.calls = []

    def split_position(self, condition_id, amount):
        self.calls.append(("split", condition_id, amount))
        return _Handle("split", condition_id, amount)

    def merge_positions(self, condition_id, amount):
        self.calls.append(("merge", condition_id, amount))
        return _Handle("merge", condition_id, amount)

    def redeem_positions(self, condition_id):
        self.calls.append(("redeem", condition_id, None))
        return _Handle("redeem", condition_id, None)


def test_official_client_signs_proxy_split_merge_redeem():
    creds = load_credentials(
        {
            "POLY_PRIVATE_KEY": SECRET,
            "POLY_FUNDER": "0xabc",
            "POLY_SIGNATURE_TYPE": "1",
            "RELAYER_API_KEY": "relayer-key",
            "RELAYER_API_KEY_ADDRESS": "0xsigner",
        }
    )
    client = _Official()
    condition = "0x" + "11" * 32
    assert official_position_call(
        creds, "split", condition, 5_000_000, client=client
    ) == ("0xsplit")
    assert official_position_call(
        creds, "merge", condition, 5_000_000, client=client
    ) == ("0xmerge")
    assert (
        official_position_call(creds, "redeem", condition, 0, client=client)
        == "0xredeem"
    )
    assert [item[0] for item in client.calls] == ["split", "merge", "redeem"]
    assert SECRET not in json.dumps(client.calls)


def test_reconcile_lag_clears_without_stopping(tmp_path):
    engine = make_engine(tmp_path)
    run(
        engine,
        [
            market_event(),
            *both_books("m1", "0.40", "0.50", 1),
            {"type": "heartbeat", "event_id": "h1", "ts": 2},
            {"type": "heartbeat", "event_id": "h2", "ts": 3},
        ],
    )
    live_ids = [
        order.order_id
        for order in engine.state.orders.values()
        if order.status == "live"
    ]
    assert live_ids
    run(engine, [{"type": "reconcile", "event_id": "a1", "ts": 4, "open_order_ids": []}])
    logs = run(
        engine,
        [{"type": "reconcile", "event_id": "a2", "ts": 3000, "open_order_ids": live_ids}],
    )
    assert any(row["reason"] == "ok" for row in logs)
    run(engine, [{"type": "reconcile", "event_id": "a3", "ts": 20000, "open_order_ids": live_ids}])
    assert engine.state.mismatch_stop is False
