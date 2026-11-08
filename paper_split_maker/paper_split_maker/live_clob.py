"""Lazy wrapper around the official CLOB V2 client.

Imported only when a confirmed live session starts. Dry-run never imports it.
The private key is passed to the client constructor and is not logged.
"""

from __future__ import annotations

from paper_split_maker.config import ConfigError
from paper_split_maker.credentials import Credentials

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def build_client(creds: Credentials):
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError as exc:
        raise ConfigError(
            "live trading needs the official package py-clob-client-v2"
        ) from exc
    try:
        client = ClobClient(
            host=HOST,
            chain_id=CHAIN_ID,
            key=creds.private_key,
            signature_type=creds.signature_type,
            funder=creds.funder,
        )
        _install_api_creds(client, creds)
    except Exception as exc:
        raise ConfigError(
            creds.redact(f"could not start the CLOB client: {exc}")
        ) from exc
    return client


def http_status(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    text = str(exc)
    for marker in ("HTTP ", "status ", "status_code="):
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        digits = []
        for ch in tail:
            if ch.isdigit():
                digits.append(ch)
            elif digits:
                break
        if len(digits) == 3:
            return int("".join(digits))
    return None


def _api_key_create_fallback(exc: BaseException) -> bool:
    """A 400 from create-api-key means the key already exists."""
    status = http_status(exc)
    text = str(exc).lower()
    return status == 400 or "could not create api key" in text


def _install_api_creds(client, creds: Credentials) -> None:
    if creds.api_key and creds.api_secret and creds.api_passphrase:
        from py_clob_client_v2 import ApiCreds

        client.set_api_creds(
            ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            )
        )
        return
    create = getattr(client, "create_api_key", None)
    derive = getattr(client, "derive_api_key", None)
    if create is not None and derive is not None:
        try:
            client.set_api_creds(create())
            return
        except Exception as exc:
            if not _api_key_create_fallback(exc):
                raise
            print(
                creds.redact(
                    f"auth/api-key create failed (HTTP {http_status(exc) or 400}): {exc}. "
                    "Non-fatal; deriving the existing API key."
                ),
                flush=True,
            )
            client.set_api_creds(derive())
            return
    both = getattr(client, "create_or_derive_api_key", None)
    if both is None:
        raise ConfigError("CLOB client cannot create or derive an API key")
    try:
        client.set_api_creds(both())
    except Exception as exc:
        if derive is None or not _api_key_create_fallback(exc):
            raise
        print(
            creds.redact(
                f"auth/api-key create failed (HTTP {http_status(exc) or 400}): {exc}. "
                "Non-fatal; deriving the existing API key."
            ),
            flush=True,
        )
        client.set_api_creds(derive())


def post_only_sells(client, orders: list[dict]) -> list[dict]:
    """Post maker sells one at a time so a 4xx rejects only that order."""
    from py_clob_client_v2 import (
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    out = []
    for order in orders:
        args = OrderArgs(
            token_id=str(order["token_id"]),
            price=float(order["price"]),
            size=float(order["size"]),
            side=Side.SELL,
        )
        options = PartialCreateOrderOptions(
            tick_size=str(order.get("tick_size") or "0.01")
        )
        signed = client.create_order(args, options)
        try:
            out.append(client.post_order(signed, OrderType.GTC, post_only=True))
        except Exception as exc:
            # A client error is this order's refusal, not a failed batch.
            out.append(
                {
                    "success": False,
                    "errorMsg": str(exc),
                    "status": http_status(exc) or "",
                }
            )
    return out


def cancel_all(client) -> None:
    client.cancel_all()


def heartbeat(client, heartbeat_id: str | None) -> str | None:
    result = client.post_heartbeat(heartbeat_id)
    if isinstance(result, dict):
        return result.get("heartbeat_id") or heartbeat_id
    return heartbeat_id
