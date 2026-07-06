#!/usr/bin/env python3
"""Параллельный препроцессинг day_data для сценария Б.
8 воркеров грузят разные дни → 1 pickle per day → ~8× быстрее."""
import gzip, io, csv, gc, os, sys, glob, pickle
from multiprocessing import Pool

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT",
         "LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
         "MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}

CACHE_DIR = "/tmp/b1m_days"
os.makedirs(CACHE_DIR, exist_ok=True)

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

def process_day(date):
    out = f"{CACHE_DIR}/{date}.pkl"
    if os.path.exists(out):
        return date, "cached"
    files = [f for f in glob.glob(os.path.expanduser(f"~/bybit_ticks/*{date}.csv.gz"))
             if "USDT" in os.path.basename(f) and os.path.basename(f).endswith("USDT"+date+".csv.gz")
             and not any(h in os.path.basename(f) for h in HEAVY)]
    day_data = {}
    for f in files:
        sym = os.path.basename(f).replace(date+".csv.gz","")
        cdl = load_1m(f)
        if cdl:
            ts = sorted(cdl.keys()); day_data[sym] = (cdl, cdl[ts[0]][0], ts)
        gc.collect()
    with open(out,"wb") as fp: pickle.dump(day_data, fp)
    return date, f"{len(day_data)} pairs"

if __name__ == "__main__":
    dates = sorted(set(os.path.basename(f).split("USDT")[-1].replace(".csv.gz","")
        for f in glob.glob(os.path.expanduser("~/bybit_ticks/*.csv.gz")) if "USDT" in os.path.basename(f)))
    print(f"Дней: {len(dates)}, воркеров: 8", flush=True)
    done = 0
    with Pool(8) as pool:
        for date, status in pool.imap_unordered(process_day, dates):
            done += 1
            print(f"  [{done}/{len(dates)}] {date}: {status}", flush=True)
    print("DONE")
