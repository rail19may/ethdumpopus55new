"""Обработка блоков: логи -> пулы -> цены/ликвидность -> детектор -> уведомления."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable

from chain.events import Event, RawLog, V2Burn, V2Swap, V2Sync, V3Burn, V3Swap, decode_log
from chain.multicall import Call, Multicall, encode_call
from chain.rpc import BlockId, RpcClient, RpcError
from config import Config
from detector.detector import DumpDetector, Signal
from detector.window import SwapRecord
from notify.format import Alert
from notify.notifiers import Notifier
from pools.cache import PoolCache, PoolInfo
from pricing.eth_price import EthPriceTracker
from pricing.liquidity import V3BalanceTracker, liquidity_usd
from pricing.math import scale_amount, v2_price_from_reserves, v3_price
from storage.db import Database

log = logging.getLogger(__name__)

_SLOT0 = encode_call("slot0()")
SECONDS_PER_BLOCK = 12
GC_EVERY_BLOCKS = 1000


def group_by_block(logs: Iterable[RawLog]) -> dict[int, list[RawLog]]:
    by_block: dict[int, list[RawLog]] = defaultdict(list)
    for lg in logs:
        by_block[lg.block_number].append(lg)
    return by_block


class Engine:
    def __init__(self, cfg: Config, rpc: RpcClient, db: Database, notifier: Notifier,
                 mode: str = "live") -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.db = db
        self.notifier = notifier
        self.mode = mode
        self.mc = Multicall(rpc, cfg.rpc.multicall_chunk)
        self.pools = PoolCache(db, self.mc, cfg.factory_versions, [q.address for q in cfg.quote_tokens])
        weth = cfg.weth_address or cfg.quote_tokens[0].address
        self.eth = EthPriceTracker(cfg.eth_usd_pool, weth, cfg.quote_by_address)
        self.v3liq = V3BalanceTracker(self.mc, cfg.detector.v3_liquidity_refresh_blocks)
        self.detector = DumpDetector(cfg.detector)
        self.alerts: list[Alert] = []  # копятся только в реплее (для итоговой сводки)
        self._last_gc = 0
        self._ts_anchor: tuple[int, int] | None = None  # (block, timestamp) для оценки времени

    # --- инициализация --------------------------------------------------------
    def _resolve_at(self, block: int) -> BlockId:
        """Блок для чтения неизменяемых данных пула/токена.

        В live — конкретный номер блока (узел точно видит пул, созданный в этом блоке).
        В реплее — latest: factory/token0/decimals не меняются, а архивный узел не обязателен.
        """
        return block if self.mode == "live" else "latest"

    async def start(self, first_block: int) -> None:
        """Готовит кэш: пул ETH/USD, метаданные котируемых токенов, начальная цена ETH."""
        at = self._resolve_at(first_block)
        await self.pools.ensure_tokens([q.address for q in self.cfg.quote_tokens] +
                                       sorted(self.pools.pending_tokens), at)
        await self.pools.resolve([self.eth.pool_address], at)
        eth_pool = self.pools.get(self.eth.pool_address)
        if eth_pool is None or eth_pool.token0 is None:
            raise RuntimeError(f"пул ETH/USD {self.eth.pool_address} не распознан: проверьте eth_usd_pool и factories")
        await self._bootstrap_eth(eth_pool, first_block - 1)

    async def _bootstrap_eth(self, pool: PoolInfo, block: int) -> None:
        d0, d1 = self._decimals(pool)
        if d0 is None or d1 is None:
            return
        await self.eth.bootstrap(self.mc, pool, d0, d1, block)
        if self.eth.eth_usd is None and self.mode == "replay":
            log.warning("исторический slot0 недоступен (нужен архивный узел?) — беру цену ETH на latest")
            await self.eth.bootstrap(self.mc, pool, d0, d1, "latest")

    def _decimals(self, pool: PoolInfo) -> tuple[int | None, int | None]:
        t0, t1 = self.pools.token(pool.token0), self.pools.token(pool.token1)
        return (t0.decimals if t0 else None), (t1.decimals if t1 else None)

    # --- обработка ------------------------------------------------------------
    async def process_range(self, from_block: int, to_block: int, logs: Iterable[RawLog]) -> None:
        by_block = group_by_block(logs)
        for b in range(from_block, to_block + 1):
            await self.process_block(b, by_block.get(b, []))

    async def process_block(self, block: int, logs: list[RawLog]) -> list[Alert]:
        events: list[Event] = [ev for ev in (decode_log(lg) for lg in sorted(logs, key=lambda x: x.log_index))
                               if ev is not None]
        if not events:
            return []
        await self.pools.resolve({ev.pool for ev in events}, self._resolve_at(block))

        eth_pool = self.pools.get(self.eth.pool_address)
        if self.eth.eth_usd is None and eth_pool is not None:
            await self._bootstrap_eth(eth_pool, block - 1)

        touched: dict[str, PoolInfo] = {}
        for ev in events:
            info = self.pools.get(ev.pool)
            if info is not None and info.tracked:
                touched[info.address] = info
        await self._prime_v3(block, [p for p in touched.values()
                                     if p.version == "v3" and not self.detector.has_price(p.address)])

        last_sync: dict[str, V2Sync] = {}
        v3_burned: set[str] = set()
        for ev in events:
            info = self.pools.get(ev.pool)
            if info is None:
                continue
            if info.address == self.eth.pool_address:
                d0, d1 = self._decimals(info)
                if d0 is not None and d1 is not None:
                    self.eth.on_event(ev, info, d0, d1)
                continue
            if not info.tracked:
                continue
            try:
                self._apply_event(ev, info, last_sync, v3_burned)
            except Exception:  # noqa: BLE001 — один кривой пул не должен ломать блок
                log.exception("ошибка обработки события %s в пуле %s", type(ev).__name__, info.address)

        await self._refresh_v3_liquidity(block, touched, v3_burned)

        out: list[Alert] = []
        for addr, info in touched.items():
            if not self.detector.is_candidate(addr, block):
                continue
            ts = await self._block_ts(block)
            signal = self.detector.evaluate(addr, block, ts)
            if signal is not None:
                alert = await self._emit(signal, info)
                if alert is not None:
                    out.append(alert)

        if block - self._last_gc >= GC_EVERY_BLOCKS:
            self._last_gc = block
            for pool in [p for p in self.v3liq.refreshed_at if p not in self.detector.states]:
                self.v3liq.forget(pool)
            removed = self.detector.gc(block)
            if removed:
                log.debug("GC: выгружено %d неактивных пулов", removed)
        return out

    def _apply_event(self, ev: Event, info: PoolInfo, last_sync: dict[str, V2Sync],
                     v3_burned: set[str]) -> None:
        d0, d1 = self._decimals(info)
        if d0 is None or d1 is None:
            return
        pool = info.address
        t0 = info.target_is_token0
        dq = d1 if t0 else d0
        quote_usd = self.eth.quote_usd(info.quote)

        if isinstance(ev, V2Sync):
            price = v2_price_from_reserves(ev.reserve0, ev.reserve1, d0, d1, t0)
            if price:
                self.detector.record_price(pool, ev.block, price)
            if quote_usd:
                reserve_q = ev.reserve1 if t0 else ev.reserve0
                self.detector.record_liquidity(pool, ev.block, liquidity_usd(scale_amount(reserve_q, dq), quote_usd))
            last_sync[pool] = ev

        elif isinstance(ev, V2Swap):
            pre = post = None
            sync = last_sync.get(pool)
            # В UniswapV2Pair.swap() Sync эмитится непосредственно перед Swap в той же транзакции,
            # поэтому резервы ДО свопа восстанавливаются точно.
            if sync is not None and sync.tx_hash == ev.tx_hash and sync.log_index == ev.log_index - 1:
                r0 = sync.reserve0 - ev.amount0_in + ev.amount0_out
                r1 = sync.reserve1 - ev.amount1_in + ev.amount1_out
                if r0 > 0 and r1 > 0:
                    pre = v2_price_from_reserves(r0, r1, d0, d1, t0)
                    if pre:
                        self.detector.record_pre_price(pool, ev.block, pre)
                    # и ликвидность до свопа: продажа уменьшает резерв котируемого токена, и если бот
                    # видит пул впервые (например, после часа без сделок и перезапуска), иначе он знал бы
                    # только ликвидность «после» и мог отсечь дамп по порогу MIN_LIQUIDITY_USD
                    if quote_usd:
                        reserve_q = r1 if t0 else r0
                        self.detector.record_pre_liquidity(pool, ev.block,
                                                           liquidity_usd(scale_amount(reserve_q, dq), quote_usd))
                post = v2_price_from_reserves(sync.reserve0, sync.reserve1, d0, d1, t0)
            target_in = ev.amount0_in if t0 else ev.amount1_in
            quote_out = ev.amount1_out if t0 else ev.amount0_out
            sell_usd = None
            if target_in > 0 and quote_out > 0 and quote_usd:
                sell_usd = scale_amount(quote_out, dq) * quote_usd
            self.detector.record_swap(pool, SwapRecord(ev.block, ev.tx_hash, ev.log_index, ev.to,
                                                       sell_usd, pre, post))

        elif isinstance(ev, V3Swap):
            before = self.detector.current_price(pool)
            price = v3_price(ev.sqrt_price_x96, d0, d1, t0)
            if price:
                self.detector.record_price(pool, ev.block, price)
            amount_target = ev.amount0 if t0 else ev.amount1
            amount_quote = ev.amount1 if t0 else ev.amount0
            sell_usd = None
            # amount > 0 — токен пришёл в пул, < 0 — ушёл из пула
            if amount_target > 0 and amount_quote < 0 and quote_usd:
                sell_usd = scale_amount(-amount_quote, dq) * quote_usd
            self.detector.record_swap(pool, SwapRecord(ev.block, ev.tx_hash, ev.log_index, ev.recipient,
                                                       sell_usd, before, price))

        elif isinstance(ev, V2Burn):
            if ev.amount0 > 0 or ev.amount1 > 0:
                self.detector.record_burn(pool, ev.block)

        elif isinstance(ev, V3Burn):
            if ev.amount > 0:  # Burn с amount=0 — это «poke» для начисления комиссий
                self.detector.record_burn(pool, ev.block)
                v3_burned.add(pool)

    async def _prime_v3(self, block: int, pools: list[PoolInfo]) -> None:
        """Для V3-пулов, встреченных впервые, берём состояние на конец предыдущего блока:
        цену (slot0) и баланс котируемого токена. Иначе первый же своп-дамп (или вывод
        ликвидности) в этом блоке не с чем было бы сравнить."""
        if not pools:
            return
        calls: list[Call] = []
        for p in pools:
            calls.append(Call(p.address, _SLOT0))
            calls.append(Call(p.quote or "", encode_call("balanceOf(address)", ["address"], [p.address])))
        try:
            res = await self.mc.call(calls, block - 1)
        except RpcError as exc:
            log.debug("состояние V3-пулов на блоке %d недоступно: %s", block - 1, exc)
            return
        for n, p in enumerate(pools):
            r_slot0, r_bal = res[2 * n], res[2 * n + 1]
            if not r_slot0.success or len(r_slot0.data) < 32:
                continue  # пул создан в этом блоке
            d0, d1 = self._decimals(p)
            if d0 is None or d1 is None:
                continue
            price = v3_price(int.from_bytes(r_slot0.data[:32], "big"), d0, d1, p.target_is_token0)
            if price:
                self.detector.record_initial_price(p.address, block - 1, price)
            quote_usd = self.eth.quote_usd(p.quote or "")
            if r_bal.success and len(r_bal.data) >= 32 and quote_usd:
                amount = scale_amount(int.from_bytes(r_bal.data[:32], "big"), d1 if p.target_is_token0 else d0)
                self.detector.record_liquidity(p.address, block - 1, liquidity_usd(amount, quote_usd))

    async def _refresh_v3_liquidity(self, block: int, touched: dict[str, PoolInfo], burned: set[str]) -> None:
        need: list[tuple[PoolInfo, int]] = []
        for addr, info in touched.items():
            if info.version != "v3":
                continue
            if addr in burned or self.v3liq.due(addr, block) or self.detector.is_candidate(addr, block):
                tq = self.pools.token(info.quote)
                if tq and tq.decimals is not None:
                    need.append((info, tq.decimals))
        for addr in await self.v3liq.refresh(need, block, block):
            usd = self.eth.quote_usd(touched[addr].quote)
            if usd:
                self.detector.record_liquidity(addr, block, liquidity_usd(self.v3liq.quote_amount[addr], usd))

    async def _block_ts(self, block: int) -> int:
        try:
            ts = await self.rpc.block_timestamp(block)
            self._ts_anchor = (block, ts)
            return ts
        except RpcError:
            if self._ts_anchor is not None:
                b0, t0 = self._ts_anchor
                return t0 + (block - b0) * SECONDS_PER_BLOCK
            return int(time.time())

    async def _emit(self, sig: Signal, info: PoolInfo) -> Alert | None:
        token = self.pools.token(info.target)
        quote = self.pools.token(info.quote)
        quote_cfg = self.cfg.quote_by_address.get(info.quote or "")
        quote_usd = self.eth.quote_usd(info.quote or "")
        main = sig.main_swap
        seller = None
        if main is not None:
            seller = await self.rpc.tx_sender(main.tx_hash) or main.trader
        dex = self.cfg.factory_names.get(info.factory or "", info.version or "?")
        if info.version == "v3" and info.fee is not None:
            dex += f" ({info.fee / 10_000:.2f}%)"
        alert = Alert(
            mode=self.mode, block=sig.block, timestamp=sig.timestamp, pool=info.address, dex=dex,
            token=info.target or "", token_symbol=token.symbol if token else "???",
            token_name=token.name if token else "???",
            quote_symbol=quote_cfg.symbol if quote_cfg else (quote.symbol if quote else "?"),
            drop_pct=sig.drop_pct, price_before=sig.price_before, price_after=sig.price_after,
            price_before_usd=sig.price_before * quote_usd if quote_usd else None,
            price_after_usd=sig.price_after * quote_usd if quote_usd else None,
            liquidity_usd=sig.liquidity_usd, liquidity_max_usd=sig.liquidity_max_usd,
            rugpull=sig.rugpull, main_tx=main.tx_hash if main else None, seller=seller,
            sell_usd=main.sell_usd if main else None,
        )
        log.info("ДАМП %s %s −%.1f%% блок %d пул %s%s", alert.token_symbol, alert.dex, alert.drop_pct,
                 alert.block, alert.pool, " [рагпул?]" if alert.rugpull else "")
        try:
            self.db.insert_alert(alert.to_row())
        except Exception:  # noqa: BLE001
            log.exception("не удалось записать алерт в SQLite")
        if self.mode == "replay":
            self.alerts.append(alert)
        await self.notifier.send(alert)
        return alert
