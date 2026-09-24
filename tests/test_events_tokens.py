from eth_abi import encode

from chain.events import (ALL_TOPICS, V2_BURN, V2_SWAP, V2_SYNC, V3_BURN, V3_SWAP, RawLog, V2Swap, V2Sync, V3Burn,
                          V3Swap, decode_log)
from chain.multicall import Call, decode_aggregate3, encode_aggregate3, selector
from pools.tokens import decode_address, decode_decimals, decode_text, sanitize_text


def test_topic_hashes_match_known_values():
    # эталонные topic0 (etherscan); в коде вычисляются keccak'ом из сигнатур
    assert V2_SYNC == "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
    assert V2_SWAP == "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
    assert V2_BURN == "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"
    assert V3_SWAP == "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
    assert V3_BURN == "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
    assert len(set(ALL_TOPICS)) == 5


def _topic_addr(a: str) -> str:
    return "0x" + "00" * 12 + a[2:]


def test_selectors():
    assert selector("balanceOf(address)").hex() == "70a08231"
    assert selector("decimals()").hex() == "313ce567"
    assert selector("aggregate3((address,bool,bytes)[])").hex() == "82ad56cb"


POOL = "0x" + "ab" * 20
ALICE = "0x" + "11" * 20
BOB = "0x" + "22" * 20


def _log(topics, data, idx=0):
    return RawLog(POOL, topics, data, 100, "0x" + "ee" * 32, idx)


def test_decode_v2():
    ev = decode_log(_log([V2_SYNC], encode(["uint112", "uint112"], [5, 7])))
    assert isinstance(ev, V2Sync) and (ev.reserve0, ev.reserve1) == (5, 7)
    ev = decode_log(_log([V2_SWAP, _topic_addr(ALICE), _topic_addr(BOB)], encode(["uint256"] * 4, [1, 0, 0, 2])))
    assert isinstance(ev, V2Swap)
    assert ev.sender == ALICE and ev.to == BOB and ev.amount0_in == 1 and ev.amount1_out == 2


def test_decode_v3():
    data = encode(["int256", "int256", "uint160", "uint128", "int24"], [10**18, -5 * 10**6, 2**96, 123, -887])
    ev = decode_log(_log([V3_SWAP, _topic_addr(ALICE), _topic_addr(BOB)], data))
    assert isinstance(ev, V3Swap)
    assert ev.amount0 == 10**18 and ev.amount1 == -5 * 10**6 and ev.tick == -887 and ev.recipient == BOB
    data = encode(["uint128", "uint256", "uint256"], [1, 2, 3])
    ev = decode_log(_log([V3_BURN, _topic_addr(ALICE), "0x" + "ff" * 32, "0x" + "00" * 32], data))
    assert isinstance(ev, V3Burn) and ev.amount == 1 and ev.amount1 == 3


def test_decode_garbage_is_none():
    # тот же topic0, но другая раскладка indexed-полей / обрезанные данные
    assert decode_log(_log([V2_SWAP], encode(["uint256"] * 4, [1, 0, 0, 2]))) is None
    assert decode_log(_log([V2_SYNC], b"\x01\x02")) is None
    assert decode_log(_log([], b"")) is None
    assert decode_log(_log(["0x" + "00" * 32], b"")) is None


def test_rawlog_from_rpc_dict():
    lg = RawLog.from_rpc({"address": "0xABab" + "ab" * 18, "topics": [V2_SYNC], "data": "0x" + "00" * 64,
                          "blockNumber": "0x10", "transactionHash": "0x" + "aa" * 32, "logIndex": "0x2"})
    assert lg.block_number == 16 and lg.log_index == 2 and lg.address == POOL


def test_multicall_roundtrip():
    calls = [Call(POOL, b"\x01\x02\x03\x04"), Call(ALICE, b"")]
    data = encode_aggregate3(calls)
    assert data[:4].hex() == "82ad56cb"
    ret = encode(["(bool,bytes)[]"], [[(True, b"\xaa"), (False, b"")]])
    res = decode_aggregate3(ret)
    assert [r.success for r in res] == [True, False] and res[0].data == b"\xaa"


class TestTokenDecoding:
    def test_string(self):
        assert decode_text(encode(["string"], ["PEPE"])) == "PEPE"

    def test_bytes32(self):
        # MKR/SAI возвращают bytes32
        assert decode_text(b"MKR" + b"\x00" * 29) == "MKR"

    def test_empty_and_short(self):
        assert decode_text(b"") is None
        assert decode_text(None) is None
        assert decode_text(b"\x01\x02") is None

    def test_malicious_text_sanitized(self):
        text = decode_text(encode(["string"], ["EVIL\n‮token" + "A" * 200]))
        assert "\n" not in text and "‮" not in text and len(text) <= 64

    def test_html_like_symbol_kept_for_escaping(self):
        assert sanitize_text("<b>X</b>") == "<b>X</b>"

    def test_decimals(self):
        assert decode_decimals(encode(["uint8"], [18])) == 18
        assert decode_decimals(encode(["uint256"], [6])) == 6
        assert decode_decimals(encode(["uint256"], [10**30])) is None
        assert decode_decimals(b"") is None

    def test_address(self):
        assert decode_address(encode(["address"], [ALICE])) == ALICE
        assert decode_address(b"\x01" * 32) is None
        assert decode_address(encode(["address"], ["0x" + "00" * 20])) is None
