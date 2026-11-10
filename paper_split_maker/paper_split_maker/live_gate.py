"""Opt-in checks for live trading. Paper mode does not call this successfully.

All three gates are required: the CLI flag, config ``live: true``, and either
``--yes-i-understand-live-risk`` or the exact confirmation phrase on a TTY.
"""

from __future__ import annotations

CONFIRMATION = "I understand live trading can lose money"


def live_gates(
    *,
    config_live: bool,
    cli_live: bool,
    cli_yes: bool,
    typed: str = "",
    is_tty: bool = False,
) -> tuple[bool, str]:
    if not cli_live:
        return False, "paper"
    if not config_live:
        return False, "config_live_false"
    if cli_yes:
        return True, "flag"
    if is_tty and typed.strip() == CONFIRMATION:
        return True, "typed"
    return False, "confirmation_missing"
