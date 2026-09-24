"""Основной цикл (live) и режим реплея по историческим блокам."""
from __future__ import annotations

import asyncio
import logging

from chain.block_source import BlockSource
from chain.events import ALL_TOPICS
from chain.rpc import RpcClient, backoff_delay
from config import Config
from engine import Engine, group_by_block
from notify.format import Alert
from storage.db import Database

log = logging.getLogger(__name__)

HEARTBEAT_BLOCKS = 25  # строка «работаю» в логе примерно раз в 5 минут


class LiveRunner:
    def __init__(self, cfg: Config, rpc: RpcClient, db: Database, engine: Engine, source: BlockSource) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.db = db
        self.engine = engine
        self.source = source
        self.last: int | None = None
        self._failures: dict[int, int] = {}

    async def _initial_last_block(self, head: int) -> int:
        target = head - self.cfg.rpc.confirmations
        saved = self.db.get_last_block()
        if saved is None:
            log.info("первый запуск: начинаю с блока %d", target)
            return target - 1
        lag = target - saved
        limit = self.cfg.rpc.max_catchup_blocks
        if limit and lag > limit:
            log.warning("бот отстал на %d блоков — догоняю только последние %d", lag, limit)
            return target - limit
        if lag > 0:
            log.info("продолжаю с блока %d (отставание %d блоков)", saved + 1, lag)
        return saved

    async def run(self) -> None:
        head = await self.rpc.block_number()
        self.last = await self._initial_last_block(head)
        await self.engine.start(self.last + 1)
        async for head in self.source.heads():
            await self._catch_up(head - self.cfg.rpc.confirmations)

    async def _catch_up(self, target: int) -> None:
        assert self.last is not None
        rcfg = self.cfg.rpc
        while self.last < target:
            start = self.last + 1
            lag = target - self.last
            # если отстали больше чем на max_lag_blocks — пачками, иначе по одному блоку
            end = min(target, start + rcfg.catchup_batch_blocks - 1) if lag > rcfg.max_lag_blocks else start
            if lag > rcfg.max_lag_blocks:
                log.info("догоняю: блоки %d-%d (отставание %d)", start, end, lag)
            try:
                logs = await self.rpc.get_logs(start, end, ALL_TOPICS)
                by_block = group_by_block(logs)
                for b in range(start, end + 1):
                    await self.engine.process_block(b, by_block.get(b, []))
                    self._advance(b)  # прогресс сохраняется поблочно — после сбоя без повторов
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — бот не должен падать
                failed = self.last + 1
                n = self._failures.get(failed, 0) + 1
                self._failures[failed] = n
                if n >= rcfg.max_block_failures:
                    log.exception("блок %d: %d неудачных попыток подряд, пропускаю", failed, n)
                    self._advance(failed)
                    continue
                delay = backoff_delay(n, rcfg.retry_base_delay_sec, rcfg.retry_max_delay_sec)
                log.warning("ошибка обработки блока %d (%s: %s), попытка %d, повтор через %.1f с",
                            failed, type(exc).__name__, exc, n, delay)
                await asyncio.sleep(delay)

    def _advance(self, block: int) -> None:
        self.last = block
        self._failures.pop(block, None)
        self.db.set_last_block(block)
        if block % HEARTBEAT_BLOCKS == 0:
            tracked = sum(1 for p in self.engine.pools.pools.values() if p.tracked)
            eth = self.engine.eth.eth_usd
            log.info("работаю: блок %d, отслеживается пулов %d, ETH $%s", block, tracked,
                     f"{eth:,.2f}" if eth else "?")


async def run_replay(cfg: Config, rpc: RpcClient, engine: Engine, from_block: int, to_block: int) -> list[Alert]:
    """Прогоняет ту же логику по историческим блокам. Сигналы — только в консоль (и SQLite)."""
    await engine.start(from_block)
    batch = max(1, cfg.rpc.catchup_batch_blocks)
    total = to_block - from_block + 1
    start = from_block
    while start <= to_block:
        end = min(to_block, start + batch - 1)
        logs = await rpc.get_logs(start, end, ALL_TOPICS)
        await engine.process_range(start, end, logs)
        done = end - from_block + 1
        log.info("реплей: %d/%d блоков (%.0f%%), логов в пачке %d, сигналов всего %d",
                 done, total, done / total * 100, len(logs), len(engine.alerts))
        start = end + 1
    return engine.alerts
