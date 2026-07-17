#!/usr/bin/env python3
"""
Прогон двух конфигов (Баланс и Умеренный) на РЕАЛЬНЫХ сделках бота.
Берём те же 46 входов из logs/trades.jsonl, переигрываем выход по минутным свечам
с новыми параметрами TP/SL/DCA.

Исходные входы = реальные (символ, цена входа, время входа, qty).
Выходы пересчитываются по новым правилам.
"""
import json, os, csv
from datetime import datetime

TRADES_FILE = "logs/trades.jsonl"
KLINES_DIR = "data/klines_1m"

# ── Конфиги ──
CONFIGS = {
    "Баланс": {
        "tp_chain": [12.5, 10.0, 7.5, 5.0],  # TP по уровням DCA
        "sl": 15.0,
        "dca_max": 3,
        "trig": 15.0,
        "dca_mult": 1.0,
    },
    "Умеренный": {
        "tp_chain": [15.0, 12.5, 10.0, 5.0],
        "sl": 20.0,
        "dca_max": 3,
        "trig": 15.0,
        "dca_mult": 1.0,
    },
    # Текущий конфиг бота (для сравнения)
    "Текущий (bot)": {
        "tp_chain": [5.0, 5.0, 5.0],  # tp_pct=5, DCA×2
        "sl": 30.0,
        "dca_max": 2,
        "trig": 10.0,
        "dca_mult": 1.0,
        "trail": True,
        "trail_pct": 3.0,
        "act_pct": 1.0,
    },
}

COMMISSION = 0.055 / 100  # 0.055% round-trip → 0.0275% per side
SLIPPAGE = 0.02 / 100
FUNDING_RATE = 0.0001  # per 8h
FUNDING_INTERVAL = 8 * 3600

def load_trades():
    return [json.loads(l) for l in open(TRADES_FILE)]

def load_klines(symbol, date_str):
    """Загрузить минутные свечи за дату. Возвращает dict ts→(o,h,l,c)"""
    fname = f"{KLINES_DIR}/{symbol}_{date_str}.csv"
    klines = {}
    if not os.path.exists(fname):
        return klines
    with open(fname) as f:
        for row in csv.reader(f):
            row = [r.strip() for r in row if r.strip()]
            if len(row) < 5:
                continue
            try:
                ts = int(float(row[0]))
                o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                klines[ts] = (o, h, l, c)
            except (ValueError, IndexError):
                continue
    return klines

def load_klines_range(symbol, start_ts, end_ts):
    """Загрузить свечи с start_ts до end_ts (может быть несколько дней)"""
    klines = {}
    dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)
    while dt <= end_dt:
        date_str = dt.strftime("%Y-%m-%d")
        kl = load_klines(symbol, date_str)
        klines.update(kl)
        from datetime import timedelta
        dt = dt + timedelta(days=1)
    return klines

def replay_trade(trade, cfg):
    """
    Переиграть сделку с новым конфигом.
    Возвращает (exit_price, reason, pnl_pct, hold_sec, dca_count, dca_events)
    """
    symbol = trade["symbol"]
    entry_ts = trade["entry_ts"]
    entry_price = trade["original_entry"] if "original_entry" in trade else trade["entry_price"]
    qty = trade.get("original_qty", trade["qty"])
    
    # Загружаем свечи на 7 дней вперёд (максимум для отработки)
    end_ts = entry_ts + 7 * 86400
    klines = load_klines_range(symbol, entry_ts, end_ts)
    
    if not klines:
        # Нет данных — используем реальный выход
        return trade["exit_price"], "no_data", 0, trade["hold_sec"], 0, []
    
    # Сортируем свечи после входа
    bars = sorted([(ts, k) for ts, k in klines.items() if ts > entry_ts + 60])
    
    if not bars:
        return trade["exit_price"], "no_bars", 0, trade["hold_sec"], 0, []
    
    tp_chain = cfg["tp_chain"]
    sl_pct = cfg["sl"]
    dca_max = cfg["dca_max"]
    trig_pct = cfg["trig"]
    dca_mult = cfg["dca_mult"]
    use_trail = cfg.get("trail", False)
    trail_pct = cfg.get("trail_pct", 3.0)
    act_pct = cfg.get("act_pct", 1.0)
    
    # Состояние позиции
    avg_entry = entry_price
    current_qty = qty
    dca_count = 0
    dca_events = []
    min_price_seen = entry_price  # для trail (шорт → минимум выгоден
    trail_active = False
    trail_stop = None
    
    # DCA trigger: цена выросла на trig% от avg_entry
    # TP: цена упала на tp_chain[dca_count]% от avg_entry
    # SL: цена выросла на sl_pct от avg_entry
    
    exit_price = None
    exit_reason = None
    exit_ts = None
    
    for ts, (o, h, l, c) in bars:
        # ── Trail логика (если включена) ──
        if use_trail:
            if l < avg_entry * (1 - act_pct/100):
                trail_active = True
            if trail_active and l < min_price_seen:
                min_price_seen = l
                trail_stop = min_price_seen * (1 + trail_pct/100)
            
            if trail_active and trail_stop and o <= trail_stop:
                exit_price = trail_stop
                exit_reason = "trail"
                exit_ts = ts
                break
            if trail_active and trail_stop and h >= trail_stop:
                exit_price = trail_stop
                exit_reason = "trail"
                exit_ts = ts
                break
        
        # ── SL: цена выросла на sl_pct ──
        sl_trigger = avg_entry * (1 + sl_pct/100)
        if h >= sl_trigger:
            exit_price = sl_trigger
            exit_reason = "stop_loss"
            exit_ts = ts
            break
        
        # ── DCA: цена выросла на trig_pct (если ещё есть усреднения) ──
        if dca_count < dca_max:
            dca_trigger_price = avg_entry * (1 + trig_pct/100)
            if h >= dca_trigger_price and dca_count < len(tp_chain) - 1:
                # Усреднение: докупаем по dca_trigger_price
                dca_qty = qty * dca_mult  # ×N от оригинального qty
                dca_fill = dca_trigger_price
                new_qty = current_qty + dca_qty
                avg_entry = (avg_entry * current_qty + dca_fill * dca_qty) / new_qty
                current_qty = new_qty
                dca_count += 1
                dca_events.append({
                    "n": dca_count,
                    "ts": ts,
                    "fill": dca_fill,
                    "avg_after": avg_entry,
                    "qty_total": current_qty,
                })
                continue  # на этой свече не выходим
        
        # ── TP: цена упала на tp_chain[dca_count]% от avg_entry ──
        tp_level = tp_chain[min(dca_count, len(tp_chain)-1)]
        tp_trigger = avg_entry * (1 - tp_level/100)
        if l <= tp_trigger:
            exit_price = tp_trigger
            exit_reason = "take_profit"
            exit_ts = ts
            break
    
    if exit_price is None:
        # Не закрылась — берём последнюю цену
        exit_price = bars[-1][1][3]  # close последней свечи
        exit_reason = "timeout"
        exit_ts = bars[-1][0]
    
    # ── P&L расчёт ──
    # Шорт: pnl = (entry - exit) * qty
    gross_pnl = (avg_entry - exit_price) * current_qty
    commission = (avg_entry * current_qty + exit_price * current_qty) * COMMISSION
    slippage_cost = (avg_entry + exit_price) * current_qty * SLIPPAGE
    
    # Funding
    hold_sec = exit_ts - entry_ts
    funding_periods = hold_sec / FUNDING_INTERVAL
    # Для шорта: если funding > 0, платим; если < 0, получаем. Берём средний.
    funding_cost = avg_entry * current_qty * FUNDING_RATE * funding_periods
    
    net_pnl = gross_pnl - commission - slippage_cost - funding_cost
    pnl_pct = (avg_entry - exit_price) / avg_entry * 100  # грубый % движения
    
    return exit_price, exit_reason, net_pnl, hold_sec, dca_count, dca_events

def run_config(name, cfg, trades):
    """Прогнать конфиг по всем сделкам"""
    results = []
    balance = 1000.0  # стартовый
    
    for t in trades:
        entry_price = t["original_entry"] if "original_entry" in t else t["entry_price"]
        # Размер позиции: risk_pct от баланса
        risk_pct = 5.0  # из конфига
        position_value = balance * risk_pct / 100
        qty = position_value / entry_price
        # Подменяем qty в trade для расчёта
        t_copy = dict(t)
        t_copy["original_qty"] = qty
        t_copy["original_entry"] = entry_price
        
        exit_price, reason, net_pnl, hold_sec, dca_count, dca_events = replay_trade(t_copy, cfg)
        
        balance += net_pnl
        results.append({
            "symbol": t["symbol"],
            "date": t["date"],
            "reason": reason,
            "net_pnl": net_pnl,
            "pnl_pct": (exit_price - entry_price) / entry_price * -100,  # шорт
            "hold_sec": hold_sec,
            "dca_count": dca_count,
            "balance_after": balance,
            "original_reason": t.get("reason", "?"),
        })
    
    return results, balance

def summarize(name, results, final_balance):
    n = len(results)
    total_pnl = final_balance - 1000.0
    roi = total_pnl / 1000 * 100
    
    reasons = {}
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    
    wins = [r for r in results if r["net_pnl"] > 0]
    losses = [r for r in results if r["net_pnl"] <= 0]
    wr = len(wins) / n * 100 if n else 0
    
    avg_win = sum(r["net_pnl"] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r["net_pnl"] for r in losses) / len(losses) if losses else 0
    
    pf = abs(sum(r["net_pnl"] for r in wins)) / abs(sum(r["net_pnl"] for r in losses)) if losses and sum(r["net_pnl"] for r in losses) != 0 else float('inf')
    
    # Max drawdown
    peak = 1000.0
    max_dd = 0
    for r in results:
        if r["balance_after"] > peak:
            peak = r["balance_after"]
        dd = (r["balance_after"] - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    
    dca_used = sum(1 for r in results if r["dca_count"] > 0)
    
    print(f"\n{'='*70}")
    print(f"  КОНФИГ: {name}")
    print(f"{'='*70}")
    print(f"  Сделок: {n}")
    print(f"  Финальный баланс: ${final_balance:.2f}")
    print(f"  Общий P&L: ${total_pnl:.2f}")
    print(f"  ROI: {roi:+.1f}%")
    print(f"  Win Rate: {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor: {pf:.2f}" if pf != float('inf') else f"  Profit Factor: ∞")
    print(f"  Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}")
    print(f"  Max Drawdown: {max_dd:.1f}%")
    print(f"  DCA использовано: {dca_used}/{n} ({dca_used/n*100:.0f}%)")
    print(f"  Причины выхода: {reasons}")
    
    return {
        "name": name, "n": n, "balance": final_balance, "roi": roi,
        "wr": wr, "pf": pf, "max_dd": max_dd, "reasons": reasons,
        "dca_used": dca_used, "results": results,
    }

def main():
    trades = load_trades()
    print(f"Загружено сделок: {len(trades)}")
    print(f"Период: {trades[0]['date']} → {trades[-1]['date']}")
    print(f"Стартовый баланс: $1000")
    
    summaries = []
    for name, cfg in CONFIGS.items():
        results, balance = run_config(name, cfg, trades)
        s = summarize(name, results, balance)
        summaries.append(s)
    
    # ── Сводная таблица ──
    print(f"\n\n{'='*90}")
    print(f"  СВОДНАЯ ТАБЛИЦА (на {len(trades)} реальных сделках бота)")
    print(f"{'='*90}")
    print(f"  {'Конфиг':<20} {'Баланс':>10} {'ROI':>8} {'WR%':>6} {'PF':>6} {'MaxDD':>7} {'DCA':>5} {'Причины'}")
    print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*6} {'-'*6} {'-'*7} {'-'*5} {'-'*30}")
    for s in summaries:
        reasons_str = ", ".join(f"{k}:{v}" for k,v in s["reasons"].items())
        print(f"  {s['name']:<20} ${s['balance']:>8.2f} {s['roi']:>+7.1f}% {s['wr']:>5.1f}% {s['pf']:>5.2f} {s['max_dd']:>+6.1f}% {s['dca_used']:>4} {reasons_str}")
    
    # ── Сделки детально ──
    print(f"\n\n{'='*90}")
    print(f"  ДЕТАЛЬНО ПО СДЕЛКАМ")
    print(f"{'='*90}")
    for s in summaries:
        print(f"\n  --- {s['name']} ---")
        print(f"  {'#':>3} {'Символ':<14} {'Дата':>10} {'Причина':<12} {'P&L$':>8} {'%':>6} {'DCA':>4} {'Было':<12}")
        for i, r in enumerate(s["results"], 1):
            print(f"  {i:>3} {r['symbol']:<14} {r['date']:>10} {r['reason']:<12} {r['net_pnl']:>+8.2f} {r['pnl_pct']:>+5.1f}% {r['dca_count']:>4} {r['original_reason']:<12}")
    
    # Сохранить
    out = {"summaries": [{k:v for k,v in s.items() if k!="results"} for s in summaries],
           "details": {s["name"]: s["results"] for s in summaries}}
    json.dump(out, open("/tmp/live_replay_results.json", "w"), indent=2)
    print(f"\n\nСохранено: /tmp/live_replay_results.json")

if __name__ == "__main__":
    main()
