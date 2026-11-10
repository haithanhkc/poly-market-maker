"""Network failures must not end a live run. No network and no keys."""

from __future__ import annotations

from types import SimpleNamespace

from paper_split_maker.live_runner import run_confirmed_live
from tests.test_rollover import FUNDER, OPEN0, _Reader, _rollover_engine


class PolyApiException(Exception):
    def __init__(self, status_code=None, error_message="Request exception!"):
        self.status_code = status_code
        self.error_message = error_message
        super().__init__(
            f"PolyApiException[status_code={status_code}, "
            f"error_message={error_message}]"
        )


def _condition(open_s: int) -> str:
    return f"0x{open_s:040x}"


class _Clock:
    def __init__(self, start: float):
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += 5.0


class _TimedReader(_Reader):
    def __init__(self, clock: _Clock):
        super().__init__()
        self.clock = clock

    def fetch_book(self, token):
        self.now_ms = int(self.clock.time() * 1000)
        return super().fetch_book(token)


class _Client:
    def __init__(self, clock: _Clock):
        self.clock = clock
        self.open_orders: list[dict] = []
        self.heartbeats = 0
        self.fail_heartbeat_until = 0.0
        self.fail_orders_until = 0.0

    def post_heartbeat(self, heartbeat_id):
        self.heartbeats += 1
        if self.clock.time() < self.fail_heartbeat_until and self.clock.time() >= getattr(
            self, "fail_heartbeat_from", 0.0
        ):
            raise PolyApiException(status_code=None, error_message="Request exception!")
        return {"heartbeat_id": heartbeat_id or "hb-1"}

    def get_open_orders(self):
        if self.clock.time() < self.fail_orders_until:
            raise PolyApiException(status_code=None, error_message="Request exception!")
        return list(self.open_orders)

    def get_order(self, order_id):
        return {"id": order_id, "status": "LIVE", "size_matched": "0"}

    def cancel_orders(self, ids):
        gone = {str(item) for item in ids}
        self.open_orders = [
            item for item in self.open_orders if str(item.get("id")) not in gone
        ]

    def cancel_all(self):
        self.open_orders.clear()


def _accepting_poster(client: _Client):
    posted = []

    def post(_client, orders):
        made = []
        for order in orders:
            ident = f"ex-{len(posted) + 1}"
            posted.append(ident)
            client.open_orders.append({"id": ident})
            made.append({"orderID": ident, "success": True})
        return made

    return post


def _run(tmp_path, monkeypatch, *, clock, client, poster, duration, live=False):
    import paper_split_maker.chain_send as chain_send

    actions = []

    def submit(creds, action, condition_id, amount, *args, **kwargs):
        actions.append((action, str(condition_id), clock.time()))
        return (f"tx-{len(actions)}", True)

    monkeypatch.setattr(chain_send, "submit_position", submit)
    monkeypatch.setattr(chain_send, "split_tokens_present", lambda *a, **k: True)
    probe = _rollover_engine(tmp_path / "probe")
    probe.config.live = live
    probe.config.sell_ready_timeout_seconds = 30
    creds = SimpleNamespace(funder=FUNDER, signature_type=3, redact=lambda text: text)
    summary = run_confirmed_live(
        probe.config,
        creds,
        tmp_path / "out",
        duration_seconds=duration,
        max_markets=3,
        client=client,
        reader=_TimedReader(clock),
        now_s=clock.time,
        sleep=clock.sleep,
        poster=poster,
    )
    return summary, actions


def _splits(actions, open_s: int):
    condition = _condition(open_s)
    return [item for item in actions if item[0] == "split" and item[1] == condition]


def test_heartbeat_network_error_does_not_stop_the_next_session(tmp_path, monkeypatch):
    clock = _Clock(OPEN0 + 260)
    client = _Client(clock)
    # Fail only after the first session is open, and recover before the next lead.
    client.fail_heartbeat_from = OPEN0 + 320
    client.fail_heartbeat_until = OPEN0 + 390
    summary, actions = _run(
        tmp_path,
        monkeypatch,
        clock=clock,
        client=client,
        poster=_accepting_poster(client),
        duration=400,
    )
    assert summary["mode"] == "live"
    assert _splits(actions, OPEN0 + 300), "the session before the outage was not split"
    assert _splits(actions, OPEN0 + 600), "the next session was not split after the outage"
    assert any("heartbeat failed" in note for note in summary["notes"])
    assert "Request exception!" in " ".join(summary["notes"])


def test_unready_exchange_skips_the_split(tmp_path, monkeypatch):
    clock = _Clock(OPEN0 + 260)
    client = _Client(clock)
    client.fail_orders_until = OPEN0 + 300
    summary, actions = _run(
        tmp_path,
        monkeypatch,
        clock=clock,
        client=client,
        poster=_accepting_poster(client),
        duration=400,
    )
    skipped = f"btc-updown-5m-{OPEN0 + 300}"
    assert any(f"{skipped} skipped; exchange not ready to sell" in note for note in summary["notes"])
    assert _splits(actions, OPEN0 + 300) == []
    assert _splits(actions, OPEN0 + 600), "a later session should still be split once the exchange answers"


def test_repeated_post_failures_merge_before_the_close(tmp_path, monkeypatch):
    clock = _Clock(OPEN0 + 260)
    client = _Client(clock)

    def post(_client, orders):
        raise PolyApiException(status_code=None, error_message="Request exception!")

    summary, actions = _run(
        tmp_path,
        monkeypatch,
        clock=clock,
        client=client,
        poster=post,
        duration=150,
        live=True,
    )
    merges = [item for item in actions if item[0] == "merge"]
    assert merges, "the pair was never merged"
    early = [item for item in merges if OPEN0 + 330 <= item[2] <= OPEN0 + 345]
    assert early, f"expected an early merge just after the sell timeout, got {merges}"
    assert any("early merge" in note and "posts failed" in note for note in summary["notes"])
    assert _splits(actions, OPEN0 + 300)
