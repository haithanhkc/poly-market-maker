"""Paper dashboard. No network, no keys, no orders."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from paper_split_maker.app import Hub, demo_config, play_demo
from paper_split_maker.models import WORKING_ORDER_STATUSES

ROOT = Path(__file__).resolve().parents[1]


def _working(engine, market_id=None):
    return [
        order
        for order in engine.state.orders.values()
        if getattr(order, "side", "") == "buy"
        and order.status in WORKING_ORDER_STATUSES
        and (market_id is None or order.market_id == market_id)
    ]


def test_demo_snapshot_has_chart_book_fills_and_settlement(tmp_path):
    hub = play_demo(tmp_path)
    assert hub.engine.listener_error is None
    snap = hub.snapshot()
    assert snap["paper"] is True
    assert snap["mode"] == "demo"
    assert snap["bot_enabled"] is True
    blob = json.dumps(snap)
    assert "private_key" not in blob
    assert "PREDICT_" not in blob
    assert len(snap["sessions"]) == 2
    live = snap["sessions"][-1]
    settled = snap["sessions"][0]
    assert live["status"] == "open"
    assert Decimal(live["price_to_beat"]) == Decimal("62161.22")
    assert live["points"][-1]["up"] == 0.185
    assert live["points"][-1]["down"] == 0.815
    assert live["points"][-1]["btc"] == 62110.32
    assert any(mark["kind"] == "place" for mark in live["marks"])
    assert any(mark["kind"] == "fill" for mark in live["marks"] + settled["marks"])
    assert any(mark["kind"] == "exit" and mark["price"] in {0.0, 1.0} for mark in settled["marks"])
    asks = live["books"]["up"]["asks"]
    bids = live["books"]["up"]["bids"]
    assert asks[0][0] == 0.21
    assert bids[0][0] == 0.19
    assert settled["winner"] == "down"
    assert snap["stats"]["redeemed"] == 1
    assert any(row["status"] == "filled" for row in snap["blotter"])


def test_bot_off_cancels_and_on_quotes_again(tmp_path):
    hub = play_demo(tmp_path)
    engine = hub.engine
    assert _working(engine, "m2"), "demo left no resting bid to cancel"
    hub.set_enabled(False)
    assert hub.control.enabled() is False
    assert engine.quoting_enabled is False
    assert _working(engine, "m2") == []
    assert any(mark["kind"] == "cancel" for mark in hub.sessions["m2"]["marks"])
    hub.set_enabled(True)
    assert _working(engine, "m2"), "turning the bot on did not rest a new bid"
    assert json.loads((tmp_path / "bot.json").read_text(encoding="utf-8"))["enabled"] is True


def test_resume_reloads_dashboard_and_ledger(tmp_path):
    hub = play_demo(tmp_path)
    cash = hub.engine.state.cash
    assert cash != Decimal("100")
    again = play_demo(tmp_path)
    assert again.engine.state.cash == cash
    assert len(again.sessions) == 2
    assert again.sessions["m2"]["points"][-1]["up"] == 0.185


def test_http_toggle_reset_and_page(tmp_path):
    hub = play_demo(tmp_path)
    from http.server import ThreadingHTTPServer
    from paper_split_maker.app import _handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(hub))
    port = server.server_address[1]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert "Both sides" in page
        assert "Price to beat" in page
        assert "predict.fun" in page
        assert "BOT ON" in page
        snap = json.loads(urlopen(base + "/api/state", timeout=5).read().decode("utf-8"))
        assert snap["sessions"][-1]["points"][-1]["up"] == 0.185
        off = _post(base + "/api/bot", {"enabled": False})
        assert off["bot_enabled"] is False
        after = json.loads(urlopen(base + "/api/state", timeout=5).read().decode("utf-8"))
        assert after["bot_enabled"] is False
        assert after["sessions"][-1]["resting"] == []
        refused = _post(base + "/api/reset", {"confirm": "no"})
        assert refused["ok"] is False
        assert (tmp_path / "state.json").exists()
        accepted = _post(base + "/api/reset", {"confirm": "RESET"})
        assert accepted["ok"] is True
        cleared = json.loads(urlopen(base + "/api/state", timeout=5).read().decode("utf-8"))
        assert cleared["sessions"] == []
        assert not (tmp_path / "state.json").exists() or _cash_is_fresh(tmp_path)
    finally:
        server.shutdown()
        worker.join(timeout=5)


def _cash_is_fresh(tmp_path: Path) -> bool:
    if not (tmp_path / "state.json").exists():
        return True
    body = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    return Decimal(body["cash"]) == Decimal("100") and not body.get("markets")


def _post(url: str, payload: dict) -> dict:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urlopen(req, timeout=5).read().decode("utf-8"))


def test_bot_flag_survives_a_new_hub(tmp_path):
    hub = Hub(tmp_path, mode="demo", config=demo_config())
    hub.set_enabled(False)
    again = Hub(tmp_path, mode="demo", config=demo_config())
    assert again.control.enabled() is False


def test_page_file_mentions_the_chart_controls():
    text = (ROOT / "paper_split_maker" / "app_page.html").read_text(encoding="utf-8")
    for needle in ("One side", "Both sides", "Price to beat", "Order Book", "Reset ledger"):
        assert needle in text
