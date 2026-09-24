"""Математика цен Uniswap V2/V3.

Все функции возвращают цену «сколько котируемого токена за 1 целевой» (с учётом decimals)
или None, если посчитать нельзя (нулевые резервы, переполнение и т.п.).
Внутри — точная рациональная арифметика (Fraction), в float переводим только в конце.
"""
from __future__ import annotations

from fractions import Fraction

Q96 = 1 << 96
Q192 = 1 << 192


def _to_float(x: Fraction) -> float | None:
    try:
        v = float(x)
    except (OverflowError, ZeroDivisionError):
        return None
    return v if v > 0 else None


def scale_amount(raw: int, decimals: int) -> float:
    """Сырое целое количество токена -> человекочитаемое (raw / 10**decimals)."""
    try:
        return float(Fraction(raw, 10 ** decimals))
    except OverflowError:
        return float("inf")


def v2_price(reserve_target: int, reserve_quote: int, dec_target: int, dec_quote: int) -> float | None:
    """Цена целевого токена в котируемом по резервам пары V2 (событие Sync).

    price = (reserve_quote / 10**dec_quote) / (reserve_target / 10**dec_target)
    """
    if reserve_target <= 0 or reserve_quote <= 0:
        return None
    return _to_float(Fraction(reserve_quote * 10 ** dec_target, reserve_target * 10 ** dec_quote))


def v2_price_from_reserves(reserve0: int, reserve1: int, dec0: int, dec1: int,
                           target_is_token0: bool) -> float | None:
    if target_is_token0:
        return v2_price(reserve0, reserve1, dec0, dec1)
    return v2_price(reserve1, reserve0, dec1, dec0)


def v3_price_token0_in_token1_exact(sqrt_price_x96: int, dec0: int, dec1: int) -> Fraction | None:
    """price(token0 в token1) = (sqrtPriceX96 / 2**96)**2 * 10**(dec0 - dec1)."""
    if sqrt_price_x96 <= 0:
        return None
    raw = Fraction(sqrt_price_x96 * sqrt_price_x96, Q192)
    return raw * Fraction(10) ** (dec0 - dec1)


def v3_price(sqrt_price_x96: int, dec0: int, dec1: int, target_is_token0: bool) -> float | None:
    """Цена целевого токена в котируемом по sqrtPriceX96 (событие Swap / slot0)."""
    p = v3_price_token0_in_token1_exact(sqrt_price_x96, dec0, dec1)
    if p is None:
        return None
    return _to_float(p if target_is_token0 else 1 / p)


def drop_pct(price_before: float, price_after: float) -> float:
    """Падение в процентах (положительное число при снижении цены)."""
    if price_before <= 0:
        return 0.0
    return (price_before - price_after) / price_before * 100.0
