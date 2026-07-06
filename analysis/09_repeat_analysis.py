#!/usr/bin/env python3
"""Анализ повторов входов на одной монете: сколько раз, какой промежуток."""
import gzip, io, csv, gc, os, math, pickle, glob
from collections import defaultdict
from datetime import datetime, timezone

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

dates = sorted(set(os.path.basename(f).split("USDT")[-1].replace(".csv.gz","")
    for f in glob.glob(os.path.expanduser("~/bybit_ticks/*.csv.gz")) if "USDT" in os.path.basename(f)))
print(f"Дней: {len(dates)}", flush=True)

entries = []  # (sym, ts_epoch)
loaded = 0
for date in dates:
    day_file = f"{CACHE_DIR}/{date}.pkl"
    if os.path.exists(day_file):
        with open(day_file,"rb") as f: day_data = pickle.load(f)
        loaded += 1
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
    if loaded % 20 == 0 and loaded > 0:
        print(f"  загружено {loaded} дней, входов {len(entries)}...", flush=True)
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
            entries.append((c2, mt))
        prev1=c1

print(f"\nВсего входов: {len(entries)}\n", flush=True)

# Группировка по символу
by_sym = defaultdict(list)
for sym, ts in entries:
    by_sym[sym].append(ts)

# Статистика повторов
sym_counts = sorted([len(v) for v in by_sym.values()], reverse=True)
from collections import Counter
count_dist = Counter(sym_counts)

print("=== Сколько раз входили в одну монету ===\n")
print(f"  Уникальных монет: {len(by_sym)}")
print(f"  Всего входов: {len(entries)}")
print(f"  Средн. входов на монету: {len(entries)/len(by_sym):.1f}")
print(f"  Макс входов в одну монету: {max(sym_counts)}")
print(f"\n  Распределение:")
for n_entries, n_syms in sorted(count_dist.items()):
    bar = "█" * min(n_syms, 50)
    print(f"    {n_entries:>3}x → {n_syms:>4} монет  {bar}")

# Промежутки между повторными входами
intervals_min = []
same_day = 0
for sym, tss in by_sym.items():
    tss_sorted = sorted(tss)
    for i in range(1, len(tss_sorted)):
        gap = (tss_sorted[i] - tss_sorted[i-1]) / 60  # минуты
        intervals_min.append(gap)
        if gap < 24*60:
            same_day += 1

def pctile(data, q):
    if not data: return 0
    s = sorted(data)
    k = (len(s)-1)*q
    f = math.floor(k); c = math.ceil(k)
    if f==c: return s[int(k)]
    return s[f]+(s[c]-s[f])*(k-f)

print(f"\n=== Промежутки между входами на одной монете ===\n")
if intervals_min:
    print(f"  Всего повторов: {len(intervals_min)}")
    print(f"  Медиана: {pctile(intervals_min,0.5):.0f} мин ({pctile(intervals_min,0.5)/60:.1f} ч)")
    print(f"  p25: {pctile(intervals_min,0.25):.0f} мин ({pctile(intervals_min,0.25)/60:.1f} ч)")
    print(f"  p75: {pctile(intervals_min,0.75):.0f} мин ({pctile(intervals_min,0.75)/60:.1f} ч)")
    print(f"  p90: {pctile(intervals_min,0.9):.0f} мин ({pctile(intervals_min,0.9)/60:.1f} ч)")
    print(f"  Минимум: {min(intervals_min):.0f} мин")
    print(f"  Максимум: {max(intervals_min):.0f} мин ({max(intervals_min)/1440:.0f} дней)")
    print(f"\n  В тот же день (<24ч): {same_day} ({same_day/len(intervals_min)*100:.0f}%)")
    #_buckets
    b1 = sum(1 for g in intervals_min if g < 60)
    b2 = sum(1 for g in intervals_min if 60 <= g < 360)
    b3 = sum(1 for g in intervals_min if 360 <= g < 1440)
    b4 = sum(1 for g in intervals_min if 1440 <= g < 10080)
    b5 = sum(1 for g in intervals_min if g >= 10080)
    print(f"  < 1 часа: {b1} ({b1/len(intervals_min)*100:.0f}%)")
    print(f"  1-6 часов: {b2} ({b2/len(intervals_min)*100:.0f}%)")
    print(f"  6-24 часов: {b3} ({b3/len(intervals_min)*100:.0f}%)")
    print(f"  1-7 дней: {b4} ({b4/len(intervals_min)*100:.0f}%)")
    print(f"  > 7 дней: {b5} ({b5/len(intervals_min)*100:.0f}%)")

# Топ-10 самых частых монет
print(f"\n=== Топ-10 монет по числу входов ===\n")
top_syms = sorted(by_sym.items(), key=lambda x: len(x[1]), reverse=True)[:10]
for sym, tss in top_syms:
    first = datetime.fromtimestamp(min(tss), tz=timezone.utc).strftime("%d.%m")
    last = datetime.fromtimestamp(max(tss), tz=timezone.utc).strftime("%d.%m")
    print(f"  {sym:>14}: {len(tss):>3}x  ({first} → {last})")

print("\nDONE")
