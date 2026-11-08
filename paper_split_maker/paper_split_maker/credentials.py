"""Load trading secrets from the environment. Never format them into errors or logs."""

from __future__ import annotations

from dataclasses import dataclass

from paper_split_maker.config import ConfigError

SIGNATURE_TYPES = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "GNOSIS_SAFE",
    3: "POLY_1271",
}


@dataclass
class Credentials:
    private_key: str
    funder: str
    signature_type: int
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""

    def __repr__(self) -> str:
        return (
            f"Credentials(funder={self.funder}, "
            f"signature_type={SIGNATURE_TYPES.get(self.signature_type, self.signature_type)}, "
            "secret=redacted)"
        )

    def public_view(self) -> dict:
        return {
            "funder": self.funder,
            "signature_type": self.signature_type,
            "signature_type_name": SIGNATURE_TYPES.get(self.signature_type, "unknown"),
            "has_api_creds": bool(
                self.api_key and self.api_secret and self.api_passphrase
            ),
            "has_relayer_creds": bool(
                self.relayer_api_key and self.relayer_api_key_address
            ),
        }

    def redact(self, text: str) -> str:
        redacted = text
        for secret in (
            self.private_key,
            self.api_secret,
            self.api_passphrase,
            self.relayer_api_key,
        ):
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted


def load_credentials(env: dict) -> Credentials:
    key = str(env.get("POLY_PRIVATE_KEY") or "").strip()
    funder = str(env.get("POLY_FUNDER") or "").strip()
    raw_type = str(env.get("POLY_SIGNATURE_TYPE") or "0").strip()
    if not key:
        raise ConfigError("POLY_PRIVATE_KEY is not set")
    if not funder:
        raise ConfigError("POLY_FUNDER is not set")
    if not raw_type.isdigit() or int(raw_type) not in SIGNATURE_TYPES:
        raise ConfigError("POLY_SIGNATURE_TYPE must be 0, 1, 2, or 3")
    return Credentials(
        private_key=key,
        funder=funder,
        signature_type=int(raw_type),
        api_key=str(env.get("POLY_API_KEY") or "").strip(),
        api_secret=str(env.get("POLY_API_SECRET") or "").strip(),
        api_passphrase=str(env.get("POLY_API_PASSPHRASE") or "").strip(),
        relayer_api_key=str(env.get("RELAYER_API_KEY") or "").strip(),
        relayer_api_key_address=str(env.get("RELAYER_API_KEY_ADDRESS") or "").strip(),
    )
