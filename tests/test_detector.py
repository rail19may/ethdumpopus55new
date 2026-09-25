import pytest

from config import DetectorCfg
from detector.detector import DumpDetector
from detector.window import BlockSeries, SwapRecord

P = "0xpool"


def det(**kw) -> DumpDetector:
    cfg = dict(window_blocks=5, drop_pct=20, min_liquidity_usd=50_000, cooldown_min=30,
               rugpull_liquidity_drop_pct=50)
    cfg.update(kw)
    return DumpDetector(DetectorCfg(**cfg))


def test_block_series_window_and_carry():
    s = BlockSeries()
    s.add(100, 10.0)
    s.add(103, 8.0)
    s.prune(106, 5)  # окно 102..106: точка 100 уходит в carry
    assert s.carry == 10.0 and s.max() == 10.0
    s.prune(200, 5)
    assert s.carry == 8.0 and s.max() == 8.0 and s.current == 8.0


def test_basic_dump_signal():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 100_000)
    d.record_price(P, 102, 0.75)  # -25%
    assert d.is_candidate(P, 102)
    sig = d.evaluate(P, 102, now_ts=1000)
    assert sig is not None and sig.drop_pct == pytest.approx(25.0)
    assert sig.price_before == 1.0 and sig.price_after == 0.75 and not sig.rugpull


def test_small_drop_ignored():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 100_000)
    d.record_price(P, 101, 0.85)
    assert not d.is_candidate(P, 101)
    assert d.evaluate(P, 101, 1000) is None


def test_drop_outside_window_not_counted():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 100_000)
    d.record_price(P, 101, 0.9)
    d.record_price(P, 106, 0.85)
    d.record_price(P, 110, 0.78)  # от 1.0 это -22%, но 1.0 давно вне окна
    # окно 106..110; на его начало действовала цена 0.9 (сделка блока 101) — она и есть carry
    assert d.current_drop(P, 110) == pytest.approx((0.9 - 0.78) / 0.9 * 100)
    assert d.evaluate(P, 110, 1000) is None


def test_rarely_traded_pool_uses_carry_price():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 200_000)
    d.record_price(P, 500, 0.5)  # следующая сделка через 400 блоков — дамп
    sig = d.evaluate(P, 500, 1000)
    assert sig is not None and sig.drop_pct == pytest.approx(50.0)


def test_min_liquidity():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 40_000)
    d.record_price(P, 101, 0.5)
    assert d.evaluate(P, 101, 1000) is None


def test_unknown_liquidity_no_signal():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_price(P, 101, 0.5)
    assert d.is_candidate(P, 101)
    assert d.evaluate(P, 101, 1000) is None


def test_cooldown():
    d = det(cooldown_min=30)
    d.record_liquidity(P, 100, 100_000)
    d.record_price(P, 100, 1.0)
    d.record_price(P, 101, 0.7)
    assert d.evaluate(P, 101, 1_000) is not None
    d.record_price(P, 102, 0.4)
    assert d.evaluate(P, 102, 1_000 + 29 * 60) is None  # ещё кулдаун
    d.record_price(P, 400, 0.4)
    d.record_price(P, 401, 0.2)
    assert d.evaluate(P, 401, 1_000 + 31 * 60) is not None


def test_rugpull_flag():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 1_000_000)
    d.record_burn(P, 101)
    d.record_liquidity(P, 101, 300_000)  # -70%
    d.record_price(P, 101, 0.6)
    sig = d.evaluate(P, 101, 1000)
    assert sig is not None and sig.rugpull and sig.burns_in_window == 1
    assert sig.liquidity_max_usd == 1_000_000 and sig.liquidity_usd == 300_000


def test_no_rugpull_without_burn():
    d = det()
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 1_000_000)
    d.record_liquidity(P, 101, 300_000)
    d.record_price(P, 101, 0.6)
    sig = d.evaluate(P, 101, 1000)
    assert sig is not None and not sig.rugpull


def test_burn_outside_window_not_rugpull():
    d = det()
    d.record_burn(P, 90)
    d.record_price(P, 100, 1.0)
    d.record_liquidity(P, 100, 1_000_000)
    d.record_liquidity(P, 101, 300_000)
    d.record_price(P, 101, 0.6)
    sig = d.evaluate(P, 101, 1000)
    assert sig is not None and not sig.rugpull


def test_main_swap_is_biggest_impact():
    d = det()
    d.record_liquidity(P, 100, 500_000)
    d.record_price(P, 100, 1.0)
    d.record_swap(P, SwapRecord(101, "0xsmall", 1, "0xa", 10_000, 1.0, 0.95))
    d.record_swap(P, SwapRecord(101, "0xbig", 2, "0xb", 50_000, 0.95, 0.6))
    d.record_swap(P, SwapRecord(102, "0xbuy", 3, "0xc", None, 0.6, 0.65))
    d.record_price(P, 102, 0.65)
    sig = d.evaluate(P, 102, 1000)
    assert sig.main_swap.tx_hash == "0xbig" and sig.main_swap.sell_usd == 50_000


def test_gc():
    d = det(state_gc_blocks=100)
    d.record_price("0xold", 10, 1.0)
    d.record_price("0xnew", 500, 1.0)
    assert d.gc(520) == 1 and "0xnew" in d.states and "0xold" not in d.states


def test_gc_evicts_alerted_pool_after_cooldown():
    d = det(state_gc_blocks=100, cooldown_min=30)  # 30 мин = 150 блоков
    d.record_liquidity(P, 10, 100_000)
    d.record_price(P, 10, 1.0)
    d.record_price(P, 11, 0.5)
    assert d.evaluate(P, 11, 1000) is not None
    assert d.gc(120) == 0 and P in d.states   # неактивен >100 блоков, но кулдаун ещё идёт
    assert d.gc(200) == 1 and P not in d.states
