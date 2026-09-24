"""Фейковый RPC для интеграционных тестов: контракты-заглушки, Multicall3, логи."""
from __future__ import annotations

from typing import Any, Callable

from eth_abi import decode, encode

from chain.events import V2_BURN, V2_SWAP, V2_SYNC, V3_BURN, V3_SWAP, RawLog
from chain.multicall import MULTICALL3_ADDRESS, selector
from chain.rpc import CallReverted

Handler = Callable[[bytes, Any], bytes]


class Revert(Exception):
    pass


def ret(types: list[str], values: list[Any]) -> Handler:
    data = encode(types, values)
    return lambda args, block: data


def reverting(args: bytes, block: Any) -> bytes:
    raise Revert()


class FakeRpc:
    def __init__(self) -> None:
        self.contracts: dict[str, dict[bytes, Handler]] = {}
        self.logs: list[RawLog] = []
        self.head = 0
        self.senders: dict[str, str] = {}
        self.fail_get_logs = 0  # сколько следующих вызовов get_logs упадут
        self.calls = 0

    # --- настройка ---------------------------------------------------------
    def contract(self, address: str) -> dict[bytes, Handler]:
        return self.contracts.setdefault(address.lower(), {})

    def fn(self, address: str, signature: str, handler: Handler) -> None:
        self.contract(address)[selector(signature)] = handler

    def token(self, address: str, symbol: Any, name: Any, decimals: Any) -> None:
        for sig, value in (("symbol()", symbol), ("name()", name), ("decimals()", decimals)):
            if value is None:
                self.fn(address, sig, reverting)
            elif isinstance(value, bytes):
                self.fn(address, sig, lambda a, b, v=value: v)
            elif isinstance(value, int):
                self.fn(address, sig, ret(["uint8"], [value]))
            else:
                self.fn(address, sig, ret(["string"], [value]))

    def pool(self, address: str, factory: str, token0: str, token1: str, fee: int | None = None) -> None:
        self.fn(address, "factory()", ret(["address"], [factory]))
        self.fn(address, "token0()", ret(["address"], [token0]))
        self.fn(address, "token1()", ret(["address"], [token1]))
        if fee is not None:
            self.fn(address, "fee()", ret(["uint256"], [fee]))  # как в сыром ответе: 32 байта

    def v2_factory(self, factory: str, pairs: dict[tuple[str, str], str]) -> None:
        def get_pair(args: bytes, block: Any) -> bytes:
            a, b = decode(["address", "address"], args)
            return encode(["address"], [pairs.get((a.lower(), b.lower()), "0x" + "00" * 20)])
        self.fn(factory, "getPair(address,address)", get_pair)

    def v3_factory(self, factory: str, pools: dict[tuple[str, str, int], str]) -> None:
        def get_pool(args: bytes, block: Any) -> bytes:
            a, b, fee = decode(["address", "address", "uint24"], args)
            return encode(["address"], [pools.get((a.lower(), b.lower(), fee), "0x" + "00" * 20)])
        self.fn(factory, "getPool(address,address,uint24)", get_pool)

    # --- логи ------------------------------------------------------------------
    def _add(self, pool: str, block: int, tx: str, idx: int, topics: list[str], data: bytes) -> None:
        self.logs.append(RawLog(pool.lower(), topics, data, block, tx, idx))

    @staticmethod
    def _t(addr: str) -> str:
        return "0x" + "00" * 12 + addr.lower()[2:]

    def v2_sync(self, pool, block, tx, idx, r0, r1):
        self._add(pool, block, tx, idx, [V2_SYNC], encode(["uint112", "uint112"], [r0, r1]))

    def v2_swap(self, pool, block, tx, idx, a0in, a1in, a0out, a1out, sender, to):
        self._add(pool, block, tx, idx, [V2_SWAP, self._t(sender), self._t(to)],
                  encode(["uint256"] * 4, [a0in, a1in, a0out, a1out]))

    def v2_burn(self, pool, block, tx, idx, a0, a1, sender, to):
        self._add(pool, block, tx, idx, [V2_BURN, self._t(sender), self._t(to)], encode(["uint256"] * 2, [a0, a1]))

    def v3_swap(self, pool, block, tx, idx, a0, a1, sqrt, liq, tick, sender, recipient):
        self._add(pool, block, tx, idx, [V3_SWAP, self._t(sender), self._t(recipient)],
                  encode(["int256", "int256", "uint160", "uint128", "int24"], [a0, a1, sqrt, liq, tick]))

    def v3_burn(self, pool, block, tx, idx, owner, amount, a0, a1):
        self._add(pool, block, tx, idx, [V3_BURN, self._t(owner), "0x" + "00" * 32, "0x" + "00" * 32],
                  encode(["uint128", "uint256", "uint256"], [amount, a0, a1]))

    # --- интерфейс RpcClient ----------------------------------------------------
    def _exec(self, to: str, data: bytes, block: Any) -> bytes:
        contract = self.contracts.get(to.lower())
        if contract is None:
            return b""  # EOA / несуществующий контракт: пустой успешный ответ
        handler = contract.get(data[:4])
        if handler is None:
            raise Revert()
        return handler(data[4:], block)

    async def eth_call(self, to: str, data: bytes, block: Any = "latest", attempts: int | None = None) -> bytes:
        self.calls += 1
        if to.lower() == MULTICALL3_ADDRESS:
            (items,) = decode(["(address,bool,bytes)[]"], data[4:])
            out = []
            for target, _allow, cd in items:
                try:
                    out.append((True, self._exec(target, cd, block)))
                except Revert:
                    out.append((False, b""))
            return encode(["(bool,bytes)[]"], [out])
        try:
            return self._exec(to, data, block)
        except Revert as exc:
            raise CallReverted("execution reverted") from exc

    async def get_logs(self, from_block: int, to_block: int, topics) -> list[RawLog]:
        if self.fail_get_logs > 0:
            self.fail_get_logs -= 1
            raise ConnectionError("fake RPC down")
        topics = set(topics)
        return [lg for lg in self.logs if from_block <= lg.block_number <= to_block and lg.topics[0] in topics]

    async def block_number(self) -> int:
        return self.head

    async def block_timestamp(self, number: int) -> int:
        return 1_700_000_000 + 12 * number

    async def tx_sender(self, tx_hash: str) -> str | None:
        return self.senders.get(tx_hash)

    async def close(self) -> None:
        pass
