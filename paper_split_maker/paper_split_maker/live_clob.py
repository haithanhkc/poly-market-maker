"""Sign and submit predict.fun orders. Dry-run never imports this module.

Maker sells are LIMIT orders with ``isPostOnly`` true, so they skip the
up/down speed bump. ``feeRateBps`` is the market's taker fee and is signed
into the order; the maker fee itself is 0. ``signatureType`` is 0.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from paper_split_maker.chain_data import WEI, chain_id
from paper_split_maker.config import ConfigError
from paper_split_maker.credentials import Credentials, refuse_mainnet
from paper_split_maker.public_client import MAINNET, TESTNET

ALLOWED_PREFIXES = (TESTNET, MAINNET)
MIN_SELL_SHARES = Decimal("1")


def build_client(creds: Credentials):
    """Construct the order client. No login and no order until a method runs."""
    try:
        return PredictClient(creds)
    except Exception as exc:
        raise ConfigError(creds.redact(f"could not start the predict client: {exc}")) from exc


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


def clob_number(value, quantum) -> float:
    """Float snapped onto the market quantum before it becomes binary."""
    number = Decimal(str(value))
    step = Decimal(str(quantum))
    if step <= 0:
        step = Decimal("0.01")
    units = (number / step).to_integral_value(rounding=ROUND_HALF_EVEN)
    snapped = units * step
    return float(format(snapped, "f"))


def post_only_sells(client, orders: list[dict]) -> list[dict]:
    """Post maker sells one at a time. A 4xx rejects only that order."""
    return _post_orders(client, orders, taker=False)


def post_taker_sells(client, orders: list[dict]) -> list[dict]:
    """Sell at the bid. Used only to finish a leg the other side already filled."""
    return _post_orders(client, orders, taker=True)


def _post_orders(client, orders: list[dict], *, taker: bool) -> list[dict]:
    creds = getattr(client, "creds", None)
    if isinstance(creds, Credentials):
        refuse_mainnet(creds)
    out = []
    for order in orders:
        try:
            size = Decimal(str(order.get("size") or "0"))
            if size < MIN_SELL_SHARES:
                out.append(
                    {
                        "success": False,
                        "errorMsg": "create_order_min_order_size_not_met",
                        "status": 400,
                    }
                )
                continue
            body = client.build_limit_sell(order, post_only=not taker, taker=taker)
            out.append(client.submit_order(body))
        except Exception as exc:
            out.append(
                {
                    "success": False,
                    "errorMsg": str(exc),
                    "status": http_status(exc) or "",
                }
            )
    return out


def cancel_all(client) -> None:
    fn = getattr(client, "cancel_all", None)
    if fn is None:
        raise RuntimeError("client has no cancel_all")
    fn()


def heartbeat(client, heartbeat_id: str | None):
    """One liveness check. Network failures come back as ``error``."""
    try:
        result = client.post_heartbeat(heartbeat_id)
    except Exception as exc:
        return heartbeat_id, exc
    if isinstance(result, dict):
        return result.get("heartbeat_id") or heartbeat_id, None
    return heartbeat_id, None


class PredictClient:
    def __init__(self, creds: Credentials, transport=None, builder=None):
        self.creds = creds
        self.transport = transport or UrllibJsonTransport()
        self._builder = builder
        self.jwt = ""
        self.base = MAINNET if creds.network == "mainnet" else TESTNET

    def build_limit_sell(self, order: dict, *, post_only: bool, taker: bool) -> dict:
        from predict_sdk import (
            BuildOrderInput,
            LimitHelperInput,
            Side,
            SignatureType,
        )

        builder = self._order_builder()
        precision = int(order.get("decimal_precision") or _precision(order.get("tick_size")))
        price = _snap(order.get("price"), precision)
        quantity = Decimal(str(order.get("size")))
        amounts = builder.get_limit_order_amounts(
            LimitHelperInput(
                side=Side.SELL,
                price_per_share_wei=_wei(price),
                quantity_wei=_wei(quantity),
            )
        )
        fee = int(order.get("fee_rate_bps") or 0)
        built = builder.build_order(
            "MARKET" if taker else "LIMIT",
            BuildOrderInput(
                side=Side.SELL,
                token_id=str(order["token_id"]),
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=fee,
                signature_type=SignatureType.EOA,
            ),
        )
        typed = builder.build_typed_data(
            built,
            is_neg_risk=bool(order.get("is_neg_risk")),
            is_yield_bearing=bool(order.get("is_yield_bearing")),
        )
        signed = builder.sign_typed_data_order(typed)
        message = _plain(typed.message)
        message["hash"] = builder.build_typed_data_hash(typed)
        message["signature"] = signed.signature
        message["signatureType"] = 0
        message["feeRateBps"] = str(fee)
        body = {
            "data": {
                "order": message,
                "pricePerShare": str(amounts.price_per_share),
                "strategy": "MARKET" if taker else "LIMIT",
            }
        }
        if not taker:
            body["data"]["isPostOnly"] = True if post_only else False
        return body

    def submit_order(self, body: dict) -> dict:
        refuse_mainnet(self.creds)
        self.ensure_jwt()
        status, payload = self.transport.request_json(
            "POST", self._url("/v1/orders"), body, self._headers(auth=True)
        )
        if not isinstance(payload, dict):
            payload = {}
        if status >= 400 or payload.get("success") is False:
            return {
                "success": False,
                "errorMsg": str(payload.get("message") or payload.get("error") or status),
                "error": str(payload.get("error") or ""),
                "status": status,
            }
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        order_hash = str(data.get("orderHash") or "")
        return {
            "success": True,
            "id": order_hash,
            "orderID": order_hash,
            "orderId": str(data.get("orderId") or ""),
            "orderHash": order_hash,
            "status": status,
        }

    def cancel_orders(self, order_ids: list[str]) -> dict:
        refuse_mainnet(self.creds)
        self.ensure_jwt()
        status, payload = self.transport.request_json(
            "POST",
            self._url("/v1/orders/remove-by-hash"),
            {"data": {"hashes": [str(item) for item in order_ids]}},
            self._headers(auth=True),
        )
        if status >= 400:
            raise RuntimeError(f"cancel failed HTTP {status}")
        return payload if isinstance(payload, dict) else {}

    def cancel_all(self) -> None:
        refuse_mainnet(self.creds)
        self.ensure_jwt()
        status, payload = self.transport.request_json(
            "GET",
            self._url("/v1/orders?status=OPEN&first=100"),
            None,
            self._headers(auth=True),
        )
        if status >= 400:
            raise RuntimeError(f"open orders failed HTTP {status}")
        rows = payload.get("data") if isinstance(payload, dict) else None
        hashes = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            order = row.get("order") if isinstance(row.get("order"), dict) else row
            order_hash = order.get("hash") or row.get("orderHash")
            if order_hash:
                hashes.append(str(order_hash))
        if hashes:
            self.cancel_orders(hashes)

    def get_order(self, order_hash: str) -> dict:
        self.ensure_jwt()
        status, payload = self.transport.request_json(
            "GET",
            self._url(f"/v1/orders/{order_hash}"),
            None,
            self._headers(auth=True),
        )
        if status >= 400 or not isinstance(payload, dict):
            raise RuntimeError(f"order read failed HTTP {status}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return order_view(order_hash, data)

    def post_heartbeat(self, heartbeat_id: str | None):
        status, _payload = self.transport.request_json(
            "GET",
            self._url("/v1/series/btc-updown-5m"),
            None,
            self._headers(auth=False),
        )
        if status >= 400:
            raise RuntimeError(f"heartbeat failed HTTP {status}")
        return {"heartbeat_id": heartbeat_id or "predict"}

    def ensure_jwt(self) -> str:
        if self.jwt:
            return self.jwt
        status, payload = self.transport.request_json(
            "GET", self._url("/v1/auth/message"), None, self._headers(auth=False)
        )
        if status >= 400 or not isinstance(payload, dict):
            raise RuntimeError(f"auth message failed HTTP {status}")
        message = str((payload.get("data") or {}).get("message") or "")
        if not message:
            raise RuntimeError("auth message was empty")
        signature = self._sign_login(message)
        status, token_payload = self.transport.request_json(
            "POST",
            self._url("/v1/auth"),
            {
                "signer": self.creds.funder,
                "message": message,
                "signature": signature,
            },
            self._headers(auth=False),
        )
        if status >= 400 or not isinstance(token_payload, dict):
            raise RuntimeError(f"login failed HTTP {status}")
        token = str((token_payload.get("data") or {}).get("token") or "")
        if not token:
            raise RuntimeError("login returned no token")
        self.jwt = token
        return token

    def _sign_login(self, message: str) -> str:
        if self.creds.predict_account:
            signature = self._order_builder().sign_predict_account_message(message)
            return signature if isinstance(signature, str) else str(signature)
        from eth_account import Account
        from eth_account.messages import encode_defunct

        signed = Account.from_key(self.creds.private_key).sign_message(
            encode_defunct(text=message)
        )
        raw = signed.signature
        return raw.to_0x_hex() if hasattr(raw, "to_0x_hex") else "0x" + raw.hex()

    def _order_builder(self):
        if self._builder is not None:
            return self._builder
        from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions

        chain = (
            ChainId.BNB_MAINNET if self.creds.network == "mainnet" else ChainId.BNB_TESTNET
        )
        if chain_id(self.creds.network) != int(chain):
            raise ConfigError("predict chain id does not match the network")
        options = None
        if self.creds.predict_account:
            options = OrderBuilderOptions(predict_account=self.creds.predict_account)
        self._builder = OrderBuilder.make(chain, self.creds.private_key, options)
        return self._builder

    def _headers(self, *, auth: bool) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.creds.network == "mainnet" and self.creds.api_key:
            headers["x-api-key"] = self.creds.api_key
        if auth and self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        return headers

    def _url(self, path: str) -> str:
        return self.base + path


def order_view(order_hash: str, data: dict) -> dict:
    """Map a predict order into the snapshot the fill poller already understands."""
    filled = _maybe_shares(data.get("amountFilled") or 0)
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    price = _price_from_amounts(order) if order else Decimal("0")
    return {
        "event_type": "order",
        "id": order_hash,
        "size_matched": format(filled, "f"),
        "price": format(price, "f"),
        "status": data.get("status") or "",
    }


class UrllibJsonTransport:
    def request_json(self, method: str, url: str, body, headers: dict | None):
        import json
        import urllib.error
        import urllib.request

        if not url.startswith(ALLOWED_PREFIXES):
            raise RuntimeError("refusing non-official host")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read().decode("utf-8")
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = int(exc.code)
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"message": raw}
        return status, parsed


def _precision(tick) -> int:
    text = format(Decimal(str(tick or "0.01")), "f")
    if "." not in text:
        return 0
    return len(text.rstrip("0").split(".", 1)[1])


def _snap(value, precision: int) -> Decimal:
    quantum = Decimal(10) ** -int(precision)
    number = Decimal(str(value))
    units = (number / quantum).to_integral_value(rounding=ROUND_HALF_EVEN)
    return units * quantum


def _wei(value: Decimal) -> int:
    return int((value * Decimal(WEI)).to_integral_value(rounding=ROUND_HALF_EVEN))


def _plain(message) -> dict:
    out = {}
    for key, value in dict(message).items():
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int):
            out[key] = int(value)
        else:
            out[key] = value
    return out


def _maybe_shares(value) -> Decimal:
    number = Decimal(str(value or "0"))
    if number >= Decimal(WEI) / Decimal(1000):
        return number / Decimal(WEI)
    return number


def _price_from_amounts(order: dict) -> Decimal:
    try:
        maker = Decimal(str(order.get("makerAmount") or "0"))
        taker = Decimal(str(order.get("takerAmount") or "0"))
    except (ValueError, ArithmeticError):
        return Decimal("0")
    if maker <= 0:
        return Decimal("0")
    return taker / maker
