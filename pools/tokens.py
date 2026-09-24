"""Метаданные токенов: устойчивое декодирование symbol()/name()/decimals()."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from eth_abi import decode

MAX_TEXT_LEN = 64


@dataclass(frozen=True, slots=True)
class TokenInfo:
    address: str  # lowercase
    symbol: str
    name: str
    decimals: int | None  # None — decimals() не читается, цену посчитать нельзя


def sanitize_text(text: str, limit: int = MAX_TEXT_LEN) -> str:
    """Убирает управляющие/невидимые символы и обрезает длину (имена токенов бывают злонамеренными)."""
    cleaned = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit - 1] + "…"
    return cleaned


def decode_text(data: bytes | None) -> str | None:
    """Декодирует ответ symbol()/name(): ABI-string, bytes32 (MKR, SAI) или None."""
    if not data:
        return None
    if len(data) >= 64:
        try:
            (value,) = decode(["string"], data)
            text = sanitize_text(value)
            return text or None
        except Exception:
            pass
    if len(data) >= 32:
        raw = data[:32].rstrip(b"\x00")
        try:
            text = sanitize_text(raw.decode("utf-8", errors="ignore"))
            return text or None
        except Exception:
            return None
    return None


def decode_decimals(data: bytes | None) -> int | None:
    """decimals() -> uint8 (некоторые токены отдают uint256). Разумный диапазон 0..77."""
    if not data or len(data) < 32:
        return None
    try:
        value = int.from_bytes(data[:32], "big")
    except Exception:
        return None
    if value > 77:  # 10**77 ~ предел uint256; больше — мусор
        return None
    return value


def decode_address(data: bytes | None) -> str | None:
    if not data or len(data) < 32:
        return None
    word = data[:32]
    if any(word[:12]):  # старшие 12 байт адреса обязаны быть нулевыми
        return None
    addr = "0x" + word[12:].hex()
    return None if addr == "0x" + "00" * 20 else addr


def decode_uint(data: bytes | None) -> int | None:
    if not data or len(data) < 32:
        return None
    return int.from_bytes(data[:32], "big")
