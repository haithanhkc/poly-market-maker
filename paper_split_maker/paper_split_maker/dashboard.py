"""Local control page. Binds to 127.0.0.1 and never returns secret values."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from paper_split_maker.config import ConfigError
from paper_split_maker.envfile import (
    BOT_KEYS,
    apply_bot_env,
    prepare_run,
    read_env,
    write_bot_values,
)

HOST = "127.0.0.1"
PORT = 8765


class DashboardError(RuntimeError):
    pass


class Supervisor:
    """One bot process. A second start is refused until this one exits."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.env_path = self.root / ".env"
        self.proc = None
        self.out_dir: Path | None = None
        self.mode = ""
        self.launched_max_markets = 1

    def form_values(self) -> dict[str, str]:
        """Effective non-secret settings: JSON, then .env. Safe to render."""
        settings = self._settings("paper")
        env = read_env(self.env_path)
        mode = env.get("BOT_MODE") or settings.mode
        if mode not in {"paper", "live"}:
            mode = settings.mode
        cfg = settings.config
        return {
            "BOT_CAPITAL": _num(cfg.initial_capital),
            "BOT_SPLIT_SIZE": _num(cfg.split_clip),
            "BOT_CLIP_SIZE": _num(cfg.clip_size),
            "BOT_RUNGS": str(cfg.ladder_levels),
            "BOT_MAX_LOSS_FRACTION": _num(cfg.max_loss_fraction),
            "BOT_SESSION_STOP": _num(cfg.session_max_loss),
            "BOT_MAX_MARKETS": str(settings.max_markets),
            "BOT_MODE": mode,
            "loss_floor": _num(cfg.loss_floor()),
            "loss_budget": _num(cfg.loss_budget()),
        }

    def save_form(self, posted: dict) -> None:
        """Validate, then write BOT_* only. Secret lines are not replaced."""
        updates = {key: str(posted.get(key) or "").strip() for key in BOT_KEYS}
        base = prepare_run(
            self.root / "config.paper.json", {}, max_markets=1
        )
        # Validate the posted values against the JSON before touching .env.
        apply_bot_env(
            base.config,
            updates,
            max_markets=base.max_markets,
        )
        write_bot_values(self.env_path, updates)

    def start(self, mode: str, *, confirmed: bool = False, runner=None) -> dict:
        if mode not in {"paper", "live"}:
            raise DashboardError("mode must be paper or live")
        if mode == "live" and not confirmed:
            raise DashboardError("live start requires the confirmation checkbox")
        if self.running():
            raise DashboardError("a bot is already running")
        self._clear_kill_switch()
        settings = self._settings(mode)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = self.root / "runs" / f"{mode}-{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        config_name = "config.live.json" if mode == "live" else "config.paper.json"
        argv = [
            sys.executable,
            "-m",
            "paper_split_maker",
            "--config",
            config_name,
            "--out",
            str(out),
            "--max-markets",
            str(settings.max_markets),
        ]
        if mode == "live":
            argv.extend(["--live", "--yes-i-understand-live-risk"])
        child_env = os.environ.copy()
        child_env.update(read_env(self.env_path))
        child_env["BOT_MODE"] = mode
        plan = {
            "argv": argv,
            "cwd": str(self.root),
            "out": str(out),
            "mode": mode,
            "max_markets": settings.max_markets,
            "loss_floor": _num(settings.config.loss_floor()),
            "capital": _num(settings.config.initial_capital),
            "split": _num(settings.config.split_clip),
        }
        (out / "launch.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        launch = runner or self._popen
        self.proc = launch(argv, child_env, out)
        self.out_dir = out
        self.mode = mode
        self.launched_max_markets = settings.max_markets
        return plan

    def stop(self) -> None:
        """Ask the bot to stop, then terminate the process if it is still up."""
        switch = self.root / "kill.switch"
        switch.write_text("stop\n", encoding="utf-8")
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            kill = getattr(proc, "kill", None)
            if kill is not None:
                kill()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        """Public status. Secret env values are not copied onto this dict."""
        state = self._read_state()
        session = ""
        cash = ""
        pnl = ""
        floor = ""
        if state:
            cash = str(state.get("cash") or "")
            pnl = str(state.get("realized_pnl") or "")
            floor = str(state.get("loss_floor") or "")
            session = _current_session(state)
        elif self.out_dir is not None:
            launch = self.out_dir / "launch.json"
            if launch.exists():
                data = json.loads(launch.read_text(encoding="utf-8"))
                floor = str(data.get("loss_floor") or "")
        return {
            "running": self.running(),
            "state": "running" if self.running() else "stopped",
            "mode": self.mode or (read_env(self.env_path).get("BOT_MODE") or "paper"),
            "session": session,
            "cash": cash,
            "realized_pnl": pnl,
            "loss_floor": floor,
            "fills": self._fill_count(),
            "out": "" if self.out_dir is None else str(self.out_dir),
            "log": self.log_tail(),
        }

    def log_tail(self, limit: int = 40) -> str:
        if self.out_dir is None:
            return ""
        for name in ("console.log", "logs/events.jsonl"):
            path = self.out_dir / name
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                if lines:
                    return "\n".join(lines[-limit:])
        return ""

    def page(self) -> str:
        values = self.form_values()
        status = self.status()
        return _PAGE.format(
            capital=_esc(values["BOT_CAPITAL"]),
            split=_esc(values["BOT_SPLIT_SIZE"]),
            clip=_esc(values["BOT_CLIP_SIZE"]),
            rungs=_esc(values["BOT_RUNGS"]),
            fraction=_esc(values["BOT_MAX_LOSS_FRACTION"]),
            session=_esc(values["BOT_SESSION_STOP"]),
            markets=_esc(values["BOT_MAX_MARKETS"]),
            mode_paper="selected" if values["BOT_MODE"] == "paper" else "",
            mode_live="selected" if values["BOT_MODE"] == "live" else "",
            floor=_esc(values["loss_floor"]),
            budget=_esc(values["loss_budget"]),
            run_state=_esc(status["state"]),
            run_mode=_esc(status["mode"]),
            run_session=_esc(status["session"] or "—"),
            cash=_esc(status["cash"] or "—"),
            pnl=_esc(status["realized_pnl"] or "—"),
            run_floor=_esc(status["loss_floor"] or values["loss_floor"]),
            fills=_esc(str(status["fills"])),
            log=_esc(status["log"]),
        )

    def _settings(self, mode: str):
        env = read_env(self.env_path)
        env["BOT_MODE"] = mode
        return prepare_run(
            self.root / ("config.live.json" if mode == "live" else "config.paper.json"),
            env,
            max_markets=1,
        )

    def _clear_kill_switch(self) -> None:
        switch = self.root / "kill.switch"
        if switch.exists():
            switch.unlink()

    def _popen(self, argv: list[str], env: dict, out: Path):
        log_path = out / "console.log"
        handle = log_path.open("a", encoding="utf-8")
        return subprocess.Popen(
            argv,
            cwd=str(self.root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def _read_state(self) -> dict | None:
        if self.out_dir is None:
            return None
        path = self.out_dir / "state.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _fill_count(self) -> int:
        if self.out_dir is None:
            return 0
        path = self.out_dir / "logs" / "events.jsonl"
        if not path.exists():
            return 0
        return sum(
            1
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if '"kind": "fill"' in line
        )


def make_handler(supervisor: Supervisor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if not _local(self.client_address):
                self._send(403, "text/plain", b"local only")
                return
            if self.path.startswith("/status"):
                body = json.dumps(supervisor.status()).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
                return
            if self.path.startswith("/log"):
                self._send(
                    200,
                    "text/plain; charset=utf-8",
                    supervisor.log_tail().encode("utf-8"),
                )
                return
            self._send(200, "text/html; charset=utf-8", supervisor.page().encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            if not _local(self.client_address):
                self._send(403, "text/plain", b"local only")
                return
            posted = _form(self)
            try:
                if self.path.startswith("/settings"):
                    supervisor.save_form(posted)
                elif self.path.startswith("/start"):
                    mode = str(posted.get("mode") or "paper")
                    confirmed = str(posted.get("confirm") or "") == "yes"
                    supervisor.start(mode, confirmed=confirmed)
                elif self.path.startswith("/stop"):
                    supervisor.stop()
                else:
                    self._send(404, "text/plain", b"not found")
                    return
            except (ConfigError, DashboardError) as exc:
                self._send(400, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(root: Path, host: str = HOST, port: int = PORT) -> None:
    if host != HOST:
        raise DashboardError("the dashboard binds to 127.0.0.1 only")
    supervisor = Supervisor(root)
    server = ThreadingHTTPServer((host, port), make_handler(supervisor))
    print(f"dashboard http://{host}:{port}/", flush=True)
    server.serve_forever()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    serve(root)
    return 0


def _form(handler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _local(address) -> bool:
    host = address[0] if address else ""
    return host in {"127.0.0.1", "::1"}


def _current_session(state: dict) -> str:
    markets = state.get("markets") or {}
    open_slug = ""
    last = ""
    for market in markets.values():
        if not isinstance(market, dict):
            continue
        slug = str(market.get("slug") or market.get("market_id") or "")
        last = slug or last
        if market.get("status") == "open":
            open_slug = slug
    return open_slug or last


def _num(value) -> str:
    return format(value, "f")


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Split maker</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; max-width: 46rem; }}
label {{ display: block; margin: 0.6rem 0; }}
input, select {{ margin-left: 0.4rem; }}
button {{ margin-right: 0.6rem; }}
pre {{ background: #f4f4f4; padding: 0.8rem; max-height: 18rem; overflow: auto; }}
</style>
</head>
<body>
<h1>Split maker</h1>
<p>Sàn lỗ hiện tại: <strong>{floor}</strong> (ngân sách lỗ {budget}). Khóa bí mật không hiện trên trang này.</p>
<form method="post" action="/settings">
<label>Vốn BOT_CAPITAL <input name="BOT_CAPITAL" value="{capital}"></label>
<label>Khối split BOT_SPLIT_SIZE <input name="BOT_SPLIT_SIZE" value="{split}"></label>
<label>Khối mỗi bậc BOT_CLIP_SIZE <input name="BOT_CLIP_SIZE" value="{clip}"></label>
<label>Số bậc BOT_RUNGS <input name="BOT_RUNGS" value="{rungs}"></label>
<label>Tỷ lệ dừng lỗ BOT_MAX_LOSS_FRACTION <input name="BOT_MAX_LOSS_FRACTION" value="{fraction}"></label>
<label>Dừng một phiên BOT_SESSION_STOP <input name="BOT_SESSION_STOP" value="{session}"></label>
<label>Số phiên BOT_MAX_MARKETS <input name="BOT_MAX_MARKETS" value="{markets}"></label>
<label>Chế độ BOT_MODE
<select name="BOT_MODE">
<option value="paper" {mode_paper}>paper</option>
<option value="live" {mode_live}>live</option>
</select>
</label>
<button type="submit">Lưu vào .env</button>
</form>
<form method="post" action="/start">
<input type="hidden" name="mode" value="paper">
<button type="submit">Chạy paper</button>
</form>
<form method="post" action="/start">
<input type="hidden" name="mode" value="live">
<label><input type="checkbox" name="confirm" value="yes"> Tôi hiểu rủi ro live</label>
<button type="submit">Chạy live</button>
</form>
<form method="post" action="/stop">
<button type="submit">Dừng</button>
</form>
<h2>Trạng thái</h2>
<ul>
<li>Trạng thái: <span id="run-state">{run_state}</span></li>
<li>Chế độ: <span id="run-mode">{run_mode}</span></li>
<li>Phiên: <span id="run-session">{run_session}</span></li>
<li>Cash: <span id="cash">{cash}</span></li>
<li>PnL đã chốt: <span id="pnl">{pnl}</span></li>
<li>Sàn lỗ: <span id="run-floor">{run_floor}</span></li>
<li>Số lệnh khớp: <span id="fills">{fills}</span></li>
</ul>
<h2>Log</h2>
<pre id="log">{log}</pre>
<script>
function refresh() {{
  fetch("/status").then(function (r) {{ return r.json(); }}).then(function (s) {{
    document.getElementById("run-state").textContent = s.state;
    document.getElementById("run-mode").textContent = s.mode;
    document.getElementById("run-session").textContent = s.session || "—";
    document.getElementById("cash").textContent = s.cash || "—";
    document.getElementById("pnl").textContent = s.realized_pnl || "—";
    document.getElementById("run-floor").textContent = s.loss_floor || "—";
    document.getElementById("fills").textContent = s.fills;
    document.getElementById("log").textContent = s.log || "";
  }});
}}
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
