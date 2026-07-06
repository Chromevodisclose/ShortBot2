#!/usr/bin/env python3
"""Сценарий Б — ШОРТ. Те же входы, но шортим #2 монету.
MFE/MAE для шорта: favorable = падение, adverse = рост."""
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

print(f"\nВсего входов: {len(entries)} (ШОРТ)\n", flush=True)

# Для ШОРТА: favorable = цена падает, adverse = цена растёт
# MFE_short = (entry - min_p) / entry * 100  — максимум падения (в нашу сторону)
# MAE_short = (max_p - entry) / entry * 100  — максимум роста (против нас)
HORIZONS = [15, 60, 180, 600, 1800, 0]
mfe_all = {h:[] for h in HORIZONS}  # favorable (down for short)
mae_all = {h:[] for h in HORIZONS}  # adverse (up for short)
giveback = []
final_pnl = []

for e in entries:
    entry = e["entry"]
    after = e["after"]
    mts = sorted(after.keys())
    max_p = entry; min_p = entry
    for i, mt in enumerate(mts):
        c = after[mt]
        if c[1] > max_p: max_p = c[1]
        if c[2] < min_p: min_p = c[2]
        age = (mt - mts[0]) // 60
        for h in HORIZONS:
            if h == 0 or age <= h:
                # short: favorable = drop, adverse = rise
                mfe_all[h].append((entry - min_p)/entry*100)   # how much it dropped = profit
                mae_all[h].append((max_p - entry)/entry*100)   # how much it rose = loss
    last = after[mts[-1]][3]
    mfe_eod = (entry - min_p)/entry*100   # max drop = peak profit
    final = (entry - last)/entry*100      # short PnL
    giveback.append((mfe_eod, final))
    final_pnl.append(final)

COMMISSION = 0.055

print("=== MFE/MAE для ШОРТА (% от входа) ===")
print("  MFE = сколько упала (в нашу сторону)")
print("  MAE = сколько выросла (против нас)\n")
print(f"{'горизонт':>10} | {'N':>6} | {'MFE med':>8} {'MFE p75':>8} {'MFE p90':>8} | {'MAE med':>8} {'MAE p75':>8} {'MAE p90':>8}")
print("-"*85)
for h in HORIZONS:
    mfes = mfe_all[h]; maes = mae_all[h]
    label = f"{h}м" if h>0 else "EOD"
    print(f"{label:>10} | {len(mfes):>6} | {pctile(mfes,0.5):>8.2f} {pctile(mfes,0.75):>8.2f} {pctile(mfes,0.9):>8.2f} | {pctile(maes,0.5):>8.2f} {pctile(maes,0.75):>8.2f} {pctile(maes,0.9):>8.2f}")

print("\n=== Giveback (EOD) — сколько отдано от пика профита ===\n")
gb_ratio = [(m-f)/m*100 for m,f in giveback if m > 0.1]
if gb_ratio:
    print(f"  Сделок с MFE>0.1%: {len(gb_ratio)}")
    print(f"  Отдано от пика: med {pctile(gb_ratio,0.5):.0f}% | p75 {pctile(gb_ratio,0.75):.0f}% | p90 {pctile(gb_ratio,0.9):.0f}%")

net_eod = sum(p - COMMISSION for p in final_pnl)
wins_eod = sum(1 for p in final_pnl if p > COMMISSION)
print(f"\n=== EOD PnL (шорт hold до конца дня) ===")
print(f"  Net: {net_eod:+.1f}% | WR: {wins_eod/len(final_pnl)*100:.1f}% | avg: {net_eod/len(final_pnl):+.2f}%/trade")

mfes = mfe_all[0]; maes = mae_all[0]

print("\n" + "="*60)
print("=== ОПТИМАЛЬНЫЕ ПАРАМЕТРЫ для ШОРТА ===")
print("="*60 + "\n")

print("STOP LOSS (для шорта — выше входа, покрывает рост против нас):")
print(f"  SL = {pctile(maes,0.50):.2f}%  → 50% (мед)")
print(f"  SL = {pctile(maes,0.75):.2f}%  → 75% — рекоменд.")
print(f"  SL = {pctile(maes,0.85):.2f}%  → 85% — консерват.")
print(f"  SL = {pctile(maes,0.90):.2f}%  → 90% — широкий")

print("\nTAKE PROFIT (для шорта — ниже входа, сколько упадет):")
print(f"  TP = {pctile(mfes,0.50):.2f}%  → 50% (мед)")
print(f"  TP = {pctile(mfes,0.60):.2f}%  → 60% — рекоменд. TP1")
print(f"  TP = {pctile(mfes,0.75):.2f}%  → 75% — TP2")
print(f"  TP = {pctile(mfes,0.90):.2f}%  → 90% — TP3/runner")

if gb_ratio:
    print("\nTRAILING:")
    print(f"  Активация: {pctile(mfes,0.5):.2f}% падения")
    print(f"  Отступ: {pctile(gb_ratio,0.5):.1f}% от пика (med)")
    print(f"  Отступ (жёстко): {pctile(gb_ratio,0.25):.1f}% от пика")

print("\n" + "="*60)
print("=== СИМУЛЯЦИЯ ШОРТА (комиссия 0.055% RT) ===")
print("="*60 + "\n")

def simulate_short(entry, after, sl, tp, trail, act):
    """Шорт: SL выше входа, TP ниже входа, trail по минимальной цене."""
    sl_p = entry*(1+sl/100)   # стоп выше (рост против нас)
    tp_p = entry*(1-tp/100)   # тейк ниже (падение = профит)
    min_p = entry; activated = False; act_p = entry*(1-act/100)  # активация при падении
    mts = sorted(after.keys())
    for mt in mts:
        c = after[mt]
        if c[2] < min_p: min_p = c[2]  #追踪 минимальную цену
        if not activated and min_p <= act_p: activated = True
        if activated:
            ts = min_p*(1+trail/100)  # trail стоп ниже минимума
            if c[1] >= ts and min_p < entry:
                return ("trail", (entry-min_p)/entry*100 - COMMISSION)
        if c[1] >= sl_p: return ("loss", -sl - COMMISSION)
        if c[2] <= tp_p: return ("win", tp - COMMISSION)
    last = after[mts[-1]][3]
    return ("eod", (entry-last)/entry*100 - COMMISSION)

CONFIGS = [
    (pctile(maes,0.75), pctile(mfes,0.60), max(1.0, min(pctile(gb_ratio,0.5), 50)) if gb_ratio else 3, pctile(mfes,0.5), "оптимальный (MAE75/MFE60/giveback50)"),
    (pctile(maes,0.85), pctile(mfes,0.75), max(1.0, min(pctile(gb_ratio,0.5), 50)) if gb_ratio else 3, pctile(mfes,0.5), "консерватив (MAE85/MFE75)"),
    (pctile(maes,0.75), 3.0, 3.0, 3.0, "базовый (MAE75 TP3 trail3 act3)"),
    (5.0, 3.0, 3.0, 3.0, "оригинал SL5 trail3 act3"),
    (pctile(maes,0.75), 2.0, 2.0, 2.0, "тугой TP2 trail2 act2"),
    (pctile(maes,0.75), 1.5, 1.5, 1.5, "тугой TP1.5 trail1.5 act1.5"),
    (pctile(maes,0.75), 5.0, 3.0, 3.0, "широкий TP5 trail3 act3"),
    (3.0, 3.0, 3.0, 3.0, "SL3 TP3 trail3 act3"),
]

for sl, tp, tr, act, label in CONFIGS:
    results = [simulate_short(e["entry"], e["after"], sl, tp, tr, act) for e in entries]
    pnls = [p for _, p in results]
    wins = [p for r, p in results if r == "win"]
    trails = [p for r, p in results if r == "trail"]
    losses = [p for r, p in results if r == "loss"]
    eods = [p for r, p in results if r == "eod"]
    net = sum(pnls)
    wr = (len(wins)+len(trails))/len(pnls)*100 if pnls else 0
    avg = statistics.mean(pnls) if pnls else 0
    sharpe = avg/statistics.stdev(pnls) if len(pnls)>2 and statistics.stdev(pnls)>0 else 0
    pf = sum(wins+trails)/abs(sum(losses)) if losses and sum(losses)!=0 else 999
    eq=0; peak=0; mdd=0
    for p in pnls: eq+=p; peak=max(peak,eq); mdd=min(mdd,eq-peak)
    print(f"--- {label} ---")
    print(f"  SL={sl:.2f}% TP={tp:.2f}% trail={tr:.1f}% act={act:.2f}%")
    print(f"  Net: {net:+.1f}% | WR: {wr:.1f}% | PF: {pf:.2f} | avg: {avg:+.2f}% | Sharpe: {sharpe:.2f} | MaxDD: {mdd:.1f}%")
    print(f"  win={len(wins)} trail={len(trails)} loss={len(losses)} eod={len(eods)}\n")

out = {
    "direction": "short",
    "entries": len(entries), "days": len(dates),
    "mfe": {str(h): {"med": pctile(mfe_all[h],0.5), "p60": pctile(mfe_all[h],0.6), "p75": pctile(mfe_all[h],0.75), "p90": pctile(mfe_all[h],0.9)} for h in HORIZONS},
    "mae": {str(h): {"med": pctile(mae_all[h],0.5), "p75": pctile(mae_all[h],0.75), "p85": pctile(mae_all[h],0.85), "p90": pctile(mae_all[h],0.9)} for h in HORIZONS},
    "giveback": {"med": pctile(gb_ratio,0.5), "p25": pctile(gb_ratio,0.25), "p75": pctile(gb_ratio,0.75), "p90": pctile(gb_ratio,0.9)} if gb_ratio else {},
    "eod_pnl": {"net": net_eod, "wr": wins_eod/len(final_pnl)*100 if final_pnl else 0},
}
json.dump(out, open("/tmp/scenario_B_short.json","w"), ensure_ascii=False, indent=2)
print("DONE → /tmp/scenario_B_short.json")
