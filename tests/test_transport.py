"""Проверка реального транспорта (web3.py HTTP/WS, Telegram) на локальных фейковых серверах."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from eth_abi import encode

from chain.block_source import PollingBlockSource, WebSocketBlockSource
from chain.events import ALL_TOPICS, V2_SYNC
from chain.multicall import Call, Multicall, encode_call
from chain.rpc import CallReverted, RpcClient, RpcError
from notify.notifiers import TelegramNotifier


POOL = "0x" + "ab" * 20


def _log(block: int, idx: int) -> dict:
    return {"address": POOL, "topics": [V2_SYNC], "data": "0x" + encode(["uint112", "uint112"], [1, 2]).hex(),
            "blockNumber": hex(block), "transactionHash": "0x" + f"{block:064x}", "logIndex": hex(idx),
            "blockHash": "0x" + "11" * 32, "transactionIndex": "0x0", "removed": False}


class FakeNode:
    def __init__(self) -> None:
        self.head = 0x100
        self.fail_next = 0
        self.max_range = 1000
        self.requests: list[dict] = []

    def result(self, method: str, params: list):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_getLogs":
            f = params[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if b - a + 1 > self.max_range:
                raise ValueError("query returned more than 10000 results")
            assert f["topics"][0] == list(ALL_TOPICS) and "address" not in f
            return [_log(n, 0) for n in range(a, b + 1)]
        if method == "eth_call":
            data = params[0]["data"]
            if data.startswith("0x" + encode_call("fail()").hex()):
                raise ValueError("execution reverted")
            return "0x" + encode(["uint256"], [42]).hex()
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(1_700_000_000), "hash": "0x" + "22" * 32,
                    "parentHash": "0x" + "33" * 32, "transactions": []}
        raise ValueError(f"unexpected {method}")

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        if self.fail_next > 0:
            self.fail_next -= 1
            return web.Response(status=503, text="overloaded")
        try:
            res = {"jsonrpc": "2.0", "id": body["id"], "result": self.result(body["method"], body["params"])}
        except ValueError as exc:
            res = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": str(exc)}}
        return web.json_response(res)


@pytest.fixture
async def node():
    fake = FakeNode()
    app = web.Application()
    app.router.add_post("/", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    fake.url = f"http://127.0.0.1:{port}/"
    yield fake
    await runner.cleanup()


def client(node) -> RpcClient:
    return RpcClient(node.url, timeout=5, retry_base=0.001, retry_max=0.01, call_attempts=3)


async def test_http_basic_calls(node):
    rpc = client(node)
    assert await rpc.block_number() == 0x100
    assert await rpc.block_timestamp(5) == 1_700_000_000
    data = await rpc.eth_call("0x" + "cd" * 20, encode_call("x()"))
    assert int.from_bytes(data, "big") == 42
    with pytest.raises(CallReverted):
        await rpc.eth_call("0x" + "cd" * 20, encode_call("fail()"))
    await rpc.close()


async def test_http_retries_after_errors(node):
    rpc = client(node)
    node.fail_next = 4  # 503 четыре раза подряд — бесконечные ретраи блок-номера переживут
    assert await rpc.block_number() == 0x100
    node.fail_next = 10
    with pytest.raises(RpcError):  # eth_call ограничен call_attempts
        await rpc.eth_call("0x" + "cd" * 20, encode_call("x()"))
    node.fail_next = 0
    await rpc.close()


async def test_get_logs_single_request_and_split(node):
    rpc = client(node)
    logs = await rpc.get_logs(10, 10, ALL_TOPICS)
    assert len(logs) == 1 and logs[0].block_number == 10 and logs[0].address == POOL
    assert sum(1 for r in node.requests if r["method"] == "eth_getLogs") == 1

    node.max_range = 3  # провайдер режет большие диапазоны — делим пополам
    logs = await rpc.get_logs(1, 10, ALL_TOPICS)
    assert [lg.block_number for lg in logs] == list(range(1, 11))
    await rpc.close()


async def test_multicall_falls_back_when_multicall_reverts(node):
    rpc = client(node)
    mc = Multicall(rpc, chunk_size=2)
    # фейковый узел отвечает 42 на любой eth_call, а aggregate3 такой ответ не декодирует ->
    # multicall переходит на вызовы по одному
    res = await mc.call([Call("0x" + "cd" * 20, encode_call("x()")),
                         Call("0x" + "cd" * 20, encode_call("fail()"))])
    assert [r.success for r in res] == [True, False]
    await rpc.close()


async def test_polling_source(node):
    rpc = client(node)
    src = PollingBlockSource(rpc, interval_sec=0.01)
    gen = src.heads()
    assert await gen.__anext__() == 0x100
    node.head = 0x102
    assert await asyncio.wait_for(gen.__anext__(), 2) == 0x102
    await gen.aclose()
    await rpc.close()


async def test_websocket_source_reconnects():
    import websockets

    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        async for raw in ws:
            req = json.loads(raw)
            if req["method"] == "eth_subscribe":
                sub = "0x" + "9" * 32
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": sub}))
                for n in (100 * connections, 100 * connections + 1):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                                              "params": {"subscription": sub,
                                                         "result": {"number": hex(n), "hash": "0x" + "00" * 32}}}))
                await asyncio.sleep(0.05)
                await ws.close()  # обрыв соединения — источник должен переподключиться
                return
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": "0x1"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    src = WebSocketBlockSource(f"ws://127.0.0.1:{port}", idle_timeout_sec=2, retry_base=0.01, retry_max=0.05)
    got = []
    gen = src.heads()
    try:
        async with asyncio.timeout(10):
            async for n in gen:
                got.append(n)
                if len(got) == 4:
                    break
    finally:
        await gen.aclose()
        server.close()
    assert got == [100, 101, 200, 201] and connections >= 2


async def test_telegram_notifier():
    sent = []
    state = {"n": 0}

    async def handle(request: web.Request) -> web.Response:
        state["n"] += 1
        if state["n"] == 1:
            return web.json_response({"ok": False, "parameters": {"retry_after": 0}}, status=429)
        sent.append(await request.json())
        return web.json_response({"ok": True, "result": {}})

    app = web.Application()
    app.router.add_post("/botTOKEN/sendMessage", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    tg = TelegramNotifier("TOKEN", "-100", timeout=5)
    tg.url = f"http://127.0.0.1:{port}/botTOKEN/sendMessage"
    try:
        assert await tg.send_text("<b>hi</b>")
    finally:
        await tg.close()
        await runner.cleanup()
    assert sent == [{"chat_id": "-100", "text": "<b>hi</b>", "parse_mode": "HTML", "disable_web_page_preview": True}]
