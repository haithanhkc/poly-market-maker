"""BOT_* settings from .env. Secrets are parsed only so they can be kept verbatim.

Nothing in this module prints environment values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import Config, ConfigError
from paper_split_maker.decimal_utils import D

# Polymarket's BTC 5-minute book rejects a sell smaller than this.
POLYMARKET_MIN_SIZE = Decimal("5")

BOT_KEYS = (
    "BOT_CAPITAL",
    "BOT_SPLIT_SIZE",
    "BOT_CLIP_SIZE",
    "BOT_RUNGS",
    "BOT_MAX_LOSS_FRACTION",
    "BOT_SESSION_STOP",
    "BOT_MAX_MARKETS",
    "BOT_MODE",
)

# Never written by the dashboard and never returned in a public view.
SECRET_KEYS = (
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
)


@dataclass
class RunSettings:
    """Config after .env is applied, plus how many sessions this run should finish."""

    config: Config
    max_markets: int
    mode: str


def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Missing file is an empty map. Values are not logged."""
    path = Path(path)
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        found[key] = _unquote(value.strip())
    return found


def write_bot_values(path: Path, updates: dict) -> None:
    """Replace BOT_* lines. Every other line, including secrets, stays byte-for-byte."""
    path = Path(path)
    clean: dict[str, str] = {}
    for key, value in updates.items():
        name = str(key).strip()
        if name not in BOT_KEYS:
            continue
        clean[name] = str(value).strip()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in clean:
            out.append(f"{key}={clean[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in clean.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def merged_bot_env(env_path: Path, process_env: dict) -> dict[str, str]:
    """File values override the JSON. A BOT_* already in the process overrides the file."""
    found = read_env(env_path)
    for key, value in process_env.items():
        if str(key).startswith("BOT_"):
            found[str(key)] = str(value)
    return found


def apply_bot_env(config: Config, env: dict, *, max_markets: int) -> RunSettings:
    """Overlay BOT_* onto a config. The loss floor follows the chosen capital."""
    cfg = replace(config)
    mode = cfg.live and "live" or "paper"
    markets = max_markets

    def present(name: str) -> str | None:
        if name not in env:
            return None
        text = str(env.get(name) or "").strip()
        return text or None

    capital = present("BOT_CAPITAL")
    if capital is not None:
        cfg.initial_capital = _decimal("BOT_CAPITAL", capital)
    split = present("BOT_SPLIT_SIZE")
    if split is not None:
        cfg.split_clip = _decimal("BOT_SPLIT_SIZE", split)
    clip = present("BOT_CLIP_SIZE")
    if clip is not None:
        cfg.clip_size = _decimal("BOT_CLIP_SIZE", clip)
    rungs = present("BOT_RUNGS")
    if rungs is not None:
        cfg.ladder_levels = _integer("BOT_RUNGS", rungs)
    fraction = present("BOT_MAX_LOSS_FRACTION")
    if fraction is not None:
        cfg.max_loss_fraction = _decimal("BOT_MAX_LOSS_FRACTION", fraction)
    session = present("BOT_SESSION_STOP")
    if session is not None:
        cfg.session_max_loss = _decimal("BOT_SESSION_STOP", session)
    limit = present("BOT_MAX_MARKETS")
    if limit is not None:
        markets = _integer("BOT_MAX_MARKETS", limit)
    chosen = present("BOT_MODE")
    if chosen is not None:
        folded = chosen.casefold()
        if folded not in {"paper", "live"}:
            raise ConfigError("BOT_MODE must be paper or live")
        mode = folded
        cfg.live = folded == "live"

    _check_sizes(cfg)
    if markets < 1 or markets > 100:
        raise ConfigError("BOT_MAX_MARKETS must be from 1 to 100")
    if cfg.ladder_levels < 1 or cfg.ladder_levels > 10:
        raise ConfigError("BOT_RUNGS must be from 1 to 10")
    # The per-market budget has to fit the split the operator just chose.
    if cfg.per_market_budget < cfg.split_clip:
        cfg.per_market_budget = cfg.split_clip
    if cfg.per_market_budget > cfg.initial_capital:
        cfg.per_market_budget = cfg.initial_capital
    cfg.validate()
    if cfg.split_clip > cfg.per_market_budget:
        raise ConfigError("split must be <= capital")
    return RunSettings(config=cfg, max_markets=markets, mode=mode)


def prepare_run(config_path: Path, process_env: dict, *, max_markets: int) -> RunSettings:
    """JSON first, then paper_split_maker/.env next to that file, then process BOT_*."""
    from paper_split_maker.config import load_config

    config = load_config(config_path)
    env = merged_bot_env(Path(config_path).resolve().parent / ".env", process_env)
    return apply_bot_env(config, env, max_markets=max_markets)


def _check_sizes(config: Config) -> None:
    if config.initial_capital <= 0:
        raise ConfigError("capital must be > 0")
    if config.split_clip <= 0:
        raise ConfigError("split must be > 0")
    if config.split_clip > config.initial_capital:
        raise ConfigError("split must be <= capital")
    minimum = (
        POLYMARKET_MIN_SIZE
        if POLYMARKET_MIN_SIZE > config.min_order_size
        else config.min_order_size
    )
    if config.split_clip < minimum or config.clip_size < minimum:
        raise ConfigError(f"split and clip must be at least {minimum}")
    if config.clip_size > config.split_clip:
        raise ConfigError("clip must be <= split")


def _decimal(name: str, value: str) -> Decimal:
    try:
        return D(value)
    except (ValueError, ArithmeticError) as exc:
        raise ConfigError(f"{name} is not a decimal") from exc


def _integer(name: str, value: str) -> int:
    try:
        number = D(value)
    except (ValueError, ArithmeticError) as exc:
        raise ConfigError(f"{name} is not an integer") from exc
    if number != number.to_integral_value():
        raise ConfigError(f"{name} is not an integer")
    return int(number)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
