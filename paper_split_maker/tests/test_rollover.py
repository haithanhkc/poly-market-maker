"""Consecutive-session rollover and the loss-floor sell. No network and no keys."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from paper_split_maker.live_runner import (
    _confirm_splits,
    _consider_pre_split,
    _poll_public,
    _send_new_quotes,
)
from paper_split_maker.risk import worst_case_equity
from tests.helpers import book_event, make_engine, market_event, run

# 1_800_000_000 is divisible by 300, so it is a 5-minute window open.
OPEN0 = 1_800_000_000
FUNDER = "0xC6C1B52664581bc51f18eC0DB9FE8ddF14396a45"


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gamma(open_s: int) -> dict:
    slug = f"btc-updown-5m-{open_s}"
    return {
        "id": slug,
        "version": "v1",
        "conditionId": f"0x{open_s:040x}",
        "slug": slug,
        "question": slug,
        "outcomes": ["Up", "Down"],
        "clobTokenIds": [f"{open_s}-up", f"{open_s}-down"],
        "endDate": _iso(open_s + 300),
        "acceptingOrders": True,
        "closed": False,
        "orderMinSize": "5",
        "orderPriceMinTickSize": "0.01",
        "negRisk": False,
    }


class _Reader:
    def __init__(self):
        self.now_ms = 0
        self.up = ("0.88", "0.90")
        self.down = ("0.08", "0.10")
        self.fail_tokens: set[str] = set()

    def discover_btc_updown(self, now_s, limit, slugs=None):
        found = []
        for slug in slugs or []:
            tail = str(slug).rsplit("-", 1)[-1]
            if not tail.isdigit():
                continue
            found.append(_gamma(int(tail)))
            if len(found) >= limit:
                break
        return found

    def fetch_book(self, token):
        from paper_split_maker.public_client import PublicDataError

        token = str(token)
        if token in self.fail_tokens:
            raise PublicDataError(f"http 404 for {token}", status=404)
        bid, ask = self.up if token.endswith("up") else self.down
        return {
            "timestamp": str(self.now_ms),
            "hash": f"{token}-{self.now_ms}-{bid}-{ask}",
            "bids": [{"price": bid, "size": "100"}],
            "asks": [{"price": ask, "size": "100"}],
            "tick_size": "0.01",
            "min_order_size": "5",
        }

    def fetch_resolution(self, condition_id):
        return {"data": []}


class _Exchange:
    def __init__(self):
        self.posts = []

    def post(self, client, orders):
        made = []
        for order in orders:
            ident = f"ex-{len(self.posts) + 1}"
            self.posts.append({"order_id": ident, **order})
            made.append({"orderID": ident, "success": True})
        return made


def _rollover_engine(tmp_path):
    engine = make_engine(
        tmp_path,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("30"),
        min_order_size=Decimal("5"),
        split_clip=Decimal("30"),
        min_pair_inventory=Decimal("5"),
        auto_split=False,
        merge_when_idle=True,
        max_open_markets=3,
        split_lead_seconds=45,
        split_confirm_timeout_ms=30_000,
        stop_quoting_before_expiry_ms=20_000,
        stale_after_ms=60_000,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        fill_mode="none",
        risk_fee_rate=Decimal("0.07"),
        maker_fee_rate=Decimal("0"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        min_edge=Decimal("0.01"),
        max_spread=Decimal("0.10"),
        market_slugs=("no-initial-discovery-placeholder",),
    )
    engine.mode = "live"
    return engine


def test_three_consecutive_sessions_are_loaded_and_quoted(tmp_path, monkeypatch):
    import paper_split_maker.chain_send as chain_send

    splits = []

    def submit(creds, action, condition_id, amount, *args, **kwargs):
        splits.append((action, condition_id, str(amount)))
        return (f"tx-{condition_id[-8:]}", True)

    monkeypatch.setattr(chain_send, "submit_position", submit)
    monkeypatch.setattr(chain_send, "split_tokens_present", lambda *a, **k: True)
    engine = _rollover_engine(tmp_path)
    reader = _Reader()
    exchange = _Exchange()
    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    posted = {}
    overlapped = False
    slug0 = f"btc-updown-5m-{OPEN0}"
    wanted = [OPEN0 + 300, OPEN0 + 600, OPEN0 + 900]

    def step(second: int) -> None:
        nonlocal overlapped
        stamp = second * 1000
        reader.now_ms = stamp
        notes: list[str] = []
        _poll_public(engine, reader, engine.config, stamp, 3, notes)
        _confirm_splits(engine, creds, stamp, notes)
        _consider_pre_split(engine, reader, engine.config, creds, stamp, notes)
        before = len(exchange.posts)
        _send_new_quotes(engine, None, exchange.post, creds, notes)
        for item in exchange.posts[before:]:
            posted.setdefault(item["token_id"], item["order_id"])
        ids = set(engine.state.markets)
        for left, right in zip(wanted, wanted[1:]):
            if f"btc-updown-5m-{left}" in ids and f"btc-updown-5m-{right}" in ids:
                overlapped = True

    # Mid-window start: the session already open is not joined.
    step(OPEN0 + 60)
    assert slug0 not in engine.state.markets
    assert splits == []

    for open_s in wanted:
        # A few passes inside the pre-split lead: load, fund, split, confirm.
        for delta in range(40, 20, -1):
            step(open_s - delta)
        # After the open, books are fresh and the confirmed split is quoted.
        for delta in (5, 15, 25):
            step(open_s + delta)
        # The closed session's book 404 must not take the feed down.
        reader.fail_tokens.add(f"{open_s - 300}-up")
        reader.fail_tokens.add(f"{open_s - 300}-down")

    for open_s in wanted:
        token = f"{open_s}-up"
        assert token in posted, f"{token} was split but never quoted"
        assert posted[token]
    assert overlapped
    assert slug0 not in {item[1] for item in splits}
    split_conditions = [item[1] for item in splits if item[0] == "split"]
    assert len(split_conditions) == 3
    # Finished flat sessions are removed, so a later session is not blocked
    # by an expired market that discovery used to keep forever.
    assert f"btc-updown-5m-{OPEN0 + 300}" not in engine.state.markets
    assert f"btc-updown-5m-{OPEN0 + 900}" in engine.state.markets
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in log.splitlines() if line]
    posted_rows = [row for row in rows if row["kind"] == "order_posted"]
    assert len(posted_rows) >= 3
    for row in posted_rows:
        assert row["order_id"].startswith("ex-")
        assert row["outcome"] in {"up", "down"}
        assert Decimal(row["size"]) >= Decimal("5")
        assert Decimal(row["price"]) > 0
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_no_split_when_the_engine_rejects_the_market(tmp_path, monkeypatch):
    import paper_split_maker.chain_send as chain_send

    splits = []
    monkeypatch.setattr(
        chain_send,
        "submit_position",
        lambda *a, **k: splits.append(a) or ("tx", True),
    )
    engine = _rollover_engine(tmp_path)
    engine.config.max_open_markets = 1
    # A market that does not end at the next window keeps the only slot.
    run(
        engine,
        [
            market_event(
                "busy",
                end_ts=(OPEN0 + 10_000) * 1000,
                event_id="open-busy",
                ts=OPEN0 * 1000,
            )
        ],
    )
    reader = _Reader()
    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    notes: list[str] = []
    stamp = (OPEN0 + 300 - 30) * 1000
    reader.now_ms = stamp
    _consider_pre_split(engine, reader, engine.config, creds, stamp, notes)
    assert splits == []
    assert any("no split" in note for note in notes)
    assert f"btc-updown-5m-{OPEN0 + 300}" not in engine.state.markets


def _floor_engine(tmp_path, name: str):
    engine = make_engine(
        tmp_path / name,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.03"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("30"),
        min_order_size=Decimal("5"),
        split_clip=Decimal("30"),
        min_pair_inventory=Decimal("5"),
        auto_split=False,
        merge_when_idle=True,
        stale_after_ms=100_000,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        fill_mode="none",
        risk_fee_rate=Decimal("0.07"),
        maker_fee_rate=Decimal("0"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        min_edge=Decimal("0.01"),
        max_spread=Decimal("0.10"),
        stop_quoting_before_expiry_ms=20_000,
    )
    engine.mode = "live"
    assert engine.state.loss_floor == Decimal("29.10")
    return engine


def _quote(engine, up, down, ts: int):
    run(
        engine,
        [
            book_event(
                "m1",
                "up",
                up[0],
                up[1],
                ts=ts,
                digest=f"up-{ts}-{up[1]}",
            ),
            book_event(
                "m1",
                "down",
                down[0],
                down[1],
                ts=ts,
                digest=f"down-{ts}-{down[1]}",
            ),
            {"type": "heartbeat", "event_id": f"hb-{ts}", "ts": ts},
        ],
    )


def test_sell_is_posted_once_the_floor_allows_min_size(tmp_path, capsys):
    """30 capital, 30 split, 0.90 budget. A 5-share sell loses 0.90 at 0.82
    before the risk-fee reserve, so a mid price must not be sold and a
    price the floor accepts must be posted without leaving the session.
    """
    engine = _floor_engine(tmp_path, "up")
    run(
        engine,
        [
            market_event(end_ts=500_000),
            {
                "type": "split",
                "event_id": "split",
                "ts": 1,
                "market_id": "m1",
                "amount": "30",
            },
        ],
    )
    market = engine.state.markets["m1"]
    _quote(engine, ("0.40", "0.50"), ("0.40", "0.50"), 2)
    assert engine.state.orders == {}
    assert market.up == Decimal("30") and market.down == Decimal("30")
    # A wide book is not a reason to merge the split. Stay and re-check.
    _quote(engine, ("0.10", "0.40"), ("0.10", "0.40"), 3)
    assert market.up == Decimal("30") and market.down == Decimal("30")
    assert engine.state.orders == {}
    # ~0.82 is the zero-fee budget price (1 - 0.90/5). With the 7% risk-fee
    # reserve the same 5 shares clear a little higher; 0.90 does.
    _quote(engine, ("0.88", "0.90"), ("0.08", "0.10"), 4)
    sells = [
        order
        for order in engine.state.orders.values()
        if order.status in {"queued", "live"}
    ]
    assert sells
    assert {order.outcome for order in sells} == {"up"}
    for order in sells:
        assert order.size >= Decimal("5")
        assert order.price > Decimal("0.88")
    assert worst_case_equity(engine.state) >= engine.state.loss_floor

    def post(client, orders):
        return [
            {"orderID": f"0xposted{index}", "success": True}
            for index, _order in enumerate(orders, start=1)
        ]

    creds = SimpleNamespace(redact=lambda text: text)
    notes: list[str] = []
    _send_new_quotes(engine, None, post, creds, notes)
    printed = capsys.readouterr().out
    assert "order_posted side=up" in printed
    assert "price=" in printed and "size=" in printed and "order_id=0xposted1" in printed
    assert any("order_id=0xposted1" in note for note in notes)
    log = (tmp_path / "up" / "logs" / "events.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in log.splitlines() if '"order_posted"' in line]
    assert rows
    assert rows[0]["order_id"] == "0xposted1"
    assert rows[0]["outcome"] == "up"
    assert Decimal(rows[0]["size"]) >= Decimal("5")

    other = _floor_engine(tmp_path, "down")
    run(
        other,
        [
            market_event(end_ts=500_000, event_id="open-down", ts=0),
            {
                "type": "split",
                "event_id": "split-down",
                "ts": 1,
                "market_id": "m1",
                "amount": "30",
            },
        ],
    )
    _quote(other, ("0.08", "0.10"), ("0.88", "0.90"), 4)
    sides = {order.outcome for order in other.state.orders.values()}
    assert sides == {"down"}
    assert worst_case_equity(other.state) >= other.state.loss_floor


def _run14(tmp_path):
    """capital 30, 3.4% loss budget, 5-share splits, one open market."""
    engine = make_engine(
        tmp_path,
        initial_capital=Decimal("30"),
        max_loss_fraction=Decimal("0.034"),
        per_market_budget=Decimal("30"),
        clip_size=Decimal("5"),
        split_clip=Decimal("5"),
        min_order_size=Decimal("5"),
        min_pair_inventory=Decimal("5"),
        auto_split=False,
        merge_when_idle=True,
        max_open_markets=1,
        split_lead_seconds=45,
        stop_quoting_before_expiry_ms=20_000,
        stale_feed_seconds=10,
        stale_after_ms=1_000_000,
        quote_cooldown_ms=0,
        ack_latency_ms=0,
        cancel_latency_ms=0,
        fill_mode="none",
        risk_fee_rate=Decimal("0.07"),
        maker_fee_rate=Decimal("0"),
        operational_reserve=Decimal("0.05"),
        global_rounding_reserve=Decimal("0.01"),
        min_edge=Decimal("0.01"),
        max_spread=Decimal("0.10"),
    )
    engine.mode = "live"
    assert engine.state.loss_floor == Decimal("28.980")
    return engine


def test_loss_budget_holds_a_fresh_pair_and_logs_every_skip(tmp_path, capsys):
    """A mid 5-share book does not fit the $1.02 budget, so nothing is posted.

    The close-improve window used to merge that unsold pair. A later book
    the budget does allow must still be quoted, and every skip is printed.
    """
    engine = _run14(tmp_path)
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {
                "type": "split",
                "event_id": "split",
                "ts": 1_000,
                "market_id": "m1",
                "amount": "5",
            },
        ],
    )
    _quote(engine, ("0.45", "0.55"), ("0.45", "0.55"), 10_000)
    _quote(engine, ("0.45", "0.55"), ("0.45", "0.55"), 12_000)
    market = engine.state.markets["m1"]
    assert market.up == Decimal("5") and market.down == Decimal("5")
    assert engine.state.orders == {}
    # 170s is inside close-improve (starts at 165s) and the books are fresh.
    _quote(engine, ("0.45", "0.55"), ("0.45", "0.55"), 170_000)
    assert market.up == Decimal("5") and market.down == Decimal("5")
    assert engine.chain_outbox == []
    printed = capsys.readouterr().out
    assert printed.count("quote_skipped") >= 4
    assert printed.count("loss_budget") >= 4
    assert "budget=1.02" in printed
    assert "min_size=5" in printed
    _quote(engine, ("0.88", "0.90"), ("0.08", "0.10"), 171_000)
    sells = [
        order
        for order in engine.state.orders.values()
        if order.status in {"queued", "live"}
    ]
    assert {order.outcome for order in sells} == {"up"}
    for order in sells:
        assert order.size >= Decimal("5")
        assert order.price > Decimal("0.88")
    assert market.up == Decimal("5") and market.down == Decimal("5")
    assert worst_case_equity(engine.state) >= engine.state.loss_floor


def test_stale_feed_is_logged_and_does_not_merge(tmp_path, capsys):
    engine = _run14(tmp_path / "stale")
    run(
        engine,
        [
            market_event(end_ts=300_000),
            {
                "type": "split",
                "event_id": "split",
                "ts": 1_000,
                "market_id": "m1",
                "amount": "5",
            },
            book_event("m1", "up", "0.88", "0.90", ts=1_000),
            book_event("m1", "down", "0.08", "0.10", ts=1_000),
            {"type": "heartbeat", "event_id": "hb-fresh", "ts": 1_000},
        ],
    )
    market = engine.state.markets["m1"]
    assert any(order.outcome == "up" for order in engine.state.orders.values())
    # Live freshness is receipt time. A clock jump with no new book is stale
    # even when stale_after_ms is huge. That skip must not merge the pair.
    run(engine, [{"type": "heartbeat", "event_id": "hb-stale", "ts": 20_000}])
    assert market.up == Decimal("5") and market.down == Decimal("5")
    assert engine.chain_outbox == []
    printed = capsys.readouterr().out
    assert "quote_skipped" in printed and "stale_feed" in printed
    assert "up_age_ms=" in printed


def test_expired_leftover_does_not_block_the_next_session(tmp_path, capsys):
    """A held Up after session 1 expires releases the open-market slot.

    The next session still quotes a price the remaining budget allows, and
    a mid book is skipped with a reason instead of being merged.
    """
    engine = _run14(tmp_path / "next")
    run(
        engine,
        [
            market_event("m1", end_ts=300_000),
            {
                "type": "split",
                "event_id": "s1",
                "ts": 1_000,
                "market_id": "m1",
                "amount": "5",
            },
        ],
    )
    first = engine.state.markets["m1"]
    first.sold_down_shares = Decimal("5")
    first.sold_down_proceeds = Decimal("4.62")
    first.down = Decimal("0")
    first.net_cash_flow += Decimal("4.62")
    engine.state.cash += Decimal("4.62")
    run(engine, [{"type": "heartbeat", "event_id": "expire", "ts": 300_000}])
    assert first.status == "expired_unresolved"
    assert first.up == Decimal("5") and first.down == Decimal("0")
    opened = run(
        engine,
        [market_event("m2", end_ts=600_000, event_id="open-m2", ts=310_000)],
    )
    assert "m2" in engine.state.markets
    assert not any(row.get("reason") == "max_open_markets" for row in opened)
    run(
        engine,
        [
            {
                "type": "split",
                "event_id": "s2",
                "ts": 310_000,
                "market_id": "m2",
                "amount": "5",
            },
            book_event("m2", "up", "0.45", "0.55", ts=320_000),
            book_event("m2", "down", "0.45", "0.55", ts=320_000),
            {"type": "heartbeat", "event_id": "mid", "ts": 320_000},
        ],
    )
    second = engine.state.markets["m2"]
    assert second.up == Decimal("5") and second.down == Decimal("5")
    assert not any(
        order.market_id == "m2" and order.status in {"queued", "live"}
        for order in engine.state.orders.values()
    )
    run(
        engine,
        [
            book_event("m2", "up", "0.88", "0.90", ts=321_000, digest="up-hi"),
            book_event("m2", "down", "0.08", "0.10", ts=321_000, digest="down-hi"),
            {"type": "heartbeat", "event_id": "hi", "ts": 321_000},
        ],
    )
    sells = [
        order
        for order in engine.state.orders.values()
        if order.market_id == "m2" and order.status in {"queued", "live"}
    ]
    assert {order.outcome for order in sells} == {"up"}
    printed = capsys.readouterr().out
    assert "quote_skipped" in printed and "loss_budget" in printed
    assert "synthetic-m2" in printed
