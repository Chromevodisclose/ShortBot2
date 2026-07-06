#!/usr/bin/env python3
"""MFE/MAE анализ сценария Б: насколько монета растёт и падает после входа.
Те же входы что в final_6m_test.py. Без стопов/тейков — просто наблюдаем.
Цель: оптимальный SL = покрывает MAE p75-85, TP = MFE p60-70, trail = MFE giveback.
"""
import gzip, io, csv, gc, os, json, statistics, glob, math

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT",
         "LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
         "MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}

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
    if not data: return 0
    s = sorted(data)
    k = (len(s)-1)*q
    f = math.floor(k); c = math.ceil(k)
    if f==c: return s[int(k)]
    return s[f] + (s[c]-s[f])*(k-f)

dates = sorted(set(os.path.basename(f).split("USDT")[-1].replace("..gz","").replace(".csv.gz","")
    for f in glob.glob(os.path.expanduser("~/bybit_ticks/*.csv.gz")) if "USDT" in os.path.basename(f)))
print(f"Дней: {len(dates)}: {dates[0]} → {dates[-1]}", flush=True)

entries = []
for date in dates:
    files = [f for f in glob.glob(os.path.expanduser(f"~/bybit_ticks/*{date}.csv.gz"))
             if "USDT" in os.path.basename(f) and os.path.basename(f).endswith("USDT"+date+".csv.gz")
             and not any(h in os.path.basename(f) for h in HEAVY)]
    day_data = {}
    for i, f in enumerate(files):
        sym = os.path.basename(f).replace(date+".csv.gz","")
        cdl = load_1m(f)
        if cdl:
            ts = sorted(cdl.keys()); day_data[sym] = (cdl, cdl[ts[0]][0], ts)
        if (i+1)%200==0: print(f"  {date} {i+1}/{len(files)}", flush=True)
        gc.collect()
    if len(day_data)<20: continue
    all_mts = sorted(set().union(*[set(d[2]) for d in day_data.values()]))
    sample = all_mts[::15]
    rankings = {}
    for mt in sample:
        g=[(s,(cdl[mt][3]-o)/o*100,cdl[mt][3]) for s,(cdl,o,ts) in day_data.items()
           if mt in cdl and o>0 and (cdl[mt][3]-o)/o*100>0]
        g.sort(key=lambda x:x[1],reverse=True); rankings[mt]=g
    first1=set(); prev1=None
    for mt in sample:
        top=rankings.get(mt,[])
        if len(top)<2: continue
        c1,c2=top[0][0],top[1][0]
        if c1 not in first1: first1.add(c1)
        if prev1==c2 and c2 in first1 and c2!=c1:
            sym=c2; entry=top[1][2]
            cdl,o,ts=day_data[sym]
            after={t:cdl[t] for t in ts if t>mt}
            if len(after)>=10:
                entries.append({"sym":sym,"date":date,"entry":entry,"after":after})
        prev1=c1
    print(f"  {date}: входов {len([e for e in entries if e['date']==date])} (всего {len(entries)})", flush=True)

print(f"\nВсего входов: {len(entries)}\n", flush=True)

# MFE/MAE в %, с горизонтами наблюдения
HORIZONS = [60, 180, 600, 1800, 0]  # 1м,3м,10м,30м,EOD минут
mfe_all = {h:[] for h in HORIZONS}
mae_all = {h:[] for h in HORIZONS}
giveback = []  # (MFE_peak, final) для EOD

for e in entries:
    entry = e["entry"]
    after = e["after"]
    mts = sorted(after.keys())
    max_p = entry; min_p = entry
    for i, mt in enumerate(mts):
        c = after[mt]
        if c[1] > max_p: max_p = c[1]
        if c[2] < min_p: min_p = c[2]
        age = (mt - mts[0]) // 60 if mts else 0  # минуты после входа
        for h in HORIZONS:
            if h == 0 or age <= h:
                mfe_all[h].append((max_p - entry)/entry*100)
                mae_all[h].append((entry - min_p)/entry*100)
    # EOD giveback
    last = after[mts[-1]][3]
    mfe_eod = (max_p - entry)/entry*100
    final = (last - entry)/entry*100
    giveback.append((mfe_eod, final))

print("=== MFE/MAE по горизонтам (% от входа) ===\n")
print(f"{'горизонт':>10} | {'N':>6} | {'MFE med':>8} {'MFE p75':>8} {'MFE p90':>8} | {'MAE med':>8} {'MAE p75':>8} {'MAE p90':>8}")
print("-"*80)
for h in HORIZONS:
    mfes = mfe_all[h]; maes = mae_all[h]
    label = f"{h}м" if h>0 else "EOD"
    print(f"{label:>10} | {len(mfes):>6} | {pctile(mfes,0.5):>8.2f} {pctile(mfes,0.75):>8.2f} {pctile(mfes,0.9):>8.2f} | {pctile(maes,0.5):>8.2f} {pctile(maes,0.75):>8.2f} {pctile(maes,0.9):>8.2f}")

# Giveback: сколько отдали от пика
print("\n=== Giveback (EOD): сколько отдано от пика MFE ===\n")
gb_ratio = []
for mfe_eod, final in giveback:
    if mfe_eod > 0.1:
        gb_ratio.append((mfe_eod - final) / mfe_eod * 100)  # % отдано
if gb_ratio:
    print(f"  Сделок с MFE>0.1%: {len(gb_ratio)}")
    print(f"  Отдано от пика: med {pctile(gb_ratio,0.5):.0f}% | p75 {pctile(gb_ratio,0.75):.0f}% | p90 {pctile(gb_ratio,0.9):.0f}%")
    kept = [100-r for r in gb_ratio]
    print(f"  Сохранено от пика: med {pctile(kept,0.5):.0f}% | p25 {pctile(kept,0.25):.0f}%")

# Оптимальные параметры
print("\n=== ОПТИМАЛЬНЫЕ ПАРАМЕТРЫ (по EOD MFE/MAE) ===\n")
mfes = mfe_all[0]; maes = mae_all[0]
print(f"  SL (покрыть 75% MAE):  {pctile(maes,0.75):.2f}%")
print(f"  SL (покрыть 85% MAE):  {pctile(maes,0.85):.2f}%")
print(f"  SL (покрыть 90% MAE):  {pctile(maes,0.90):.2f}%")
print(f"  TP1 (MFE p50):         {pctile(mfes,0.5):.2f}%")
print(f"  TP1 (MFE p60):         {pctile(mfes,0.6):.2f}%")
print(f"  TP2 (MFE p75):         {pctile(mfes,0.75):.2f}%")
print(f"  TP3 (MFE p90):         {pctile(mfes,0.9):.2f}%")
# Trail: активация при MFE p50, отступ = giveback p50
if gb_ratio:
    print(f"  Trail активация:       {pctile(mfes,0.5):.2f}% (MFE p50)")
    print(f"  Trail отступ:          {pctile(gb_ratio,0.5):.2f}% от пика (giveback p50)")
    print(f"  Trail отступ (p25):    {pctile(gb_ratio,0.25):.2f}% (жёстче, для жадных)")

# Сохранить
out = {
    "entries": len(entries),
    "days": len(dates),
    "mfe": {str(h): {"med": pctile(mfe_all[h],0.5), "p75": pctile(mfe_all[h],0.75), "p90": pctile(mfe_all[h],0.9)} for h in HORIZONS},
    "mae": {str(h): {"med": pctile(mae_all[h],0.5), "p75": pctile(mae_all[h],0.75), "p90": pctile(mae_all[h],0.9)} for h in HORIZONS},
    "giveback": {"med": pctile(gb_ratio,0.5), "p75": pctile(gb_ratio,0.75), "p90": pctile(gb_ratio,0.9)} if gb_ratio else {},
}
json.dump(out, open("/tmp/scenario_B_mfe.json","w"), ensure_ascii=False, indent=2)
print("\nDONE → /tmp/scenario_B_mfe.json")
