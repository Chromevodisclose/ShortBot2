#!/usr/bin/env python3
"""Сценарий Б — полный grid search для ШОРТА + распределение движений."""
import gzip, io, csv, gc, os, json, math, pickle, glob, statistics, itertools

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
print(f"Дней: {len(dates)}: {dates[0]} → {dates[-1]}", flush=True)

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
        print(f"  загружено {loaded_days} дней, входов {len(entries)}...", flush=True)
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
    n_day=len([e for e in entries if e['date']==date])
    print(f"  {date}: входов {n_day} (всего {len(entries)})", flush=True)

print(f"\nВсего входов: {len(entries)} (ШОРТ grid)\n", flush=True)

# === Распределение движений от точки входа ===
print("="*70)
print("=== ДВИЖЕНИЯ от точки входа (EOD) ===")
print("="*70 + "\n")

down_moves = []  # насколько упала (профит шорта)
up_moves = []    # насколько выросла (убыток шорта)

for e in entries:
    entry = e["entry"]
    after = e["after"]
    mts = sorted(after.keys())
    min_p = entry; max_p = entry
    for mt in mts:
        c = after[mt]
        if c[1] > max_p: max_p = c[1]
        if c[2] < min_p: min_p = c[2]
    down_moves.append((entry - min_p)/entry*100)  # max drop
    up_moves.append((max_p - entry)/entry*100)    # max rise

print(f"Падение (профит шорта):")
print(f"  med {pctile(down_moves,0.5):.2f}% | p60 {pctile(down_moves,0.6):.2f}% | p70 {pctile(down_moves,0.7):.2f}% | p75 {pctile(down_moves,0.75):.2f}% | p80 {pctile(down_moves,0.8):.2f}% | p90 {pctile(down_moves,0.9):.2f}%")
print(f"\nРост (против шорта):")
print(f"  med {pctile(up_moves,0.5):.2f}% | p60 {pctile(up_moves,0.6):.2f}% | p70 {pctile(up_moves,0.7):.2f}% | p75 {pctile(up_moves,0.75):.2f}% | p80 {pctile(up_moves,0.8):.2f}% | p90 {pctile(up_moves,0.9):.2f}%")

# Как часто монета падает хотя бы на X%
print(f"\nВероятность падения (профит шорта) хотя бы на:")
for pct in [1, 2, 3, 5, 7, 10, 15, 20]:
    frac = sum(1 for d in down_moves if d >= pct) / len(down_moves) * 100
    print(f"  ≥{pct:>2}%: {frac:.0f}% сделок")

print(f"\nВероятность роста (риск шорта) хотя бы на:")
for pct in [1, 2, 3, 5, 7, 10, 15, 20]:
    frac = sum(1 for u in up_moves if u >= pct) / len(up_moves) * 100
    print(f"  ≥{pct:>2}%: {frac:.0f}% сделок")

# === GRID SEARCH ===
COMMISSION = 0.055

def simulate_short(entry, after, sl, tp, trail, act):
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
                return ("trail", (entry-min_p)/entry*100 - COMMISSION)
        if c[1] >= sl_p: return ("loss", -sl - COMMISSION)
        if c[2] <= tp_p: return ("win", tp - COMMISSION)
    last = after[mts[-1]][3]
    return ("eod", (entry-last)/entry*100 - COMMISSION)

# Grid: SL × TP × Trail × Act
SL_vals = [15, 20, 25, 30, 40]
TP_vals = [15, 20, 25, 30, 35]
TR_vals = [7, 10, 12, 15]
ACT_vals = [1, 1.5, 2, 3]

print("\n" + "="*70)
print("=== GRID SEARCH (ШОРТ) ===")
print("="*70 + f"\nКомбинаций: {len(SL_vals)*len(TP_vals)*len(TR_vals)*len(ACT_vals)}\n")

results_grid = []
for sl, tp, tr, act in itertools.product(SL_vals, TP_vals, TR_vals, ACT_vals):
    pnls = []
    w=t=l=e=0
    for entry_dict in entries:
        r, pnl = simulate_short(entry_dict["entry"], entry_dict["after"], sl, tp, tr, act)
        pnls.append(pnl)
        if r=="win": w+=1
        elif r=="trail": t+=1
        elif r=="loss": l+=1
        else: e+=1
    total = len(pnls)
    net = sum(pnls)
    wr = (w+t)/total*100
    avg = statistics.mean(pnls)
    sharpe = avg/statistics.stdev(pnls) if len(pnls)>2 and statistics.stdev(pnls)>0 else 0
    pf = sum(p for p in pnls if p>0)/abs(sum(p for p in pnls if p<0)) if any(p<0 for p in pnls) else 999
    eq=0; peak=0; mdd=0
    for p in pnls: eq+=p; peak=max(peak,eq); mdd=min(mdd,eq-peak)
    results_grid.append({
        "sl":sl,"tp":tp,"tr":tr,"act":act,
        "net":round(net,1),"wr":round(wr,1),"pf":round(pf,2),
        "avg":round(avg,2),"sharpe":round(sharpe,2),"mdd":round(mdd,1),
        "w":w,"t":t,"l":l,"e":e
    })

# Топ-20 по Net
results_grid.sort(key=lambda x: x["net"], reverse=True)
print("ТОП-20 по Net PnL:\n")
print(f"{'SL':>5} {'TP':>5} {'TR':>4} {'ACT':>4} | {'Net':>8} {'WR':>5} {'PF':>5} {'avg':>6} {'Sh':>5} {'MaxDD':>7} | W/T/L/E")
print("-"*80)
for r in results_grid[:20]:
    print(f"{r['sl']:>5} {r['tp']:>5} {r['tr']:>4} {r['act']:>4} | {r['net']:>+8.1f} {r['wr']:>5.1f} {r['pf']:>5.2f} {r['avg']:>+6.2f} {r['sharpe']:>5.2f} {r['mdd']:>+7.1f} | {r['w']}/{r['t']}/{r['l']}/{r['e']}")

# Топ-10 по Sharpe
results_grid.sort(key=lambda x: x["sharpe"], reverse=True)
print("\n\nТОП-10 по Sharpe (риск-скорректированный):\n")
print(f"{'SL':>5} {'TP':>5} {'TR':>4} {'ACT':>4} | {'Net':>8} {'WR':>5} {'PF':>5} {'avg':>6} {'Sh':>5} {'MaxDD':>7} | W/T/L/E")
print("-"*80)
for r in results_grid[:10]:
    print(f"{r['sl']:>5} {r['tp']:>5} {r['tr']:>4} {r['act']:>4} | {r['net']:>+8.1f} {r['wr']:>5.1f} {r['pf']:>5.2f} {r['avg']:>+6.2f} {r['sharpe']:>5.2f} {r['mdd']:>+7.1f} | {r['w']}/{r['t']}/{r['l']}/{r['e']}")

# Топ-10 по PF (с MinDD<150)
low_dd = [r for r in results_grid if r["mdd"] > -150]
low_dd.sort(key=lambda x: x["net"], reverse=True)
print("\n\nТОП-10 по Net с MaxDD > -150% (безопасные):\n")
print(f"{'SL':>5} {'TP':>5} {'TR':>4} {'ACT':>4} | {'Net':>8} {'WR':>5} {'PF':>5} {'avg':>6} {'Sh':>5} {'MaxDD':>7} | W/T/L/E")
print("-"*80)
for r in low_dd[:10]:
    print(f"{r['sl']:>5} {r['tp']:>5} {r['tr']:>4} {r['act']:>4} | {r['net']:>+8.1f} {r['wr']:>5.1f} {r['pf']:>5.2f} {r['avg']:>+6.2f} {r['sharpe']:>5.2f} {r['mdd']:>+7.1f} | {r['w']}/{r['t']}/{r['l']}/{r['e']}")

json.dump({"entries":len(entries),"grid":results_grid}, open("/tmp/short_grid.json","w"), ensure_ascii=False, indent=2)
print("\nDONE → /tmp/short_grid.json")
