"""RPC-клиент поверх web3.py (AsyncWeb3 + HTTP) с ретраями и экспоненциальной задержкой."""
from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict
from typing import Awaitable, Callable, Iterable, TypeVar

import aiohttp
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import ContractLogicError, ProviderConnectionError, RequestTimedOut, TooManyRequests

from .events import RawLog

log = logging.getLogger(__name__)
T = TypeVar("T")

BlockId = int | str


class RpcError(RuntimeError):
    """RPC-вызов не удался после всех попыток."""


class CallReverted(RuntimeError):
    """eth_call завершился revert'ом — детерминированная ошибка, повторять бессмысленно."""


def backoff_delay(attempt: int, base: float, maximum: float) -> float:
    """Экспоненциальная задержка с небольшим джиттером: base, 2*base, 4*base ... <= maximum."""
    delay = min(maximum, base * (2 ** max(0, attempt - 1)))
    return delay * (0.8 + 0.4 * random.random())


def is_transient(exc: BaseException) -> bool:
    """Временный сбой (сеть, таймаут, 429, 5xx) — стоит ждать и повторять.

    Всё остальное (4xx, ошибка JSON-RPC, нет такого блока у узла) считается постоянной ошибкой:
    её повторяем ограниченно и отдаём наверх, где решают, пропустить ли блок.
    """
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError,
                            TimeoutError, ConnectionError, ProviderConnectionError, RequestTimedOut,
                            TooManyRequests))


async def retry_async(what: str, fn: Callable[[], Awaitable[T]], *, attempts: int | None,
                      base_delay: float, max_delay: float,
                      no_retry: tuple[type[BaseException], ...] = (),
                      transient_forever: bool = False) -> T:
    """Вызывает fn с ретраями и экспоненциальной задержкой.

    attempts=None — пытаться бесконечно. transient_forever=True — лимит attempts действует только
    на постоянные ошибки, а временные (сеть лежит, 429) повторяются, пока не пройдут.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except no_retry:
            raise
        except Exception as exc:  # noqa: BLE001 — любые ошибки RPC/сети
            forever = transient_forever and is_transient(exc)
            if attempts is not None and attempt >= attempts and not forever:
                raise RpcError(f"{what}: {type(exc).__name__}: {exc}") from exc
            delay = backoff_delay(attempt, base_delay, max_delay)
            log.warning("RPC %s: ошибка (%s: %s), попытка %d, повтор через %.1f с",
                        what, type(exc).__name__, _short(exc), attempt, delay)
            await asyncio.sleep(delay)


def _short(exc: BaseException, limit: int = 200) -> str:
    s = str(exc)
    return s if len(s) <= limit else s[:limit] + "…"


def _is_revert(exc: BaseException) -> bool:
    if isinstance(exc, ContractLogicError):
        return True
    msg = str(exc).lower()
    return "execution reverted" in msg


class RpcClient:
    def __init__(self, http_url: str, *, timeout: float = 30.0, retry_base: float = 1.0,
                 retry_max: float = 60.0, call_attempts: int = 5) -> None:
        # Встроенные ретраи web3 отключены: ими управляем сами.
        provider = AsyncHTTPProvider(http_url, request_kwargs={"timeout": timeout},
                                     exception_retry_configuration=None)
        self.w3 = AsyncWeb3(provider)
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.call_attempts = call_attempts
        self._ts_cache: OrderedDict[int, int] = OrderedDict()

    async def close(self) -> None:
        try:
            await self.w3.provider.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _retry(self, what: str, fn: Callable[[], Awaitable[T]], attempts: int | None = None,
                     no_retry: tuple[type[BaseException], ...] = (), transient_forever: bool = False) -> T:
        return await retry_async(what, fn, attempts=attempts, base_delay=self.retry_base,
                                 max_delay=self.retry_max, no_retry=no_retry,
                                 transient_forever=transient_forever)

    async def block_number(self) -> int:
        return int(await self._retry("eth_blockNumber", lambda: self.w3.eth.block_number))

    async def block_timestamp(self, number: int) -> int:
        if number in self._ts_cache:
            return self._ts_cache[number]

        async def _get() -> int:
            block = await self.w3.eth.get_block(number)
            return int(block["timestamp"])

        ts = await self._retry(f"eth_getBlockByNumber({number})", _get, attempts=self.call_attempts)
        self._ts_cache[number] = ts
        while len(self._ts_cache) > 512:
            self._ts_cache.popitem(last=False)
        return ts

    async def tx_sender(self, tx_hash: str) -> str | None:
        async def _get() -> str:
            tx = await self.w3.eth.get_transaction(tx_hash)
            return str(tx["from"]).lower()

        try:
            return await self._retry(f"eth_getTransactionByHash({tx_hash[:10]}…)", _get, attempts=3)
        except RpcError as exc:
            log.warning("не удалось получить отправителя tx %s: %s", tx_hash, exc)
            return None

    async def eth_call(self, to: str, data: bytes, block: BlockId = "latest",
                       attempts: int | None = None) -> bytes:
        """eth_call с ретраями. Revert -> CallReverted (без повторов).

        Временные сбои повторяются до победного; постоянные ошибки (например, архивный узел
        нужен, а его нет) — attempts раз, затем RpcError."""
        params = {"to": AsyncWeb3.to_checksum_address(to), "data": "0x" + data.hex()}

        async def _call() -> bytes:
            try:
                return bytes(await self.w3.eth.call(params, block_identifier=block))
            except Exception as exc:
                if _is_revert(exc):
                    raise CallReverted(str(exc)) from exc
                raise

        return await self._retry(f"eth_call({to[:10]}…)", _call,
                                 attempts=attempts or self.call_attempts, no_retry=(CallReverted,),
                                 transient_forever=True)

    async def _get_logs_once(self, from_block: int, to_block: int, topics: Iterable[str]) -> list[RawLog]:
        params = {"fromBlock": from_block, "toBlock": to_block, "topics": [list(topics)]}
        entries = await self.w3.eth.get_logs(params)
        return [RawLog.from_rpc(e) for e in entries]

    async def get_logs(self, from_block: int, to_block: int, topics: Iterable[str]) -> list[RawLog]:
        """ОДИН eth_getLogs без фильтра по адресу на диапазон блоков.

        Временные сбои (сеть, 429) повторяются с backoff'ом, пока не пройдут. Постоянная ошибка на
        диапазоне (лимит провайдера на число блоков/логов) — диапазон делится пополам. Постоянная
        ошибка на одном блоке после call_attempts попыток отдаётся наверх как RpcError: основной
        цикл решит, пропускать ли блок, и бот не зависнет навсегда.
        """
        topics = list(topics)
        if from_block == to_block:
            return await self._retry(f"eth_getLogs({from_block})",
                                     lambda: self._get_logs_once(from_block, to_block, topics),
                                     attempts=self.call_attempts, transient_forever=True)
        try:
            return await self._retry(f"eth_getLogs({from_block}-{to_block})",
                                     lambda: self._get_logs_once(from_block, to_block, topics),
                                     attempts=1, transient_forever=True)
        except RpcError as exc:
            mid = (from_block + to_block) // 2
            log.debug("eth_getLogs %d-%d не удался (%s), делим диапазон", from_block, to_block, _short(exc, 120))
            left = await self.get_logs(from_block, mid, topics)
            right = await self.get_logs(mid + 1, to_block, topics)
            return left + right
