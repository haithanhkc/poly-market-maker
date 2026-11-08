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
        if creds.api_key and creds.api_secret and creds.api_passphrase:
            from py_clob_client_v2 import ApiCreds

            client.set_api_creds(
                ApiCreds(
                    api_key=creds.api_key,
                    api_secret=creds.api_secret,
                    api_passphrase=creds.api_passphrase,
                )
            )
        else:
            client.set_api_creds(client.create_or_derive_api_key())
    except Exception as exc:
        raise ConfigError(
            creds.redact(f"could not start the CLOB client: {exc}")
        ) from exc
    return client


def post_only_sells(client, orders: list[dict]) -> list[dict]:
    """Post maker sells. Prices and sizes are decimals in the public order fields."""
    from py_clob_client_v2 import (
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    signed = []
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
        signed.append(client.create_order(args, options))
    if len(signed) > 1 and hasattr(client, "post_orders"):
        result = client.post_orders(signed, post_only=True)
        return result if isinstance(result, list) else [result]
    out = []
    for item in signed:
        out.append(client.post_order(item, OrderType.GTC, post_only=True))
    return out


def cancel_all(client) -> None:
    client.cancel_all()


def heartbeat(client, heartbeat_id: str | None) -> str | None:
    result = client.post_heartbeat(heartbeat_id)
    if isinstance(result, dict):
        return result.get("heartbeat_id") or heartbeat_id
    return heartbeat_id
