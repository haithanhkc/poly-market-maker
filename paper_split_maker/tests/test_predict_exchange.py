"""predict.fun exchange shapes. Network calls are mocked. No order is sent."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from paper_split_maker.chain_data import (
    exchange_for,
    shares_to_base_units,
)
from paper_split_maker.config import ConfigError
from paper_split_maker.credentials import Credentials
from paper_split_maker.live_clob import PredictClient, post_only_sells
from paper_split_maker.normalize import complement_price
from paper_split_maker.public_client import category_to_market


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


def test_share_units_and_the_non_yield_exchange():
    assert shares_to_base_units("5") == 5 * 10**18
    assert (
        exchange_for(False, False, "mainnet")
        == "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689"
    )
