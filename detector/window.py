"""Скользящие окна цен/ликвидности по блокам для одного пула."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


class BlockSeries:
    """Значения в последних `window` блоках + значение, действовавшее на начало окна (carry).

    carry нужен для редко торгуемых пулов: если прошлая сделка была 100 блоков назад,
    именно её цена — «цена до» для текущего дампа.

    Точки с pre=True — промежуточные значения (цена ДО свопа внутри блока): участвуют в максимуме,
    но не становятся текущим значением и не переносятся в carry.
    """

    __slots__ = ("points", "carry", "current")

    def __init__(self) -> None:
        self.points: deque[tuple[int, float, bool]] = deque()
        self.carry: float | None = None
        self.current: float | None = None

    def add(self, block: int, value: float) -> None:
        self.points.append((block, value, False))
        self.current = value

    def add_pre(self, block: int, value: float) -> None:
        self.points.append((block, value, True))

    def prune(self, block: int, window: int) -> None:
        """Оставляет точки блоков (block - window, block]."""
        limit = block - window
        pts = self.points
        while pts and pts[0][0] <= limit:
            _, value, pre = pts.popleft()
            if not pre:
                self.carry = value

    def max(self) -> float | None:
        values = [v for _, v, _ in self.points]
        if self.carry is not None:
            values.append(self.carry)
        return max(values) if values else None

    def __bool__(self) -> bool:
        return self.current is not None or self.carry is not None


@dataclass(slots=True)
class SwapRecord:
    block: int
    tx_hash: str
    log_index: int
    trader: str | None          # получатель из события (запасной вариант «продавца»)
    sell_usd: float | None      # сколько котируемого токена (в USD) ушло из пула; None — это покупка
    price_before: float | None
    price_after: float | None

    @property
    def impact(self) -> float:
        """Вклад в падение: во сколько раз своп уронил цену (1.0 — не уронил)."""
        if not self.price_before or not self.price_after:
            return 1.0
        return self.price_before / self.price_after


@dataclass(slots=True)
class PoolState:
    price: BlockSeries = field(default_factory=BlockSeries)
    liquidity: BlockSeries = field(default_factory=BlockSeries)
    swaps: deque[SwapRecord] = field(default_factory=deque)
    burns: deque[int] = field(default_factory=deque)
    last_alert_ts: int | None = None
    last_block: int = 0

    def prune(self, block: int, window: int) -> None:
        self.price.prune(block, window)
        self.liquidity.prune(block, window)
        limit = block - window
        while self.swaps and self.swaps[0].block <= limit:
            self.swaps.popleft()
        while self.burns and self.burns[0] <= limit:
            self.burns.popleft()
