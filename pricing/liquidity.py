"""Оценка ликвидности пулов в USD.

V2: 2 × резерв котируемого токена × его цена в USD (резервы — из Sync, без RPC).
V3: 2 × balanceOf(pool) котируемого токена × цена в USD (грубо), обновление не чаще раза в N блоков.
"""
from __future__ import annotations

import logging
from typing import Iterable

from chain.multicall import Call, Multicall, encode_call
from chain.rpc import BlockId, RpcError
from pools.cache import PoolInfo

from .math import scale_amount

log = logging.getLogger(__name__)


def liquidity_usd(quote_amount: float, quote_usd: float) -> float:
    return 2.0 * quote_amount * quote_usd


class V3BalanceTracker:
    def __init__(self, multicall: Multicall, refresh_blocks: int = 10) -> None:
        self.mc = multicall
        self.refresh_blocks = max(1, refresh_blocks)
        self.quote_amount: dict[str, float] = {}  # pool -> баланс котируемого токена
        self.refreshed_at: dict[str, int] = {}

    def due(self, pool: str, block: int) -> bool:
        last = self.refreshed_at.get(pool)
        return last is None or block - last >= self.refresh_blocks

    def forget(self, pool: str) -> None:
        self.quote_amount.pop(pool, None)
        self.refreshed_at.pop(pool, None)

    async def refresh(self, pools: Iterable[tuple[PoolInfo, int]], block: BlockId, block_number: int) -> list[str]:
        """pools: (пул, decimals котируемого токена). Возвращает адреса обновлённых пулов.

        Ошибки RPC не пробрасываются — ликвидность просто остаётся старой.
        """
        items = list(pools)
        if not items:
            return []
        calls = [Call(p.quote, encode_call("balanceOf(address)", ["address"], [p.address])) for p, _ in items]
        try:
            res = await self.mc.call(calls, block)
        except RpcError as exc:
            log.warning("balanceOf для %d V3-пулов на блоке %s не удался: %s", len(items), block, exc)
            return []
        updated = []
        for (pool, dec), r in zip(items, res):
            if r.success and len(r.data) >= 32:
                self.quote_amount[pool.address] = scale_amount(int.from_bytes(r.data[:32], "big"), dec)
                self.refreshed_at[pool.address] = block_number
                updated.append(pool.address)
        return updated
