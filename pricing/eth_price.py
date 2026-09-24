"""Цена ETH в USD из пула WETH/USDC и USD-цены котируемых токенов."""
from __future__ import annotations

import logging

from chain.events import Event, V2Sync, V3Swap
from chain.multicall import Call, Multicall, encode_call
from chain.rpc import BlockId, RpcError
from config import QuoteTokenCfg
from pools.cache import PoolInfo

from .math import v2_price_from_reserves, v3_price

log = logging.getLogger(__name__)

_SLOT0 = encode_call("slot0()")
_GET_RESERVES = encode_call("getReserves()")


class EthPriceTracker:
    """Следит за пулом WETH/стейбл и держит актуальную цену ETH в USD.

    Стейбл во второй стороне пула считается равным своей USD-цене из конфига (для USDC — 1.0).
    """

    def __init__(self, pool_address: str, weth: str, quotes: dict[str, QuoteTokenCfg]) -> None:
        self.pool_address = pool_address.lower()
        self.weth = weth.lower()
        self.quotes = quotes
        self.eth_usd: float | None = None
        self.updated_block: int | None = None

    def _stable_usd(self, pool: PoolInfo) -> float:
        other = pool.token1 if pool.token0 == self.weth else pool.token0
        cfg = self.quotes.get(other or "")
        return float(cfg.usd) if cfg and cfg.usd != "eth" else 1.0

    def price_from_sqrt(self, pool: PoolInfo, sqrt_price_x96: int, dec0: int, dec1: int) -> float | None:
        p = v3_price(sqrt_price_x96, dec0, dec1, target_is_token0=pool.token0 == self.weth)
        return p * self._stable_usd(pool) if p else None

    def on_event(self, ev: Event, pool: PoolInfo, dec0: int, dec1: int) -> None:
        price = None
        if isinstance(ev, V3Swap):
            price = self.price_from_sqrt(pool, ev.sqrt_price_x96, dec0, dec1)
        elif isinstance(ev, V2Sync):
            p = v2_price_from_reserves(ev.reserve0, ev.reserve1, dec0, dec1, pool.token0 == self.weth)
            price = p * self._stable_usd(pool) if p else None
        if price:
            self.eth_usd = price
            self.updated_block = ev.block

    async def bootstrap(self, mc: Multicall, pool: PoolInfo, dec0: int, dec1: int, block: BlockId) -> None:
        """Начальная цена из slot0()/getReserves() — до первого свопа в пуле."""
        try:
            if pool.version == "v3":
                r = await mc.call_single(Call(pool.address, _SLOT0), block)
                if r.success and len(r.data) >= 32:
                    sqrt_price = int.from_bytes(r.data[:32], "big")
                    self.eth_usd = self.price_from_sqrt(pool, sqrt_price, dec0, dec1) or self.eth_usd
            else:
                r = await mc.call_single(Call(pool.address, _GET_RESERVES), block)
                if r.success and len(r.data) >= 64:
                    r0 = int.from_bytes(r.data[:32], "big")
                    r1 = int.from_bytes(r.data[32:64], "big")
                    p = v2_price_from_reserves(r0, r1, dec0, dec1, pool.token0 == self.weth)
                    if p:
                        self.eth_usd = p * self._stable_usd(pool)
        except RpcError as exc:
            log.warning("не удалось получить начальную цену ETH на блоке %s: %s", block, exc)
        if self.eth_usd:
            log.info("цена ETH: $%.2f (блок %s)", self.eth_usd, block)

    def quote_usd(self, quote: str) -> float | None:
        """USD-цена котируемого токена."""
        cfg = self.quotes.get(quote)
        if cfg is None:
            return None
        if cfg.usd == "eth":
            return self.eth_usd
        return float(cfg.usd)
