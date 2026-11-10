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


BALANCE_RPCS = ("https://polygon.drpc.org", "https://polygon-bor-rpc.publicnode.com")


def pusd_balance_shares(owner: str, rpc_call=None):
    """pUSD ERC-20 balanceOf(owner), in shares (6 decimals)."""
    from decimal import Decimal

    data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
    call = rpc_call or _rpc_eth_call
    units = int(str(call(PUSD, data)), 16)
    return Decimal(units) / Decimal("1000000")


def erc1155_balance(owner: str, token_id: str, rpc_call=None) -> int:
    """CTF balanceOf(owner, id) in base units, read from a public RPC."""
    from paper_split_maker.chain_data import CTF

    data = (
        "0x00fdd58e"
        + owner.lower().replace("0x", "").rjust(64, "0")
        + format(int(str(token_id)), "x").rjust(64, "0")
    )
    if rpc_call is None:
        rpc_call = _rpc_eth_call
    return int(str(rpc_call(CTF, data)), 16)


def _rpc_eth_call(to: str, data: str) -> str:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }
    ).encode("utf-8")
    last: Exception | None = None
    for url in BALANCE_RPCS:
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
        except Exception as exc:  # try the next RPC
            last = exc
    raise RuntimeError(f"balance read failed: {last}")


def split_tokens_present(
    owner: str, up_token: str, down_token: str, amount_shares, rpc_call=None
) -> bool:
    """True only when the funder holds at least amount of BOTH outcome tokens."""
    from paper_split_maker.chain_data import shares_to_base_units

    need = shares_to_base_units(str(amount_shares))
    return all(
        erc1155_balance(owner, token, rpc_call) >= need
        for token in (up_token, down_token)
    )


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
) -> tuple[str, bool]:
    """Send split, merge, or redeem.

    An EOA transaction is submitted and confirmed later from the receipt.
    Proxy, Safe, and deposit wallets go through the official SecureClient,
    which signs the relayer request and waits until it settles. The second
    value is true when that wait has already finished.
    """
    from paper_split_maker.chain_data import (
        encode_merge,
        encode_redeem,
        encode_split,
        position_target,
        shares_to_base_units,
    )

    if action not in {"split", "merge", "redeem"}:
        raise RuntimeError("unknown position action")
    base = 0 if action == "redeem" else shares_to_base_units(str(amount_shares))
    if action == "split":
        data = encode_split(version, condition_id, base)
    elif action == "merge":
        data = encode_merge(version, condition_id, base)
    else:
        data = encode_redeem(version, condition_id)
    plan = {
        "action": action,
        "target": position_target(version, neg_risk),
        "data": data,
    }
    if creds.signature_type == 0:
        return (
            send_eoa(plan, creds, gas_limit, max_fee_gwei, priority_gwei),
            False,
        )
    return (
        official_position_call(creds, action, condition_id, base, client=client),
        True,
    )


def official_position_call(
    creds: Credentials, action: str, condition_id: str, amount_base: int, client=None
) -> str:
    """Call the official client's split, merge, or redeem. No unsigned POST."""
    if not creds.relayer_api_key or not creds.relayer_api_key_address:
        raise RuntimeError(
            "RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS are required for this wallet"
        )
    if client is None:
        client = build_secure_client(creds)
    if action == "split":
        handle = client.split_position(condition_id=condition_id, amount=amount_base)
    elif action == "merge":
        handle = client.merge_positions(condition_id=condition_id, amount=amount_base)
    elif action == "redeem":
        handle = client.redeem_positions(condition_id=condition_id)
    else:
        raise RuntimeError("unknown position action")
    outcome = handle.wait() if hasattr(handle, "wait") else handle
    if isinstance(outcome, dict):
        ident = outcome.get("transaction_hash") or outcome.get("transaction_id") or ""
    else:
        ident = (
            getattr(outcome, "transaction_hash", None)
            or getattr(outcome, "transaction_id", None)
            or ""
        )
    ident = str(ident)
    if not ident:
        raise RuntimeError("official position call did not return a transaction id")
    return ident


def build_secure_client(creds: Credentials):
    try:
        from polymarket import RelayerApiKey, SecureClient
    except ImportError as exc:
        raise RuntimeError(
            "this wallet needs the official polymarket package for split, merge, and redeem"
        ) from exc
    try:
        return SecureClient.create(
            private_key=creds.private_key,
            wallet=creds.funder,
            api_key=RelayerApiKey(
                key=creds.relayer_api_key,
                address=creds.relayer_api_key_address,
            ),
        )
    except Exception as exc:
        raise RuntimeError(
            creds.redact(f"could not start the position client: {exc}")
        ) from exc


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
