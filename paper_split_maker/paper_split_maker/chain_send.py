"""Submit split, merge, and redeem through the predict.fun SDK.

Dry-run never calls this module. Amounts are 18-decimal wei. The SDK selects
the conditional-token contract from ``isNegRisk`` and ``isYieldBearing``.
Errors are passed through ``redact`` so a secret in an exception does not
reach the log.
"""

from __future__ import annotations

import json
import urllib.request
from decimal import Decimal

from paper_split_maker.chain_data import (
    WEI,
    conditional_tokens,
    network_name,
    position_plan,
    shares_to_base_units,
    usdt_address,
)
from paper_split_maker.credentials import Credentials, refuse_mainnet

RPC = {
    "testnet": (
        "https://bsc-testnet-rpc.publicnode.com",
        "https://data-seed-prebsc-1-s1.binance.org:8545",
    ),
    "mainnet": (
        "https://bsc-dataseed.binance.org",
        "https://bsc-rpc.publicnode.com",
    ),
}


def approval_needed(allowance: int, needed: int) -> bool:
    return allowance < needed


def position_request(plan: dict, creds: Credentials) -> dict:
    """Public description of a position call. The private key is not copied."""
    return {
        "action": plan.get("action"),
        "conditionId": plan.get("condition_id"),
        "amountWei": str(plan.get("amount_wei") or "0"),
        "isNegRisk": bool(plan.get("is_neg_risk")),
        "isYieldBearing": bool(plan.get("is_yield_bearing")),
        "signatureType": 0,
        "maker": creds.funder,
        "network": creds.network,
        "exchange": plan.get("exchange"),
    }


def collateral_balance_shares(owner: str, rpc_call=None, network: str = "testnet"):
    """USDT balanceOf(owner), in whole tokens (18 decimals)."""
    chosen = network_name(network)
    data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
    call = rpc_call or (lambda to, payload: _rpc_eth_call(chosen, to, payload))
    units = int(str(call(usdt_address(chosen), data)), 16)
    return Decimal(units) / Decimal(WEI)


def pusd_balance_shares(owner: str, rpc_call=None):
    """Compatibility name. The balance is USDT on BNB testnet unless told otherwise."""
    return collateral_balance_shares(owner, rpc_call=rpc_call, network="testnet")


def erc1155_balance(
    owner: str,
    token_id: str,
    rpc_call=None,
    *,
    ctf_address: str | None = None,
    network: str = "testnet",
    is_yield_bearing: bool = False,
) -> int:
    """Conditional-token balanceOf(owner, id) in wei."""
    chosen = network_name(network)
    token = ctf_address or conditional_tokens(False, is_yield_bearing, chosen)
    data = (
        "0x00fdd58e"
        + owner.lower().replace("0x", "").rjust(64, "0")
        + format(int(str(token_id)), "x").rjust(64, "0")
    )
    call = rpc_call or (lambda to, payload: _rpc_eth_call(chosen, to, payload))
    return int(str(call(token, data)), 16)


def _rpc_eth_call(network: str, to: str, data: str) -> str:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }
    ).encode("utf-8")
    last: Exception | None = None
    for url in RPC[network_name(network)]:
        try:
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "paper-split-maker/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            if parsed.get("result"):
                return parsed["result"]
            last = RuntimeError(str(parsed.get("error") or "empty eth_call result"))
        except Exception as exc:
            last = exc
    raise RuntimeError(f"balance read failed: {last}")


def split_tokens_present(
    owner: str,
    up_token: str,
    down_token: str,
    amount_shares,
    rpc_call=None,
    *,
    network: str = "testnet",
    is_yield_bearing: bool = False,
) -> bool:
    """True only when the funder holds at least ``amount`` of both outcome tokens."""
    need = shares_to_base_units(str(amount_shares))
    return all(
        erc1155_balance(
            owner,
            token,
            rpc_call,
            network=network,
            is_yield_bearing=is_yield_bearing,
        )
        >= need
        for token in (up_token, down_token)
    )


def eoa_confirmed(tx_hash: str, network: str = "testnet") -> bool:
    from web3 import Web3

    chosen = network_name(network)
    last: Exception | None = None
    for url in RPC[chosen]:
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                return False
            return int(receipt["status"]) == 1
        except Exception as exc:
            last = exc
    raise RuntimeError(f"receipt read failed: {last}")


def submit_position(
    creds: Credentials,
    action: str,
    condition_id: str,
    amount_shares: str,
    version: str,
    neg_risk: bool,
    gas_limit: int,
    max_fee_gwei: str,
    priority_gwei: str,
    client=None,
    yield_bearing: bool = False,
) -> tuple[str, bool]:
    """Send split, merge, or redeem. The SDK waits for the receipt.

    ``version``, gas, and the fee fields are accepted so existing call sites
    keep working. predict.fun prices gas itself. The second value is true
    when the SDK has already confirmed the transaction.
    """
    del version, gas_limit, max_fee_gwei, priority_gwei
    refuse_mainnet(creds)
    if action not in {"split", "merge", "redeem"}:
        raise RuntimeError("unknown position action")
    plan = position_plan(
        action,
        condition_id,
        amount_shares,
        is_neg_risk=bool(neg_risk),
        is_yield_bearing=bool(yield_bearing),
        network=creds.network,
    )
    if client is None:
        client = sdk_builder(creds)
    ident = official_position_call(
        creds,
        action,
        condition_id,
        int(plan["amount_wei"]),
        client=client,
        is_neg_risk=bool(neg_risk),
        is_yield_bearing=bool(yield_bearing),
    )
    return ident, True


def official_position_call(
    creds: Credentials,
    action: str,
    condition_id: str,
    amount_base: int,
    client=None,
    is_neg_risk: bool = False,
    is_yield_bearing: bool = False,
) -> str:
    """Call the SDK split, merge, or redeem. No unsigned request is built."""
    refuse_mainnet(creds)
    if client is None:
        client = sdk_builder(creds)
    flags = {"is_neg_risk": bool(is_neg_risk), "is_yield_bearing": bool(is_yield_bearing)}
    if action == "split":
        result = client.split_positions(condition_id, int(amount_base), **flags)
        return _tx_id(result)
    if action == "merge":
        result = client.merge_positions(condition_id, int(amount_base), **flags)
        return _tx_id(result)
    if action != "redeem":
        raise RuntimeError("unknown position action")
    # A binary redeem pays the winning index. The loser reverts, so try both.
    found = ""
    last_error: Exception | None = None
    for index_set in (1, 2):
        amount = int(amount_base) if is_neg_risk else None
        try:
            result = client.redeem_positions(condition_id, index_set, amount, **flags)
        except Exception as exc:
            last_error = exc
            continue
        if getattr(result, "success", True) is False:
            last_error = getattr(result, "cause", None) or RuntimeError("redeem failed")
            continue
        try:
            found = found or _tx_id(result)
        except Exception as exc:
            last_error = exc
    if not found:
        raise RuntimeError(str(last_error or "redeem failed"))
    return found


def sdk_builder(creds: Credentials):
    from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions

    refuse_mainnet(creds)
    chain = ChainId.BNB_MAINNET if creds.network == "mainnet" else ChainId.BNB_TESTNET
    options = None
    if creds.predict_account:
        options = OrderBuilderOptions(predict_account=creds.predict_account)
    return OrderBuilder.make(chain, creds.private_key, options)


def _tx_id(result) -> str:
    if hasattr(result, "wait"):
        result = result.wait()
    if isinstance(result, dict):
        ident = result.get("transaction_hash") or result.get("transaction_id") or ""
        if not ident:
            raise RuntimeError("position call returned no transaction")
        return str(ident)
    if getattr(result, "success", True) is False:
        cause = getattr(result, "cause", None)
        raise RuntimeError(str(cause or "position call failed"))
    receipt = getattr(result, "receipt", None)
    if isinstance(receipt, dict):
        ident = receipt.get("transactionHash") or receipt.get("transaction_hash") or ""
        if ident:
            return ident.hex() if hasattr(ident, "hex") else str(ident)
    if receipt is not None:
        ident = getattr(receipt, "transactionHash", None) or getattr(
            receipt, "transaction_hash", None
        )
        if ident:
            return ident.hex() if hasattr(ident, "hex") else str(ident)
    return "submitted"
