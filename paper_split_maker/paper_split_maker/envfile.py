"""BOT_* settings and wallet lines in .env.

The dashboard edits the split and the session count. Clip size, rung count,
and the loss stops are derived from that split unless an advanced BOT_* line
overrides them. Wallet secrets are written only by the wallet form. Nothing
in this module prints environment values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from paper_split_maker.config import Config, ConfigError
from paper_split_maker.decimal_utils import D, qdown

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

# Returned only as "đã lưu" plus, for a long value, its last 4 characters.
SECRET_KEYS = (
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
)

# The live runner reads these. Funder and signature type are not secrets.
WALLET_KEYS = SECRET_KEYS + (
    "POLY_FUNDER",
    "POLY_SIGNATURE_TYPE",
)

# Shipped ladder at a 25 pUSD split: 59 bankroll, 6 session stop, 20% daily.
# Multiply then divide so a 25 split stays Decimal("59"), not Decimal("59.00").
_CAPITAL_NUM = Decimal("59")
_CAPITAL_DEN = Decimal("25")
_SESSION_NUM = Decimal("6")
_SESSION_DEN = Decimal("25")
_DAILY_FRACTION = Decimal("0.20")
_POSTED_FRACTION = Decimal("0.60")
_MAX_RUNGS = 10


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
    _write_keys(path, updates, allowed=BOT_KEYS, skip_blank=False)


def write_wallet_values(path: Path, updates: dict) -> None:
    """Replace only the wallet lines that have a new value. A blank keeps the old one."""
    clean = _wallet_updates(updates, skip_blank=True)
    _check_wallet(clean)
    _write_keys(path, clean, allowed=WALLET_KEYS, skip_blank=True)


def clear_wallet_values(path: Path) -> None:
    """Remove every wallet secret and the funder. Signature type goes back to EOA."""
    blank = {key: "" for key in WALLET_KEYS}
    blank["POLY_SIGNATURE_TYPE"] = "0"
    _write_keys(path, blank, allowed=WALLET_KEYS, skip_blank=False)


def saved_hint(value: str) -> str:
    """Public label for a secret. The full value is not included."""
    text = str(value or "").strip()
    if not text:
        return "chưa lưu"
    if len(text) < 12:
        return "đã lưu"
    return f"đã lưu · …{text[-4:]}"


def redact_secrets(text: str, env: dict) -> str:
    """Replace stored secrets. Short values are left alone so they cannot eat the page."""
    secrets = []
    for key in SECRET_KEYS:
        value = str(env.get(key) or "").strip()
        if len(value) < 8:
            continue
        secrets.append(value)
        if value.lower().startswith("0x") and len(value) > 10:
            secrets.append(value[2:])
    secrets.sort(key=len, reverse=True)
    redacted = text
    for secret in secrets:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted


def wallet_public(env: dict) -> dict:
    """Funder, signer address, and masked hints. Raw secrets are not copied."""
    from paper_split_maker.wallet_address import signer_address

    hints = {key: saved_hint(str(env.get(key) or "")) for key in SECRET_KEYS}
    funder = str(env.get("POLY_FUNDER") or "").strip()
    signature = str(env.get("POLY_SIGNATURE_TYPE") or "0").strip() or "0"
    return {
        "funder": funder,
        "signer": signer_address(str(env.get("POLY_PRIVATE_KEY") or "")),
        "signature_type": signature,
        "hints": hints,
    }


def merged_bot_env(env_path: Path, process_env: dict) -> dict[str, str]:
    """File values override the JSON. A BOT_* already in the process overrides the file."""
    found = read_env(env_path)
    for key, value in process_env.items():
        if str(key).startswith("BOT_"):
            found[str(key)] = str(value)
    return found


def derive_from_split(split: Decimal) -> tuple[Decimal, Decimal, int, Decimal, Decimal]:
    """Capital, clip, rungs, session stop, and the daily fraction for one split.

    Each side posts about 60% of the split, which is the unpaired cap of the
    shipped ladder. Rungs are 5-share clips and grow first, up to 10. After
    that the clip grows in steps of 5. A 25 split is 3 rungs of 5 (15 shares).
    A 50 split is 6 rungs of 5 (30 shares).

    Paper capital is split * 59/25, the shipped bankroll for a 25 split.
    The session stop is split * 6/25. The daily stop stays 20% of capital.
    A live pUSD balance, when one is supplied to ``apply_wallet_balance``,
    replaces that capital only when it can fund the split.
    """
    if split <= 0:
        raise ConfigError("split must be > 0")
    target = _posted_shares(split)
    rungs = int(target / POLYMARKET_MIN_SIZE)
    if rungs < 1:
        rungs = 1
    if rungs > _MAX_RUNGS:
        rungs = _MAX_RUNGS
    clip = _qdown_size(target / Decimal(rungs))
    if clip < POLYMARKET_MIN_SIZE:
        clip = POLYMARKET_MIN_SIZE
    while rungs > 1 and clip * Decimal(rungs) > split:
        rungs -= 1
    capital = split * _CAPITAL_NUM / _CAPITAL_DEN
    session = split * _SESSION_NUM / _SESSION_DEN
    if session <= 0:
        session = Decimal("0.01")
    return capital, clip, rungs, session, _DAILY_FRACTION


def apply_wallet_balance(config: Config, balance: Decimal) -> None:
    """Use the wallet's pUSD as capital when no BOT_CAPITAL override is set.

    A balance below the split is refused so the ledger is not created with a
    floor above the funds on hand. The 20% daily stop then applies to the
    real balance. The dashboard does not call this; it has no chain access.
    """
    if getattr(config, "capital_from_env", False):
        return
    amount = D(balance)
    if amount < config.split_clip:
        raise ConfigError("pUSD balance is below the split")
    config.initial_capital = amount
    if config.per_market_budget > config.initial_capital:
        config.per_market_budget = config.initial_capital
    config.validate()


def apply_bot_env(config: Config, env: dict, *, max_markets: int) -> RunSettings:
    """Overlay BOT_* onto a config. Missing ladder fields are derived from the split."""
    cfg = replace(config)
    mode = "live" if cfg.live else "paper"
    markets = max_markets
    split = _present(env, "BOT_SPLIT_SIZE")
    if split is not None:
        cfg.split_clip = _decimal("BOT_SPLIT_SIZE", split)
    _apply_derived(cfg, env)
    markets = _apply_markets_and_mode(cfg, env, markets)
    mode = "live" if cfg.live else "paper"
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


def _apply_derived(cfg: Config, env: dict) -> None:
    """Fill clip, rungs, and stops from the split, then let advanced keys win."""
    cfg.capital_from_env = False
    if cfg.split_clip <= 0:
        _apply_explicit(cfg, env)
        return
    capital, clip, rungs, session, fraction = derive_from_split(cfg.split_clip)
    cfg.initial_capital = capital
    cfg.clip_size = clip
    cfg.ladder_levels = rungs
    cfg.session_max_loss = session
    cfg.max_loss_fraction = fraction
    _apply_explicit(cfg, env)


def _apply_explicit(cfg: Config, env: dict) -> None:
    capital = _present(env, "BOT_CAPITAL")
    if capital is not None:
        cfg.initial_capital = _decimal("BOT_CAPITAL", capital)
        cfg.capital_from_env = True
    clip = _present(env, "BOT_CLIP_SIZE")
    if clip is not None:
        cfg.clip_size = _decimal("BOT_CLIP_SIZE", clip)
    rungs = _present(env, "BOT_RUNGS")
    if rungs is not None:
        cfg.ladder_levels = _integer("BOT_RUNGS", rungs)
    fraction = _present(env, "BOT_MAX_LOSS_FRACTION")
    if fraction is not None:
        cfg.max_loss_fraction = _decimal("BOT_MAX_LOSS_FRACTION", fraction)
    session = _present(env, "BOT_SESSION_STOP")
    if session is not None:
        cfg.session_max_loss = _decimal("BOT_SESSION_STOP", session)


def _apply_markets_and_mode(cfg: Config, env: dict, markets: int) -> int:
    limit = _present(env, "BOT_MAX_MARKETS")
    if limit is not None:
        markets = _integer("BOT_MAX_MARKETS", limit)
    chosen = _present(env, "BOT_MODE")
    if chosen is None:
        return markets
    folded = chosen.casefold()
    if folded not in {"paper", "live"}:
        raise ConfigError("BOT_MODE must be paper or live")
    cfg.live = folded == "live"
    return markets


def _posted_shares(split: Decimal) -> Decimal:
    target = _qdown_size(split * _POSTED_FRACTION)
    if target >= POLYMARKET_MIN_SIZE:
        return target
    if split >= POLYMARKET_MIN_SIZE:
        return POLYMARKET_MIN_SIZE
    return split


def _qdown_size(value: Decimal) -> Decimal:
    return qdown(value, POLYMARKET_MIN_SIZE)


def _present(env: dict, name: str) -> str | None:
    if name not in env:
        return None
    text = str(env.get(name) or "").strip()
    return text or None


def _write_keys(path: Path, updates: dict, *, allowed: tuple[str, ...], skip_blank: bool) -> None:
    path = Path(path)
    clean: dict[str, str] = {}
    for key, value in updates.items():
        name = str(key).strip()
        if name not in allowed:
            continue
        text = str(value).strip()
        if skip_blank and text == "":
            continue
        if "\n" in text or "\r" in text:
            raise ConfigError("a wallet value cannot span lines")
        clean[name] = text
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


def _wallet_updates(updates: dict, *, skip_blank: bool) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in updates.items():
        name = str(key).strip()
        if name not in WALLET_KEYS:
            continue
        text = str(value).strip()
        if skip_blank and text == "":
            continue
        clean[name] = text
    return clean


def _check_wallet(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ConfigError("a wallet value cannot span lines")
        if key == "POLY_PRIVATE_KEY" and not _is_hex_key(value):
            raise ConfigError("POLY_PRIVATE_KEY must be a 32-byte hex key")
        if key in {"POLY_FUNDER", "RELAYER_API_KEY_ADDRESS"} and not _is_address(value):
            raise ConfigError(f"{key} must be an address")
        if key == "POLY_SIGNATURE_TYPE" and value not in {"0", "1", "2", "3"}:
            raise ConfigError("POLY_SIGNATURE_TYPE must be 0, 1, 2, or 3")


def _is_hex_key(value: str) -> bool:
    text = value[2:] if value.lower().startswith("0x") else value
    if len(text) != 64:
        return False
    try:
        scalar = int(text, 16)
    except ValueError:
        return False
    return scalar > 0


def _is_address(value: str) -> bool:
    text = value[2:] if value.lower().startswith("0x") else value
    if len(text) != 40:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text)


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
