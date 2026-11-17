"""predict.fun contracts and share amounts. No network and no signing.

Collateral is USDT with 18 decimals on BNB Chain. A short up/down market is
settled by the conditional-token contract and traded on the CTF exchange
selected by ``isNegRisk`` and ``isYieldBearing``. On BNB mainnet the
non-yield, non-neg-risk exchange is
``0x8BC070BEdAB741406F4B1Eb65A72bee27894B689``. The SDK picks the same
address when it signs an order.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

WEI = 10**18
CHAIN_ID_MAINNET = 56
CHAIN_ID_TESTNET = 97

USDT = {
    "mainnet": "0x55d398326f99059fF775485246999027B3197955",
    "testnet": "0xB32171ecD878607FFc4F8FC0bCcE6852BB3149E0",
}

# (is_neg_risk, is_yield_bearing) -> contract
_CONDITIONAL = {
    "mainnet": {
        (False, False): "0x22DA1810B194ca018378464a58f6Ac2B10C9d244",
        (True, False): "0x22DA1810B194ca018378464a58f6Ac2B10C9d244",
        (False, True): "0x9400F8Ad57e9e0F352345935d6D3175975eb1d9F",
        (True, True): "0xF64b0b318AAf83BD9071110af24D24445719A07F",
    },
    "testnet": {
        (False, False): "0x2827AAef52D71910E8FBad2FfeBC1B6C2DA37743",
        (True, False): "0x2827AAef52D71910E8FBad2FfeBC1B6C2DA37743",
        (False, True): "0x38BF1cbD66d174bb5F3037d7068E708861D68D7f",
        (True, True): "0x26e865CbaAe99b62fbF9D18B55c25B5E079A93D5",
    },
}
_EXCHANGE = {
    "mainnet": {
        (False, False): "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689",
        (True, False): "0x365fb81bd4A24D6303cd2F19c349dE6894D8d58A",
        (False, True): "0x6bEb5a40C032AFc305961162d8204CDA16DECFa5",
        (True, True): "0x8A289d458f5a134bA40015085A8F50Ffb681B41d",
    },
    "testnet": {
        (False, False): "0x2A6413639BD3d73a20ed8C95F634Ce198ABbd2d7",
        (True, False): "0xd690b2bd441bE36431F6F6639D7Ad351e7B29680",
        (False, True): "0x8a6B4Fa700A1e310b106E7a48bAFa29111f66e89",
        (True, True): "0x95D5113bc50eD201e319101bbca3e0E250662fCC",
    },
}
_ADAPTER = {
    "mainnet": {
        False: "0xc3Cf7c252f65E0d8D88537dF96569AE94a7F1A6E",
        True: "0x41dCe1A4B8FB5e6327701750aF6231B7CD0B2A40",
    },
    "testnet": {
        False: "0x285c1B939380B130D7EBd09467b93faD4BA623Ed",
        True: "0xb74aea04bdeBE912Aa425bC9173F9668e6f11F99",
    },
}

SERIES_WINDOWS = {
    "btc-updown-5m": 300,
    "btc-updown-15m": 900,
    "btc-updown-1h": 3600,
}


def network_name(value: str | None) -> str:
    text = str(value or "testnet").strip().lower()
    if text not in {"testnet", "mainnet"}:
        raise ValueError("network must be testnet or mainnet")
    return text


def chain_id(network: str) -> int:
    return CHAIN_ID_MAINNET if network_name(network) == "mainnet" else CHAIN_ID_TESTNET


def usdt_address(network: str = "mainnet") -> str:
    return USDT[network_name(network)]


def _flags(is_neg_risk: bool, is_yield_bearing: bool) -> tuple[bool, bool]:
    return bool(is_neg_risk), bool(is_yield_bearing)


def conditional_tokens(
    is_neg_risk: bool, is_yield_bearing: bool, network: str = "mainnet"
) -> str:
    return _CONDITIONAL[network_name(network)][_flags(is_neg_risk, is_yield_bearing)]


def exchange_for(
    is_neg_risk: bool, is_yield_bearing: bool, network: str = "mainnet"
) -> str:
    """Exchange the order is signed against. Maker orders use this contract."""
    return _EXCHANGE[network_name(network)][_flags(is_neg_risk, is_yield_bearing)]


def position_contract(
    is_neg_risk: bool, is_yield_bearing: bool, network: str = "mainnet"
) -> str:
    """Contract that splits, merges, and redeems. Neg-risk uses the adapter."""
    if is_neg_risk:
        return _ADAPTER[network_name(network)][bool(is_yield_bearing)]
    return conditional_tokens(False, is_yield_bearing, network)


def position_target(version: str, neg_risk: bool, *, yield_bearing: bool = False, network: str = "mainnet") -> str:
    """Split/merge target. ``version`` is ignored; predict.fun has one protocol."""
    del version
    return position_contract(neg_risk, yield_bearing, network)


def shares_to_base_units(shares: str) -> int:
    """Human shares or USDT to 18-decimal wei, rounded down."""
    units = (Decimal(str(shares)) * Decimal(WEI)).to_integral_value(rounding=ROUND_DOWN)
    if units < 0:
        raise ValueError("shares")
    return int(units)


def wei_to_shares(amount: int | str) -> Decimal:
    return Decimal(int(str(amount))) / Decimal(WEI)


def window_seconds(series: str) -> int:
    name = str(series or "btc-updown-5m")
    for slug, seconds in SERIES_WINDOWS.items():
        if name == slug or name.startswith(slug + "-"):
            return seconds
    return SERIES_WINDOWS["btc-updown-5m"]


def series_of(slug: str) -> str:
    text = str(slug or "")
    for name in SERIES_WINDOWS:
        if text == name or text.startswith(name + "-"):
            return name
    return "btc-updown-5m"


def position_plan(
    action: str,
    condition_id: str,
    shares: str,
    *,
    is_neg_risk: bool = False,
    is_yield_bearing: bool = False,
    network: str = "mainnet",
) -> dict:
    """Describe one split, merge, or redeem. Nothing is signed or sent."""
    if action not in {"split", "merge", "redeem"}:
        raise ValueError("action")
    base = 0 if action == "redeem" else shares_to_base_units(shares)
    return {
        "action": action,
        "target": position_contract(is_neg_risk, is_yield_bearing, network),
        "exchange": exchange_for(is_neg_risk, is_yield_bearing, network),
        "condition_id": condition_id,
        "amount": "0" if action == "redeem" else str(shares),
        "amount_wei": base,
        "is_neg_risk": bool(is_neg_risk),
        "is_yield_bearing": bool(is_yield_bearing),
        "signature_type": 0,
        "network": network_name(network),
        "confirmed": False,
    }
