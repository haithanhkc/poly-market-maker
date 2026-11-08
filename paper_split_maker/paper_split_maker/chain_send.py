"""Submit split, merge, and redeem. Dry-run never calls this module.

EOA wallets send a Polygon transaction. Proxy, Safe, and deposit wallets use
the official relayer and wait for STATE_CONFIRMED. Errors are passed through
``redact`` so a secret in an exception does not reach the log.
"""

from __future__ import annotations

import json
import urllib.request

from paper_split_maker.chain_data import (
    DEPOSIT_WALLET_FACTORY,
    PUSD,
    RELAYER,
)
from paper_split_maker.credentials import Credentials

POLYGON_RPC = "https://polygon-rpc.com"
RELAYER_STATE_CONFIRMED = "STATE_CONFIRMED"


def approval_needed(allowance: int, needed: int) -> bool:
    return allowance < needed


def eoa_transaction(
    plan: dict, gas_limit: int, max_fee_gwei: str, priority_gwei: str
) -> dict:
    tx = {
        "to": plan["target"],
        "data": plan["data"],
        "gas": int(gas_limit),
        "chainId": 137,
        "value": 0,
    }
    if max_fee_gwei and float(max_fee_gwei) > 0:
        tx["maxFeePerGas"] = int(float(max_fee_gwei) * 1_000_000_000)
        tx["maxPriorityFeePerGas"] = int(float(priority_gwei or 0) * 1_000_000_000)
    return tx


def relayer_body(plan: dict, creds: Credentials) -> dict:
    target = plan["target"]
    if creds.signature_type == 3:
        target = DEPOSIT_WALLET_FACTORY
    return {
        "type": "WALLET",
        "from": creds.funder,
        "to": target,
        "data": plan["data"],
        "signatureType": creds.signature_type,
    }


def send_eoa(
    plan: dict,
    creds: Credentials,
    gas_limit: int,
    max_fee_gwei: str,
    priority_gwei: str,
) -> str:
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError("live chain transactions need the web3 package") from exc
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    account = w3.eth.account.from_key(creds.private_key)
    tx = eoa_transaction(plan, gas_limit, max_fee_gwei, priority_gwei)
    tx["from"] = account.address
    tx["nonce"] = w3.eth.get_transaction_count(account.address)
    if "maxFeePerGas" not in tx:
        tx["maxFeePerGas"] = w3.eth.gas_price
        tx["maxPriorityFeePerGas"] = w3.eth.max_priority_fee
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    sent = w3.eth.send_raw_transaction(raw)
    return sent.hex()


def eoa_confirmed(tx_hash: str) -> bool:
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    if receipt is None:
        return False
    return int(receipt["status"]) == 1


def submit_relayer(plan: dict, creds: Credentials) -> str:
    body = relayer_body(plan, creds)
    payload = json.dumps(body).encode("utf-8")
    method = "POST"
    request = urllib.request.Request(
        RELAYER,
        data=payload,
        method=method,
        headers={
            "Content-Type": "application/json",
            "RELAYER_API_KEY": creds.relayer_api_key,
            "RELAYER_API_KEY_ADDRESS": creds.relayer_api_key_address,
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    ident = str(parsed.get("transactionID") or parsed.get("id") or "")
    if not ident:
        raise RuntimeError("relayer did not return a transaction id")
    return ident


def pusd_allowance_spender(target: str) -> str:
    """Approve pUSD to the contract that will pull collateral."""
    return target or PUSD
