"""BOT_* precedence and the local dashboard. No network and no real keys."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.dashboard import DashboardError, Supervisor
from paper_split_maker.engine import Engine
from paper_split_maker.envfile import apply_bot_env, prepare_run, write_bot_values
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
    assert settings.config.loss_floor() == Decimal("56")


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
    assert "BOT_CAPITAL=90" in saved
    page = board.page()
    assert SECRET not in page
    assert "from-the-form" not in page


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


def test_dashboard_paper_start_runs_three_sessions_offline(tmp_path):
    root = _root(tmp_path)
    (root / ".env").write_text(
        "\n".join(
            [
                f"POLY_PRIVATE_KEY={SECRET}",
                "BOT_CAPITAL=59",
                "BOT_SPLIT_SIZE=25",
                "BOT_CLIP_SIZE=5",
                "BOT_RUNGS=3",
                "BOT_MAX_LOSS_FRACTION=0.20",
                "BOT_SESSION_STOP=6",
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
        assert SECRET not in " ".join(argv)
        assert "--live" not in argv
        settings = prepare_run(root / "config.paper.json", env, max_markets=3)
        assert settings.mode == "paper"
        assert settings.config.live is False
        assert settings.config.split_clip == Decimal("25")
        assert settings.config.loss_floor() == Decimal("47.20")
        engine = Engine.create(settings.config, out / "state.json", out / "logs")
        engine.mode = "paper"
        _play(engine, 3)
        (out / "console.log").write_text("paper finished\n", encoding="utf-8")
        return Done()

    board.start("paper", runner=runner)
    status = board.status()
    assert status["state"] == "stopped"
    assert status["mode"] == "paper"
    assert status["fills"] == 3
    assert status["loss_floor"] == "47.20"
    assert Decimal(status["cash"]) == Decimal("51.50")
    assert Decimal(status["realized_pnl"]) == Decimal("-7.50")
    assert SECRET not in status["log"]
    assert "paper finished" in status["log"]
    state = json.loads((board.out_dir / "state.json").read_text(encoding="utf-8"))
    assert len(state["markets"]) == 3
    sold = sum(Decimal(market["sold_up_shares"]) for market in state["markets"].values())
    assert sold == Decimal("15")
