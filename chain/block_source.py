"""Источники новых блоков: HTTP-поллинг и WebSocket-подписка newHeads.

Оба реализуют один интерфейс BlockSource, выбор — через rpc.block_source в конфиге.
Источник только сообщает номер нового head; какие блоки обрабатывать, решает основной цикл
(поэтому пропущенные WS-уведомления не страшны — пропуски догоняются по номеру).
"""
from __future__ import annotations

import abc
import asyncio
import logging
from typing import AsyncIterator

from web3 import AsyncWeb3, WebSocketProvider

from .rpc import RpcClient, backoff_delay

log = logging.getLogger(__name__)


class BlockSource(abc.ABC):
    @abc.abstractmethod
    def heads(self) -> AsyncIterator[int]:
        """Бесконечный асинхронный поток номеров новых head-блоков."""


class PollingBlockSource(BlockSource):
    """Опрос eth_blockNumber.

    Блоки в Ethereum выходят раз в 12 с, поэтому сразу после нового блока опрашивать бессмысленно:
    выжидаем quiet_after_block_sec (отсчёт от момента, когда блок увидели), затем опрашиваем каждые
    interval_sec, пока не появится следующий. Это ~2 запроса на блок вместо ~6 — экономия лимита
    провайдера (Alchemy считает каждый вызов) без потери скорости.
    """

    def __init__(self, rpc: RpcClient, interval_sec: float = 2.0, quiet_after_block_sec: float = 0.0) -> None:
        self.rpc = rpc
        self.interval = interval_sec
        self.quiet_after_block = quiet_after_block_sec

    async def heads(self) -> AsyncIterator[int]:
        loop = asyncio.get_running_loop()
        last = -1
        while True:
            head = await self.rpc.block_number()  # внутри бесконечные ретраи
            if head > last:
                last = head
                seen_at = loop.time()
                yield head
                # пока основной цикл обрабатывал блок, часть паузы уже прошла
                wait = seen_at + self.quiet_after_block - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
            else:
                await asyncio.sleep(self.interval)


def _parse_number(value) -> int:
    return value if isinstance(value, int) else int(str(value), 16)


class WebSocketBlockSource(BlockSource):
    def __init__(self, ws_url: str, *, idle_timeout_sec: float = 60.0,
                 retry_base: float = 1.0, retry_max: float = 60.0) -> None:
        self.ws_url = ws_url
        self.idle_timeout = idle_timeout_sec
        self.retry_base = retry_base
        self.retry_max = retry_max

    async def heads(self) -> AsyncIterator[int]:
        attempt = 0
        while True:
            try:
                async with AsyncWeb3(WebSocketProvider(self.ws_url)) as w3:
                    await w3.eth.subscribe("newHeads")
                    log.info("WebSocket: подписка newHeads активна")
                    attempt = 0
                    stream = w3.socket.process_subscriptions().__aiter__()
                    while True:
                        msg = await asyncio.wait_for(stream.__anext__(), timeout=self.idle_timeout)
                        result = msg.get("result") if isinstance(msg, dict) else None
                        if result is None:
                            continue
                        yield _parse_number(result["number"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — любые обрывы/таймауты -> переподключение
                attempt += 1
                delay = backoff_delay(attempt, self.retry_base, self.retry_max)
                log.warning("WebSocket: %s: %s — переподключение через %.1f с",
                            type(exc).__name__, exc, delay)
                await asyncio.sleep(delay)
