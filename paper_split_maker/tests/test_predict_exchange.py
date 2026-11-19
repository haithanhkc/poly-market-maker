"""predict.fun exchange shapes. Network calls are mocked. No order is sent."""

from __future__ import annotations

import json
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from paper_split_maker.chain_data import (
    exchange_for,
    shares_to_base_units,
)
from paper_split_maker.config import Config, ConfigError
from paper_split_maker.credentials import Credentials
from paper_split_maker.live_clob import (
    PredictClient,
    UrllibJsonTransport,
    check_connection,
    post_only_sells,
)
from paper_split_maker.normalize import complement_price
from paper_split_maker.public_client import (
    MAINNET,
    TESTNET,
    USER_AGENT,
    PublicDataError,
    PublicReadClient,
    category_to_market,
    client_from_env,
)
from paper_split_maker.session import run_paper

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = json.loads(
    (ROOT / "tests" / "fixtures" / "btc_updown_5m_rounds.json").read_text(
        encoding="utf-8"
    )
)
CLOCK = 1_700_000_000
NEXT_SLUG = "btc-updown-5m-1700000100"


class _Transport:
    def __init__(self):
        self.calls = []

    def request_json(self, method, url, body, headers):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "headers": headers,
            }
        )
        return 200, {"success": True, "data": {"orderHash": "0xhash"}}


def test_complement_price_adds_to_one():
    assert complement_price("0.492", 3) == "0.508"
    assert Decimal(complement_price("0.492", 3)) + Decimal("0.492") == Decimal("1")


def test_category_becomes_an_up_down_market():
    start = (1_700_000_000 // 300) * 300
    market = category_to_market(
        {
            "startsAt": start,
            "endsAt": 1_893_456_000,
            "isNegRisk": False,
            "isYieldBearing": False,
            "markets": [
                {
                    "id": 11,
                    "conditionId": "0xabc",
                    "decimalPrecision": 3,
                    "feeRateBps": 200,
                    "tradingStatus": "OPEN",
                    "outcomes": [
                        {"name": "Yes", "indexSet": 1, "onChainId": "111"},
                        {"name": "No", "indexSet": 2, "onChainId": "222"},
                    ],
                }
            ],
        },
        "btc-updown-5m",
    )
    assert market["slug"] == f"btc-updown-5m-{start}"
    assert json.loads(market["outcomes"]) == ["Up", "Down"]
    assert json.loads(market["clobTokenIds"]) == ["111", "222"]
    assert market["feeRateBps"] == 200
    assert market["isYieldBearing"] is False
    assert market["predictMarketId"] == "11"


def test_post_only_sell_is_signed_locally_and_posted_to_testnet():
    from eth_account import Account

    key = Account.create().key.hex()
    creds = Credentials(private_key=key, funder="0x" + "11" * 20, network="testnet")
    transport = _Transport()
    client = PredictClient(creds, transport=transport)
    client.jwt = "preset-jwt"
    result = post_only_sells(
        client,
        [
            {
                "token_id": "111",
                "price": "0.50",
                "size": "5",
                "tick_size": "0.01",
                "fee_rate_bps": 200,
                "is_neg_risk": False,
                "is_yield_bearing": False,
                "decimal_precision": 2,
            }
        ],
    )
    assert result[0]["success"] is True
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-testnet.predict.fun/v1/orders"
    order = call["body"]["data"]["order"]
    assert call["body"]["data"]["isPostOnly"] is True
    assert order["signatureType"] == 0
    assert order["feeRateBps"] == "200"
    assert order["side"] == 1
    dumped = json.dumps(call["body"])
    assert key not in dumped
    assert "preset-jwt" not in dumped


def test_undersized_sell_does_not_post():
    creds = Credentials(private_key="0x" + "11" * 32, funder="0x" + "11" * 20)
    transport = _Transport()
    client = PredictClient(creds, transport=transport)
    client.jwt = "preset-jwt"
    result = post_only_sells(
        client,
        [{"token_id": "111", "price": "0.50", "size": "0.5", "tick_size": "0.01"}],
    )
    assert result[0]["success"] is False
    assert transport.calls == []


def test_mainnet_orders_are_refused_without_the_allow_flag():
    creds = Credentials(
        private_key="0x" + "11" * 32,
        funder="0x" + "11" * 20,
        network="mainnet",
        api_key="not-used",
        allow_mainnet=False,
    )
    client = PredictClient(creds, transport=_Transport())
    client.jwt = "preset-jwt"
    with pytest.raises(ConfigError, match="PREDICT_ALLOW_MAINNET"):
        post_only_sells(
            client,
            [{"token_id": "111", "price": "0.50", "size": "5", "tick_size": "0.01"}],
        )


class _Rounds:
    def __init__(self, payload=None, status: int = 200):
        self.payload = ROUNDS if payload is None else payload
        self.status = status
        self.urls = []

    def get_json(self, url: str):
        self.urls.append(url)
        if self.status >= 400:
            raise PublicDataError(f"http {self.status} for {url}: series not found", status=self.status)
        return self.payload


def test_recorded_mainnet_round_opens_the_next_session_only(tmp_path):
    transport = _Rounds()
    client = PublicReadClient(transport, network="mainnet", api_key="not-a-real-key")
    clock = {"t": float(CLOCK)}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    summary = run_paper(
        Config(split_lead_seconds=300, poll_interval_seconds=2, paper_duration_seconds=2),
        tmp_path / "paper",
        duration_seconds=2,
        max_markets=5,
        client=client,
        now_s=now,
        sleep=sleep,
    )
    text = json.dumps(summary)
    assert "not-a-real-key" not in text
    assert summary["opened"] == 1
    assert summary["discovery"]["network"] == "mainnet"
    assert summary["discovery"]["rounds"] == 2
    assert summary["discovery"]["matched"] == 1
    assert any("already started" in note for note in summary["notes"])
    assert any(f"opened {NEXT_SLUG}" in note for note in summary["notes"])
    state = json.loads((tmp_path / "paper" / "state.json").read_text(encoding="utf-8"))
    assert NEXT_SLUG in state["markets"]
    assert "btc-updown-5m-1699999800" not in state["markets"]
    market = state["markets"][NEXT_SLUG]
    assert market["up_token_id"] == "333"
    assert market["down_token_id"] == "444"
    assert transport.urls[0].startswith(f"{MAINNET}/v1/series/btc-updown-5m/rounds")


def test_paper_reads_mainnet_and_reports_a_missing_series(tmp_path, capsys):
    transport = _Rounds(status=404)
    client = PublicReadClient(transport, network="mainnet", api_key="not-a-real-key")
    clock = {"t": float(CLOCK)}
    summary = run_paper(
        Config(split_lead_seconds=300, poll_interval_seconds=2),
        tmp_path / "miss",
        duration_seconds=2,
        max_markets=5,
        client=client,
        now_s=lambda: clock["t"],
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
    )
    assert summary["opened"] == 0
    assert summary["public_data_ok"] is False
    assert summary["discovery"]["http_status"] == 404
    joined = "\n".join(summary["notes"])
    assert "discovery failed" in joined
    assert "series not found" in joined
    assert "polls=" in joined
    err = capsys.readouterr().err
    assert "discovery failed" in err
    assert "not-a-real-key" not in err
    assert "not-a-real-key" not in json.dumps(summary)


def test_client_from_env_does_not_fall_back_when_mainnet_is_set():
    client = client_from_env(
        {"PREDICT_NETWORK": "mainnet", "PREDICT_API_KEY": "header-only"}
    )
    assert client.network == "mainnet"
    assert client.base == MAINNET
    assert client.transport.headers["x-api-key"] == "header-only"
    unset = client_from_env({})
    assert unset.network == "testnet"
    assert unset.base == TESTNET
    assert "x-api-key" not in unset.transport.headers


def test_urllib_user_agent_is_not_the_python_default(monkeypatch):
    seen = []

    class _Response:
        status = 200

        def read(self):
            return b'{"success":true,"data":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout=0):
        seen.append(request.get_header("User-agent"))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    UrllibJsonTransport().request_json("GET", f"{TESTNET}/v1/series", None, {})
    from paper_split_maker.public_client import UrllibTransport

    public = UrllibTransport()
    public._opener.open = fake_open
    public.get_json(f"{TESTNET}/v1/series")
    assert seen == [USER_AGENT, USER_AGENT]
    assert all("python-urllib" not in item.lower() for item in seen)


class _Login:
    def __init__(self):
        self.calls = []

    def request_json(self, method, url, body, headers):
        self.calls.append((method, url, body, dict(headers or {})))
        if "/auth/message" in url:
            return 200, {"data": {"message": "Sign in"}}
        if url.endswith("/auth"):
            return 200, {"data": {"token": "jwt-do-not-print"}}
        if "/rounds" in url:
            return 200, {"success": True, "data": ROUNDS["data"][:1]}
        return 404, {"message": "missing"}


def test_check_connection_reports_login_without_secrets():
    from eth_account import Account

    key = Account.create().key.hex()
    creds = Credentials(
        private_key=key,
        funder="0x" + "11" * 20,
        network="mainnet",
        api_key="not-a-real-key",
        allow_mainnet=False,
    )
    transport = _Login()
    report = check_connection(
        creds,
        transport=transport,
        balance_reader=lambda owner: Decimal("12.5"),
    )
    dumped = json.dumps(report)
    assert report["login_ok"] is True
    assert report["rounds_status"] == 200
    assert report["rounds"] == 1
    assert report["usdt"] == "12.5"
    assert report["network"] == "mainnet"
    assert "jwt-do-not-print" not in dumped
    assert key not in dumped
    assert "not-a-real-key" not in dumped
    assert any(call[0] == "POST" and call[1].endswith("/v1/auth") for call in transport.calls)
    assert all(call[3].get("User-Agent") == USER_AGENT for call in transport.calls)


def test_share_units_and_the_non_yield_exchange():
    assert shares_to_base_units("5") == 5 * 10**18
    assert (
        exchange_for(False, False, "mainnet")
        == "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689"
    )
