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
log.info(f"=== Short Bot #2 старт | risk={CFG['risk_pct']}% SL={CFG['sl_pct']}% TP={CFG['tp_pct']}% Trail={CFG['trail_pct']}% Act={CFG['activation_pct']}% ===")


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
        # funding tracking
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
FUNDING_INTERVAL = float(CFG["funding_interval_hours"]) * 3600
FUNDING_RATE = float(CFG["funding_rate_avg"])


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
        "open_day": day_key(datetime.fromtimestamp(ts, tz=timezone.utc)),
    }
    STATE.balance -= commission  # комиссия списывается
    STATE.open_positions.append(pos)
    log.info(f"OPEN SHORT {symbol} qty={qty:.6f} entry={fill:.6f} notional={notional:.2f} comm={commission:.4f} SL={pos['sl_price']:.6f} TP={pos['tp_price']:.6f}")
    return pos


def close_position(pos, exit_price, reason, ts):
    """Закрыть позицию: выход по bid с проскальзыванием + комиссия + фандинг."""
    fill = exit_price * (1 - SLIPPAGE)  # шорт закрываем по bid (ниже)
    # PnL шорта: (entry - exit) * qty
    pnl = (pos["entry_price"] - fill) * pos["qty"]
    notional = pos["qty"] * fill
    commission = notional * COMMISSION
    STATE.balance -= commission
    # фандинг: за время удержания
    hold_sec = ts - pos["entry_ts"]
    funding_periods = hold_sec / FUNDING_INTERVAL
    # шорт платит фандинг если rate > 0 (обычно при пампе rate позитивный)
    funding = pos["notional"] * FUNDING_RATE * funding_periods
    STATE.balance -= funding
    net_pnl = pnl - commission - funding
    STATE.balance += net_pnl  # PnL добавляется к балансу
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
        "balance_after": STATE.balance,
        "date": day_key(datetime.fromtimestamp(ts, tz=timezone.utc)),
    }
    write_trade(trade)
    log.info(f"CLOSE {pos['symbol']} reason={reason} exit={fill:.6f} pnl={pnl:.4f} net={net_pnl:.4f} comm={commission:.4f} fund={funding:.4f} hold={hold_sec}s balance={STATE.balance:.2f}")
    STATE.open_positions.remove(pos)
    STATE.closed_today += 1


def apply_funding(pos, ts):
    """Применить фандинг при пересечении интервала."""
    elapsed = ts - pos["last_funding_ts"]
    if elapsed >= FUNDING_INTERVAL:
        periods = int(elapsed // FUNDING_INTERVAL)
        funding = pos["notional"] * FUNDING_RATE * periods
        STATE.balance -= funding
        pos["funding_paid"] += funding
        pos["last_funding_ts"] += periods * FUNDING_INTERVAL
        log.debug(f"FUNDING {pos['symbol']} periods={periods} paid={funding:.4f}")


def process_position(pos, high, low, price, ts):
    """Проверить SL/TP/Trail по цене."""
    # anti-noise gate
    if ts - pos["entry_ts"] < CFG["min_sl_hold_seconds"]:
        return
    # update min_price
    if low < pos["min_price"]:
        pos["min_price"] = low
    # активация trail
    if not pos["activated"] and pos["min_price"] <= pos["act_price"]:
        pos["activated"] = True
        log.info(f"TRAIL ACTIVATED {pos['symbol']} min={pos['min_price']:.6f} act={pos['act_price']:.6f}")
    # trail stop
    if pos["activated"]:
        trail_stop = pos["min_price"] * (1 + pos["trail_pct"]/100)
        if high >= trail_stop and pos["min_price"] < pos["entry_price"]:
            close_position(pos, trail_stop, "trail", ts)
            return
    # SL (монета выросла против шорта)
    if high >= pos["sl_price"]:
        close_position(pos, pos["sl_price"], "stop_loss", ts)
        return
    # TP (монета упала)
    if low <= pos["tp_price"]:
        close_position(pos, pos["tp_price"], "take_profit", ts)
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
    """Обработать ticker: last price, 24h volume."""
    try:
        last = data.get("lastPrice")
        if last:
            STATE.last_price[sym] = float(last)
        vol24 = data.get("turnover24h")
        if vol24 and sym not in STATE.day_volume:
            # если нет kline данных, используем 24h turnover как приближение
            STATE.day_volume[sym] = float(vol24)
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
                 f"day_open={len(STATE.day_open)} first1={len(STATE.first1_today)} traded={len(STATE.traded_today)}")
        await asyncio.sleep(120)  # каждые 2 мин


# ── Main ──
async def main():
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
