"""Загрузка конфигурации: config.yaml + секреты из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from eth_utils import is_address


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FactoryCfg:
    name: str
    address: str  # lowercase
    version: str  # "v2" | "v3"


@dataclass(frozen=True)
class QuoteTokenCfg:
    symbol: str
    address: str  # lowercase
    # "eth" -> цена в USD берётся из пула ETH/USD, число -> фиксированная цена (стейблкоин)
    usd: str | float


@dataclass(frozen=True)
class RpcCfg:
    http_url: str
    ws_url: str | None
    block_source: str = "polling"  # polling | websocket
    poll_interval_sec: float = 2.0
    ws_idle_timeout_sec: float = 60.0
    request_timeout_sec: float = 30.0
    confirmations: int = 0
    max_lag_blocks: int = 5
    catchup_batch_blocks: int = 20
    max_catchup_blocks: int = 2000
    retry_base_delay_sec: float = 1.0
    retry_max_delay_sec: float = 60.0
    call_max_attempts: int = 5
    multicall_chunk: int = 150
    max_block_failures: int = 10


@dataclass(frozen=True)
class DetectorCfg:
    window_blocks: int = 5
    drop_pct: float = 20.0
    min_liquidity_usd: float = 50_000.0
    cooldown_min: float = 30.0
    rugpull_liquidity_drop_pct: float = 50.0
    v3_liquidity_refresh_blocks: int = 10
    state_gc_blocks: int = 5000


@dataclass(frozen=True)
class TelegramCfg:
    enabled: bool
    bot_token: str | None
    chat_id: str | None
    timeout_sec: float = 15.0


@dataclass(frozen=True)
class Config:
    rpc: RpcCfg
    factories: tuple[FactoryCfg, ...]
    quote_tokens: tuple[QuoteTokenCfg, ...]
    eth_usd_pool: str
    detector: DetectorCfg
    telegram: TelegramCfg
    sqlite_path: str
    log_level: str = "INFO"

    @property
    def factory_versions(self) -> dict[str, str]:
        return {f.address: f.version for f in self.factories}

    @property
    def factory_names(self) -> dict[str, str]:
        return {f.address: f.name for f in self.factories}

    @property
    def quote_by_address(self) -> dict[str, QuoteTokenCfg]:
        return {q.address: q for q in self.quote_tokens}

    @property
    def weth_address(self) -> str | None:
        for q in self.quote_tokens:
            if q.usd == "eth":
                return q.address
        return None


def _addr(value: Any, what: str) -> str:
    s = str(value).strip()
    if not is_address(s):
        raise ConfigError(f"{what}: некорректный адрес {s!r}")
    return s.lower()


def _section(raw: dict, name: str) -> dict:
    sec = raw.get(name) or {}
    if not isinstance(sec, dict):
        raise ConfigError(f"секция {name} должна быть словарём")
    return sec


def _pick(sec: dict, cls, **overrides) -> dict:
    """Берём из секции только известные поля dataclass'а."""
    names = set(cls.__dataclass_fields__)
    out = {k: v for k, v in sec.items() if k in names}
    out.update(overrides)
    return out


def load_config(path: str | os.PathLike = "config.yaml", env_file: str | None = ".env",
                require_rpc: bool = True) -> Config:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"файл конфига не найден: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    rpc_sec = _section(raw, "rpc")
    http_url = os.getenv("RPC_HTTP_URL", "").strip()
    ws_url = os.getenv("RPC_WS_URL", "").strip() or None
    if require_rpc and not http_url:
        raise ConfigError("RPC_HTTP_URL не задан (см. .env.example)")
    rpc = RpcCfg(**_pick(rpc_sec, RpcCfg, http_url=http_url, ws_url=ws_url))
    if rpc.block_source not in ("polling", "websocket"):
        raise ConfigError("rpc.block_source должен быть polling или websocket")
    if require_rpc and rpc.block_source == "websocket" and not ws_url:
        raise ConfigError("rpc.block_source=websocket, но RPC_WS_URL не задан")

    factories = []
    for f in raw.get("factories") or []:
        version = str(f.get("version", "")).lower()
        if version not in ("v2", "v3"):
            raise ConfigError(f"factories: version должен быть v2 или v3, получено {version!r}")
        factories.append(FactoryCfg(name=str(f.get("name") or version),
                                    address=_addr(f.get("address"), "factories"),
                                    version=version))
    if not factories:
        raise ConfigError("список factories пуст")

    quotes = []
    for q in raw.get("quote_tokens") or []:
        usd = q.get("usd", 1.0)
        if isinstance(usd, str) and usd.lower() == "eth":
            usd = "eth"
        else:
            usd = float(usd)
        quotes.append(QuoteTokenCfg(symbol=str(q.get("symbol") or "?"),
                                    address=_addr(q.get("address"), "quote_tokens"), usd=usd))
    if not quotes:
        raise ConfigError("список quote_tokens пуст")

    det = DetectorCfg(**_pick(_section(raw, "detector"), DetectorCfg))
    if det.window_blocks < 1:
        raise ConfigError("detector.window_blocks должен быть >= 1")

    tg_sec = _section(raw, "telegram")
    telegram = TelegramCfg(
        enabled=bool(tg_sec.get("enabled", True)),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        timeout_sec=float(tg_sec.get("timeout_sec", 15.0)),
    )

    storage = _section(raw, "storage")
    logging_sec = _section(raw, "logging")
    return Config(
        rpc=rpc,
        factories=tuple(factories),
        quote_tokens=tuple(quotes),
        eth_usd_pool=_addr(raw.get("eth_usd_pool"), "eth_usd_pool"),
        detector=det,
        telegram=telegram,
        sqlite_path=str(storage.get("sqlite_path", "data/bot.sqlite3")),
        log_level=str(logging_sec.get("level", "INFO")).upper(),
    )
