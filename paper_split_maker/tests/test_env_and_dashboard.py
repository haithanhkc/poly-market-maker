"""BOT_* precedence and the local dashboard. No network and no real keys."""

from __future__ import annotations

import http.client
import json
import secrets
import shutil
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.dashboard import (
    DashboardError,
    Supervisor,
    make_handler,
    request_allowed,
)
from paper_split_maker.engine import Engine
from paper_split_maker.envfile import (
    apply_bot_env,
    apply_wallet_balance,
    prepare_run,
    write_bot_values,
)
from paper_split_maker.live_runner import run_confirmed_live
from paper_split_maker.wallet_address import signer_address
from tests.test_paper_sessions import _play

ROOT = Path(__file__).resolve().parents[1]
SECRET = "supersecret-do-not-print"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "bot"
    root.mkdir()
    shutil.copy(ROOT / "config.paper.json", root / "config.paper.json")
    shutil.copy(ROOT / "config.live.json", root / "config.live.json")
    return root


def test_env_overrides_json_and_derives_the_floor(tmp_path):
    root = _root(tmp_path)
    (root / ".env").write_text(
        "\n".join(
            [
                "BOT_CAPITAL=100",
                "BOT_SPLIT_SIZE=20",
                "BOT_CLIP_SIZE=5",
                "BOT_RUNGS=2",
                "BOT_MAX_LOSS_FRACTION=0.20",
                "BOT_SESSION_STOP=4",
                "BOT_MAX_MARKETS=3",
                "BOT_MODE=paper",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    settings = prepare_run(root / "config.paper.json", {}, max_markets=1)
    assert settings.config.initial_capital == Decimal("100")
    assert settings.config.split_clip == Decimal("20")
    assert settings.config.clip_size == Decimal("5")
    assert settings.config.ladder_levels == 2
    assert settings.config.session_max_loss == Decimal("4")
    assert settings.config.live is False
    assert settings.max_markets == 3
    assert settings.config.loss_floor() == Decimal("80")
    assert settings.config.per_market_budget == Decimal("25")


def test_process_bot_value_overrides_the_file(tmp_path):
    root = _root(tmp_path)
    (root / ".env").write_text("BOT_CAPITAL=80\n", encoding="utf-8")
    settings = prepare_run(
        root / "config.paper.json",
        {"BOT_CAPITAL": "70"},
        max_markets=1,
    )
    assert settings.config.initial_capital == Decimal("70")
    fraction = Decimal("182.3864") / Decimal("2250")
    assert settings.config.max_loss_fraction == fraction
    from paper_split_maker.config import q_loss_budget

    assert settings.config.loss_floor() == Decimal("70") - q_loss_budget(
        Decimal("70"), fraction
    )


def test_split_derives_clip_rungs_and_stops():
    from paper_split_maker.envfile import derive_from_split

    small = derive_from_split(Decimal("25"))
    large = derive_from_split(Decimal("50"))
    # 9.32% of 25 and of 50 is under 5 shares, so both sides stop at one minimum clip.
    assert small.sell_shares == Decimal("5")
    assert large.sell_shares == Decimal("5")
    assert small.clip == Decimal("5")
    assert large.clip == Decimal("5")
    assert small.rungs == 1
    assert large.rungs == 1
    assert small.sell_fraction == Decimal("5") / Decimal("25")
    assert large.sell_fraction == Decimal("5") / Decimal("50")
    assert small.capital == Decimal("75")
    assert large.capital == Decimal("150")
    assert small.session_stop == Decimal("25") * Decimal("8.828") / Decimal("750")
    assert large.session_stop == Decimal("50") * Decimal("8.828") / Decimal("750")
    assert small.daily_fraction == Decimal("182.3864") / Decimal("2250")
    assert small.late_window_seconds == 120
    assert small.winner_min_price == Decimal("0.90")
    # Their own 750 split keeps the 10-share clip and three rungs inside the 9.32% budget.
    theirs = derive_from_split(Decimal("750"))
    assert theirs.clip == Decimal("10")
    assert theirs.rungs == 3
    assert theirs.sell_shares == Decimal("65")
    config = load_config(ROOT / "config.paper.json")
    applied = apply_bot_env(config, {"BOT_SPLIT_SIZE": "25"}, max_markets=1)
    assert applied.config.initial_capital == small.capital
    assert applied.config.clip_size == small.clip
    assert applied.config.ladder_levels == small.rungs
    assert applied.config.max_unpaired_fraction == small.sell_fraction
    assert applied.config.session_max_loss == small.session_stop
    assert applied.config.max_loss_fraction == small.daily_fraction
    assert applied.config.late_window_seconds == 120
    assert applied.config.split_lead_seconds == 300
    assert applied.config.max_open_markets == 3
    assert applied.config.session_sell_fraction == Decimal("0.35")
    assert applied.config.queue_priority is True
    assert applied.config.capital_from_env is False
    assert applied.config.per_market_budget == Decimal("25")


def test_wallet_balance_sets_capital_before_the_ledger(tmp_path):
    config = load_config(ROOT / "config.paper.json")
    locked = apply_bot_env(
        config,
        {"BOT_SPLIT_SIZE": "25", "BOT_CAPITAL": "59"},
        max_markets=1,
    )
    apply_wallet_balance(locked.config, Decimal("200"))
    assert locked.config.initial_capital == Decimal("59")
    tuned = apply_bot_env(config, {"BOT_SPLIT_SIZE": "25"}, max_markets=1)
    with pytest.raises(ConfigError, match="below the split"):
        apply_wallet_balance(tuned.config, Decimal("10"))
    funded = apply_bot_env(config, {"BOT_SPLIT_SIZE": "25"}, max_markets=1)
    switch = tmp_path / "stop.switch"
    switch.write_text("stop\n", encoding="utf-8")
    funded.config.kill_switch_file = str(switch)
    out = tmp_path / "live-out"
    summary = run_confirmed_live(
        funded.config,
        SimpleNamespace(
            funder="0x" + "11" * 20,
            signature_type=0,
            redact=lambda text: text,
        ),
        out,
        duration_seconds=5,
        max_markets=1,
        client=SimpleNamespace(cancel_all=lambda: None),
        reader=SimpleNamespace(),
        read_chain_balance=True,
        collateral_balance=lambda funder: Decimal("80"),
    )
    assert Decimal(summary["initial_capital"]) == Decimal("80")
    from paper_split_maker.config import q_loss_budget

    fraction = Decimal("182.3864") / Decimal("2250")
    assert Decimal(summary["loss_floor"]) == Decimal("80") - q_loss_budget(
        Decimal("80"), fraction
    )
    assert not any(str(item).startswith("http") for item in summary.get("notes") or [])
    short = apply_bot_env(config, {"BOT_SPLIT_SIZE": "25"}, max_markets=1)
    with pytest.raises(ConfigError, match="below the split"):
        run_confirmed_live(
            short.config,
            SimpleNamespace(funder="0x" + "22" * 20, redact=lambda text: text),
            tmp_path / "refused",
            duration_seconds=5,
            max_markets=1,
            read_chain_balance=True,
            collateral_balance=lambda funder: Decimal("10"),
        )
    assert not (tmp_path / "refused" / "state.json").exists()


def test_split_must_be_positive_within_capital_and_the_minimum(tmp_path):
    config = load_config(ROOT / "config.paper.json")
    with pytest.raises(ConfigError, match="split must be > 0"):
        apply_bot_env(config, {"BOT_SPLIT_SIZE": "0"}, max_markets=1)
    with pytest.raises(ConfigError, match="split must be <= capital"):
        apply_bot_env(
            config,
            {"BOT_CAPITAL": "10", "BOT_SPLIT_SIZE": "25"},
            max_markets=1,
        )
    with pytest.raises(ConfigError, match="at least 5"):
        apply_bot_env(config, {"BOT_SPLIT_SIZE": "4"}, max_markets=1)
    with pytest.raises(ConfigError, match="at least 5"):
        apply_bot_env(config, {"BOT_CLIP_SIZE": "1"}, max_markets=1)
    with pytest.raises(ConfigError, match="paper or live"):
        apply_bot_env(config, {"BOT_MODE": "both"}, max_markets=1)


def test_dashboard_write_leaves_secrets_untouched(tmp_path):
    root = _root(tmp_path)
    (root / ".env").write_text(
        f"POLY_PRIVATE_KEY={SECRET}\nBOT_CAPITAL=59\n# keep me\n",
        encoding="utf-8",
    )
    write_bot_values(
        root / ".env",
        {
            "BOT_CAPITAL": "80",
            "BOT_SPLIT_SIZE": "20",
            "POLY_PRIVATE_KEY": "replaced",
            "NOT_A_SETTING": "nope",
        },
    )
    text = (root / ".env").read_text(encoding="utf-8")
    assert f"POLY_PRIVATE_KEY={SECRET}" in text
    assert "replaced" not in text
    assert "NOT_A_SETTING" not in text
    assert "BOT_CAPITAL=80" in text
    assert "BOT_SPLIT_SIZE=20" in text
    assert "# keep me" in text
    board = Supervisor(root)
    board.save_form(
        {
            "BOT_CAPITAL": "90",
            "BOT_SPLIT_SIZE": "15",
            "BOT_CLIP_SIZE": "5",
            "BOT_RUNGS": "3",
            "BOT_MAX_LOSS_FRACTION": "0.20",
            "BOT_SESSION_STOP": "6",
            "BOT_MAX_MARKETS": "2",
            "BOT_MODE": "paper",
            "POLY_PRIVATE_KEY": "from-the-form",
        }
    )
    saved = (root / ".env").read_text(encoding="utf-8")
    assert SECRET in saved
    assert "from-the-form" not in saved
    assert "BOT_CAPITAL=80" in saved
    assert "BOT_CAPITAL=90" not in saved
    assert "BOT_SPLIT_SIZE=15" in saved
    assert "BOT_MAX_MARKETS=2" in saved
    page = board.page()
    assert SECRET not in page
    assert "from-the-form" not in page
    assert 'name="BOT_CAPITAL"' not in page
    assert 'name="BOT_CLIP_SIZE"' not in page
    assert 'name="BOT_RUNGS"' not in page
    assert 'name="BOT_SESSION_STOP"' not in page
    assert 'name="BOT_MAX_LOSS_FRACTION"' not in page
    assert 'name="BOT_SPLIT_SIZE"' in page
    assert 'name="BOT_MAX_MARKETS"' in page


def test_start_is_single_and_stop_writes_the_kill_switch(tmp_path):
    root = _root(tmp_path)
    (root / ".env").write_text(f"POLY_PRIVATE_KEY={SECRET}\nBOT_MODE=paper\n", encoding="utf-8")
    board = Supervisor(root)
    switch = root / "kill.switch"
    switch.write_text("stale\n", encoding="utf-8")

    class Alive:
        def __init__(self):
            self.code = None
            self.stopped = False

        def poll(self):
            return self.code

        def terminate(self):
            self.stopped = True
            self.code = 0

        def kill(self):
            self.code = -9

        def wait(self, timeout=None):
            return self.code

    holder = {}

    def runner(argv, env, out):
        assert SECRET not in " ".join(argv)
        assert "--live" not in argv
        holder["out"] = out
        holder["env"] = env
        return Alive()

    plan = board.start("paper", runner=runner)
    assert not switch.exists()
    assert plan["mode"] == "paper"
    assert Path(plan["out"]) == holder["out"]
    with pytest.raises(DashboardError, match="already running"):
        board.start("paper", runner=runner)
    board.stop()
    assert switch.read_text(encoding="utf-8") == "stop\n"
    assert holder["env"]["POLY_PRIVATE_KEY"] == SECRET
    body = json.dumps(board.status())
    assert SECRET not in body
    with pytest.raises(DashboardError, match="confirmation"):
        board.start("live", confirmed=False, runner=runner)


def _dummy_key() -> str:
    """64 hex chars generated here. Not a key anyone should fund."""
    for _ in range(8):
        key = secrets.token_hex(32)
        if signer_address(key):
            return key
    raise AssertionError("could not draw a usable dummy key")


def test_wallet_blank_save_keeps_the_key_and_clear_removes_it(tmp_path):
    root = _root(tmp_path)
    key = _dummy_key()
    funder = "0x" + "ab" * 20
    (root / ".env").write_text(
        "\n".join(
            [
                f"POLY_PRIVATE_KEY={key}",
                f"POLY_FUNDER={funder}",
                "POLY_SIGNATURE_TYPE=2",
                "BOT_SPLIT_SIZE=25",
                "# keep me",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    board = Supervisor(root)
    board.save_wallet(
        {
            "POLY_PRIVATE_KEY": "",
            "POLY_API_SECRET": "",
            "POLY_FUNDER": "",
            "BOT_SPLIT_SIZE": "50",
        }
    )
    kept = (root / ".env").read_text(encoding="utf-8")
    assert f"POLY_PRIVATE_KEY={key}" in kept
    assert f"POLY_FUNDER={funder}" in kept
    assert "BOT_SPLIT_SIZE=50" not in kept
    assert "BOT_SPLIT_SIZE=25" in kept
    page = board.page()
    body = json.dumps(board.status())
    assert key not in page
    assert key not in body
    assert signer_address(key) in page
    assert funder in page
    assert "đã lưu" in page
    board.save_wallet({"POLY_FUNDER": "0x" + "cd" * 20, "POLY_SIGNATURE_TYPE": "1"})
    changed = (root / ".env").read_text(encoding="utf-8")
    assert f"POLY_PRIVATE_KEY={key}" in changed
    assert "POLY_FUNDER=0x" + "cd" * 20 in changed
    assert "POLY_SIGNATURE_TYPE=1" in changed
    with pytest.raises(ConfigError, match="must be an address"):
        board.save_wallet({"POLY_FUNDER": key})
    assert key not in (root / ".env").read_text(encoding="utf-8").split("POLY_FUNDER=", 1)[-1]
    board.clear_wallet()
    cleared = (root / ".env").read_text(encoding="utf-8")
    assert key not in cleared
    assert "POLY_PRIVATE_KEY=" in cleared
    assert "BOT_SPLIT_SIZE=25" in cleared
    assert "# keep me" in cleared
    assert "đã lưu" not in board.page() or "chưa lưu" in board.page()
    assert key not in board.page()
    assert key not in json.dumps(board.status())


def test_wallet_change_is_refused_while_a_bot_is_running(tmp_path):
    root = _root(tmp_path)
    key = _dummy_key()
    (root / ".env").write_text(
        f"POLY_PRIVATE_KEY={key}\nBOT_SPLIT_SIZE=25\nBOT_MAX_MARKETS=1\n",
        encoding="utf-8",
    )
    board = Supervisor(root)

    class Alive:
        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return None

    board.start("paper", runner=lambda argv, env, out: Alive())
    before = (root / ".env").read_text(encoding="utf-8")
    with pytest.raises(DashboardError, match="while a bot is running"):
        board.save_wallet({"POLY_PRIVATE_KEY": _dummy_key()})
    with pytest.raises(DashboardError, match="while a bot is running"):
        board.clear_wallet()
    assert (root / ".env").read_text(encoding="utf-8") == before
    board.save_form({"BOT_SPLIT_SIZE": "50", "BOT_MAX_MARKETS": "2"})
    after = (root / ".env").read_text(encoding="utf-8")
    assert f"POLY_PRIVATE_KEY={key}" in after
    assert "BOT_SPLIT_SIZE=50" in after
    assert key not in board.page()
    assert key not in json.dumps(board.status())


def test_foreign_host_or_origin_is_rejected(tmp_path):
    root = _root(tmp_path)
    key = _dummy_key()
    (root / ".env").write_text(
        f"POLY_PRIVATE_KEY={key}\nPOLY_FUNDER={'0x' + '11' * 20}\nBOT_SPLIT_SIZE=25\n",
        encoding="utf-8",
    )
    board = Supervisor(root)
    log_dir = tmp_path / "shown"
    log_dir.mkdir()
    (log_dir / "console.log").write_text(f"leaked {key}\n", encoding="utf-8")
    board.out_dir = log_dir
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(board))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert request_allowed("127.0.0.1", f"127.0.0.1:{port}", None, "GET")
        assert not request_allowed("127.0.0.1", f"127.0.0.1:{port}", None, "POST")
        assert not request_allowed("127.0.0.1", "evil.example", None, "GET")
        assert not request_allowed(
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "http://evil.example",
            "POST",
        )
        status, body = _exchange(
            port,
            "POST",
            "/wallet",
            f"POLY_PRIVATE_KEY={_dummy_key()}",
            {"Origin": "http://evil.example", "Host": f"127.0.0.1:{port}"},
        )
        assert status == 403
        assert key not in body
        assert (root / ".env").read_text(encoding="utf-8").count(key) == 1
        status, body = _exchange(
            port,
            "POST",
            "/wallet",
            f"POLY_FUNDER={key}",
            {"Origin": f"http://127.0.0.1:{port}", "Host": f"127.0.0.1:{port}"},
        )
        assert status == 400
        assert key not in body
        assert f"POLY_PRIVATE_KEY={key}" in (root / ".env").read_text(encoding="utf-8")
        status, body = _exchange(
            port,
            "POST",
            "/settings",
            "BOT_SPLIT_SIZE=50&BOT_MAX_MARKETS=2",
            {"Origin": f"http://127.0.0.1:{port}"},
        )
        assert status == 303
        saved = (root / ".env").read_text(encoding="utf-8")
        assert "BOT_SPLIT_SIZE=50" in saved
        assert f"POLY_PRIVATE_KEY={key}" in saved
        assert "BOT_CAPITAL" not in saved
        status, body = _exchange(port, "GET", "/status", "", {"Host": "evil.example"})
        assert status == 403
        assert key not in body
        status, body = _exchange(port, "GET", "/status", "", {})
        assert status == 200
        assert key not in body
        assert "[redacted]" in body
        assert "đã lưu" in body
        status, body = _exchange(port, "GET", "/", "", {})
        assert status == 200
        assert key not in body
        assert signer_address(key) in body
        status, body = _exchange(
            port,
            "GET",
            "/wallet/clear",
            "",
            {},
        )
        assert status == 200
        assert f"POLY_PRIVATE_KEY={key}" in (root / ".env").read_text(encoding="utf-8")
        status, body = _exchange(
            port,
            "POST",
            "/wallet/clear",
            "",
            {"Origin": f"http://localhost:{port}", "Host": f"localhost:{port}"},
        )
        assert status == 303
        assert key not in (root / ".env").read_text(encoding="utf-8")
        assert key not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_paper_start_runs_three_sessions_offline(tmp_path):
    root = _root(tmp_path)
    key = _dummy_key()
    (root / ".env").write_text(
        "\n".join(
            [
                f"POLY_PRIVATE_KEY={key}",
                "POLY_FUNDER=0x" + "33" * 20,
                "BOT_SPLIT_SIZE=25",
                "BOT_MAX_MARKETS=3",
                "BOT_MODE=paper",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    board = Supervisor(root)

    class Done:
        def poll(self):
            return 0

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    def runner(argv, env, out):
        assert key not in " ".join(argv)
        assert "--live" not in argv
        settings = prepare_run(root / "config.paper.json", env, max_markets=3)
        assert settings.mode == "paper"
        assert settings.config.live is False
        assert settings.config.split_clip == Decimal("25")
        assert settings.config.clip_size == Decimal("5")
        assert settings.config.ladder_levels == 1
        assert settings.config.max_unpaired_fraction == Decimal("0.2")
        assert settings.config.initial_capital == Decimal("75")
        engine = Engine.create(settings.config, out / "state.json", out / "logs")
        engine.mode = "paper"
        _play(engine, 2)
        (out / "console.log").write_text(
            f"paper finished\n{key}\n", encoding="utf-8"
        )
        return Done()

    board.start("paper", runner=runner)
    status = board.status()
    rendered = board.page()
    launch = (board.out_dir / "launch.json").read_text(encoding="utf-8")
    assert status["state"] == "stopped"
    assert status["running"] is False
    assert status["mode"] == "paper"
    # Two 5-share fills at 0.50. A third crosses the daily floor, so the
    # harness stops at two. Cash is the 75 bankroll minus 5 of locked loss.
    assert status["fills"] == 2
    assert status["loss_floor"] == "68.920454"
    assert Decimal(status["cash"]) == Decimal("70")
    assert Decimal(status["realized_pnl"]) == Decimal("-5")
    assert "paper finished" in status["log"]
    assert "[redacted]" in status["log"]
    for public in (status["log"], rendered, launch, json.dumps(status)):
        assert key not in public
    state = json.loads((board.out_dir / "state.json").read_text(encoding="utf-8"))
    assert len(state["markets"]) == 2
    sold = sum(Decimal(market["sold_up_shares"]) for market in state["markets"].values())
    assert sold == Decimal("10")


def _exchange(port: int, method: str, path: str, body: str, headers: dict) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = body.encode("utf-8")
    sent = {"Content-Type": "application/x-www-form-urlencoded"}
    sent.update(headers)
    conn.request(method, path, body=payload, headers=sent)
    response = conn.getresponse()
    text = response.read().decode("utf-8", "replace")
    status = response.status
    conn.close()
    return status, text
