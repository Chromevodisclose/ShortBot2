#!/usr/bin/env python3
"""
Пересчёт 46 live-сделок бота по УМЕРЕННОМУ конфигу.
Логика sim_fast из grid (строка 136-148) — проверяет DCA первым, потом TP, потом SL.
TP/SL/DCA trig считаются от avg_entry (как в grid).
Position sizing: 5% от баланса на сделку, сложный процент.
"""
import json, os, csv, math
from datetime import datetime, timedelta
from collections import Counter

KLINES_DIR = "data/klines_1m"
COMMISSION_PCT = 0.055   # round-trip %
SLIPPAGE_PCT = 0.02
FUNDING_RATE = 0.0001    # per 8h
FUNDING_INTERVAL = 8 * 3600

# УМЕРЕННЫЙ конфиг
TP_CHAIN = [15.0, 12.5, 10.0, 5.0]
SL_PCT = 20.0
DCA_MAX = 3
TRIG_PCT = 15.0
DCA_MULT = 1.0
RISK_PCT = 5.0  # от баланса на сделку
INITIAL_BALANCE = 1000.0

def load_klines_range(symbol, start_ts, days=7):
    klines = {}
    dt = datetime.utcfromtimestamp(start_ts)
    for d in range(days + 1):
        ds = dt.strftime("%Y-%m-%d")
        fname = f"{KLINES_DIR}/{symbol}_{ds}.csv"
        if os.path.exists(fname):
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
        dt += timedelta(days=1)
    return klines

def sim_moderate(entry_price, qty, klines, entry_ts):
    """
    Переигрываем сделку по Умеренному. 
    Возвращает: exit_price, reason, gross_pnl, dca_count, dca_events, exit_ts, hold_sec
    Логика = sim_fast из grid, но с $ размерами и funding.
    """
    avg = entry_price
    current_qty = qty
    orig_qty = qty
    dca = 0
    dca_events = []
    
    tp_level = TP_CHAIN[0]
    sl_price = avg * (1 + SL_PCT/100)
    tp_price = avg * (1 - tp_level/100)
    trig_price = avg * (1 + TRIG_PCT/100)
    
    bars = sorted([(ts, k) for ts, k in klines.items() if ts > entry_ts + 60])
    
    if not bars:
        return None, "no_data", 0, 0, [], entry_ts, 0
    
    exit_price = None
    exit_reason = None
    exit_ts = None
    
    for ts, (o, h, l, c) in bars:
        # 1. DCA — проверяем ПЕРВЫМ (как в grid)
        if dca < DCA_MAX and h >= trig_price:
            fill = trig_price * (1 + SLIPPAGE_PCT/100)
            dca_qty = orig_qty * DCA_MULT
            avg = (avg * current_qty + fill * dca_qty) / (current_qty + dca_qty)
            current_qty += dca_qty
            dca += 1
            dca_events.append({"n": dca, "fill": fill, "avg_after": avg, "qty": current_qty})
            # Пересчитываем уровни от нового avg
            tp_level = TP_CHAIN[min(dca, len(TP_CHAIN)-1)]
            sl_price = avg * (1 + SL_PCT/100)
            tp_price = avg * (1 - tp_level/100)
            trig_price = avg * (1 + TRIG_PCT/100)
            continue
        
        # 2. TP
        if l <= tp_price:
            exit_price = tp_price
            exit_reason = "take_profit"
            exit_ts = ts
            break
        
        # 3. SL
        if h >= sl_price:
            exit_price = sl_price
            exit_reason = "stop_loss"
            exit_ts = ts
            break
    
    if exit_price is None:
        # Timeout — берём close последней свечи
        exit_price = bars[-1][1][3]
        exit_reason = "timeout"
        exit_ts = bars[-1][0]
    
    # P&L
    gross_pnl = (avg - exit_price) * current_qty
    commission = (avg * current_qty + exit_price * current_qty) * COMMISSION_PCT / 100 / 2
    slippage_cost = exit_price * current_qty * SLIPPAGE_PCT / 100
    
    hold_sec = exit_ts - entry_ts
    funding_periods = hold_sec / FUNDING_INTERVAL
    funding_cost = avg * current_qty * FUNDING_RATE * funding_periods
    
    net_pnl = gross_pnl - commission - slippage_cost - funding_cost
    
    return exit_price, exit_reason, net_pnl, dca, dca_events, exit_ts, hold_sec

def main():
    live = [json.loads(l) for l in open("logs/trades.jsonl")]
    print(f"Live сделок: {len(live)}")
    print(f"Конфиг: УМЕРЕННЫЙ — TP {TP_CHAIN} SL {SL_PCT}% DCA×{DCA_MAX} trig {TRIG_PCT}%")
    print(f"Risk: {RISK_PCT}% от баланса, старт ${INITIAL_BALANCE}")
    print(f"{'='*120}")
    
    balance = INITIAL_BALANCE
    results = []
    equity_curve = [balance]
    
    for i, t in enumerate(live, 1):
        sym = t["symbol"]
        entry_ts = t["entry_ts"]
        original_entry = t.get("original_entry", t["entry_price"])
        
        # Position sizing: risk% от текущего баланса
        position_value = balance * RISK_PCT / 100
        qty = position_value / original_entry
        
        # Загружаем свечи
        klines = load_klines_range(sym, entry_ts, days=7)
        
        if not klines:
            # Нет данных — пропускаем
            results.append({
                "n": i, "symbol": sym, "date": t["date"],
                "reason": "no_data", "net_pnl": 0, "balance_after": balance,
                "dca_count": 0, "hold_sec": 0, "original_reason": t.get("reason","?"),
                "entry": original_entry, "exit": None,
            })
            equity_curve.append(balance)
            print(f"  {i:>3} {sym:<14} {t['date']} NO DATA — пропуск")
            continue
        
        exit_price, reason, net_pnl, dca, dca_events, exit_ts, hold_sec = \
            sim_moderate(original_entry, qty, klines, entry_ts)
        
        balance += net_pnl
        pnl_pct = (original_entry - exit_price) / original_entry * 100 if exit_price else 0
        hold_h = hold_sec / 3600 if hold_sec else 0
        
        results.append({
            "n": i, "symbol": sym, "date": t["date"],
            "reason": reason, "net_pnl": net_pnl, "balance_after": balance,
            "dca_count": dca, "hold_sec": hold_sec, "original_reason": t.get("reason","?"),
            "entry": original_entry, "exit": exit_price,
            "dca_events": dca_events, "pnl_pct": pnl_pct,
        })
        equity_curve.append(balance)
        
        dca_str = f"DCA×{dca}" if dca > 0 else "—"
        print(f"  {i:>3} {sym:<14} {t['date']} {reason:<12} pnl=${net_pnl:>+7.2f} "
              f"bal=${balance:>8.2f} {dca_str:<6} hold={hold_h:.1f}h  (было: {t.get('reason','?')})")
    
    # ── СВОДКА ──
    n = len(results)
    valid = [r for r in results if r["reason"] != "no_data"]
    total_pnl = balance - INITIAL_BALANCE
    roi = total_pnl / INITIAL_BALANCE * 100
    
    reasons = Counter(r["reason"] for r in results)
    wins = [r for r in valid if r["net_pnl"] > 0]
    losses = [r for r in valid if r["net_pnl"] <= 0]
    wr = len(wins) / len(valid) * 100 if valid else 0
    
    avg_win = sum(r["net_pnl"] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r["net_pnl"] for r in losses) / len(losses) if losses else 0
    
    gross_win = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    
    # Max drawdown
    peak = INITIAL_BALANCE
    max_dd = 0
    for r in results:
        if r["balance_after"] > peak:
            peak = r["balance_after"]
        dd = (r["balance_after"] - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    
    dca_used = sum(1 for r in valid if r["dca_count"] > 0)
    
    print(f"\n{'='*80}")
    print(f"  ИТОГ ПО УМЕРЕННОМУ КОНФИГУ ({n} live-сделок бота)")
    print(f"{'='*80}")
    print(f"  Стартовый баланс:     ${INITIAL_BALANCE:.2f}")
    print(f"  Финальный баланс:     ${balance:.2f}")
    print(f"  Общий P&L:            ${total_pnl:+.2f}")
    print(f"  ROI:                  {roi:+.1f}%")
    print(f"  Win Rate:             {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor:        {pf:.2f}")
    print(f"  Avg Win:              ${avg_win:+.2f}")
    print(f"  Avg Loss:             ${avg_loss:+.2f}")
    print(f"  Max Drawdown:         {max_dd:.1f}%")
    print(f"  DCA использовано:     {dca_used}/{len(valid)} ({dca_used/len(valid)*100:.0f}%)")
    print(f"  Причины выхода:       {dict(reasons)}")
    
    # Сравнение с оригинальным ботом
    print(f"\n{'='*80}")
    print(f"  СРАВНЕНИЕ С РЕАЛЬНЫМ БОТОМ")
    print(f"{'='*80}")
    real_pnl = sum(t["net_pnl"] for t in live)
    real_final = live[-1]["balance_after"]
    real_roi = (real_final - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    print(f"  Реальный бот:         ${real_final:.2f} (ROI {real_roi:+.1f}%)")
    print(f"  Умеренный (replay):   ${balance:.2f} (ROI {roi:+.1f}%)")
    print(f"  Разница:              ${balance - real_final:+.2f} ({roi - real_roi:+.1f}%)")
    
    # Сохранить
    out = {
        "config": "Умеренный", "tp_chain": TP_CHAIN, "sl": SL_PCT, "dca_max": DCA_MAX,
        "trig": TRIG_PCT, "risk_pct": RISK_PCT,
        "summary": {"balance": balance, "roi": roi, "wr": wr, "pf": pf,
                     "max_dd": max_dd, "dca_used": dca_used, "reasons": dict(reasons)},
        "trades": results, "equity_curve": equity_curve,
    }
    json.dump(out, open("/tmp/moderate_replay.json", "w"), indent=2, default=str)
    print(f"\nСохранено: /tmp/moderate_replay.json")

if __name__ == "__main__":
    main()
