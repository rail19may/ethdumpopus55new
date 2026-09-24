import math
from fractions import Fraction

import pytest

from pricing.math import (Q96, drop_pct, scale_amount, v2_price, v2_price_from_reserves, v3_price,
                          v3_price_token0_in_token1_exact)
from pricing.liquidity import liquidity_usd

# Эталон из «A Primer on Uniswap v3 Math» (пул USDC/WETH 0.05%, token0=USDC(6), token1=WETH(18)):
# sqrtPriceX96 = 2018382873588440326581633304624437 -> raw price 649004842.70137 -> 1 ETH = 1540.82 USDC
PRIMER_SQRT = 2018382873588440326581633304624437


class TestV2:
    def test_usdc_weth_pair(self):
        # USDC/WETH V2: token0=USDC(6), token1=WETH(18). 30M USDC / 10k WETH -> 1 WETH = 3000 USDC
        r_usdc = 30_000_000 * 10**6
        r_weth = 10_000 * 10**18
        assert v2_price_from_reserves(r_usdc, r_weth, 6, 18, target_is_token0=False) == pytest.approx(3000.0)
        assert v2_price_from_reserves(r_usdc, r_weth, 6, 18, target_is_token0=True) == pytest.approx(1 / 3000)

    def test_target_18_quote_18(self):
        # 1 000 000 TOKEN против 50 WETH -> 0.00005 WETH за токен
        assert v2_price(1_000_000 * 10**18, 50 * 10**18, 18, 18) == pytest.approx(5e-5)

    def test_decimals_mismatch(self):
        # 9 decimals у токена, 6 у USDT: 2e6 TOKEN и 1e6 USDT -> 0.5 USDT
        assert v2_price(2_000_000 * 10**9, 1_000_000 * 10**6, 9, 6) == pytest.approx(0.5)

    def test_zero_reserves(self):
        assert v2_price(0, 10, 18, 18) is None
        assert v2_price(10, 0, 18, 18) is None

    def test_exactness_small_prices(self):
        # очень маленькая цена (мемкоины): 1e15 токенов против 1 WETH
        p = v2_price(10**15 * 10**18, 10**18, 18, 18)
        assert p == pytest.approx(1e-15, rel=1e-12)

    def test_constant_product_sell_drop(self):
        # продажа 25% резерва токена в пул x*y=k (без комиссии) роняет цену в 1.5625 раза (-36%)
        x, y = 1_000_000 * 10**18, 100 * 10**18
        before = v2_price(x, y, 18, 18)
        x2 = x + x // 4
        y2 = x * y // x2
        after = v2_price(x2, y2, 18, 18)
        assert drop_pct(before, after) == pytest.approx(36.0, abs=1e-6)


class TestV3:
    def test_primer_raw_price(self):
        raw = v3_price_token0_in_token1_exact(PRIMER_SQRT, 0, 0)
        assert float(raw) == pytest.approx(649004842.70137, rel=1e-12)

    def test_primer_eth_price(self):
        # целевой (для цены ETH) — WETH = token1, котируемый — USDC = token0
        assert v3_price(PRIMER_SQRT, 6, 18, target_is_token0=False) == pytest.approx(1540.8205520, rel=1e-9)
        # и обратная ориентация: цена USDC в WETH
        assert v3_price(PRIMER_SQRT, 6, 18, target_is_token0=True) == pytest.approx(1 / 1540.8205520, rel=1e-9)

    def test_price_one(self):
        assert v3_price(Q96, 18, 18, True) == pytest.approx(1.0)
        assert v3_price(Q96, 18, 18, False) == pytest.approx(1.0)

    def test_price_from_known_sqrt(self):
        # цена 4 -> sqrt = 2 -> sqrtPriceX96 = 2 * 2**96
        assert v3_price(2 * Q96, 18, 18, True) == pytest.approx(4.0)
        assert v3_price(2 * Q96, 18, 18, False) == pytest.approx(0.25)

    def test_decimals_adjustment(self):
        # token0 с 18 decimals, token1 с 6 (TOKEN/USDC): цена 2.5 USDC за токен
        # raw = 2.5 * 10**6 / 10**18 = 2.5e-12
        raw = Fraction(25, 10) * Fraction(10**6, 10**18)
        sqrt = math.isqrt(int(raw * (1 << 192)))
        assert v3_price(sqrt, 18, 6, target_is_token0=True) == pytest.approx(2.5, rel=1e-12)

    def test_tick_consistency(self):
        # sqrtPriceX96 для тика t: sqrt(1.0001**t) * 2**96
        tick = 200000
        sqrt = int(math.sqrt(1.0001**tick) * Q96)
        assert v3_price(sqrt, 0, 0, True) == pytest.approx(1.0001**tick, rel=1e-9)

    def test_invalid(self):
        assert v3_price(0, 18, 18, True) is None


def test_scale_amount():
    assert scale_amount(1_500_000, 6) == 1.5
    assert scale_amount(10**18, 18) == 1.0


def test_liquidity_usd():
    # V2: 2 × резерв котируемого × цена: 100 WETH по $2000 -> $400k
    assert liquidity_usd(100.0, 2000.0) == 400_000.0


def test_drop_pct():
    assert drop_pct(100.0, 80.0) == pytest.approx(20.0)
    assert drop_pct(100.0, 120.0) == pytest.approx(-20.0)
    assert drop_pct(0.0, 1.0) == 0.0
