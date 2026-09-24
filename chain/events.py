"""Сигнатуры событий Uniswap V2/V3, topic0 и декодирование логов."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from eth_abi import decode
from eth_utils import keccak


def event_topic(signature: str) -> str:
    """topic0 = keccak256(сигнатура события), в виде 0x-hex в нижнем регистре."""
    return "0x" + keccak(text=signature).hex()


V2_SYNC_SIG = "Sync(uint112,uint112)"
V2_SWAP_SIG = "Swap(address,uint256,uint256,uint256,uint256,address)"
V2_BURN_SIG = "Burn(address,uint256,uint256,address)"
V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)"
V3_BURN_SIG = "Burn(address,int24,int24,uint128,uint256,uint256)"

V2_SYNC = event_topic(V2_SYNC_SIG)
V2_SWAP = event_topic(V2_SWAP_SIG)
V2_BURN = event_topic(V2_BURN_SIG)
V3_SWAP = event_topic(V3_SWAP_SIG)
V3_BURN = event_topic(V3_BURN_SIG)

ALL_TOPICS: tuple[str, ...] = (V2_SYNC, V2_SWAP, V2_BURN, V3_SWAP, V3_BURN)


def _hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    s = str(value).lower()
    return s if s.startswith("0x") else "0x" + s


def _bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value)
    if s.startswith(("0x", "0X")):
        s = s[2:]
    return bytes.fromhex(s)


def _int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 16)


def _topic_addr(topic: str) -> str:
    return "0x" + topic[-40:]


@dataclass(slots=True)
class RawLog:
    address: str  # lowercase
    topics: list[str]  # lowercase 0x-hex
    data: bytes
    block_number: int
    tx_hash: str
    log_index: int

    @classmethod
    def from_rpc(cls, entry: Any) -> "RawLog":
        """Принимает лог в виде web3 AttributeDict или сырого JSON-RPC словаря."""
        return cls(
            address=str(entry["address"]).lower(),
            topics=[_hex(t) for t in entry["topics"]],
            data=_bytes(entry["data"]),
            block_number=_int(entry["blockNumber"]),
            tx_hash=_hex(entry["transactionHash"]),
            log_index=_int(entry["logIndex"]),
        )


@dataclass(slots=True)
class _Base:
    pool: str
    block: int
    tx_hash: str
    log_index: int


@dataclass(slots=True)
class V2Sync(_Base):
    reserve0: int
    reserve1: int


@dataclass(slots=True)
class V2Swap(_Base):
    sender: str
    to: str
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int


@dataclass(slots=True)
class V2Burn(_Base):
    amount0: int
    amount1: int


@dataclass(slots=True)
class V3Swap(_Base):
    sender: str
    recipient: str
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int


@dataclass(slots=True)
class V3Burn(_Base):
    owner: str
    amount: int
    amount0: int
    amount1: int


Event = Union[V2Sync, V2Swap, V2Burn, V3Swap, V3Burn]


def decode_log(log: RawLog) -> Event | None:
    """Декодирует лог в событие. Для логов с чужой раскладкой (другое число indexed-полей,
    обрезанные данные и т.п.) возвращает None, без исключений."""
    if not log.topics:
        return None
    t0 = log.topics[0]
    base = (log.address, log.block_number, log.tx_hash, log.log_index)
    try:
        if t0 == V2_SYNC and len(log.topics) == 1:
            r0, r1 = decode(["uint112", "uint112"], log.data)
            return V2Sync(*base, reserve0=r0, reserve1=r1)
        if t0 == V2_SWAP and len(log.topics) == 3:
            a0i, a1i, a0o, a1o = decode(["uint256"] * 4, log.data)
            return V2Swap(*base, sender=_topic_addr(log.topics[1]), to=_topic_addr(log.topics[2]),
                          amount0_in=a0i, amount1_in=a1i, amount0_out=a0o, amount1_out=a1o)
        if t0 == V2_BURN and len(log.topics) == 3:
            a0, a1 = decode(["uint256", "uint256"], log.data)
            return V2Burn(*base, amount0=a0, amount1=a1)
        if t0 == V3_SWAP and len(log.topics) == 3:
            a0, a1, sp, liq, tick = decode(["int256", "int256", "uint160", "uint128", "int24"], log.data)
            return V3Swap(*base, sender=_topic_addr(log.topics[1]), recipient=_topic_addr(log.topics[2]),
                          amount0=a0, amount1=a1, sqrt_price_x96=sp, liquidity=liq, tick=tick)
        if t0 == V3_BURN and len(log.topics) == 4:
            amount, a0, a1 = decode(["uint128", "uint256", "uint256"], log.data)
            return V3Burn(*base, owner=_topic_addr(log.topics[1]), amount=amount, amount0=a0, amount1=a1)
    except Exception:
        return None
    return None
