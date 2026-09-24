"""Логирование без секретов: ключи RPC и токен Telegram вырезаются из всех строк лога.

Ключ провайдера обычно сидит прямо в URL (https://eth-mainnet.g.alchemy.com/v2/<KEY>), а тексты
ошибок aiohttp/web3 содержат URL целиком — поэтому фильтруем уже отформатированную строку,
включая traceback'и.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlsplit

MASK = "***"
MIN_SECRET_LEN = 12  # короче — не похоже на ключ, не маскируем (чтобы не портить обычный текст)


def secret_fragments(*values: str | None) -> list[str]:
    """Полные значения + похожие на ключи части URL (сегменты пути, параметры запроса, user:pass)."""
    out: set[str] = set()
    for value in values:
        if not value:
            continue
        out.add(value)
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        if not parts.scheme:
            continue
        candidates = [seg for seg in parts.path.split("/") if seg]
        candidates += [v for _, v in parse_qsl(parts.query)]
        candidates += [x for x in (parts.username, parts.password) if x]
        out.update(c for c in candidates if len(c) >= MIN_SECRET_LEN)
    # длинные строки заменяем первыми, чтобы от них не оставалось хвостов
    return sorted((s for s in out if len(s) >= MIN_SECRET_LEN), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, MASK)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, datefmt: str | None, secrets: list[str]) -> None:
        super().__init__(fmt, datefmt)
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)
