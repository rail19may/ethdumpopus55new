"""Пакетные eth_call через Multicall3.aggregate3."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from .rpc import BlockId, CallReverted, RpcClient

log = logging.getLogger(__name__)

MULTICALL3_ADDRESS = "0xca11bde05977b3631167028862be2a173976ca11"


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, arg_types: Sequence[str] = (), args: Sequence[Any] = ()) -> bytes:
    return selector(signature) + (encode(list(arg_types), list(args)) if arg_types else b"")


AGGREGATE3_SIG = "aggregate3((address,bool,bytes)[])"
_AGGREGATE3_SELECTOR = selector(AGGREGATE3_SIG)


@dataclass(frozen=True, slots=True)
class Call:
    target: str
    data: bytes


@dataclass(frozen=True, slots=True)
class CallResult:
    success: bool
    data: bytes


def encode_aggregate3(calls: Sequence[Call]) -> bytes:
    items = [(to_checksum_address(c.target), True, c.data) for c in calls]
    return _AGGREGATE3_SELECTOR + encode(["(address,bool,bytes)[]"], [items])


def decode_aggregate3(data: bytes) -> list[CallResult]:
    (results,) = decode(["(bool,bytes)[]"], data)
    return [CallResult(bool(ok), bytes(ret)) for ok, ret in results]


class Multicall:
    def __init__(self, rpc: RpcClient, chunk_size: int = 150,
                 address: str = MULTICALL3_ADDRESS) -> None:
        self.rpc = rpc
        self.chunk_size = max(1, chunk_size)
        self.address = address

    async def call(self, calls: Sequence[Call], block: BlockId = "latest") -> list[CallResult]:
        """Выполняет вызовы пачками (allowFailure=true для каждого).

        Бросает RpcError, если сам RPC недоступен после всех попыток: вызывающий код
        должен отличать «вызов в контракте упал» (success=False) от «сеть не ответила».
        """
        out: list[CallResult] = []
        for i in range(0, len(calls), self.chunk_size):
            out.extend(await self._call_chunk(calls[i:i + self.chunk_size], block))
        return out

    async def _call_chunk(self, chunk: Sequence[Call], block: BlockId) -> list[CallResult]:
        if len(chunk) == 1:
            return [await self.call_single(chunk[0], block)]
        try:
            raw = await self.rpc.eth_call(self.address, encode_aggregate3(chunk), block)
            results = decode_aggregate3(raw)
            if len(results) != len(chunk):
                raise ValueError("multicall: число результатов не совпадает")
            return results
        except (CallReverted, DecodingError, ValueError) as exc:
            # aggregate3 с allowFailure не должен ревертить; если всё же упал (какой-то вызов съел
            # весь газ) или вернул мусор — делим пачку пополам: плохой вызов изолируется за
            # ~2·log2(N) запросов вместо N запросов по одному.
            log.debug("multicall из %d вызовов не удался (%s), делю пополам", len(chunk), exc)
            mid = len(chunk) // 2
            return await self._call_chunk(chunk[:mid], block) + await self._call_chunk(chunk[mid:], block)

    async def call_single(self, call: Call, block: BlockId = "latest") -> CallResult:
        try:
            data = await self.rpc.eth_call(call.target, call.data, block)
            return CallResult(True, data)
        except CallReverted:
            return CallResult(False, b"")
