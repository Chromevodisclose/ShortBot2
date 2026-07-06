#!/usr/bin/env python3
"""Бэктест momentum. 2 сценария:
А) Попадание в топ-3/5 роста → движение после
Б) Монета ВПЕРВЫЕ стала #1 → откатилась на #2 → лонгуем → движение после"""
import urllib.request, gzip, io, csv, json, gc, statistics

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT",
         "LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
         "MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}

req = urllib.request.Request("https://api.bybit.com/v5/market/instruments-info?category=linear",
    headers={"User-Agent":"Mozilla/5.0"})
resp = urllib.request.urlopen(req, timeout=20)
data = json.loads(resp.read())
all_pairs = [r["symbol"] for r in data["result"]["list"] if r.get("status")=="Trading"]
pairs = [p for p in all_pairs if p.endswith("USDT") and not p.endswith("PERP") and p not in HEAVY]
print(f"Пар: {len(pairs)}", flush=True)

DAYS = ["2026-07-02","2026-07-01","2026-06-30","2026-06-29","2026-06-28",
        "2026-06-27","2026-06-26","2026-06-25","2026-06-24","2026-06-23",
        "2026-06-22","2026-06-21","2026-06-20","2026-06-19"]

def load_1m(symbol, date):
    fname = f"{symbol}{date}.csv.gz"
    url = f"https://public.bybit.com/trading/{symbol}/{fname}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=25)
        raw = gzip.decompress(resp.read())
    except:
        return None
    candles = {}
    reader = csv.reader(io.StringIO(raw.decode("utf-8","ignore")))
    next(reader)
    for row in reader:
        if len(row) < 5: continue
        try:
            ts = int(float(row[0])); price = float(row[4]); size = float(row[3])
        except: continue
        mt = ts - (ts % 60)
        if mt not in candles:
            candles[mt] = [price,price,price,price,size]
        else:
            c = candles[mt]
            c[1] = max(c[1],price); c[2] = min(c[2],price); c[3] = price; c[4] += size
    if len(candles) < 60: return None
    return candles

results_A = []
results_B = []

for date in DAYS:
    print(f"\n=== {date} ===", flush=True)
    day_data = {}
    for i, sym in enumerate(pairs):
        cdl = load_1m(sym, date)
        if cdl:
            ts_sorted = sorted(cdl.keys())
            o = cdl[ts_sorted[0]][0]
            day_data[sym] = (cdl, o, ts_sorted)
        if (i+1) % 100 == 0:
            print(f"  {i+1}/{len(pairs)}, данных {len(day_data)}", flush=True)
        gc.collect()
    print(f"  Пар: {len(day_data)}", flush=True)
    if len(day_data) < 20: continue

    all_mts = sorted(set().union(*[set(d[2]) for d in day_data.values()]))
    sample_mts = all_mts[::15]

    rankings = {}
    for mt in sample_mts:
        gains = []
        for sym,(cdl,o,ts) in day_data.items():
            if mt not in cdl or o == 0: continue
            g = (cdl[mt][3] - o) / o * 100
            if g > 0: gains.append((sym, g, cdl[mt][3]))
        gains.sort(key=lambda x: x[1], reverse=True)
        rankings[mt] = gains

    # СЦЕНАРИЙ А: первое попадание в топ-5
    top5_seen = set()
    for mt in sample_mts:
        for sym, g, price in rankings.get(mt, [])[:5]:
            if sym not in top5_seen:
                top5_seen.add(sym)
                cdl, o, ts = day_data[sym]
                m1 = m4 = eod = None
                for h, lbl in [(60,"1h"),(240,"4h")]:
                    tgt = mt + h*60
                    if tgt in cdl: mv = (cdl[tgt][3]-price)/price*100
                    else:
                        after = [t for t in ts if t >= tgt]
                        mv = (cdl[after[0]][3]-price)/price*100 if after else None
                    if lbl=="1h": m1 = mv
                    else: m4 = mv
                eod = (cdl[ts[-1]][3]-price)/price*100
                results_A.append({"date":date,"symbol":sym,"entry_gain":round(g,2),
                    "move_1h":round(m1,2) if m1 is not None else None,
                    "move_4h":round(m4,2) if m4 is not None else None,
                    "move_eod":round(eod,2)})

    # СЦЕНАРИЙ Б: впервые стала #1 → откат на #2 → лонг
    first_time_rank1 = set()  # монеты которые уже были #1
    prev_rank1 = None
    for mt in sample_mts:
        top = rankings.get(mt, [])
        if len(top) < 2: continue
        cur_rank1 = top[0][0]
        cur_rank2 = top[1][0]
        # Фиксируем первое восхождение на #1
        if cur_rank1 not in first_time_rank1:
            first_time_rank1.add(cur_rank1)
        # Сигнал: текущая #2 БЫЛА #1 впервые в предыдущий шаг → откат
        if prev_rank1 == cur_rank2 and cur_rank2 in first_time_rank1 and cur_rank2 != cur_rank1:
            sym = cur_rank2
            price = top[1][2]
            entry_gain = top[1][1]
            cdl, o, ts = day_data[sym]
            m1 = m4 = eod = None
            for h, lbl in [(60,"1h"),(240,"4h")]:
                tgt = mt + h*60
                if tgt in cdl: mv = (cdl[tgt][3]-price)/price*100
                else:
                    after = [t for t in ts if t >= tgt]
                    mv = (cdl[after[0]][3]-price)/price*100 if after else None
                if lbl=="1h": m1 = mv
                else: m4 = mv
            eod = (cdl[ts[-1]][3]-price)/price*100
            results_B.append({"date":date,"symbol":sym,"entry_gain":round(entry_gain,2),
                "move_1h":round(m1,2) if m1 is not None else None,
                "move_4h":round(m4,2) if m4 is not None else None,
                "move_eod":round(eod,2)})
        prev_rank1 = cur_rank1

    print(f"  А: {sum(1 for r in results_A if r['date']==date)} | Б: {sum(1 for r in results_B if r['date']==date)}", flush=True)

with open("/tmp/momentum_results.json","w") as f:
    json.dump({"A_top5":results_A, "B_1to2":results_B}, f, ensure_ascii=False, indent=2)

def report(name, entries):
    print(f"\n=== {name} ===")
    print(f"Всего: {len(entries)}")
    if not entries: return
    for h in ["move_1h","move_4h","move_eod"]:
        v = [e for e in entries if e[h] is not None]
        if v:
            w = sum(1 for e in v if e[h]>0)
            print(f"  {h}: win {w}/{len(v)} ({w/len(v)*100:.0f}%) | avg {statistics.mean([e[h] for e in v]):+.2f}% | med {statistics.median([e[h] for e in v]):+.2f}%")

report("СЦЕНАРИЙ А: топ-5 вход", results_A)
report("СЦЕНАРИЙ Б: впервые #1 → откат #2 → лонг", results_B)
print("\nDONE")
