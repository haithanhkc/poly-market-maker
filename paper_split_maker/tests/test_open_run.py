"""Open-ended paper runs. No network and no orders."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from paper_split_maker.cli import _apply_strategy, _load, main
from paper_split_maker.config import Config
from paper_split_maker.public_client import PublicReadClient
from paper_split_maker.session import run_paper
from tests.helpers import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class _Empty:
    def get_json(self, url: str):
        return {"success": True, "data": []}


def _config(**overrides):
    values = dict(DEFAULTS)
    values.update(
        strategy="small_pair",
        initial_capital=Decimal("100"),
        per_market_budget=Decimal("25"),
        round_budget=Decimal("25"),
        max_loss_fraction=Decimal("0.99"),
        max_open_markets=4,
        small_pair_binance=True,
        session_budget_fraction=Decimal("0.001"),
    )
    values.update(overrides)
    config = Config(**values)
    config.validate()
    return config


def _run(tmp_path, config, **kwargs):
    clock = {"t": 1_700_000_000.0}
    return run_paper(
        config,
        tmp_path / "paper",
        client=PublicReadClient(_Empty(), network="testnet"),
        now_s=lambda: clock["t"],
        sleep=kwargs.pop("sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds)),
        **kwargs,
    )


def test_unlimited_run_stops_when_a_session_cannot_be_funded(tmp_path):
    summary = _run(tmp_path, _config(), duration_seconds=0, max_markets=0)
    assert summary["stop_reason"] == "bust"
    assert summary["progress"]["sessions"] == 0
    assert "equity" in summary["progress"]
    text = (tmp_path / "paper" / "sessions.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("slug,")
    again = _run(tmp_path, _config(), duration_seconds=0, max_markets=0)
    assert again["resumed"] is True
    assert again["stop_reason"] == "bust"


def test_stop_file_is_a_manual_stop(tmp_path):
    out = tmp_path / "paper"
    out.mkdir()
    (out / "STOP").write_text("stop\n", encoding="utf-8")
    summary = run_paper(
        _config(session_budget_fraction=Decimal("0.25")),
        out,
        duration_seconds=30,
        max_markets=1,
        client=PublicReadClient(_Empty(), network="testnet"),
        now_s=lambda: 1_700_000_000.0,
        sleep=lambda seconds: None,
    )
    assert summary["stop_reason"] == "manual"
    assert summary["polls"] == 0


def test_keyboard_interrupt_writes_the_summary(tmp_path):
    def interrupt(_seconds):
        raise KeyboardInterrupt

    summary = _run(
        tmp_path,
        _config(session_budget_fraction=Decimal("0.25")),
        duration_seconds=30,
        max_markets=1,
        sleep=interrupt,
    )
    assert summary["stop_reason"] == "manual"
    assert (tmp_path / "paper" / "summary.json").is_file()


def test_duration_still_ends_a_short_run(tmp_path):
    summary = _run(
        tmp_path,
        _config(session_budget_fraction=Decimal("0.25")),
        duration_seconds=1,
        max_markets=0,
    )
    assert summary["stop_reason"] == "duration"
    assert summary["max_markets"] == 0


def test_cli_zero_duration_and_omitted_markets_are_unlimited(tmp_path, monkeypatch):
    raw = json.loads((ROOT / "config.paper.json").read_text(encoding="utf-8"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    seen = {}

    def fake_run(config, out, duration_seconds, max_markets, env):
        seen["duration"] = duration_seconds
        seen["markets"] = max_markets
        seen["capital"] = config.initial_capital
        return {"markets": [], "stop_reason": "bust"}

    monkeypatch.setattr("paper_split_maker.cli.run_paper", fake_run)
    code = main(
        [
            "--config",
            str(path),
            "--out",
            str(tmp_path / "out"),
            "--strategy",
            "small_pair",
            "--binance-lean",
            "--duration-seconds",
            "0",
            "--capital",
            "100",
            "--max-loss-fraction",
            "0.99",
        ]
    )
    assert code == 0
    assert seen["duration"] == 0
    assert seen["markets"] == 0
    assert seen["capital"] == Decimal("100")
    from argparse import Namespace

    loaded = _apply_strategy(
        _load(
            Namespace(
                config=path,
                max_markets=0,
                strategy="small_pair",
                series=None,
                capital="100",
                max_loss_fraction="0.99",
                binance_lean=True,
            )
        ),
        Namespace(
            config=path,
            max_markets=0,
            strategy="small_pair",
            series=None,
            capital="100",
            max_loss_fraction="0.99",
            binance_lean=True,
        ),
    )
    assert loaded.small_pair_binance is True
    assert loaded.initial_capital == Decimal("100")
