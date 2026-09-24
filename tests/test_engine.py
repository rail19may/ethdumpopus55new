"""Интеграционные тесты: фейковая цепочка -> Engine -> алерты (реплей и live-цикл)."""
from __future__ import annotations

import dataclasses
import math
from fractions import Fraction
from pathlib import Path

import pytest

from chain.block_source import BlockSource
from config import load_config
from engine import Engine
from notify.format import format_html, format_text
from notify.notifiers import Notifier
from pools.cache import IGNORED_BROKEN, IGNORED_FACTORY, IGNORED_FAKE, IGNORED_NO_QUOTE, QUOTE_PAIR, TRACKED
from runner import LiveRunner, run_replay
from storage.db import Database

from .fakechain import FakeRpc, ret

ROOT = Path(__file__).resolve().parent.parent

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
V2F = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
V3F = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
SUSHI_F = "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"
ETH_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"

PEPE = "0x" + "aa" * 20          # V2 TOKEN/WETH (token0=WETH? нет: 0xaa.. < 0xc0.. -> token0=PEPE)
RUG = "0x" + "dd" * 20           # V3 WETH/RUG (0xc0.. < 0xdd.. -> token0=WETH)
B32 = "0x" + "b3" * 20           # токен с bytes32-символом
BROKEN = "0x" + "bb" * 20        # decimals() ревертит
OTHER = "0x" + "0e" * 20         # токен без котируемой пары

V2_POOL = "0x" + "01" * 20
V3_POOL = "0x" + "02" * 20
FAKE_POOL = "0x" + "03" * 20     # factory() врёт
FORK_POOL = "0x" + "04" * 20     # чужая фабрика
NOQUOTE_POOL = "0x" + "05" * 20
BROKEN_POOL = "0x" + "06" * 20
B32_POOL = "0x" + "07" * 20

SELLER = "0x" + "5e" * 20
ROUTER = "0x" + "7a" * 20
E18 = 10**18


def sqrt_x96(price_token0_in_token1_raw: Fraction) -> int:
    return math.isqrt(int(price_token0_in_token1_raw * (1 << 192)))


ETH_SQRT_2000 = sqrt_x96(Fraction(1, 2000) * 10**12)   # USDC(6)/WETH(18), 1 ETH = 2000 USDC
ETH_SQRT_2100 = sqrt_x96(Fraction(1, 2100) * 10**12)
RUG_SQRT_BEFORE = sqrt_x96(Fraction(1000))             # 1 WETH = 1000 RUG -> 0.001 WETH за RUG
RUG_SQRT_AFTER = sqrt_x96(Fraction(5000, 3))           # 0.0006 WETH за RUG (-40%)


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def build_chain() -> FakeRpc:
    c = FakeRpc()
    c.token(WETH, "WETH", "Wrapped Ether", 18)
    c.token(USDC, "USDC", "USD Coin", 6)
    c.token(PEPE, "PEPE", "Pepe <script>", 18)
    c.token(RUG, "RUG", "Rug Token", 18)
    c.token(B32, b"MKR" + b"\x00" * 29, b"Maker" + b"\x00" * 27, 18)
    c.token(BROKEN, "BRK", "Broken", None)
    c.token(OTHER, "OTH", "Other", 18)

    c.pool(ETH_POOL, V3F, USDC, WETH, 500)
    c.fn(ETH_POOL, "slot0()", ret(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                                  [ETH_SQRT_2000, 0, 0, 0, 0, 0, True]))
    c.pool(V2_POOL, V2F, PEPE, WETH)
    c.pool(V3_POOL, V3F, WETH, RUG, 3000)
    c.fn(V3_POOL, "slot0()", ret(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                                 [RUG_SQRT_BEFORE, 0, 0, 0, 0, 0, True]))
    c.pool(FAKE_POOL, V2F, PEPE, WETH)          # фабрика его не знает
    c.pool(FORK_POOL, SUSHI_F, PEPE, WETH)
    c.pool(NOQUOTE_POOL, V2F, OTHER, PEPE)
    c.pool(BROKEN_POOL, V2F, BROKEN, WETH)
    c.pool(B32_POOL, V2F, B32, WETH)
    c.v2_factory(V2F, {(PEPE, WETH): V2_POOL, (OTHER, PEPE): NOQUOTE_POOL, (BROKEN, WETH): BROKEN_POOL,
                       (B32, WETH): B32_POOL})
    c.v3_factory(V3F, {(USDC, WETH, 500): ETH_POOL, (WETH, RUG, 3000): V3_POOL})

    # баланс WETH в V3-пуле: 50 WETH, после рагпула (блок 1002) — 10 WETH
    def weth_balance(args: bytes, block) -> bytes:
        from eth_abi import decode, encode
        (holder,) = decode(["address"], args)
        if holder.lower() != V3_POOL:
            return encode(["uint256"], [0])
        amount = 10 * E18 if isinstance(block, int) and block >= 1002 else 50 * E18
        return encode(["uint256"], [amount])
    c.fn(WETH, "balanceOf(address)", weth_balance)

    # --- блок 1000: обычная торговля ---
    x, y = 1_000_000 * E18, 100 * E18  # PEPE / WETH: 0.0001 WETH за PEPE, ликвидность 2*100*$2000=$400k
    c.v2_sync(V2_POOL, 1000, tx(1), 0, x, y)
    c.v3_swap(ETH_POOL, 1000, tx(2), 1, -2100 * 10**6, E18, ETH_SQRT_2100, 10**18, 0, ROUTER, ROUTER)
    c.v2_sync(FAKE_POOL, 1000, tx(3), 2, 10**18, 10**30)
    c.v2_sync(FORK_POOL, 1000, tx(4), 3, 10**18, 10**30)
    c.v2_sync(NOQUOTE_POOL, 1000, tx(5), 4, 10**18, 10**18)
    c.v2_sync(BROKEN_POOL, 1000, tx(6), 5, 10**18, 10**18)
    c.v2_sync(B32_POOL, 1000, tx(7), 6, 10**18, 10**18)
    # маленький своп в V3-пуле RUG (цена почти не меняется)
    c.v3_swap(V3_POOL, 1000, tx(8), 7, -E18 // 100, 10 * E18, RUG_SQRT_BEFORE, 10**20, 0, ROUTER, ROUTER)

    # --- блок 1001: дамп PEPE в V2 — продажа 400k PEPE ---
    sell = 400_000 * E18
    x2 = x + sell
    y2 = x * y // x2
    out = y - y2
    c.v2_sync(V2_POOL, 1001, tx(10), 0, x2, y2)
    c.v2_swap(V2_POOL, 1001, tx(10), 1, sell, 0, 0, out, ROUTER, ROUTER)
    c.senders[tx(10)] = SELLER
    # фейковый пул тоже «дампит» — сигнала быть не должно
    c.v2_sync(FAKE_POOL, 1001, tx(11), 2, 10**18, 10**20)

    # --- блок 1002: рагпул в V3 — Burn ликвидности + продажа RUG ---
    c.v3_burn(V3_POOL, 1002, tx(20), 0, ROUTER, 10**20, 40 * E18, 10_000 * E18)
    c.v3_swap(V3_POOL, 1002, tx(21), 1, -20 * E18, 25_000 * E18, RUG_SQRT_AFTER, 10**19, 0, ROUTER, SELLER)
    c.senders[tx(21)] = SELLER
    # Burn с amount=0 (poke) — не считается выводом ликвидности
    c.v3_burn(V3_POOL, 1002, tx(22), 2, ROUTER, 0, 0, 0)
    c.head = 1002
    return c


class CaptureNotifier(Notifier):
    def __init__(self) -> None:
        self.alerts = []

    async def send(self, alert) -> None:
        self.alerts.append(alert)


def make_cfg(tmp_path):
    cfg = load_config(ROOT / "config.yaml", env_file=None, require_rpc=False)
    return dataclasses.replace(
        cfg, sqlite_path=str(tmp_path / "t.sqlite3"),
        rpc=dataclasses.replace(cfg.rpc, retry_base_delay_sec=0.001, retry_max_delay_sec=0.01,
                                max_lag_blocks=1, catchup_batch_blocks=2))


async def test_replay_finds_dumps(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = build_chain()
    db = Database(cfg.sqlite_path)
    notifier = CaptureNotifier()
    engine = Engine(cfg, chain, db, notifier, mode="replay")
    alerts = await run_replay(cfg, chain, engine, 1000, 1002)

    # статусы пулов
    st = {a: engine.pools.get(a).status for a in
          (ETH_POOL, V2_POOL, V3_POOL, FAKE_POOL, FORK_POOL, NOQUOTE_POOL, BROKEN_POOL, B32_POOL)}
    assert st == {ETH_POOL: QUOTE_PAIR, V2_POOL: TRACKED, V3_POOL: TRACKED, FAKE_POOL: IGNORED_FAKE,
                  FORK_POOL: IGNORED_FACTORY, NOQUOTE_POOL: IGNORED_NO_QUOTE, BROKEN_POOL: IGNORED_BROKEN,
                  B32_POOL: TRACKED}
    assert engine.pools.token(B32).symbol == "MKR"
    # цена ETH обновилась из события Swap пула WETH/USDC
    assert engine.eth.eth_usd == pytest.approx(2100, rel=1e-9)

    assert [a.pool for a in alerts] == [V2_POOL, V3_POOL]
    assert notifier.alerts == alerts

    v2 = alerts[0]
    assert v2.block == 1001 and v2.dex == "Uniswap V2" and v2.token == PEPE and v2.token_symbol == "PEPE"
    assert v2.quote_symbol == "WETH"
    # цена до 1e-4 WETH, после: y2/x2
    assert v2.price_before == pytest.approx(1e-4)
    assert v2.price_after == pytest.approx((100 / 1.4) / 1_400_000)
    assert v2.drop_pct == pytest.approx((1 - 1 / 1.4**2) * 100)
    assert v2.price_before_usd == pytest.approx(1e-4 * 2100)
    assert v2.liquidity_max_usd == pytest.approx(2 * 100 * 2000)  # Sync в блоке 1000 был до свопа ETH
    assert v2.main_tx == tx(10) and v2.seller == SELLER
    assert v2.sell_usd == pytest.approx((100 - 100 / 1.4) * 2100, rel=1e-6)
    assert not v2.rugpull

    v3 = alerts[1]
    assert v3.block == 1002 and v3.dex == "Uniswap V3 (0.30%)" and v3.token == RUG
    assert v3.drop_pct == pytest.approx(40.0, rel=1e-6)
    assert v3.price_before == pytest.approx(0.001, rel=1e-9)
    assert v3.liquidity_max_usd == pytest.approx(2 * 50 * 2100)
    assert v3.liquidity_usd == pytest.approx(2 * 10 * 2100)
    assert v3.rugpull
    assert v3.main_tx == tx(21) and v3.sell_usd == pytest.approx(20 * 2100)

    rows = db.alerts()
    assert len(rows) == 2 and {r["mode"] for r in rows} == {"replay"}
    assert rows[0]["rugpull"] == 1

    html = format_html(v2)
    assert "Pepe &lt;script&gt;" in html and "<script>" not in html
    assert "dexscreener.com/ethereum/" + V2_POOL in html
    assert "etherscan.io/tx/" + tx(10) in html and "etherscan.io/token/" in html
    text = format_text(v3)
    assert "возможный рагпул" in text and "<" not in text.split("Etherscan")[0]


async def test_pool_cache_persisted(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = build_chain()
    db = Database(cfg.sqlite_path)
    engine = Engine(cfg, chain, db, CaptureNotifier(), mode="replay")
    await run_replay(cfg, chain, engine, 1000, 1000)
    calls = chain.calls

    engine2 = Engine(cfg, chain, db, CaptureNotifier(), mode="replay")
    assert engine2.pools.get(FORK_POOL).status == IGNORED_FACTORY
    assert engine2.pools.get(V2_POOL).status == TRACKED and engine2.pools.get(V2_POOL).target == PEPE
    await engine2.pools.resolve([V2_POOL, FORK_POOL, FAKE_POOL], "latest")
    assert chain.calls == calls  # повторно не опрашиваем


class ListSource(BlockSource):
    def __init__(self, heads):
        self._heads = heads

    async def heads(self):
        for h in self._heads:
            yield h


async def test_live_runner_survives_rpc_errors_and_saves_state(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = build_chain()
    db = Database(cfg.sqlite_path)
    db.set_last_block(999)
    notifier = CaptureNotifier()
    engine = Engine(cfg, chain, db, notifier, mode="live")
    chain.fail_get_logs = 3  # RPC «лежит» первые три запроса
    runner = LiveRunner(cfg, chain, db, engine, ListSource([1000, 1002]))
    await runner.run()
    assert db.get_last_block() == 1002
    assert [a.pool for a in notifier.alerts] == [V2_POOL, V3_POOL]
    assert {r["mode"] for r in db.alerts()} == {"live"}

    # повторный запуск: начинает с сохранённого блока, дублей нет
    engine2 = Engine(cfg, chain, db, notifier, mode="live")
    runner2 = LiveRunner(cfg, chain, db, engine2, ListSource([1002]))
    await runner2.run()
    assert len(notifier.alerts) == 2


async def test_live_runner_skips_poison_block(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg = dataclasses.replace(cfg, rpc=dataclasses.replace(cfg.rpc, max_block_failures=2))
    chain = build_chain()
    db = Database(cfg.sqlite_path)
    db.set_last_block(1000)
    engine = Engine(cfg, chain, db, CaptureNotifier(), mode="live")

    orig = engine.process_block

    async def broken(block, logs):
        if block == 1001:
            raise RuntimeError("boom")
        return await orig(block, logs)

    engine.process_block = broken
    runner = LiveRunner(cfg, chain, db, engine, ListSource([1002]))
    await runner.run()
    assert db.get_last_block() == 1002


async def test_cooldown_blocks_repeat(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = build_chain()
    # второй дамп PEPE через 10 блоков (~2 минуты) — в пределах кулдауна
    x, y = 1_400_000 * E18, (1_000_000 * 100 * E18 * E18) // (1_400_000 * E18)
    sell = 1_000_000 * E18
    x2, y2 = x + sell, x * y // (x + sell)
    chain.v2_sync(V2_POOL, 1011, tx(30), 0, x2, y2)
    chain.v2_swap(V2_POOL, 1011, tx(30), 1, sell, 0, 0, y - y2, ROUTER, ROUTER)
    db = Database(cfg.sqlite_path)
    engine = Engine(cfg, chain, db, CaptureNotifier(), mode="replay")
    alerts = await run_replay(cfg, chain, engine, 1000, 1011)
    assert [a.block for a in alerts if a.pool == V2_POOL] == [1001]


async def test_live_first_sight_v3_rugpull_uses_previous_block_state(tmp_path):
    """Бот впервые видит V3-пул в блоке рагпула: цена и ликвидность берутся на блок N-1."""
    cfg = make_cfg(tmp_path)
    chain = build_chain()
    db = Database(cfg.sqlite_path)
    db.set_last_block(1001)
    notifier = CaptureNotifier()
    engine = Engine(cfg, chain, db, notifier, mode="live")
    await LiveRunner(cfg, chain, db, engine, ListSource([1002])).run()
    assert len(notifier.alerts) == 1
    a = notifier.alerts[0]
    assert a.pool == V3_POOL and a.rugpull and a.liquidity_max_usd == pytest.approx(2 * 50 * 2000)
