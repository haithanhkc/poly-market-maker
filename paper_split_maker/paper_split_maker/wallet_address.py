"""Derive the signer address from a hex private key. The key is never formatted."""

from __future__ import annotations

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 55066263022277343669578718895168534326250603453777594175500187360389116729240
_GY = 32670510020758816978083085130507043184471273380659243275938904335757337482424
_MASK64 = (1 << 64) - 1
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)


def signer_address(hex_key: str) -> str:
    """EIP-55 address for a 32-byte hex key. Empty when the key cannot be used."""
    scalar = _scalar(hex_key)
    if scalar is None:
        return ""
    point = _mul(scalar, (_GX, _GY))
    if point is None:
        return ""
    x_coord, y_coord = point
    digest = keccak256(x_coord.to_bytes(32, "big") + y_coord.to_bytes(32, "big"))
    return _checksum(digest[-20:].hex())


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (not NIST SHA3-256). Used only to format a public address."""
    rate = 136
    state = [[0] * 5 for _ in range(5)]
    padded = _pad(data, rate)
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for lane in range(rate // 8):
            start = lane * 8
            word = int.from_bytes(block[start : start + 8], "little")
            state[lane % 5][lane // 5] ^= word
        _permute(state)
    out = bytearray()
    for lane in range(4):
        out.extend(state[lane % 5][lane // 5].to_bytes(8, "little"))
    return bytes(out)


def _scalar(hex_key: str) -> int | None:
    text = str(hex_key or "").strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) != 64:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    if value <= 0 or value >= _N:
        return None
    return value


def _checksum(raw_hex: str) -> str:
    digest = keccak256(raw_hex.encode("ascii"))
    chars = []
    for index, char in enumerate(raw_hex):
        nibble = digest[index // 2]
        high = (nibble >> 4) & 0xF if index % 2 == 0 else nibble & 0xF
        chars.append(char.upper() if char.isalpha() and high >= 8 else char)
    return "0x" + "".join(chars)


def _pad(data: bytes, rate: int) -> bytes:
    out = bytearray(data)
    out.append(0x01)
    while len(out) % rate != 0:
        out.append(0)
    out[-1] |= 0x80
    return bytes(out)


def _rotl(value: int, bits: int) -> int:
    bits %= 64
    if bits == 0:
        return value & _MASK64
    return ((value << bits) | (value >> (64 - bits))) & _MASK64


def _permute(state: list[list[int]]) -> None:
    for round_index in range(24):
        columns = [
            state[x_coord][0]
            ^ state[x_coord][1]
            ^ state[x_coord][2]
            ^ state[x_coord][3]
            ^ state[x_coord][4]
            for x_coord in range(5)
        ]
        mixed = [
            columns[(x_coord - 1) % 5] ^ _rotl(columns[(x_coord + 1) % 5], 1)
            for x_coord in range(5)
        ]
        for x_coord in range(5):
            for y_coord in range(5):
                state[x_coord][y_coord] ^= mixed[x_coord]
        rotated = [[0] * 5 for _ in range(5)]
        for x_coord in range(5):
            for y_coord in range(5):
                rotated[y_coord][(2 * x_coord + 3 * y_coord) % 5] = _rotl(
                    state[x_coord][y_coord], _ROTATIONS[x_coord][y_coord]
                )
        for x_coord in range(5):
            for y_coord in range(5):
                state[x_coord][y_coord] = (
                    rotated[x_coord][y_coord]
                    ^ (
                        (~rotated[(x_coord + 1) % 5][y_coord])
                        & rotated[(x_coord + 2) % 5][y_coord]
                    )
                ) & _MASK64
        state[0][0] ^= _RC[round_index]


def _add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if x1 == x2 and y1 == y2:
        slope = (3 * x1 * x1) * pow(2 * y1, _P - 2, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    y3 = (slope * (x1 - x3) - y1) % _P
    return x3, y3


def _mul(scalar: int, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result
