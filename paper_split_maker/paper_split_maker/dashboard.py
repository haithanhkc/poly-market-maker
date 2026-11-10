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

from paper_split_maker.config import ConfigError, load_config
from paper_split_maker.envfile import (
    apply_bot_env,
    clear_wallet_values,
    prepare_run,
    read_env,
    redact_secrets,
    wallet_public,
    write_bot_values,
    write_wallet_values,
)

HOST = "127.0.0.1"
PORT = 8765
_LOCAL_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


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
        """Effective non-secret settings: JSON, then .env, then the split formula."""
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
            "sell_shares": _num(cfg.max_unpaired_fraction * cfg.split_clip),
            "sell_fraction": _num(cfg.max_unpaired_fraction),
            "late_window": str(cfg.late_window_seconds),
            "winner_min": _num(cfg.winner_min_price),
        }

    def save_form(self, posted: dict) -> None:
        """Write the split and the session count. Derived fields stay out of .env."""
        split = str(posted.get("BOT_SPLIT_SIZE") or "").strip()
        markets = str(posted.get("BOT_MAX_MARKETS") or "").strip()
        if not split or not markets:
            raise ConfigError("split and session count are required")
        env = read_env(self.env_path)
        env["BOT_SPLIT_SIZE"] = split
        env["BOT_MAX_MARKETS"] = markets
        apply_bot_env(
            load_config(self.root / "config.paper.json"),
            env,
            max_markets=1,
        )
        write_bot_values(
            self.env_path,
            {"BOT_SPLIT_SIZE": split, "BOT_MAX_MARKETS": markets},
        )

    def save_wallet(self, posted: dict) -> None:
        """Write non-blank wallet fields. A blank field keeps the saved value."""
        if self.running():
            raise DashboardError("refusing to change keys while a bot is running")
        write_wallet_values(self.env_path, posted)

    def clear_wallet(self) -> None:
        """Drop every wallet secret and the funder."""
        if self.running():
            raise DashboardError("refusing to change keys while a bot is running")
        clear_wallet_values(self.env_path)

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
        env = read_env(self.env_path)
        return {
            "running": self.running(),
            "state": "running" if self.running() else "stopped",
            "mode": self.mode or (env.get("BOT_MODE") or "paper"),
            "session": session,
            "cash": cash,
            "realized_pnl": pnl,
            "loss_floor": floor,
            "fills": self._fill_count(),
            "out": "" if self.out_dir is None else str(self.out_dir),
            "log": self.log_tail(),
            "wallet": wallet_public(env),
        }

    def log_tail(self, limit: int = 40) -> str:
        if self.out_dir is None:
            return ""
        for name in ("console.log", "logs/events.jsonl"):
            path = self.out_dir / name
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                if lines:
                    text = "\n".join(lines[-limit:])
                    return redact_secrets(text, read_env(self.env_path))
        return ""

    def page(self) -> str:
        values = self.form_values()
        status = self.status()
        wallet = status["wallet"]
        hints = wallet["hints"]
        if wallet["signer"]:
            signer = wallet["signer"]
        elif hints["POLY_PRIVATE_KEY"] != "chưa lưu":
            signer = "không suy ra được"
        else:
            signer = "chưa có"
        signature = wallet["signature_type"]
        html = _PAGE.format(
            split=_esc(values["BOT_SPLIT_SIZE"]),
            markets=_esc(values["BOT_MAX_MARKETS"]),
            capital=_esc(values["BOT_CAPITAL"]),
            clip=_esc(values["BOT_CLIP_SIZE"]),
            rungs=_esc(values["BOT_RUNGS"]),
            session=_esc(values["BOT_SESSION_STOP"]),
            floor=_esc(values["loss_floor"]),
            budget=_esc(values["loss_budget"]),
            offered=_esc(values["sell_shares"]),
            sell_pct=_esc(values["sell_fraction"]),
            late=_esc(values["late_window"]),
            winner_px=_esc(values["winner_min"]),
            run_state=_esc(status["state"]),
            run_mode=_esc(status["mode"]),
            run_session=_esc(status["session"] or "—"),
            cash=_esc(status["cash"] or "—"),
            pnl=_esc(status["realized_pnl"] or "—"),
            run_floor=_esc(status["loss_floor"] or values["loss_floor"]),
            fills=_esc(str(status["fills"])),
            log=_esc(status["log"]),
            signer=_esc(signer),
            funder=_esc(wallet["funder"] or "chưa lưu"),
            hint_key=_esc(hints["POLY_PRIVATE_KEY"]),
            hint_api=_esc(hints["POLY_API_KEY"]),
            hint_secret=_esc(hints["POLY_API_SECRET"]),
            hint_pass=_esc(hints["POLY_API_PASSPHRASE"]),
            hint_relayer=_esc(hints["RELAYER_API_KEY"]),
            hint_relayer_address=_esc(hints["RELAYER_API_KEY_ADDRESS"]),
            sig0="selected" if signature == "0" else "",
            sig1="selected" if signature == "1" else "",
            sig2="selected" if signature == "2" else "",
            sig3="selected" if signature == "3" else "",
            running_note=(
                "Bot đang chạy — không đổi khóa." if self.running() else ""
            ),
        )
        return redact_secrets(html, read_env(self.env_path))

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
            _handle_get(self, supervisor)

        def do_POST(self) -> None:  # noqa: N802
            _handle_post(self, supervisor)

    return Handler


def _handle_get(handler, supervisor: Supervisor) -> None:
    if not _allowed(handler, supervisor):
        return
    if handler.path.startswith("/status"):
        body = json.dumps(supervisor.status(), ensure_ascii=False).encode("utf-8")
        _send(handler, supervisor, 200, "application/json; charset=utf-8", body)
        return
    if handler.path.startswith("/log"):
        _send(
            handler,
            supervisor,
            200,
            "text/plain; charset=utf-8",
            supervisor.log_tail().encode("utf-8"),
        )
        return
    _send(
        handler,
        supervisor,
        200,
        "text/html; charset=utf-8",
        supervisor.page().encode("utf-8"),
    )


def _handle_post(handler, supervisor: Supervisor) -> None:
    if not _allowed(handler, supervisor):
        return
    posted = _form(handler)
    try:
        handled = _dispatch(supervisor, handler.path, posted)
    except (ConfigError, DashboardError) as exc:
        _send(handler, supervisor, 400, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
        return
    if not handled:
        _send(handler, supervisor, 404, "text/plain", b"not found")
        return
    handler.send_response(303)
    handler.send_header("Location", "/")
    handler.end_headers()


def _dispatch(supervisor: Supervisor, path: str, posted: dict) -> bool:
    if path.startswith("/settings"):
        supervisor.save_form(posted)
    elif path.startswith("/wallet/clear"):
        supervisor.clear_wallet()
    elif path.startswith("/wallet"):
        supervisor.save_wallet(posted)
    elif path.startswith("/start"):
        mode = str(posted.get("mode") or "paper")
        confirmed = str(posted.get("confirm") or "") == "yes"
        supervisor.start(mode, confirmed=confirmed)
    elif path.startswith("/stop"):
        supervisor.stop()
    else:
        return False
    return True


def _allowed(handler, supervisor: Supervisor) -> bool:
    client = handler.client_address[0] if handler.client_address else ""
    if request_allowed(
        client,
        handler.headers.get("Host") or "",
        handler.headers.get("Origin"),
        handler.command,
    ):
        return True
    _send(handler, supervisor, 403, "text/plain; charset=utf-8", b"local only")
    return False


def _send(handler, supervisor: Supervisor, code: int, content_type: str, body: bytes) -> None:
    if "text" in content_type or "json" in content_type:
        text = redact_secrets(
            body.decode("utf-8", "replace"),
            read_env(supervisor.env_path),
        )
        body = text.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def request_allowed(client_host: str, host_header: str, origin, method: str) -> bool:
    """Local client, local Host, and a local Origin on any request that sends one.

    A POST with no Origin is refused. That is the cross-site check for the forms.
    """
    if client_host not in {"127.0.0.1", "::1"}:
        return False
    if not _host_is_local(host_header):
        return False
    if origin is not None and str(origin).strip():
        return _origin_is_local(str(origin))
    return method.upper() != "POST"


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


def _host_is_local(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text or "@" in text:
        return False
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return False
        name = text[: end + 1]
        port = text[end + 1 :]
        if port and not _port(port[1:] if port.startswith(":") else port):
            return False
        return name in {"[::1]"}
    if text.count(":") > 1:
        return False
    name, separator, port = text.partition(":")
    if separator and not _port(port):
        return False
    return name in _LOCAL_NAMES


def _origin_is_local(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text.startswith("http://"):
        return False
    host = text[len("http://") :].split("/", 1)[0]
    return _host_is_local(host)


def _port(value: str) -> bool:
    return bool(value) and value.isdigit() and int(value) < 65536


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
<p>Trang chỉ lắng nghe 127.0.0.1. Khóa bí mật không được gửi lại trang này.</p>
<h2>Cài đặt</h2>
<form method="post" action="/settings">
<label>Khối split (pUSD) <input name="BOT_SPLIT_SIZE" value="{split}" inputmode="decimal"></label>
<label>Số phiên <input name="BOT_MAX_MARKETS" value="{markets}" inputmode="numeric"></label>
<button type="submit">Lưu</button>
</form>
<h3>Bot sẽ dùng</h3>
<p>Tính từ khối split theo ví bubblebust (mỗi split của họ là 750). Mỗi phía bán trung bình 9.32% split, clip hay gặp là 10 cổ, và tối thiểu 5 cổ. Split 25 và 50 đều ra clip 5, một bậc, 5 cổ mỗi phía vì 9.32% nhỏ hơn 5 cổ.</p>
<ul>
<li>Vốn: {capital} pUSD (3 split, vì họ giữ 2–3 split chồng nhau)</li>
<li>Mỗi phía bán tối đa: {offered} cổ ({sell_pct} split)</li>
<li>Clip mỗi bậc: {clip} cổ</li>
<li>Số bậc cùng lúc: {rungs}</li>
<li>Cửa sổ cuối: {late}s, winner tối thiểu {winner_px}</li>
<li>Dừng một phiên: {session} pUSD</li>
<li>Dừng ngày: ngân sách {budget}, sàn lỗ {floor}</li>
</ul>
<p>Live, nếu đọc được số dư pUSD và .env không có BOT_CAPITAL, dùng số dư đó làm vốn khi số dư đủ cho khối split. Dòng BOT_CAPITAL, BOT_CLIP_SIZE, BOT_RUNGS, BOT_MAX_LOSS_FRACTION, BOT_SESSION_STOP vẫn ghi đè, nhưng không có ô trên trang này.</p>
<h2>Chạy</h2>
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
<h2>Ví</h2>
<p>{running_note}</p>
<p>Địa chỉ ký (từ khóa riêng): <strong>{signer}</strong></p>
<p>Funder đang dùng: <strong>{funder}</strong></p>
<ul>
<li>Khóa riêng: {hint_key}</li>
<li>CLOB API key: {hint_api}</li>
<li>CLOB API secret: {hint_secret}</li>
<li>CLOB passphrase: {hint_pass}</li>
<li>Relayer API key: {hint_relayer}</li>
<li>Relayer API key address: {hint_relayer_address}</li>
</ul>
<form method="post" action="/wallet">
<label>Khóa riêng POLY_PRIVATE_KEY <input type="password" name="POLY_PRIVATE_KEY" value="" autocomplete="off" spellcheck="false"></label>
<label>Funder POLY_FUNDER <input type="text" name="POLY_FUNDER" value="" autocomplete="off" placeholder="để trống nếu giữ nguyên"></label>
<label>Kiểu chữ ký POLY_SIGNATURE_TYPE
<select name="POLY_SIGNATURE_TYPE">
<option value="0" {sig0}>0 EOA</option>
<option value="1" {sig1}>1 POLY_PROXY</option>
<option value="2" {sig2}>2 GNOSIS_SAFE</option>
<option value="3" {sig3}>3 POLY_1271</option>
</select>
</label>
<label>CLOB API key <input type="password" name="POLY_API_KEY" value="" autocomplete="off"></label>
<label>CLOB API secret <input type="password" name="POLY_API_SECRET" value="" autocomplete="off"></label>
<label>CLOB passphrase <input type="password" name="POLY_API_PASSPHRASE" value="" autocomplete="off"></label>
<label>Relayer API key <input type="password" name="RELAYER_API_KEY" value="" autocomplete="off"></label>
<label>Relayer API key address <input type="password" name="RELAYER_API_KEY_ADDRESS" value="" autocomplete="off"></label>
<button type="submit">Lưu ví</button>
</form>
<p>Ô để trống giữ giá trị cũ. Nút dưới xóa khóa, funder và các API key.</p>
<form method="post" action="/wallet/clear">
<button type="submit">Xóa khóa</button>
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
