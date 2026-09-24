"""Модель алерта и форматирование уведомления (Telegram HTML и простой текст)."""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from eth_utils import to_checksum_address

ETHERSCAN = "https://etherscan.io"
DEXSCREENER = "https://dexscreener.com/ethereum"


@dataclass(slots=True)
class Alert:
    mode: str                  # live | replay
    block: int
    timestamp: int | None
    pool: str
    dex: str                   # "Uniswap V2" / "Uniswap V3 (0.30%)"
    token: str
    token_symbol: str
    token_name: str
    quote_symbol: str
    drop_pct: float
    price_before: float
    price_after: float
    price_before_usd: float | None
    price_after_usd: float | None
    liquidity_usd: float
    liquidity_max_usd: float
    rugpull: bool
    main_tx: str | None
    seller: str | None
    sell_usd: float | None

    def to_row(self) -> dict:
        return {
            "mode": self.mode, "block_number": self.block, "block_timestamp": self.timestamp,
            "pool": self.pool, "dex": self.dex, "token": self.token, "token_symbol": self.token_symbol,
            "token_name": self.token_name, "quote_symbol": self.quote_symbol, "drop_pct": self.drop_pct,
            "price_before": self.price_before, "price_after": self.price_after,
            "price_before_usd": self.price_before_usd, "price_after_usd": self.price_after_usd,
            "liquidity_usd": self.liquidity_usd, "main_tx": self.main_tx, "seller": self.seller,
            "sell_usd": self.sell_usd, "rugpull": self.rugpull,
            "extra": {"liquidity_max_usd": self.liquidity_max_usd},
        }


def fmt_price(value: float | None) -> str:
    """Цена с 4 значащими цифрами без экспоненты: 0.0000001234, 12.34, 1,234.5."""
    if value is None:
        return "—"
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    digits = 3 - math.floor(math.log10(abs(value)))  # 4 значащие цифры
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"${value:,.0f}"
    return "$" + fmt_price(value)


def checksum(addr: str | None) -> str:
    if not addr:
        return "—"
    try:
        return to_checksum_address(addr)
    except Exception:
        return addr


def links(a: Alert) -> dict[str, str]:
    out = {"etherscan_token": f"{ETHERSCAN}/token/{checksum(a.token)}",
           "dexscreener": f"{DEXSCREENER}/{a.pool}"}
    if a.main_tx:
        out["etherscan_tx"] = f"{ETHERSCAN}/tx/{a.main_tx}"
    return out


def _time(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_html(a: Alert) -> str:
    e = html.escape
    lk = links(a)
    lines = [f"🔻 <b>ДАМП −{a.drop_pct:.1f}%</b> · <b>{e(a.token_symbol)}</b> ({e(a.token_name)})"]
    if a.mode == "replay":
        lines[0] = "[REPLAY] " + lines[0]
    if a.rugpull:
        lines.append("⚠️ <b>возможный рагпул</b> (Burn в окне, ликвидность упала "
                     f"с {fmt_usd(a.liquidity_max_usd)} до {fmt_usd(a.liquidity_usd)})")
    lines += [
        f"Токен: <code>{checksum(a.token)}</code>",
        f"DEX: {e(a.dex)} · пул <code>{checksum(a.pool)}</code>",
        f"Цена: {fmt_price(a.price_before)} → {fmt_price(a.price_after)} {e(a.quote_symbol)}",
        f"Цена USD: {fmt_usd(a.price_before_usd)} → {fmt_usd(a.price_after_usd)}",
        f"Ликвидность: {fmt_usd(a.liquidity_usd)}"
        + (f" (до дампа {fmt_usd(a.liquidity_max_usd)})" if a.liquidity_max_usd > a.liquidity_usd * 1.05 else ""),
        f"Блок: {a.block}" + (f" · {_time(a.timestamp)}" if a.timestamp else ""),
    ]
    if a.main_tx:
        lines.append(f"Главный своп: <a href=\"{lk['etherscan_tx']}\">{a.main_tx[:10]}…{a.main_tx[-6:]}</a>"
                     f" · продажа {fmt_usd(a.sell_usd)}")
        lines.append(f"Продавец: <code>{checksum(a.seller)}</code>")
    link_parts = [f"<a href=\"{lk['etherscan_token']}\">Etherscan токен</a>"]
    if "etherscan_tx" in lk:
        link_parts.append(f"<a href=\"{lk['etherscan_tx']}\">Etherscan tx</a>")
    link_parts.append(f"<a href=\"{lk['dexscreener']}\">DexScreener</a>")
    lines.append("🔗 " + " | ".join(link_parts))
    return "\n".join(lines)


_TAG = re.compile(r"<[^>]+>")


def format_text(a: Alert) -> str:
    """Простой текст для консоли: тот же формат без HTML, со ссылками целиком."""
    body = format_html(a).split("\n")[:-1]  # без строки со ссылками-якорями
    text = html.unescape(_TAG.sub("", "\n".join(body)))
    lk = links(a)
    text += f"\nEtherscan токен: {lk['etherscan_token']}"
    if "etherscan_tx" in lk:
        text += f"\nEtherscan tx:    {lk['etherscan_tx']}"
    text += f"\nDexScreener:     {lk['dexscreener']}"
    return text
