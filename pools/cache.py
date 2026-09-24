"""Кэш пулов: определение фабрики, токенов, фильтрация по разрешённым фабрикам и котируемым токенам."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from chain.multicall import Call, CallResult, Multicall, encode_call
from chain.rpc import BlockId
from storage.db import Database

from .tokens import TokenInfo, decode_address, decode_decimals, decode_text, decode_uint

log = logging.getLogger(__name__)

TRACKED = "tracked"              # целевой токен + котируемый токен
QUOTE_PAIR = "quote_pair"        # оба токена котируемые (WETH/USDC) — только для цены ETH
IGNORED_FACTORY = "ignored_factory"    # фабрика не из списка / не пул вовсе
IGNORED_FAKE = "ignored_fake"          # factory() врёт: фабрика не знает такого пула
IGNORED_NO_QUOTE = "ignored_no_quote"  # ни одного котируемого токена
IGNORED_BROKEN = "ignored_broken"      # у целевого токена не читается decimals()

MAX_V3_FEE = 1 << 24  # fee в Uniswap V3 — uint24

_FACTORY = encode_call("factory()")
_TOKEN0 = encode_call("token0()")
_TOKEN1 = encode_call("token1()")
_FEE = encode_call("fee()")
_SYMBOL = encode_call("symbol()")
_NAME = encode_call("name()")
_DECIMALS = encode_call("decimals()")


@dataclass(slots=True)
class PoolInfo:
    address: str
    status: str
    version: str | None = None
    factory: str | None = None
    token0: str | None = None
    token1: str | None = None
    fee: int | None = None
    # вычисляемые поля (не хранятся в БД)
    quote: str | None = None
    target: str | None = None

    @property
    def tracked(self) -> bool:
        return self.status == TRACKED

    @property
    def target_is_token0(self) -> bool:
        return self.target == self.token0


class PoolCache:
    def __init__(self, db: Database, multicall: Multicall, factories: dict[str, str],
                 quote_tokens: Iterable[str]) -> None:
        self.db = db
        self.mc = multicall
        self.factories = {k.lower(): v for k, v in factories.items()}
        self.quotes = {q.lower() for q in quote_tokens}
        self.pools: dict[str, PoolInfo] = {}
        self.tokens: dict[str, TokenInfo] = {}
        self.pending_tokens: set[str] = set()  # токены без метаданных (после смены конфига)
        for row in db.load_tokens():
            self.tokens[row["address"]] = TokenInfo(row["address"], row["symbol"] or "?",
                                                    row["name"] or "?", row["decimals"])
        for row in db.load_pools():
            info = PoolInfo(row["address"], row["status"], row["version"], row["factory"],
                            row["token0"], row["token1"], row["fee"])
            if info.status == IGNORED_FACTORY and info.factory in self.factories:
                continue  # фабрику добавили в конфиг — пул будет определён заново
            # список фабрик/котируемых токенов мог поменяться — пересматриваем статус
            self._classify(info)
            if info.status in (TRACKED, QUOTE_PAIR):
                self.pending_tokens.update(t for t in (info.token0, info.token1) if t not in self.tokens)
            self.pools[info.address] = info

    def get(self, address: str) -> PoolInfo | None:
        return self.pools.get(address)

    def token(self, address: str | None) -> TokenInfo | None:
        return self.tokens.get(address) if address else None

    def unknown(self, addresses: Iterable[str]) -> list[str]:
        return sorted({a for a in addresses if a not in self.pools})

    def _classify(self, info: PoolInfo) -> None:
        """Выставляет статус по фабрике и котируемым токенам (без RPC)."""
        if info.status == IGNORED_FAKE or info.token0 is None or info.token1 is None:
            return
        if not info.factory or info.factory not in self.factories:
            info.status = IGNORED_FACTORY
            return
        info.version = self.factories[info.factory]
        q0, q1 = info.token0 in self.quotes, info.token1 in self.quotes
        if q0 and q1:
            info.status, info.quote, info.target = QUOTE_PAIR, None, None
        elif not q0 and not q1:
            info.status = IGNORED_NO_QUOTE
        else:
            info.quote = info.token0 if q0 else info.token1
            info.target = info.token1 if q0 else info.token0
            tok = self.tokens.get(info.target)
            if tok is not None and tok.decimals is None:
                info.status = IGNORED_BROKEN
            else:
                info.status = TRACKED

    async def _calls_with_fallback(self, calls: list[Call], required: set[int],
                                   block: BlockId) -> list[CallResult]:
        """Multicall; обязательные вызовы, упавшие внутри multicall, перепроверяем по одному
        (защита от контрактов, съедающих весь газ пачки)."""
        results = await self.mc.call(calls, block)
        for i in required:
            if not results[i].success:
                results[i] = await self.mc.call_single(calls[i], block)
        return results

    async def resolve(self, addresses: Iterable[str], block: BlockId) -> list[PoolInfo]:
        """Определяет и кэширует пулы по адресам, которых ещё нет в кэше.

        Ошибки сети (RpcError) пробрасываются — пул НЕ помечается как игнор, блок будет
        обработан повторно.
        """
        todo = self.unknown(addresses)
        if not todo:
            return []

        # 1) factory/token0/token1/fee каждого кандидата
        calls: list[Call] = []
        for a in todo:
            calls += [Call(a, _FACTORY), Call(a, _TOKEN0), Call(a, _TOKEN1), Call(a, _FEE)]
        required = {i for i in range(len(calls)) if i % 4 == 0}  # factory() — ключевой вызов
        res = await self._calls_with_fallback(calls, required, block)

        infos: list[PoolInfo] = []
        for n, a in enumerate(todo):
            r_factory, r_t0, r_t1, r_fee = res[4 * n: 4 * n + 4]
            factory = decode_address(r_factory.data) if r_factory.success else None
            info = PoolInfo(a, IGNORED_FACTORY, factory=factory)
            if factory in self.factories:
                t0 = decode_address(r_t0.data) if r_t0.success else None
                t1 = decode_address(r_t1.data) if r_t1.success else None
                info.version = self.factories[factory]
                # fee() отдаёт недоверенный контракт: у V3 это uint24, у V2 его нет вовсе.
                # Мусорное значение не должно дойти до ABI-кодирования getPool и до SQLite.
                fee = decode_uint(r_fee.data) if r_fee.success and info.version == "v3" else None
                if fee is not None and fee >= MAX_V3_FEE:
                    fee = None
                info.token0, info.token1, info.fee = t0, t1, fee
                if t0 is None or t1 is None or t0 == t1 or (info.version == "v3" and fee is None):
                    info.status = IGNORED_FAKE
            infos.append(info)

        # 2) сверка с фабрикой: getPair / getPool должны вернуть этот же адрес
        verify = [i for i in infos if i.factory in self.factories and i.status != IGNORED_FAKE]
        if verify:
            vcalls = []
            for i in verify:
                if i.version == "v2":
                    data = encode_call("getPair(address,address)", ["address", "address"], [i.token0, i.token1])
                else:
                    data = encode_call("getPool(address,address,uint24)", ["address", "address", "uint24"],
                                       [i.token0, i.token1, i.fee])
                vcalls.append(Call(i.factory, data))
            vres = await self._calls_with_fallback(vcalls, set(range(len(vcalls))), block)
            for i, r in zip(verify, vres):
                if not r.success or decode_address(r.data) != i.address:
                    i.status = IGNORED_FAKE
                else:
                    i.status = TRACKED  # предварительно, уточнит _classify

        # 3) метаданные новых токенов (только для пулов с котируемым токеном)
        need_tokens: set[str] = set()
        for i in infos:
            if i.status == TRACKED and (i.token0 in self.quotes or i.token1 in self.quotes):
                need_tokens.update(t for t in (i.token0, i.token1) if t not in self.tokens)
        if need_tokens:
            await self._resolve_tokens(sorted(need_tokens), block)

        for i in infos:
            if i.status == TRACKED:
                self._classify(i)
            self.pools[i.address] = i
        self.db.save_pools([(i.address, i.status, i.version, i.factory, i.token0, i.token1, i.fee)
                            for i in infos])
        tracked = sum(1 for i in infos if i.status in (TRACKED, QUOTE_PAIR))
        log.debug("пулы: новых %d, отслеживаются %d", len(infos), tracked)
        return infos

    async def _resolve_tokens(self, tokens: list[str], block: BlockId) -> None:
        calls: list[Call] = []
        for t in tokens:
            calls += [Call(t, _SYMBOL), Call(t, _NAME), Call(t, _DECIMALS)]
        required = {i for i in range(len(calls)) if i % 3 == 2}  # decimals()
        res = await self._calls_with_fallback(calls, required, block)
        rows = []
        for n, t in enumerate(tokens):
            r_sym, r_name, r_dec = res[3 * n: 3 * n + 3]
            symbol = (decode_text(r_sym.data) if r_sym.success else None) or "???"
            name = (decode_text(r_name.data) if r_name.success else None) or symbol
            decimals = decode_decimals(r_dec.data) if r_dec.success else None
            info = TokenInfo(t, symbol, name, decimals)
            self.tokens[t] = info
            rows.append((t, symbol, name, decimals))
        self.db.save_tokens(rows)

    async def ensure_tokens(self, tokens: Iterable[str], block: BlockId = "latest") -> None:
        missing = sorted({t for t in tokens if t not in self.tokens})
        if missing:
            await self._resolve_tokens(missing, block)
