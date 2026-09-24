"""Бот-уведомлялка о дампах токенов на Uniswap V2/V3 (Ethereum). Только чтение: никакой торговли.

Запуск:
    python main.py                                   # live-режим
    python main.py --replay --from-block X --to-block Y
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from chain.block_source import BlockSource, PollingBlockSource, WebSocketBlockSource
from chain.rpc import RpcClient, backoff_delay
from config import Config, ConfigError, load_config
from engine import Engine
from notify.format import fmt_usd
from notify.notifiers import ConsoleNotifier, MultiNotifier, Notifier, TelegramNotifier
from runner import LiveRunner, run_replay
from storage.db import Database

log = logging.getLogger("dumpbot")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Уведомления о резких падениях цены токенов на Uniswap V2/V3")
    p.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    p.add_argument("--env", default=".env", help="путь к .env с секретами")
    p.add_argument("--replay", action="store_true", help="режим реплея по историческим блокам")
    p.add_argument("--from-block", type=int, help="первый блок реплея (включительно)")
    p.add_argument("--to-block", type=int, help="последний блок реплея (включительно)")
    p.add_argument("--no-telegram", action="store_true", help="не отправлять уведомления в Telegram")
    p.add_argument("--log-level", help="уровень логирования (DEBUG/INFO/WARNING)")
    args = p.parse_args(argv)
    if args.replay:
        if args.from_block is None or args.to_block is None:
            p.error("для --replay нужны --from-block и --to-block")
        if args.from_block > args.to_block:
            p.error("--from-block должен быть не больше --to-block")
    elif args.from_block is not None or args.to_block is not None:
        p.error("--from-block/--to-block используются только вместе с --replay")
    return args


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("web3", "websockets", "aiohttp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_notifier(cfg: Config, replay: bool, no_telegram: bool) -> Notifier:
    notifiers: list[Notifier] = [ConsoleNotifier(prefix=cfg.telegram.message_prefix)]
    if replay or no_telegram or not cfg.telegram.enabled:
        return MultiNotifier(notifiers)
    if cfg.telegram.bot_token and cfg.telegram.chat_id:
        notifiers.append(TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id,
                                          cfg.telegram.timeout_sec, prefix=cfg.telegram.message_prefix))
        log.info("Telegram-уведомления включены (чат %s)", cfg.telegram.chat_id)
    else:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — уведомления только в консоль")
    return MultiNotifier(notifiers)


def build_source(cfg: Config, rpc: RpcClient) -> BlockSource:
    if cfg.rpc.block_source == "websocket":
        assert cfg.rpc.ws_url
        return WebSocketBlockSource(cfg.rpc.ws_url, idle_timeout_sec=cfg.rpc.ws_idle_timeout_sec,
                                    retry_base=cfg.rpc.retry_base_delay_sec,
                                    retry_max=cfg.rpc.retry_max_delay_sec)
    return PollingBlockSource(rpc, cfg.rpc.poll_interval_sec)


def make_rpc(cfg: Config) -> RpcClient:
    return RpcClient(cfg.rpc.http_url, timeout=cfg.rpc.request_timeout_sec,
                     retry_base=cfg.rpc.retry_base_delay_sec, retry_max=cfg.rpc.retry_max_delay_sec,
                     call_attempts=cfg.rpc.call_max_attempts)


async def live(cfg: Config, args: argparse.Namespace) -> None:
    db = Database(cfg.sqlite_path)
    rpc = make_rpc(cfg)
    notifier = build_notifier(cfg, replay=False, no_telegram=args.no_telegram)
    attempt = 0
    try:
        while True:
            # Супервизор: любая непредвиденная ошибка -> пауза и перезапуск цикла с сохранённого блока.
            engine = Engine(cfg, rpc, db, notifier, mode="live")
            runner = LiveRunner(cfg, rpc, db, engine, build_source(cfg, rpc))
            try:
                await runner.run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                attempt += 1
                delay = backoff_delay(attempt, cfg.rpc.retry_base_delay_sec, cfg.rpc.retry_max_delay_sec)
                log.exception("основной цикл упал, перезапуск через %.1f с", delay)
                await asyncio.sleep(delay)
    finally:
        await notifier.close()
        await rpc.close()
        db.close()


async def replay(cfg: Config, args: argparse.Namespace) -> None:
    db = Database(cfg.sqlite_path)
    rpc = make_rpc(cfg)
    notifier = build_notifier(cfg, replay=True, no_telegram=True)
    try:
        engine = Engine(cfg, rpc, db, notifier, mode="replay")
        alerts = await run_replay(cfg, rpc, engine, args.from_block, args.to_block)
        print(f"\nРеплей {args.from_block}-{args.to_block}: сигналов {len(alerts)}")
        for a in alerts:
            print(f"  блок {a.block}  {a.token_symbol:<12} {a.dex:<22} −{a.drop_pct:5.1f}%  "
                  f"ликв. {fmt_usd(a.liquidity_max_usd):>12}  {'РАГПУЛ? ' if a.rugpull else ''}{a.pool}")
    finally:
        await notifier.close()
        await rpc.close()
        db.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    setup_logging(args.log_level or cfg.log_level)

    coro = replay(cfg, args) if args.replay else live(cfg, args)
    loop = asyncio.new_event_loop()
    task = loop.create_task(coro)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        loop.run_until_complete(task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("остановлено")
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
