"""Calldata for split, merge, redeem, and allowance. No network and no signing.

Collateral is pUSD. CTF v1 markets use the collateral adapter. Version v2
markets use the Router. Selectors are keccak256 of the documented signatures.
"""

from __future__ import annotations

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
V2_ROUTER = "0x12121212006e4CD160D18e3f00711DA5c3372600"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
RELAYER = "https://relayer-v2.polymarket.com/submit"

SEL_SPLIT = "72ce4275"
SEL_MERGE = "9e7212ad"
SEL_REDEEM = "01b7037c"
SEL_APPROVE = "095ea7b3"
SEL_V2_SPLIT = "82d3b9f1"
SEL_V2_MERGE = "5b63685e"
SEL_V2_REDEEM = "abc0e7ce"


def position_target(version: str, neg_risk: bool) -> str:
    if str(version).lower() == "v2":
        return V2_ROUTER
    if neg_risk:
        return NEG_RISK_ADAPTER
    return CTF_ADAPTER


def encode_approve(spender: str, amount_base_units: int) -> str:
    if amount_base_units < 0:
        raise ValueError("amount")
    return "0x" + SEL_APPROVE + _addr(spender) + _uint(amount_base_units)


def encode_split(version: str, condition_id: str, amount_base_units: int) -> str:
    if str(version).lower() == "v2":
        return "0x" + SEL_V2_SPLIT + _bytes31(condition_id) + _uint(amount_base_units)
    return (
        "0x"
        + SEL_SPLIT
        + _addr(PUSD)
        + _bytes32_zero()
        + _bytes32(condition_id)
        + _tail_partition(amount_base_units)
    )


def encode_merge(version: str, condition_id: str, amount_base_units: int) -> str:
    if str(version).lower() == "v2":
        return "0x" + SEL_V2_MERGE + _bytes31(condition_id) + _uint(amount_base_units)
    return (
        "0x"
        + SEL_MERGE
        + _addr(PUSD)
        + _bytes32_zero()
        + _bytes32(condition_id)
        + _tail_partition(amount_base_units)
    )


def encode_redeem(version: str, condition_id: str) -> str:
    if str(version).lower() == "v2":
        return "0x" + SEL_V2_REDEEM + _bytes31(condition_id)
    # redeemPositions(address,bytes32,bytes32,uint256[])
    # head: token, parent, condition, offset; tail: length 2, 1, 2
    offset = 32 * 4
    return (
        "0x"
        + SEL_REDEEM
        + _addr(PUSD)
        + _bytes32_zero()
        + _bytes32(condition_id)
        + _uint(offset)
        + _uint(2)
        + _uint(1)
        + _uint(2)
    )


def shares_to_base_units(shares: str) -> int:
    from decimal import Decimal, ROUND_DOWN

    units = (Decimal(shares) * Decimal("1000000")).to_integral_value(
        rounding=ROUND_DOWN
    )
    if units < 0:
        raise ValueError("shares")
    return int(units)


def _addr(value: str) -> str:
    text = value.lower().replace("0x", "")
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError("address")
    return text.rjust(64, "0")


def _uint(value: int) -> str:
    if value < 0 or value >= 2**256:
        raise ValueError("uint")
    return hex(value)[2:].rjust(64, "0")


def _bytes32(value: str) -> str:
    text = value.lower().replace("0x", "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError("bytes32")
    return text


def _bytes31(value: str) -> str:
    text = _bytes32(value)
    # A v2 condition id is 31 bytes. The high byte must be zero; we keep the
    # right-most 31 bytes in a 32-byte word.
    if text[:2] != "00":
        text = "00" + text[2:]
    return text


def _bytes32_zero() -> str:
    return "0" * 64


def _tail_partition(amount: int) -> str:
    # Dynamic array starts after the five head words (addr, parent, condition, offset, amount).
    offset = 32 * 5
    return _uint(offset) + _uint(amount) + _uint(2) + _uint(1) + _uint(2)
