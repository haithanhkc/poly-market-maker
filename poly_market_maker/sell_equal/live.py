"""Opt-in live adapter.

Nothing in this module is used by paper or replay. Importing it does not
read the environment and does not open a network connection.

The one-shot place helper in polymarket-client 0.17.0 is intentionally unused:
that helper can submit an ERC-1155 approval after an allowance rejection.
This adapter signs with ``create_limit_order`` and posts with ``post_order``
only. It never asks the SDK to deploy a wallet, and it never sends a builder
or relayer key.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from poly_market_maker.sell_equal.config import SellConfig
from poly_market_maker.sell_equal.engine import (
    CancelResult,
    MarketView,
    SellEqualEngine,
    SubmitResult,
)
from poly_market_maker.sell_equal.quant import D
from poly_market_maker.sell_equal.verify import verify_public_market

LIVE_FLAG = "POLY_SELL_BOT_ENABLE_LIVE"
USER_WS_FLAG = "POLY_SELL_BOT_USER_WS"

# Exchange addresses from polymarket-client 0.17.0 environments.py and the
# public order signing-domain table. Used only to read one allowance entry.
V1_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
V1_NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
V2_EXCHANGE = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"


class LiveDisabled(RuntimeError):
    pass


def live_opt_in_enabled() -> bool:
    return os.environ.get(LIVE_FLAG) == "1"


def user_ws_opt_in_enabled() -> bool:
    return live_opt_in_enabled() and os.environ.get(USER_WS_FLAG) == "1"


def require_live_opt_in() -> None:
    if not live_opt_in_enabled():
        raise LiveDisabled(
            "Live mode is disabled. Use paper or replay. Set "
            + LIVE_FLAG
            + "=1 only for a manual opt-in on your own machine."
        )


def spender_for(version: str, neg_risk: bool) -> str:
    if version == "v2":
        return V2_EXCHANGE
    if version == "v1" and neg_risk:
        return V1_NEG_RISK_EXCHANGE
    if version == "v1":
        return V1_EXCHANGE
    raise LiveDisabled("unsupported CLOB version")


def asset_type_for(version: str) -> str:
    if version == "v2":
        return "CONDITIONAL-V2"
    if version == "v1":
        return "CONDITIONAL"
    raise LiveDisabled("unsupported CLOB version")


def shares_from_base_units(raw: int, decimals: int) -> Decimal:
    if raw < 0:
        raise LiveDisabled("negative balance")
    return Decimal(raw) / (Decimal(10) ** decimals)


def allowance_base_units(allowances: dict, spender: str) -> int:
    target = spender.lower()
    for key, value in allowances.items():
        if str(key).lower() == target:
            return int(value)
    return 0


def cap_inventory(
    balance_shares: Decimal,
    allowance_shares: Decimal,
    configured: Decimal,
    minimum: Decimal,
) -> Decimal:
    if allowance_shares < minimum:
        raise LiveDisabled(
            "allowance is below the minimum size; approve it manually, this bot will not"
        )
    capped = min(balance_shares, allowance_shares, configured)
    if capped < minimum:
        raise LiveDisabled("existing balance is below the minimum size")
    return capped


class LiveBroker:
    """Sell-only wrapper around an already constructed client."""

    def __init__(self, client, condition_id: str, token_ids: set[str]):
        self.client = client
        self.condition_id = condition_id
        self.token_ids = set(token_ids)

    def submit_sell(self, token_id: str, price: Decimal, size: Decimal) -> SubmitResult:
        require_live_opt_in()
        if token_id not in self.token_ids:
            return SubmitResult("rejected")
        try:
            signed = self.client.create_limit_order(
                asset_id=token_id,
                price=str(price),
                size=str(size),
                side="SELL",
                post_only=True,
            )
            response = self.client.post_order(signed)
        except Exception:
            return SubmitResult("unknown")
        return interpret_order_response(response)

    def cancel(self, order_ids: list[str]) -> CancelResult:
        require_live_opt_in()
        if not order_ids:
            return CancelResult("known", frozenset(), {})
        try:
            response = self.client.cancel_orders(order_ids=list(order_ids))
        except Exception:
            return CancelResult("unknown")
        return interpret_cancel_response(response, order_ids)


def interpret_order_response(response) -> SubmitResult:
    ok = getattr(response, "ok", None)
    order_id = getattr(response, "order_id", None)
    if ok is True and order_id:
        return SubmitResult("accepted", str(order_id))
    if ok is False:
        return SubmitResult("rejected")
    return SubmitResult("unknown", str(order_id) if order_id else None)


def interpret_cancel_response(response, requested: list[str]) -> CancelResult:
    canceled = {str(item) for item in (getattr(response, "canceled", ()) or ())}
    raw_failed = getattr(response, "not_canceled", {}) or {}
    failed = {str(key): str(value) for key, value in dict(raw_failed).items()}
    known = canceled | set(failed)
    if any(item not in known for item in requested):
        return CancelResult("unknown")
    return CancelResult("known", frozenset(canceled), failed)


def _present(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise LiveDisabled("missing " + name)
    return value


def build_client_from_env():
    """Construct a sync secure client. Not called by tests or paper mode."""

    require_live_opt_in()
    private_key = _present("POLYMARKET_PRIVATE_KEY")
    wallet = _present("POLYMARKET_WALLET_ADDRESS")
    key = _present("POLYMARKET_API_KEY")
    secret = _present("POLYMARKET_API_SECRET")
    passphrase = _present("POLYMARKET_API_PASSPHRASE")
    try:
        from polymarket import ApiKeyCreds, SecureClient
    except Exception:
        raise LiveDisabled("polymarket-client is not installed") from None
    try:
        client = SecureClient.create(
            private_key=private_key,
            wallet=wallet,
            credentials=ApiKeyCreds(key=key, secret=secret, passphrase=passphrase),
        )
    except Exception:
        raise LiveDisabled("secure client setup failed") from None
    signer = str(getattr(client, "signer", "") or "")
    funder = str(getattr(client, "wallet", "") or "")
    if not signer or not funder or signer.lower() == funder.lower():
        _close_quietly(client)
        raise LiveDisabled("signer and funder must be two different addresses")
    return client


def _close_quietly(client) -> None:
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            return


def read_balance_allowance(client, token_id: str, version: str, neg_risk: bool):
    require_live_opt_in()
    balance = client.get_balance_allowance(
        asset_type=asset_type_for(version),
        asset_id=token_id,
    )
    spender = spender_for(version, neg_risk)
    raw_allowance = allowance_base_units(getattr(balance, "allowances", {}), spender)
    return int(balance.balance), raw_allowance


def trade_to_wire(trade) -> dict:
    makers = []
    for maker in getattr(trade, "maker_orders", ()) or ():
        makers.append(
            {
                "order_id": maker.order_id,
                "side": maker.side,
                "matched_amount": str(maker.matched_amount),
                "price": str(maker.price),
            }
        )
    return {
        "event_type": "trade",
        "id": trade.id,
        "status": trade.status,
        "trader_side": trade.trader_side,
        "taker_order_id": trade.taker_order_id,
        "side": trade.side,
        "size": str(trade.size),
        "price": str(trade.price),
        "maker_orders": makers,
    }


def run_user_stream(subscribe, condition_id: str, handler) -> None:
    """Feed an authenticated user stream into ``handler``.

    ``subscribe`` receives the condition-id filter and returns an iterator of
    raw messages. REST reconciliation stays available when this is not started.
    """

    if not user_ws_opt_in_enabled():
        raise LiveDisabled(
            "user websocket is off; use account reconciliation as the fallback"
        )
    for message in subscribe([condition_id]):
        handler(message)


def poll_account(client, condition_id: str):
    """Read open orders and account trades. Returns read_ok plus raw rows."""

    require_live_opt_in()
    try:
        orders = list(client.list_open_orders(market=condition_id).iter_items())
        trades = list(client.list_account_trades(market=condition_id).iter_items())
    except Exception:
        return False, [], []
    return True, orders, trades


def apply_poll(engine, orders, trades, read_ok: bool, now: Decimal) -> None:
    from poly_market_maker.sell_equal.events import parse_user_payload

    own = {order.exchange_id for order in engine.state.orders if order.exchange_id}
    if read_ok:
        engine.consume_user_batch(
            parse_user_payload([trade_to_wire(trade) for trade in trades], own)
        )
    matched = {}
    open_ids = set()
    for order in orders:
        if str(getattr(order, "market", "")) not in ("", engine_condition(engine)):
            continue
        open_ids.add(str(order.id))
        matched[str(order.id)] = D(str(order.size_matched))
    engine.reconcile(open_ids, matched, read_ok, now)


def engine_condition(engine) -> str:
    return engine.config.condition_id


def clob_market_url(condition_id: str) -> str:
    return "https://clob.polymarket.com/clob-markets/" + condition_id


def gamma_market_url(condition_id: str) -> str:
    query = urllib.parse.urlencode({"condition_ids": condition_id})
    return "https://gamma-api.polymarket.com/markets?" + query


def book_url(token_id: str) -> str:
    query = urllib.parse.urlencode({"token_id": token_id})
    return "https://clob.polymarket.com/book?" + query


def fetch_public_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "poly-sell-equal/0.1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def run_prepared_cycle(config: SellConfig, client, fetch_json, now: datetime):
    """One verified read-only preflight plus at most one seed/hedge pass.

    The CLI does not call this unless live opt-in and ``--send`` are both set.
    """

    require_live_opt_in()
    if now.tzinfo is None:
        raise LiveDisabled("now must be timezone-aware")
    clob = fetch_json(clob_market_url(config.condition_id))
    gamma_rows = fetch_json(gamma_market_url(config.condition_id))
    if not isinstance(gamma_rows, list) or len(gamma_rows) != 1:
        raise LiveDisabled("gamma did not return exactly one market")
    gamma = gamma_rows[0]
    up_book = fetch_json(book_url(config.up_token_id))
    down_book = fetch_json(book_url(config.down_token_id))
    errors = verify_public_market(config, clob, gamma, up_book, down_book, now)
    if errors:
        raise LiveDisabled("market verification failed: " + "; ".join(errors))
    version = str(clob.get("v"))
    neg_risk = gamma.get("negRisk")
    if not isinstance(neg_risk, bool):
        raise LiveDisabled("negRisk missing")
    capped = _cap_both(client, config, version, neg_risk)
    prepared = replace(
        config,
        inventory_up=capped["up"],
        inventory_down=capped["down"],
    )
    broker = LiveBroker(
        client,
        config.condition_id,
        {config.up_token_id, config.down_token_id},
    )
    market_ts = min(_book_unix(up_book), _book_unix(down_book))
    unix = max(_as_unix(now), market_ts)
    engine = SellEqualEngine(prepared, broker, lambda: unix)
    engine.observe_market(
        MarketView(
            ts=market_ts,
            accepting=True,
            ended=False,
            tick_size=prepared.tick_size,
            min_order_size=prepared.min_order_size,
        )
    )
    engine.observe_user(unix)
    engine.tick()
    read_ok, orders, trades = poll_account(client, config.condition_id)
    apply_poll(engine, orders, trades, read_ok, unix)
    return engine


def _cap_both(client, config: SellConfig, version: str, neg_risk: bool) -> dict:
    capped = {}
    for outcome in ("up", "down"):
        balance, allowance = read_balance_allowance(
            client, config.token_id(outcome), version, neg_risk
        )
        capped[outcome] = cap_inventory(
            shares_from_base_units(balance, config.share_decimals),
            shares_from_base_units(allowance, config.share_decimals),
            config.inventory(outcome),
            config.min_order_size,
        )
    return capped


def _book_unix(book: dict) -> Decimal:
    return D(str(book["timestamp"])) / Decimal("1000")


def _as_unix(now: datetime) -> Decimal:
    return D(str(int(now.timestamp())))
