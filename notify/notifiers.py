"""Каналы уведомлений: консоль и Telegram (Bot API sendMessage)."""
from __future__ import annotations

import abc
import asyncio
import logging
import sys

import aiohttp

from .format import Alert, format_html, format_text

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    @abc.abstractmethod
    async def send(self, alert: Alert) -> None: ...

    async def close(self) -> None:
        pass


class ConsoleNotifier(Notifier):
    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout

    async def send(self, alert: Alert) -> None:
        sep = "=" * 72
        print(f"\n{sep}\n{format_text(alert)}\n{sep}", file=self.stream, flush=True)


class TelegramNotifier(Notifier):
    API = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0, attempts: int = 4) -> None:
        self.url = f"{self.API}/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.attempts = attempts
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def send_text(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(1, self.attempts + 1):
            try:
                session = await self._get_session()
                async with session.post(self.url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200 and body.get("ok"):
                        return True
                    if resp.status == 429:
                        delay = float((body.get("parameters") or {}).get("retry_after", 5))
                    elif 400 <= resp.status < 500:
                        # неверный токен/чат/разметка — повтор не поможет
                        log.error("Telegram: HTTP %s: %s", resp.status, body.get("description"))
                        return False
                    else:
                        delay = 2 ** attempt
                    log.warning("Telegram: HTTP %s, повтор через %.0f с", resp.status, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                delay = 2 ** attempt
                log.warning("Telegram: %s: %s, повтор через %.0f с", type(exc).__name__, exc, delay)
            if attempt < self.attempts:
                await asyncio.sleep(delay)
        log.error("Telegram: сообщение не отправлено после %d попыток", self.attempts)
        return False

    async def send(self, alert: Alert) -> None:
        await self.send_text(format_html(alert))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    async def send(self, alert: Alert) -> None:
        for n in self.notifiers:
            try:
                await n.send(alert)
            except Exception:  # noqa: BLE001 — канал уведомлений не должен ронять бота
                log.exception("ошибка отправки уведомления через %s", type(n).__name__)

    async def close(self) -> None:
        for n in self.notifiers:
            await n.close()
