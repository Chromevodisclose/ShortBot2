#!/usr/bin/env python3
"""Шорт сценария Б — реальная просадка с position sizing.
Риск = X% депозита на сделку. Position size = risk / SL_price%.
Реинвестирование (compounding)."""
import gzip, io, csv, gc, os, json, math, pickle, glob, statistics

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT",
         "LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
         "MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}
CACHE_DIR = "/tmp/b1m_days"

def load_1m(path):
    try: raw = gzip.decompress(open(path,"rb").read())
    except: return None
    candles = {}
    r = csv.reader(io.StringIO(raw.decode("utf-8","ignore")))
    try: next(r)
    except StopIteration: return None
    for row in r:
        if len(row)<5: continue
        try: ts=int(float(row[0])); p=float(row[4])
        except: continue
        mt = ts-(ts%60)
        if mt not in candles: candles[mt]=[p,p,p,p]
        else:
            c=candles[mt]; c[1]=max(c[1],p); c[2]=min(c[2],p); c[3]=p
    return candles if len(candles)>=60 else None

def pctile(data, q):
    if not data: return 0.0
    s = sorted(data)
    k = (len(s)-1)*q
    f = math.floor(k); c = math.ceil(k)
    if f==c: return s[int(k)]
    return s[f]+(s[c]-s[f])*(k-f)

dates = sorted(set(os.path.basename(f).split("USDT")[-1].replace(".csv.gz","")
    for f in glob.glob(os.path.expanduser("~/bybit_ticks/*.csv.gz")) if "USDT" in os.path.basename(f)))
print(f"Дней: {len(dates)}", flush=True)

entries = []
loaded_days = 0
for date in dates:
    day_file = f"{CACHE_DIR}/{date}.pkl"
    if os.path.exists(day_file):
        with open(day_file,"rb") as f: day_data = pickle.load(f)
        loaded_days += 1
    else:
        files = [f for f in glob.glob(os.path.expanduser(f"~/bybit_ticks/*{date}.csv.gz"))
                 if "USDT" in os.path.basename(f) and os.path.basename(f).endswith("USDT"+date+".csv.gz")
                 and not any(h in os.path.basename(f) for h in HEAVY)]
        day_data = {}
        for ff in files:
            sym = os.path.basename(ff).replace(date+".csv.gz","")
            cdl = load_1m(ff)
            if cdl:
                ts = sorted(cdl.keys()); day_data[sym] = (cdl, cdl[ts[0]][0], ts)
        gc.collect()
    if loaded_days % 20 == 0 and loaded_days > 0:
        print(f"  загружено {loaded_days} дней...", flush=True)
    if len(day_data)<20: continue
    all_mts = sorted(set().union(*[set(d[2]) for d in day_data.values()]))
    sample = all_mts[::15]
    rankings = {}
    for mt in sample:
        g=[(s,(cdl[mt][3]-o)/o*100,cdl[mt][3]) for s,(cdl,o,ts) in day_data.items()
           if mt in cdl and o>0 and (cdl[mt][3]-o)/o*100>0]
        g.sort(key=lambda x:x[1],reverse=True); rankings[mt]=g
    first1=set(); traded=set(); prev1=None
    for mt in sample:
        top=rankings.get(mt,[])
        if len(top)<2: continue
        c1,c2=top[0][0],top[1][0]
        if c1 not in first1 and c1 not in traded: first1.add(c1)
        if prev1==c2 and c2 in first1 and c2 not in traded and c2!=c1:
            sym=c2; entry=top[1][2]
            cdl,o,ts=day_data[sym]
            after={t:cdl[t] for t in ts if t>mt}
            if len(after)>=10:
                entries.append({"sym":sym,"date":date,"entry":entry,"after":after})
            traded.add(sym)
        prev1=c1

print(f"\nВсего входов: {len(entries)}\n", flush=True)

COMMISSION = 0.055

def simulate_short_pct(entry, after, sl, tp, trail, act):
    """Возвращает % PnL от входа (цена)."""
    sl_p = entry*(1+sl/100)
    tp_p = entry*(1-tp/100)
    min_p = entry; activated = False; act_p = entry*(1-act/100)
    mts = sorted(after.keys())
    for mt in mts:
        c = after[mt]
        if c[2] < min_p: min_p = c[2]
        if not activated and min_p <= act_p: activated = True
        if activated:
            ts = min_p*(1+trail/100)
            if c[1] >= ts and min_p < entry:
                return ("trail", (entry-min_p)/entry*100)
        if c[1] >= sl_p: return ("loss", -sl)
        if c[2] <= tp_p: return ("win", tp)
    last = after[mts[-1]][3]
    return ("eod", (entry-last)/entry*100)

# === POSITION SIZING SIMULATION ===
# Риск на сделку = RISK% от депозита
# Если SL = sl% (цена), position_size = RISK / sl  (доля депозита)
# PnL на депозит = trade_pct * position_size - commission * position_size
# С реинвестированием

CONFIGS = [
    (30, 35, 12, 1, "макс прибыль"),
    (30, 15, 10, 1, "макс Sharpe"),
    (20, 15, 10, 1, "баланс"),
    (15, 15, 10, 1, "консерватив"),
]

RISKS = [0.5, 1, 2, 3, 5]  # % депозита на сделку

print("="*80)
print("=== POSITION SIZING: реальная просадка с риском X% депозита на сделку ===")
print("="*80 + "\n")

for sl, tp, tr, act, label in CONFIGS:
    print(f"\n{'='*60}")
    print(f"--- {label}: SL={sl}% TP={tp}% Trail={tr}% Act={act}% ---")
    print(f"{'='*60}\n")
    print(f"{'риск%':>6} | {'финал деп':>12} | {'Net%':>10} | {'MaxDD%':>8} | {'Sharpe':>7} | {'WR%':>5} | {'макс серия лосс':>15}")
    print("-"*80)

    # Сначала посчитать % PnL каждой сделки
    trade_results = []
    for e in entries:
        r, pct = simulate_short_pct(e["entry"], e["after"], sl, tp, tr, act)
        trade_results.append((r, pct))

    for risk_pct in RISKS:
        equity = 100.0  # старт 100 единиц
        peak = equity
        max_dd = 0
        pos_size = risk_pct / sl  # доля депозита в позиции
        losses_in_row = 0
        max_losses_row = 0
        wins = 0

        for r, pct in trade_results:
            # PnL на депозит = price_pct * position_size - commission * position_size
            trade_pnl = (pct - COMMISSION) * pos_size / 100 * equity
            equity += trade_pnl
            peak = max(peak, equity)
            dd = (equity - peak) / peak * 100
            max_dd = min(max_dd, dd)

            if pct < 0:
                losses_in_row += 1
                max_losses_row = max(max_losses_row, losses_in_row)
            else:
                losses_in_row = 0
                wins += 1

        net = (equity - 100) / 100 * 100
        wr = wins / len(trade_results) * 100
        # Sharpe на trade-by-trade basis
        trade_pnls = [(pct - COMMISSION) * pos_size for _, pct in trade_results]
        avg = statistics.mean(trade_pnls)
        std = statistics.stdev(trade_pnls) if len(trade_pnls)>2 else 0
        sharpe = avg/std if std > 0 else 0

        print(f"{risk_pct:>5}% | {equity:>10.1f}x | {net:>+9.0f}% | {max_dd:>+7.1f}% | {sharpe:>7.2f} | {wr:>4.0f}% | {max_losses_row:>15}")

print("\n" + "="*80)
print("=== ДЕТАЛЬНО: лучший конфиг по Sharpe (SL30 TP15 TR10 Act1) ===")
print("="*80 + "\n")

# Детальный разбор для лучшего конфига
sl, tp, tr, act = 30, 15, 10, 1
trade_results = [simulate_short_pct(e["entry"], e["after"], sl, tp, tr, act) for e in entries]

print("Распределение PnL на сделку (% от цены):")
pnls = [p for _, p in trade_results]
print(f"  win: med {pctile([p for r,p in trade_results if r=='win'], 0.5):.1f}% | trail: med {pctile([p for r,p in trade_results if r=='trail'], 0.5):.1f}% | loss: med {pctile([p for r,p in trade_results if r=='loss'], 0.5):.1f}% | eod: med {pctile([p for r,p in trade_results if r=='eod'], 0.5):.1f}%")
print(f"  всего: win={sum(1 for r,_ in trade_results if r=='win')} trail={sum(1 for r,_ in trade_results if r=='trail')} loss={sum(1 for r,_ in trade_results if r=='loss')} eod={sum(1 for r,_ in trade_results if r=='eod')}")

print("\n=== Просадка по рискам (compounding) ===")
print(f"\n{'риск':>5} | {'депозит':>10} | {'MaxDD':>8} | {'годовых*':>10} | {'серия лосс':>10}")
print("-"*60)
for risk_pct in [0.5, 1, 2, 3, 5, 10]:
    equity = 100.0; peak = equity; max_dd = 0; pos_size = risk_pct / sl
    lr = 0; mlr = 0
    for r, pct in trade_results:
        trade_pnl = (pct - COMMISSION) * pos_size / 100 * equity
        equity += trade_pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / peak * 100)
        if pct < 0: lr += 1; mlr = max(mlr, lr)
        else: lr = 0
    # годовых: 923 сделки за 180 дней → ~1868 сделок/год
    annual = ((equity/100) ** (365/180) - 1) * 100
    print(f"{risk_pct:>4}% | {equity:>8.1f}x | {max_dd:>+7.1f}% | {annual:>+9.0f}% | {mlr:>10}")

print("\n* годовых экстраполяция compounding")
print("\nDONE")
