#!/usr/bin/env python3
"""Оптимизация сценарария Б: впервые #1 → откат #2 → лонг.
Grid: SL × TP × time_stop. Поиск лучшего combo по PF."""
import urllib.request, gzip, io, csv, json, gc, statistics, os, itertools

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT",
         "LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
         "MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}

req = urllib.request.Request("https://api.bybit.com/v5/market/instruments-info?category=linear",
    headers={"User-Agent":"Mozilla/5.0"})
resp = urllib.request.urlopen(req, timeout=20)
data = json.loads(resp.read())
all_pairs = [r["symbol"] for r in data["result"]["list"] if r.get("status")=="Trading"]
pairs = [p for p in all_pairs if p.endswith("USDT") and not p.endswith("PERP") and p not in HEAVY]

DAYS = ["2026-07-02","2026-07-01","2026-06-30","2026-06-29","2026-06-28",
        "2026-06-27","2026-06-26","2026-06-25","2026-06-24","2026-06-23",
        "2026-06-22","2026-06-21","2026-06-20","2026-06-19"]

def load_1m(symbol, date):
    # Сначала локальный файл
    local = f"/tmp/bybit_ticks/{symbol}{date}.csv.gz"
    if os.path.exists(local):
        raw = gzip.decompress(open(local,"rb").read())
    else:
        url = f"https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz"
        try:
            rq = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            raw = gzip.decompress(urllib.request.urlopen(rq, timeout=25).read())
        except: return None
    candles = {}
    reader = csv.reader(io.StringIO(raw.decode("utf-8","ignore")))
    next(reader)
    for row in reader:
        if len(row) < 5: continue
        try:
            ts = int(float(row[0])); price = float(row[4]); size = float(row[3])
        except: continue
        mt = ts - (ts % 60)
        if mt not in candles: candles[mt] = [price,price,price,price,size]
        else:
            c = candles[mt]; c[1]=max(c[1],price); c[2]=min(c[2],price); c[3]=price; c[4]+=size
    if len(candles) < 60: return None
    return candles

# Grid
SL_vals = [0.5, 0.8, 1.0, 1.5, 2.0]
TP_vals = [1.5, 2.0, 3.0, 4.0, 5.0]
TS_vals = [0, 60, 120, 240]  # 0 = без time stop
COMMISSION = 0.055  # round-trip taker Bybit

def simulate(entry, after_items, sl, tp, ts_min, trail_pct=None):
    """after_items = [(mt, [o,h,l,c,v]), ...]. Возвращает (result, pnl_pct)."""
    sl_price = entry * (1 - sl/100)
    tp_price = entry * (1 + tp/100)
    max_price = entry
    bars = 0
    for mt, c in after_items:
        bars += 1
        if ts_min and bars > ts_min: return ("timeout", 0.0)
        # трейлинг
        if trail_pct:
            if c[1] > max_price: max_price = c[1]
            trail_stop = max_price * (1 - trail_pct/100)
            if c[2] <= trail_stop and max_price > entry:
                return ("trail", (max_price-entry)/entry*100 - COMMISSION)
        if c[2] <= sl_price: return ("loss", -sl - COMMISSION)
        if c[1] >= tp_price: return ("win", tp - COMMISSION)
    return ("timeout", 0.0)

# Собираем входы сценария Б
entries = []
for date in DAYS:
    print(f"=== {date} ===", flush=True)
    day_data = {}
    for i, sym in enumerate(pairs):
        cdl = load_1m(sym, date)
        if cdl:
            ts_sorted = sorted(cdl.keys())
            o = cdl[ts_sorted[0]][0]
            day_data[sym] = (cdl, o, ts_sorted)
        if (i+1) % 100 == 0: print(f"  {i+1}/{len(pairs)}", flush=True)
        gc.collect()
    if len(day_data) < 20: continue
    all_mts = sorted(set().union(*[set(d[2]) for d in day_data.values()]))
    sample_mts = all_mts[::15]
    rankings = {}
    for mt in sample_mts:
        gains = []
        for sym,(cdl,o,ts) in day_data.items():
            if mt not in cdl or o == 0: continue
            g = (cdl[mt][3]-o)/o*100
            if g > 0: gains.append((sym,g,cdl[mt][3]))
        gains.sort(key=lambda x: x[1], reverse=True)
        rankings[mt] = gains
    first_rank1 = set()
    prev_rank1 = None
    for mt in sample_mts:
        top = rankings.get(mt, [])
        if len(top) < 2: continue
        cur1, cur2 = top[0][0], top[1][0]
        if cur1 not in first_rank1: first_rank1.add(cur1)
        if prev_rank1 == cur2 and cur2 in first_rank1 and cur2 != cur1:
            sym = cur2; price = top[1][2]
            cdl, o, ts = day_data[sym]
            after = [(t, cdl[t]) for t in ts if t > mt]
            if len(after) >= 10:
                entries.append({"sym":sym,"date":date,"entry":price,"after":after})
        prev_rank1 = cur1
    print(f"  Входов: {len([e for e in entries if e['date']==date])} (всего {len(entries)})", flush=True)

print(f"\nВсего входов: {len(entries)}", flush=True)

# Grid search
print("\n=== GRID SEARCH ===", flush=True)
results_grid = []
best_pf = 0; best = None
for sl, tp, ts in itertools.product(SL_vals, TP_vals, TS_vals):
    wins = losses = timeouts = 0; net = 0
    pnls = []
    for e in entries:
        r, pnl = simulate(e["entry"], e["after"], sl, tp, ts)
        if r == "win": wins += 1; net += pnl; pnls.append(pnl)
        elif r == "loss": losses += 1; net += pnl; pnls.append(pnl)
        else: timeouts += 1
    total = wins + losses
    if total < 20: continue
    pf = (wins*tp)/(losses*sl) if losses > 0 else 999
    wr = wins/total*100
    avg = statistics.mean(pnls) if pnls else 0
    sharpe = avg/statistics.stdev(pnls) if len(pnls)>2 and statistics.stdev(pnls)>0 else 0
    results_grid.append({"sl":sl,"tp":tp,"ts":ts,"wr":round(wr,1),"pf":round(pf,2),
                         "net":round(net,1),"wins":wins,"losses":losses,"timeouts":timeouts,
                         "avg":round(avg,2),"sharpe":round(sharpe,2)})
    if pf > best_pf and wr > 0:
        best_pf = pf; best = (sl,tp,ts,wr,pf,net,wins,losses)

# Топ-15 по PF
results_grid.sort(key=lambda x: x["pf"], reverse=True)
print("\nТОП-15 combos по PF:", flush=True)
for r in results_grid[:15]:
    print(f"  SL{r['sl']} TP{r['tp']} TS{r['ts']}: WR {r['wr']}% | PF {r['pf']} | Net {r['net']}% | {r['wins']}W/{r['losses']}L/{r['timeouts']}T | avg {r['avg']} | Sharpe {r['sharpe']}", flush=True)

with open("/tmp/scenario_B_grid.json","w") as f:
    json.dump({"entries":len(entries), "grid":results_grid}, f, ensure_ascii=False, indent=2)
print("\nDONE")
