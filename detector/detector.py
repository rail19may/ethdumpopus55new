"""Правила детекта дампа, кулдауны, пометка рагпула и поиск главного свопа."""
from __future__ import annotations

from dataclasses import dataclass

from config import DetectorCfg
from pricing.math import drop_pct

from .window import PoolState, SwapRecord


@dataclass(slots=True)
class Signal:
    pool: str
    block: int
    timestamp: int | None
    drop_pct: float
    price_before: float        # максимум цены за окно (в котируемом токене)
    price_after: float         # текущая цена
    liquidity_usd: float       # текущая ликвидность
    liquidity_max_usd: float   # максимум ликвидности за окно
    rugpull: bool
    burns_in_window: int
    main_swap: SwapRecord | None


class DumpDetector:
    def __init__(self, cfg: DetectorCfg) -> None:
        self.cfg = cfg
        self.states: dict[str, PoolState] = {}

    # --- запись данных -----------------------------------------------------
    def state(self, pool: str) -> PoolState:
        st = self.states.get(pool)
        if st is None:
            st = self.states[pool] = PoolState()
        return st

    def has_price(self, pool: str) -> bool:
        st = self.states.get(pool)
        return bool(st and st.price)

    def current_price(self, pool: str) -> float | None:
        st = self.states.get(pool)
        return st.price.current if st else None

    def _touch(self, pool: str, block: int) -> PoolState:
        st = self.state(pool)
        st.last_block = max(st.last_block, block)
        return st

    def record_price(self, pool: str, block: int, price: float) -> None:
        self._touch(pool, block).price.add(block, price)

    def record_pre_price(self, pool: str, block: int, price: float) -> None:
        """Цена непосредственно перед свопом (реконструирована из события)."""
        st = self._touch(pool, block)
        st.price.add_pre(block, price)
        if st.price.current is None:
            st.price.current = price

    def record_initial_price(self, pool: str, block: int, price: float) -> None:
        """Цена на конец предыдущего блока (slot0 при первой встрече пула)."""
        st = self.state(pool)
        if st.price.current is None:
            st.price.add(block, price)

    def record_swap(self, pool: str, rec: SwapRecord) -> None:
        self._touch(pool, rec.block).swaps.append(rec)

    def record_burn(self, pool: str, block: int) -> None:
        self._touch(pool, block).burns.append(block)

    def record_liquidity(self, pool: str, block: int, usd: float) -> None:
        self.state(pool).liquidity.add(block, usd)

    # --- правила -----------------------------------------------------------
    def current_drop(self, pool: str, block: int) -> float:
        st = self.states.get(pool)
        if st is None:
            return 0.0
        st.prune(block, self.cfg.window_blocks)
        top, cur = st.price.max(), st.price.current
        if not top or not cur:
            return 0.0
        return drop_pct(top, cur)

    def is_candidate(self, pool: str, block: int) -> bool:
        """Цена упала на DROP_PCT+ от максимума окна (остальные условия ещё не проверены)."""
        return self.current_drop(pool, block) >= self.cfg.drop_pct

    def in_cooldown(self, pool: str, now_ts: int) -> bool:
        st = self.states.get(pool)
        if st is None or st.last_alert_ts is None:
            return False
        return now_ts - st.last_alert_ts < self.cfg.cooldown_min * 60

    def evaluate(self, pool: str, block: int, now_ts: int) -> Signal | None:
        """Проверяет все правила. При срабатывании ставит кулдаун и возвращает Signal."""
        drop = self.current_drop(pool, block)
        if drop < self.cfg.drop_pct:
            return None
        st = self.states[pool]
        liq_cur, liq_max = st.liquidity.current, st.liquidity.max()
        if liq_cur is None or liq_max is None:
            return None
        # Порог ликвидности сравниваем с максимумом окна: сама продажа (и тем более рагпул)
        # уменьшает ликвидность, а важно, насколько значимым пул был до дампа.
        if liq_max < self.cfg.min_liquidity_usd:
            return None
        if self.in_cooldown(pool, now_ts):
            return None

        liq_drop = drop_pct(liq_max, liq_cur) if liq_max > 0 else 0.0
        rugpull = bool(st.burns) and liq_drop > self.cfg.rugpull_liquidity_drop_pct

        st.last_alert_ts = now_ts
        return Signal(pool=pool, block=block, timestamp=now_ts, drop_pct=drop,
                      price_before=st.price.max() or 0.0, price_after=st.price.current or 0.0,
                      liquidity_usd=liq_cur, liquidity_max_usd=liq_max, rugpull=rugpull,
                      burns_in_window=len(st.burns), main_swap=self.main_swap(st))

    @staticmethod
    def main_swap(st: PoolState) -> SwapRecord | None:
        """Своп с наибольшим вкладом в падение (максимальное отношение цены до/после)."""
        sells = [s for s in st.swaps if s.sell_usd is not None]
        best = max(st.swaps, key=lambda s: (s.impact, s.sell_usd or 0.0), default=None)
        if best is not None and best.impact > 1.0:
            return best
        return max(sells, key=lambda s: s.sell_usd or 0.0, default=None)

    # --- обслуживание ------------------------------------------------------
    def gc(self, block: int) -> int:
        """Удаляет состояния пулов без активности за state_gc_blocks (кроме активных кулдаунов)."""
        limit = block - self.cfg.state_gc_blocks
        stale = [p for p, st in self.states.items() if st.last_block < limit and st.last_alert_ts is None]
        for p in stale:
            del self.states[p]
        return len(stale)
