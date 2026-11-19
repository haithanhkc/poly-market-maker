"""Load trading secrets from the environment. Never format them into errors or logs."""

from __future__ import annotations

from dataclasses import dataclass

from paper_split_maker.chain_data import network_name
from paper_split_maker.config import ConfigError

# Orders are signed as signature type 0. A Predict account still uses 0;
# the SDK wraps the signature for the Kernel account (ERC-1271).
SIGNATURE_TYPE_EOA = 0


@dataclass
class Credentials:
    private_key: str
    funder: str
    signature_type: int = SIGNATURE_TYPE_EOA
    api_key: str = ""
    network: str = "testnet"
    predict_account: str = ""
    allow_mainnet: bool = False

    def __repr__(self) -> str:
        kind = "predict-account" if self.predict_account else "EOA"
        return (
            f"Credentials(funder={self.funder}, network={self.network}, "
            f"account={kind}, signature_type=0, secret=redacted)"
        )

    def public_view(self) -> dict:
        return {
            "funder": self.funder,
            "network": self.network,
            "signature_type": SIGNATURE_TYPE_EOA,
            "signature_type_name": "EOA",
            "predict_account": bool(self.predict_account),
            "has_api_key": bool(self.api_key),
            "allow_mainnet": self.allow_mainnet,
        }

    def redact(self, text: str) -> str:
        redacted = text
        for secret in (self.private_key, self.api_key, self.predict_account):
            if secret and len(secret) >= 8:
                redacted = redacted.replace(secret, "[redacted]")
                if secret.lower().startswith("0x") and len(secret) > 10:
                    redacted = redacted.replace(secret[2:], "[redacted]")
        return redacted


def load_credentials(env: dict) -> Credentials:
    key = str(env.get("PREDICT_PRIVATE_KEY") or "").strip()
    if not key:
        raise ConfigError("PREDICT_PRIVATE_KEY is not set")
    try:
        network = network_name(str(env.get("PREDICT_NETWORK") or "testnet"))
    except ValueError as exc:
        raise ConfigError("PREDICT_NETWORK must be testnet or mainnet") from exc
    api_key = str(env.get("PREDICT_API_KEY") or "").strip()
    if network == "mainnet" and not api_key:
        raise ConfigError("PREDICT_API_KEY is required on mainnet")
    account = str(env.get("PREDICT_ACCOUNT") or "").strip()
    if account and not _is_address(account):
        raise ConfigError("PREDICT_ACCOUNT must be an address")
    from paper_split_maker.wallet_address import signer_address

    signer = signer_address(key)
    if not signer and not account:
        raise ConfigError("PREDICT_PRIVATE_KEY must be a 32-byte hex key")
    allow = str(env.get("PREDICT_ALLOW_MAINNET") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    return Credentials(
        private_key=key,
        funder=account or signer,
        signature_type=SIGNATURE_TYPE_EOA,
        api_key=api_key,
        network=network,
        predict_account=account,
        allow_mainnet=allow,
    )


def refuse_mainnet(creds: Credentials) -> None:
    """Mainnet orders stay off until the operator sets PREDICT_ALLOW_MAINNET=yes."""
    if creds.network == "mainnet" and not creds.allow_mainnet:
        raise ConfigError(
            "mainnet trading is refused until PREDICT_ALLOW_MAINNET=yes"
        )


def _is_address(value: str) -> bool:
    text = value[2:] if value.lower().startswith("0x") else value
    if len(text) != 40:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text)
