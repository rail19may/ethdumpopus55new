# Бот-уведомлялка о дампах токенов на Uniswap V2/V3 (Ethereum)

Бот в реальном времени читает свопы во **всех** пулах Uniswap V2 и V3 в Ethereum mainnet и
присылает уведомление (консоль + Telegram), когда цена токена в пуле резко падает.

**Бот ничего не покупает и не продаёт.** Приватные ключи не используются и не читаются, транзакции
не подписываются. Нужен только доступ на чтение к RPC-узлу Ethereum.

## Как это работает

1. **Новые блоки.** Источник head-блоков (выбирается в конфиге): HTTP-поллинг `eth_blockNumber`
   раз в ~2 с или WebSocket-подписка `newHeads`. Оба реализуют интерфейс `chain.block_source.BlockSource`.
2. **Логи.** На каждый новый блок делается **один** `eth_getLogs` без фильтра по адресу, с `topic0` из
   списка (хэши вычисляются keccak'ом из сигнатур в `chain/events.py`):
   - V2: `Sync(uint112,uint112)`, `Swap(address,uint256,uint256,uint256,uint256,address)`,
     `Burn(address,uint256,uint256,address)`
   - V3: `Swap(address,address,int256,int256,uint160,uint128,int24)`,
     `Burn(address,int24,int24,uint128,uint256,uint256)`

   Номер последнего обработанного блока хранится в SQLite. Если бот отстал больше чем на
   `max_lag_blocks`, он догоняет пачками по `catchup_batch_blocks` блоков. Если провайдер отказывает
   на большом диапазоне, диапазон делится пополам.
3. **Кэш пулов.** При первой встрече адреса через Multicall3 (`0xcA11bde05977b3631167028862bE2a173976CA11`)
   читаются `factory()`, `token0()`, `token1()`, `fee()`, а для токенов `symbol()`, `name()`, `decimals()`.
   Пул принимается, только если `factory()` входит в разрешённый список **и** сама фабрика подтверждает пул
   (`getPair` / `getPool` возвращают тот же адрес). Так отсекаются фейковые контракты, которые «врут» в
   `factory()`. Пулы чужих фабрик, фейки, пары без котируемого токена и токены без `decimals()` тоже
   кэшируются (со статусом `ignored_*`) и повторно не опрашиваются. Кривые `symbol()` (revert, `bytes32`,
   мусор, управляющие символы) обрабатываются без падения.
4. **Цена** целевого токена в котируемом (WETH/USDC/USDT) и в USD:
   - V2: из резервов события `Sync` с учётом decimals. Цена *до* свопа восстанавливается точно из
     `Sync` и `Swap` той же транзакции.
   - V3: `price = (sqrtPriceX96 / 2^96)^2 · 10^(dec0 − dec1)`, затем ориентация token0/token1.
   - ETH/USD берётся из пула Uniswap V3 WETH/USDC 0.05% (`0x88e6…5640`) по тем же событиям `Swap`.
     Стартовое значение читается из `slot0()`.
5. **Ликвидность:** V2 — `2 × резерв котируемого × USD`; V3 — `2 × balanceOf(pool) котируемого × USD`
   (грубая оценка), не чаще раза в `v3_liquidity_refresh_blocks` блоков на пул. Внеочередное обновление
   бывает после `Burn` и при подозрении на дамп.
6. **Детект.** Для каждого пула в памяти хранятся цены за последние `window_blocks` блоков. Сигнал
   срабатывает, если одновременно:
   - текущая цена ниже максимума за окно на `drop_pct`% и больше;
   - ликвидность пула ≥ `min_liquidity_usd`. Берётся максимум за окно, то есть ликвидность *до* дампа:
     сама продажа, а тем более рагпул, её уменьшает;
   - по пулу не было алерта последние `cooldown_min` минут (по времени блоков).

   Цена, действовавшая на начало окна, тоже учитывается. Поэтому дамп в редко торгуемом пуле
   (прошлая сделка 100 блоков назад) не теряется. Для V3-пула, встреченного впервые, цена и
   ликвидность на конец предыдущего блока берутся из `slot0()`/`balanceOf`.
7. **Пометки в алерте:**
   - «⚠️ возможный рагпул», если в окне были `Burn` (вывод ликвидности, V3 `Burn` с `amount=0` не
     считается) и ликвидность упала больше чем на `rugpull_liquidity_drop_pct`%;
   - главный своп: тот, что сильнее всего уронил цену (максимум отношения цены до/после).
     Показываются tx hash, продавец (`from` транзакции) и объём продажи в USD.
8. **Уведомление** уходит в консоль и Telegram (`sendMessage`, HTML) и записывается в таблицу `alerts`.

Ошибки RPC ретраятся с экспоненциальной задержкой (1, 2, 4 … до 60 с). Блок, который не удаётся
обработать `max_block_failures` раз подряд, пропускается с записью в лог. Основной цикл обёрнут в
супервизор, поэтому бот не падает при ошибках RPC.

## Установка

Нужен Python 3.11+.

```bash
git clone <repo> && cd <repo>
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # для тестов: pip install -r requirements-dev.txt
cp .env.example .env               # и заполнить своими значениями
```

### .env

```dotenv
# HTTP(S) endpoint Ethereum mainnet (Alchemy / Infura / QuickNode / свой узел)
RPC_HTTP_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
# WebSocket endpoint — только для rpc.block_source: websocket
RPC_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
# Telegram
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=-1001234567890
```

Секреты берутся только из `.env` или переменных окружения. `.env` добавлен в `.gitignore`.

### Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather) и скопируйте токен в `TELEGRAM_BOT_TOKEN`.
2. Добавьте бота в нужный чат или канал (в канале — администратором) либо просто напишите ему в личку.
3. Узнайте `chat_id`: отправьте в чат любое сообщение и откройте
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, id будет в поле `chat.id`. У каналов и групп он
   начинается с `-100`.

Если токен или chat id не заданы, бот работает, но пишет алерты только в консоль.

### RPC-провайдер

- Нужен endpoint, который поддерживает `eth_getLogs` без фильтра по адресу на диапазоне хотя бы в
  несколько блоков. Подойдут Alchemy, Infura, QuickNode или свой узел.
- Для **реплея** старых блоков (глубже ~128 блоков от head) нужен **архивный** узел: бот делает
  `eth_call` (`balanceOf`, `slot0`) на исторических блоках. Метаданные пулов в реплее читаются
  на `latest`.

## Конфигурация (`config.yaml`)

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `rpc.block_source` | `polling` | `polling` или `websocket` |
| `rpc.poll_interval_sec` | `2` | период опроса в режиме polling |
| `rpc.confirmations` | `0` | обрабатывать блок `head − N`. Значение 1–2 снижает риск алертов по реорганизованным блокам |
| `rpc.max_lag_blocks` | `5` | при большем отставании догоняем пачками |
| `rpc.catchup_batch_blocks` | `20` | размер пачки `eth_getLogs` при догонке и в реплее |
| `rpc.max_catchup_blocks` | `2000` | при старте не догонять глубже (0 — без ограничения) |
| `rpc.retry_base_delay_sec` / `retry_max_delay_sec` | `1` / `60` | экспоненциальная задержка ретраев |
| `rpc.multicall_chunk` | `150` | вызовов в одном `aggregate3` |
| `factories` | Uniswap V2 и V3 | разрешённые фабрики, список расширяемый (`name`, `address`, `version: v2/v3`) |
| `quote_tokens` | WETH, USDC, USDT | котируемые токены; `usd: eth` — цена из пула ETH/USD, число — фиксированная |
| `eth_usd_pool` | `0x88e6…5640` | пул для цены ETH в USD |
| `detector.window_blocks` | `5` | WINDOW_BLOCKS |
| `detector.drop_pct` | `20` | DROP_PCT |
| `detector.min_liquidity_usd` | `50000` | MIN_LIQUIDITY_USD |
| `detector.cooldown_min` | `30` | COOLDOWN_MIN |
| `detector.rugpull_liquidity_drop_pct` | `50` | порог падения ликвидности для пометки рагпула |
| `detector.v3_liquidity_refresh_blocks` | `10` | `balanceOf` V3-пула не чаще раза в N блоков |
| `storage.sqlite_path` | `data/bot.sqlite3` | база SQLite |

Адреса фабрик сверены с официальной документацией Uniswap (Ethereum deployments):
V2 Factory `0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f`, V3 Factory `0x1F98431c8aD98523631AE4a59f267346ea31F984`.

## Запуск

```bash
# live-режим (Ctrl+C — остановка; при следующем запуске продолжит с сохранённого блока)
python main.py

# без Telegram, с подробным логом
python main.py --no-telegram --log-level DEBUG

# реплей по историческим блокам: та же логика, сигналы только в консоль (без Telegram)
python main.py --replay --from-block 19000000 --to-block 19001000
```

Реплей в конце печатает сводку всех сигналов. Для подбора порогов удобно держать отдельный конфиг
(`--config tuning.yaml`) с другими `detector.*`. Алерты реплея тоже пишутся в SQLite
(`alerts.mode = 'replay'`), сохранённый номер блока live-режима реплей не трогает.

Пример уведомления (синтетические данные из интеграционного теста):

```
🔻 ДАМП −49.0% · PEPE (Pepe)
Токен: 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa
DEX: Uniswap V2 · пул 0x0101010101010101010101010101010101010101
Цена: 0.0001 → 0.00005102 WETH
Цена USD: $0.21 → $0.1071
Ликвидность: $300,000 (до дампа $400,000)
Блок: 1001 · 2023-11-15 01:33:32 UTC
Главный своп: 0x00000000…00000a · продажа $60,000
Продавец: 0x5e5E5e5e5E5e5E5E5e5E5E5e5e5E5E5E5e5E5E5e
🔗 Etherscan токен | Etherscan tx | DexScreener
```

## SQLite

- `pools`: кэш пулов (`status`: `tracked`, `quote_pair`, `ignored_factory`, `ignored_fake`,
  `ignored_no_quote`, `ignored_broken`)
- `tokens`: метаданные токенов
- `state`: `last_block`
- `alerts`: все алерты (цены, ликвидность, главный своп, продавец, флаг рагпула, `mode`)

```bash
sqlite3 data/bot.sqlite3 "select block_number, token_symbol, round(drop_pct,1), rugpull, main_tx from alerts order by id desc limit 20"
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_pricing.py`: математика V2/V3 на известных значениях. Среди них эталон из
  «A Primer on Uniswap v3 Math»: `sqrtPriceX96 = 2018382873588440326581633304624437` → 1 ETH = 1540.82 USDC.
- `tests/test_events_tokens.py`: topic0-хэши, декодирование событий, `symbol()` в виде `bytes32` и мусора, Multicall3.
- `tests/test_detector.py`: окна, пороги, кулдаун, рагпул, главный своп.
- `tests/test_engine.py`: интеграция на фейковой цепочке: фейковые пулы, чужие фабрики, дамп V2,
  рагпул V3, ретраи RPC, сохранение состояния.
- `tests/test_transport.py`: web3.py HTTP/WebSocket и Telegram на локальных фейковых серверах
  (ретраи, деление диапазона `eth_getLogs`, переподключение WS).

## Структура

```
main.py            запуск, CLI
config.py          загрузка config.yaml + .env
engine.py          обработка блока: логи → пулы → цены → детектор → алерты
runner.py          live-цикл (догонка, ретраи, состояние) и реплей
chain/             RPC-клиент с ретраями, источники блоков, события, Multicall3
pools/             кэш пулов, метаданные токенов
pricing/           математика V2/V3, цена ETH, ликвидность
detector/          окна цен, правила, кулдауны
notify/            формат сообщения, консоль, Telegram
storage/           SQLite
tests/
```

## Ограничения

- Мемпул не отслеживается (следующий этап): сигнал приходит после включения свопа в блок.
- Ликвидность V3 — грубая оценка по `balanceOf` (включает невостребованные комиссии, не учитывает диапазоны).
- USDC/USDT считаются равными $1 (настраивается в `quote_tokens`).
- Реорганизации цепочки не откатываются. Если это важно, поставьте `rpc.confirmations: 1`–`2`.
