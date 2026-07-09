#!/usr/bin/env python3
"""Short Bot #2 — paper trading на реальных данных Bybit.
Сценарий Б: монета впервые #1 по росту → откат на #2 → шорт #2.
WS kline + ticker, ранжирование, paper executor с комиссиями/проскальзыванием/фандингом."""
import asyncio, json, os, sys, time, logging, signal as sig
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import yaml
import aiohttp
import websockets

# ── Конфиг ──
CFG_PATH = os.environ.get("SHORT_BOT_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml"))
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

# ── Логирование ──
Path(CFG["log_dir"]).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CFG.get("log_level", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(CFG["log_dir"], "bot.log")),
    ],
)
log = logging.getLogger("short_bot")
dca_info = f" DCA={CFG.get('dca_enabled',False)}×{CFG.get('dca_max_count',0)} trig={CFG.get('dca_trigger_pct',0)}%" if CFG.get('dca_enabled') else ""
log.info(f"=== Short Bot #2 старт | risk={CFG['risk_pct']}% SL={CFG['sl_pct']}% TP={CFG['tp_pct']}% Trail={CFG['trail_pct']}% Act={CFG['activation_pct']}%{dca_info} ===")


def utc_now():
    return datetime.now(timezone.utc)


def day_key(dt):
    return dt.strftime("%Y-%m-%d")


# ── Состояние ──
class State:
    def __init__(self):
        self.balance = float(CFG["initial_balance"])  # paper balance USDT
        self.equity = self.balance
        self.open_positions = []  # list of dict
        self.closed_today = 0
        self.cur_day = day_key(utc_now())
        # ranking data
        self.day_open = {}        # symbol -> open price (first tick of day)
        self.last_price = {}      # symbol -> last price
        self.day_volume = {}      # symbol -> volume USDT (approx)
        self.first1_today = set() # монеты которые были #1 сегодня
        self.traded_today = set() # монеты по которым вошли (бан на день)
        self.prev_rank1 = None    # предыдущая #1
        self.last_rank_ts = 0
        # funding tracking — реальные ставки/интервалы из WS tickers (per-symbol)
        self.funding_rate = {}       # symbol -> текущий fundingRate (из tickers)
        self.next_funding_time = {}  # symbol -> nextFundingTime (ms epoch)
        self.funding_applied = {}    # symbol -> последний nextFundingTime (ms) по которому уже списали
        self.last_funding_check = 0
        self.symbols = []         # активные USDT perp символы

    def reset_day(self):
        d = day_key(utc_now())
        if d != self.cur_day:
            log.info(f"Новый день: {d} (был {self.cur_day})")
            self.cur_day = d
            self.first1_today = set()
            self.traded_today = set()
            self.prev_rank1 = None
            self.day_open.clear()
            self.day_volume.clear()
            self.closed_today = 0


STATE = State()


# ── Position sizing ──
def calc_position_size(entry_price, sl_pct, risk_pct, balance, leverage=1):
    """Размер позиции в монетах.
    risk = balance * risk_pct/100 — сколько теряем при стопе.
    При шорте: loss = qty * entry * sl_pct/100 = risk
    qty = risk / (entry * sl_pct/100) / leverage
    """
    risk_usd = balance * risk_pct / 100.0
    qty = risk_usd / (entry_price * sl_pct / 100.0)
    return qty


# ── Paper executor ──
COMMISSION = float(CFG["commission_taker_pct"]) / 100.0  # 0.00055
SLIPPAGE = float(CFG["slippage_pct"]) / 100.0             # 0.0002
# Фандинг берётся из WS tickers.{symbol} (fundingRate + nextFundingTime) —
# реальные ставки и интервалы per-symbol (1ч/2ч/4ч/8ч), не захардкожены.


def open_short(symbol, entry_price, qty, ts):
    """Открыть шорт: вход по ask с проскальзыванием + комиссия."""
    fill = entry_price * (1 + SLIPPAGE)  # шортим по ask (выше)
    notional = qty * fill
    commission = notional * COMMISSION
    pos = {
        "id": f"{symbol}_{int(ts)}",
        "symbol": symbol,
        "side": "short",
        "entry_price": fill,
        "qty": qty,
        "notional": notional,
        "entry_ts": ts,
        "sl_price": fill * (1 + CFG["sl_pct"]/100),
        "tp_price": fill * (1 - CFG["tp_pct"]/100),
        "trail_pct": CFG["trail_pct"],
        "act_price": fill * (1 - CFG["activation_pct"]/100),
        "activated": False,
        "min_price": fill,
        "commission_paid": commission,
        "funding_paid": 0.0,
        "last_funding_ts": ts,
        "entry_next_funding_ms": STATE.next_funding_time.get(symbol, 0),  # ms epoch
        "funding_rate_at_open": STATE.funding_rate.get(symbol, 0.0),
        "open_day": day_key(datetime.fromtimestamp(ts, tz=timezone.utc)),
        # ── DCA поля ──
        "dca_count": 0,                          # сколько усреднений сделано
        "original_qty": qty,                      # изначальный qty (для DCA докупки)
        "dca_trigger_price": fill * (1 + CFG.get("dca_trigger_pct", 10.0)/100),  # цена DCA триггера
        "dca_max_count": CFG.get("dca_max_count", 0),  # макс усреднений
        "dca_qty_mult": CFG.get("dca_qty_multiplier", 1.0),  # множитель докупки
        "original_entry": fill,                   # изначальный entry (для отчёта)
    }
    STATE.balance -= commission  # комиссия списывается
    STATE.open_positions.append(pos)
    dca_trig = f" DCA_trig={pos['dca_trigger_price']:.6f}({CFG.get('dca_trigger_pct',0)}%)" if CFG.get("dca_enabled") else ""
    log.info(f"OPEN SHORT {symbol} qty={qty:.6f} entry={fill:.6f} notional={notional:.2f} comm={commission:.4f} SL={pos['sl_price']:.6f} TP={pos['tp_price']:.6f}{dca_trig}")
    return pos


def close_position(pos, exit_price, reason, ts):
    """Закрыть позицию: выход по bid с проскальзыванием + комиссия + фандинг."""
    fill = exit_price * (1 - SLIPPAGE)  # шорт закрываем по bid (ниже)
    # PnL шорта: (entry - exit) * qty
    pnl = (pos["entry_price"] - fill) * pos["qty"]
    notional = pos["qty"] * fill
    commission = notional * COMMISSION
    STATE.balance -= commission
    # фандинг списывался по реальным funding event'ам (apply_funding),
    # каждый раз меняя STATE.balance напрямую. На закрытии добираем
    # фандинг за неполный период после последнего event'а по текущей ставке.
    hold_sec = ts - pos["entry_ts"]
    sym = pos["symbol"]
    last_fund_ts = pos.get("last_funding_ts", pos["entry_ts"])
    cur_rate = STATE.funding_rate.get(sym, 0.0)
    interval_sec = 28800  # дефолт 8ч; Bybit отдаёт интервал per-symbol
    frac_sec = ts - last_fund_ts
    frac = frac_sec / interval_sec if interval_sec > 0 else 0
    if frac > 1: frac = 1  # не больше одного интервала
    partial_funding = notional * cur_rate * frac
    STATE.balance += partial_funding
    pos["funding_paid"] += partial_funding
    funding = pos["funding_paid"]  # суммарный фандинг за позицию (>0=получили, <0=заплатили)
    # net_pnl — для отчёта: PnL − комиссия + фандинг (со знаком)
    net_pnl = pnl - commission + funding
    # balance: commission уже списан, фандинг учтён по event'ам + partial → добавляем PnL
    STATE.balance += pnl
    # запись сделки
    trade = {
        "id": pos["id"], "symbol": pos["symbol"], "side": "short",
        "entry_price": pos["entry_price"], "exit_price": fill, "qty": pos["qty"],
        "pnl": pnl, "commission": commission + pos["commission_paid"],
        "funding": funding + pos["funding_paid"],
        "net_pnl": net_pnl, "reason": reason,
        "entry_ts": pos["entry_ts"], "exit_ts": ts,
        "hold_sec": hold_sec,
        "entry_price_pct": (pos["entry_price"] - fill) / pos["entry_price"] * 100,
        "dca_count": pos.get("dca_count", 0),
        "original_entry": pos.get("original_entry", pos["entry_price"]),
        "balance_after": STATE.balance,
        "date": day_key(datetime.fromtimestamp(ts, tz=timezone.utc)),
    }
    write_trade(trade)
    log.info(f"CLOSE {pos['symbol']} reason={reason} exit={fill:.6f} pnl={pnl:.4f} net={net_pnl:.4f} comm={commission:.4f} fund={funding:.4f} hold={hold_sec}s balance={STATE.balance:.2f}")
    STATE.open_positions.remove(pos)
    STATE.closed_today += 1


def apply_funding(pos, ts):
    """Применить фандинг по реальным funding event'ам Bybit.

    Контроль времени экспирации: nextFundingTime из ticker может меняться,
    Bybit обновляет его после event'а. Чтобы не пропустить/не задвоить:
      - отслеживаем последний применённый event ts (pos['last_funding_ts'])
      - тянем /v5/market/funding/history для точных прошлых событий
      - применяем все пропущенные event'ы между last_funding_ts и ts

    Конвенция Bybit:
      rate > 0 → лонги платят шортам → шорт ПОЛУЧАЕТ (balance растёт)
      rate < 0 → шорты платят лонгам → шорт ПЛАТИТ  (balance падает)
    Для шорта: Δbalance = +notional*rate  (rate>0 → плюс, rate<0 → минус).
    """
    sym = pos["symbol"]
    last_fund_ts = pos.get("last_funding_ts", pos["entry_ts"])
    # быстрый путь: проверим nextFundingTime из ticker
    nf_ms = STATE.next_funding_time.get(sym)
    nf_ts = nf_ms / 1000.0 if nf_ms else None

    # если nextFundingTime есть и ещё не настал — ничего не делаем
    if nf_ts and ts < nf_ts:
        return

    # nextFundingTime настал/прошёл — значит был event.
    # Но nextFundingTime мог уже обновиться на следующий → тянем history
    # чтобы поймать все event'ы между last_fund_ts и ts
    events = fetch_funding_history(sym, last_fund_ts, ts)
    if not events:
        # history пуста — возможно API лагает, пробуем по ticker rate
        if nf_ts and ts >= nf_ts and STATE.funding_applied.get(sym) != nf_ms:
            rate = STATE.funding_rate.get(sym, 0.0)
            cur_price = STATE.last_price.get(sym, pos["entry_price"])
            cur_notional = pos["qty"] * cur_price
            funding = cur_notional * rate
            _apply_one_funding(pos, sym, nf_ts, rate, cur_notional, funding)
        return

    # применяем все пропущенные event'ы из history (точные ставка + время)
    for ev in events:
        ev_ts, ev_rate = ev
        if ev_ts <= last_fund_ts:
            continue  # уже применён
        cur_price = STATE.last_price.get(sym, pos["entry_price"])
        cur_notional = pos["qty"] * cur_price
        funding = cur_notional * ev_rate
        _apply_one_funding(pos, sym, ev_ts, ev_rate, cur_notional, funding)


def _apply_one_funding(pos, sym, ev_ts, rate, cur_notional, funding):
    """Применить один funding event к позиции и балансу."""
    STATE.balance += funding
    pos["funding_paid"] += funding  # >0 = получили, <0 = заплатили
    pos["last_funding_ts"] = ev_ts
    nf_ms = STATE.next_funding_time.get(sym)
    if nf_ms:
        STATE.funding_applied[sym] = nf_ms
    direction = "RECEIVE" if funding >= 0 else "PAY"
    log.info(f"FUNDING {sym} rate={rate*100:.4f}% notional={cur_notional:.2f} {direction}={funding:+.4f} event_ts={ev_ts:.0f} balance={STATE.balance:.2f}")


# кэш funding history чтобы не дёргать API каждый тик
_funding_history_cache = {}  # sym -> (last_fetch_ts, events)


def fetch_funding_history(sym, since_ts, until_ts):
    """Получить прошлые funding events из Bybit /v5/market/funding/history.

    Возвращает [(event_ts, rate), ...] отсортированные по возрастанию.
    Кэширует на 60с чтобы не спамить API.
    """
    now = time.time()
    cached = _funding_history_cache.get(sym)
    if cached and (now - cached[0]) < 60:
        events = cached[1]
    else:
        url = f"{CFG['bybit_rest_url']}/v5/market/funding/history?category=linear&symbol={sym}&limit=50"
        events = []
        try:
            import aiohttp as _aio
            # синхронный запрос через requests нет — используем urllib
            import urllib.request, json as _json
            req = urllib.request.Request(url, headers={"User-Agent": "short-bot"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            for r in data.get("result", {}).get("list", []):
                # Bybit: {fundingRate, fundingRateTimestamp}
                try:
                    ev_ts = int(r.get("fundingRateTimestamp", 0)) / 1000.0
                    ev_rate = float(r.get("fundingRate", 0))
                    events.append((ev_ts, ev_rate))
                except (TypeError, ValueError):
                    pass
            events.sort(key=lambda x: x[0])
        except Exception as e:
            log.debug(f"funding history {sym}: {e}")
            return []
        _funding_history_cache[sym] = (now, events)
    # фильтруем по окну [since_ts, until_ts]
    return [(t, r) for t, r in events if since_ts < t <= until_ts]


def dca_average(pos, dca_price, ts):
    """Усреднить шорт: докупаем qty по dca_price, пересчитываем avg entry, SL, TP, trail.
    Для шорта: DCA при росте цены → докупаем выше → avg entry повышается.
    Новый avg = (old_entry×old_qty + dca_price×new_qty) / total_qty
    """
    new_qty = pos["original_qty"] * pos["dca_qty_mult"]
    fill = dca_price * (1 + SLIPPAGE)  # докупка по ask
    old_cost = pos["entry_price"] * pos["qty"]
    new_cost = fill * new_qty
    total_qty = pos["qty"] + new_qty
    avg_entry = (old_cost + new_cost) / total_qty
    commission = fill * new_qty * COMMISSION

    # Обновляем позицию
    old_entry = pos["entry_price"]
    pos["entry_price"] = avg_entry
    pos["qty"] = total_qty
    pos["notional"] = total_qty * avg_entry
    pos["commission_paid"] += commission
    pos["dca_count"] += 1

    # Пересчитываем SL/TP/Act от нового avg entry
    pos["sl_price"] = avg_entry * (1 + CFG["sl_pct"]/100)
    pos["tp_price"] = avg_entry * (1 - CFG["tp_pct"]/100)
    pos["act_price"] = avg_entry * (1 - CFG["activation_pct"]/100)
    pos["dca_trigger_price"] = avg_entry * (1 + CFG.get("dca_trigger_pct", 10.0)/100)

    # Сбрасываем активацию — trail пересчитается от нового avg
    pos["activated"] = False
    # min_price оставляем как есть — реальный минимум цены

    STATE.balance -= commission
    log.info(f"DCA#{pos['dca_count']} {pos['symbol']} avg={old_entry:.6f}→{avg_entry:.6f} "
             f"qty={total_qty:.6f} dca_fill={fill:.6f} comm={commission:.4f} "
             f"new_SL={pos['sl_price']:.6f} new_TP={pos['tp_price']:.6f}")


def process_position(pos, high, low, price, ts):
    """Проверить DCA/SL/TP/Trail по цене."""
    # anti-noise gate
    if ts - pos["entry_ts"] < CFG["min_sl_hold_seconds"]:
        return
    # update min_price
    if low < pos["min_price"]:
        pos["min_price"] = low
    # ── DCA: усреднение при росте на dca_trigger_pct% ──
    # После DCA — continue (как в бэктесте): TP/SL/Trail проверяются со след. свечи
    # Иначе на той же свече low мог достичь TP, а high — trigger, порядок неопределён
    if CFG.get("dca_enabled") and pos["dca_count"] < pos["dca_max_count"]:
        if high >= pos["dca_trigger_price"]:
            dca_average(pos, pos["dca_trigger_price"], ts)
            return  # пропускаем проверки на этой свече (как continue в бэктесте)
    # активация trail — при падении на act% от avg entry
    # SL остаётся на sl_pct% от avg (как в бэктесте), НЕ переносится на breakeven
    if not pos["activated"] and pos["min_price"] <= pos["act_price"]:
        pos["activated"] = True
        log.info(f"TRAIL ACTIVATED {pos['symbol']} min={pos['min_price']:.6f} act={pos['act_price']:.6f}")
    # TP (монета упала) — проверяем ПЕРВЫМ, не давая trail перебить тейк-профит
    if low <= pos["tp_price"]:
        close_position(pos, pos["tp_price"], "take_profit", ts)
        return
    # SL (монета выросла против шорта) — может быть breakeven после активации
    if high >= pos["sl_price"]:
        reason = "breakeven" if pos["activated"] and pos["sl_price"] <= pos["entry_price"] * 1.0001 else "stop_loss"
        close_position(pos, pos["sl_price"], reason, ts)
        return
    # Trail stop — ТОЛЬКО в профите: стоп = min×(1+trail%), работает когда < entry
    # Если trail_stop >= entry → трейл не активен (стоп выше entry = не имеет смысла для шорта)
    if pos["activated"]:
        trail_stop = pos["min_price"] * (1 + pos["trail_pct"]/100)
        if trail_stop < pos["entry_price"] and high >= trail_stop:
            close_position(pos, trail_stop, "trail", ts)
            return


# ── Ранжирование ──
def get_ranking():
    """Возвращает [(symbol, gain_pct, price), ...] отсортированный по убыванию роста."""
    gains = []
    for sym, price in STATE.last_price.items():
        op = STATE.day_open.get(sym)
        if not op or op <= 0:
            continue
        g = (price - op) / op * 100
        if g >= CFG["min_gain_pct"]:
            vol = STATE.day_volume.get(sym, 0)
            if vol >= CFG["min_volume_usd"]:
                gains.append((sym, g, price))
    gains.sort(key=lambda x: x[1], reverse=True)
    return gains


def check_signal(ts):
    """Проверить сигнал: #1 впервые → откат на #2 → шорт #2."""
    if len(STATE.open_positions) >= CFG["max_open_positions"]:
        return
    if STATE.closed_today >= CFG["max_daily_entries"]:
        return
    ranking = get_ranking()
    if len(ranking) < 2:
        STATE.prev_rank1 = None
        return
    c1, c2 = ranking[0][0], ranking[1][0]
    # c1 стала #1 впервые
    if c1 not in STATE.first1_today and c1 not in STATE.traded_today:
        STATE.first1_today.add(c1)
    # сигнал: c2 БЫЛА #1 на прошлом шаге, сейчас упала на #2
    if (STATE.prev_rank1 == c2 and c2 in STATE.first1_today
            and c2 not in STATE.traded_today and c2 != c1):
        entry = ranking[1][2]
        gain = ranking[1][1]
        qty = calc_position_size(entry, CFG["sl_pct"], CFG["risk_pct"], STATE.balance)
        if qty > 0:
            open_short(c2, entry, qty, ts)
            STATE.traded_today.add(c2)
    STATE.prev_rank1 = c1


# ── Bybit: получить список символов ──
async def fetch_symbols():
    """USDT linear perp, статус Trading, исключить heavy."""
    url = f"{CFG['bybit_rest_url']}/v5/market/instruments-info?category=linear"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = await resp.json()
    syms = []
    heavy = set(CFG.get("heavy_symbols", [])) if CFG.get("exclude_heavy") else set()
    for r in data["result"]["list"]:
        s = r["symbol"]
        if (s.endswith("USDT") and not s.endswith("PERP")
                and r.get("status") == "Trading" and s not in heavy
                and s not in set(CFG.get("exclude_symbols", []) or [])):
            syms.append(s)
    log.info(f"Символов для торговли: {len(syms)}")
    return syms


# ── Bybit WS ──
async def ws_subscribe(symbols, ws):
    """Подписаться на kline 1m + ticker для всех символов."""
    # Bybit V5 public kline: kline.1 (1m), kline.5, kline.15 — НЕ kline.1m
    # tickers.{symbol} — last price + 24h volume
    args_kline = [f"kline.1.{s}" for s in symbols]
    args_ticker = [f"tickers.{s}" for s in symbols]
    # bybit WS принимает до ~1000 args за раз, но безопаснее батчами по 200
    for i in range(0, len(args_kline), 200):
        batch = args_kline[i:i+200]
        await ws.send(json.dumps({"op": "subscribe", "args": batch}))
    for i in range(0, len(args_ticker), 200):
        batch = args_ticker[i:i+200]
        await ws.send(json.dumps({"op": "subscribe", "args": batch}))
    log.info(f"Подписан на {len(args_kline)} kline + {len(args_ticker)} tickers")


async def ws_handler(symbols):
    """Главный WS цикл."""
    uri = CFG["bybit_ws_url"]
    reconnect = 0
    while True:
        try:
            log.info(f"WS подключение #{reconnect} к {uri}")
            async with websockets.connect(uri, ping_interval=CFG["bybit_ws_ping_sec"], ping_timeout=None, max_size=2**24) as ws:
                await ws_subscribe(symbols, ws)
                reconnect = 0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except:
                        continue
                    topic = msg.get("topic", "")
                    data = msg.get("data", [])
                    ts = time.time()
                    # ── kline update ──
                    if topic.startswith("kline.1."):
                        sym = topic.split(".")[-1]
                        if isinstance(data, list):
                            for k in data:
                                process_kline(sym, k, ts)
                        elif isinstance(data, dict):
                            process_kline(sym, data, ts)
                    # ── ticker update ──
                    elif topic.startswith("tickers."):
                        sym = topic.split(".")[-1]
                        if isinstance(data, dict):
                            process_ticker(sym, data, ts)
                    # ── периодические задачи ──
                    STATE.reset_day()
                    # фандинг для открытых позиций
                    for pos in list(STATE.open_positions):
                        apply_funding(pos, ts)
                    # проверка сигнала по интервалу
                    if ts - STATE.last_rank_ts >= CFG["ranking_interval_min"] * 60:
                        check_signal(ts)
                        STATE.last_rank_ts = ts
                    # сохранение equity
                    STATE.equity = STATE.balance + sum(
                        (p["entry_price"] - STATE.last_price.get(p["symbol"], p["entry_price"])) * p["qty"]
                        for p in STATE.open_positions
                    )
        except Exception as e:
            reconnect += 1
            wait = min(5 * reconnect, 60)
            log.error(f"WS ошибка: {e}, реконнект через {wait}s")
            await asyncio.sleep(wait)


def process_kline(sym, k, ts):
    """Обработать 1m свечу: обновить day_open, last_price, day_volume."""
    try:
        # Bybit kline: start, open, high, low, close, volume, turnover
        open_p = float(k.get("open", 0))
        high = float(k.get("high", 0))
        low = float(k.get("low", 0))
        close = float(k.get("close", 0))
        vol = float(k.get("volume", 0))
        turnover = float(k.get("turnover", 0))
    except (TypeError, ValueError):
        return
    if close <= 0:
        return
    # first candle of day → set day_open
    if sym not in STATE.day_open:
        STATE.day_open[sym] = open_p if open_p > 0 else close
    STATE.last_price[sym] = close
    STATE.day_volume[sym] = STATE.day_volume.get(sym, 0) + turnover
    # проверить открытые позиции по этой свече
    for pos in list(STATE.open_positions):
        if pos["symbol"] == sym:
            process_position(pos, high, low, close, ts)


def process_ticker(sym, data, ts):
    """Обработать ticker: last price, 24h volume, fundingRate, nextFundingTime."""
    try:
        last = data.get("lastPrice")
        if last:
            STATE.last_price[sym] = float(last)
        vol24 = data.get("turnover24h")
        if vol24 and sym not in STATE.day_volume:
            # если нет kline данных, используем 24h turnover как приближение
            STATE.day_volume[sym] = float(vol24)
        # funding rate (перевод в долю: Bybit отдаёт как строку, напр. "0.0001")
        fr = data.get("fundingRate")
        if fr is not None and fr != "":
            STATE.funding_rate[sym] = float(fr)
        # next funding time (ms epoch) — когда произойдёт следующий funding event
        nft = data.get("nextFundingTime")
        if nft is not None and nft != "":
            nft_ms = int(nft)
            # если Bybit прислал новый nextFundingTime — сбрасываем отметку "уже списано"
            if STATE.next_funding_time.get(sym) != nft_ms:
                old = STATE.next_funding_time.get(sym)
                STATE.next_funding_time[sym] = nft_ms
                STATE.funding_applied.pop(sym, None)
                # контроль экспирации: логируем изменение для открытых позиций
                if any(p["symbol"] == sym for p in STATE.open_positions):
                    old_str = f"{old/1000:.0f}" if old else "none"
                    log.info(f"FUNDING-SCHEDULE {sym}: nextFundingTime {old_str} → {nft_ms/1000:.0f} (UTC {datetime.fromtimestamp(nft_ms/1000, tz=timezone.utc).strftime('%H:%M')})")
    except (TypeError, ValueError):
        pass


# ── Логирование сделок ──
def write_trade(trade):
    path = CFG["trades_file"]
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(trade, ensure_ascii=False) + "\n")


def save_positions():
    path = CFG["positions_file"]
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "balance": STATE.balance,
            "equity": STATE.equity,
            "open": STATE.open_positions,
            "day": STATE.cur_day,
            "closed_today": STATE.closed_today,
            "updated": utc_now().isoformat(),
        }, f, ensure_ascii=False, indent=2, default=str)


def load_positions():
    """Загрузить состояние из positions.json при старте — чтобы рестарт не сбрасывал позиции."""
    path = CFG["positions_file"]
    if not os.path.exists(path):
        log.info("load_positions: файл не найден, старт с чистого листа")
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"load_positions: ошибка чтения {e}, старт с чистого листа")
        return
    # восстанавливаем только если день совпадает (не загружаем вчерашние позиции в новый день)
    today = day_key(utc_now())
    saved_day = data.get("day")
    if saved_day != today:
        log.info(f"load_positions: файл за {saved_day}, сегодня {today} — старт с чистого листа")
        return
    open_pos = data.get("open", [])
    if not open_pos:
        log.info(f"load_positions: открытых позиций нет, balance=${data.get('balance',0):.2f}")
        STATE.balance = float(data.get("balance", STATE.balance))
        STATE.equity = float(data.get("equity", STATE.balance))
        STATE.closed_today = int(data.get("closed_today", 0))
        return
    # восстанавливаем позиции
    STATE.balance = float(data.get("balance", STATE.balance))
    STATE.equity = float(data.get("equity", STATE.balance))
    STATE.closed_today = int(data.get("closed_today", 0))
    STATE.open_positions = open_pos
    # traded_today — монеты по которым уже входили (чтобы не входить повторно)
    STATE.traded_today = {p["symbol"] for p in open_pos}
    log.info(f"load_positions: восстановлено {len(open_pos)} позиций, balance=${STATE.balance:.2f}, "
             f"traded_today={STATE.traded_today}")


async def periodic_save():
    while True:
        save_positions()
        # equity snapshot
        with open(CFG["equity_file"], "a") as f:
            f.write(json.dumps({"ts": time.time(), "balance": STATE.balance, "equity": STATE.equity,
                                "open": len(STATE.open_positions)}) + "\n")
        await asyncio.sleep(300)  # каждые 5 мин


# ── Статус ──
async def periodic_status():
    while True:
        log.info(f"STATUS balance={STATE.balance:.2f} equity={STATE.equity:.2f} open={len(STATE.open_positions)} "
                 f"closed_today={STATE.closed_today} symbols={len(STATE.last_price)} "
                 f"day_open={len(STATE.day_open)} first1={len(STATE.first1_today)} traded={len(STATE.traded_today)} "
                 f"funding_known={len(STATE.funding_rate)}")
        await asyncio.sleep(120)  # каждые 2 мин


# ── Main ──
async def main():
    load_positions()  # восстановить позиции и баланс после рестарта
    symbols = await fetch_symbols()
    STATE.symbols = symbols
    # для каждого символа ставим day_open из первого тикера
    tasks = [
        asyncio.create_task(ws_handler(symbols)),
        asyncio.create_task(periodic_save()),
        asyncio.create_task(periodic_status()),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
        save_positions()
