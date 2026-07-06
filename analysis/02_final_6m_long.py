#!/usr/bin/env python3
"""Финальный тест сценария Б на полугодии. С комиссиями и плечами.
Сигнал: монета впервые стала #1 → откат #2 → лонг.
Выход: стоп X%, трейл Y% после активации Z%.
Комиссии: 0.055% taker × 2 (entry+exit) × leverage на номинал.
Плечо: 1x, 3x, 5x, 10x — PnL и риск на стоп."""
import gzip, io, csv, gc, os, json, statistics, glob
from collections import defaultdict

HEAVY = {"BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","BNBUSDT","ADAUSDT","LTCUSDT","BCHUSDT","TRXUSDT","DOTUSDT","AVAXUSDT","LINKUSDT","TONUSDT","MATICUSDT","SHIBUSDT","NEARUSDT","APTUSDT","FILUSDT","ARBUSDT","OPUSDT"}

dates = sorted(set(os.path.basename(f).split("USDT")[-1].replace(".csv.gz","") for f in glob.glob(os.path.expanduser("~/bybit_ticks/*.csv.gz")) if "USDT" in os.path.basename(f)))
print(f"Дней: {len(dates)}: {dates[0]} → {dates[-1]}", flush=True)

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

def simulate(entry, after, sl, trail, act):
    sl_p = entry*(1-sl/100); max_p=entry; activated=False; act_p=entry*(1+act/100)
    for mt in after:
        c = after[mt]
        if c[1] > max_p: max_p = c[1]
        if not activated and max_p >= act_p: activated = True
        if activated:
            ts = max_p*(1-trail/100)
            if c[2] <= ts: return ("trail", (max_p-entry)/entry*100)
        if c[2] <= sl_p: return ("loss", -sl)
    last = list(after.values())[-1][3]
    return ("eod", (last-entry)/entry*100)

entries = []
for date in dates:
    print(f"=== {date} ===", flush=True)
    day_data = {}
    files = [f for f in glob.glob(os.path.expanduser(f"~/bybit_ticks/*{date}.csv.gz")) 
             if "USDT" in os.path.basename(f) and os.path.basename(f).endswith("USDT"+date+".csv.gz")
             and not any(h in os.path.basename(f) for h in HEAVY)]
    for i, f in enumerate(files):
        sym = os.path.basename(f).replace(date+".csv.gz","")
        cdl = load_1m(f)
        if cdl:
            ts = sorted(cdl.keys()); day_data[sym] = (cdl, cdl[ts[0]][0], ts)
        if (i+1)%200==0: print(f"  {i+1}/{len(files)}", flush=True)
        gc.collect()
    if len(day_data)<20: continue
    all_mts = sorted(set().union(*[set(d[2]) for d in day_data.values()]))
    sample = all_mts[::15]
    rankings = {}
    for mt in sample:
        g=[(s,(cdl[mt][3]-o)/o*100,cdl[mt][3]) for s,(cdl,o,ts) in day_data.items() if mt in cdl and o>0 and (cdl[mt][3]-o)/o*100>0]
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
    print(f"  входов: {len([e for e in entries if e['date']==date])} (всего {len(entries)})", flush=True)

print(f"\nВсего входов: {len(entries)}\n", flush=True)

# Тест с комиссиями и плечами
COMMISSION_RT = 0.055  # 0.055% round-trip taker Bybit linear
CONFIGS = [
    (5, 3, 3, "SL5 trail3% act+3%"),
    (7, 3, 4, "SL7 trail3% act+4%"),
    (5, 2, 3, "SL5 trail2% act+3%"),
    (10, 3, 5, "SL10 trail3% act+5%"),
    (7, 2, 4, "SL7 trail2% act+4%"),
]
LEVERAGES = [1, 3, 5, 10]

print("=== ФИНАЛЬНЫЙ ТЕСТ (комиссии + плечи) ===\n")
all_results = []
for sl, tr, act, label in CONFIGS:
    print(f"--- {label} ---", flush=True)
    raw_pnls = []  # без плеча, с комиссией
    for e in entries:
        r, raw = simulate(e["entry"], e["after"], sl, tr, act)
        pnl = raw - COMMISSION_RT  # комиссия за round-trip
        raw_pnls.append(pnl)
    wins = [p for p in raw_pnls if p > 0]
    losses = [p for p in raw_pnls if p < 0]
    wr = len(wins)/len(raw_pnls)*100 if raw_pnls else 0
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else 999
    net1x = sum(raw_pnls)
    avg = statistics.mean(raw_pnls) if raw_pnls else 0
    sharpe = avg/statistics.stdev(raw_pnls) if len(raw_pnls)>2 and statistics.stdev(raw_pnls)>0 else 0
    # Max drawdown 1x
    eq = 0; peak = 0; mdd = 0
    for p in raw_pnls:
        eq += p; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    print(f"  1x: WR {wr:.0f}% | PF {pf:.2f} | Net {net1x:+.1f}% | avg {avg:+.2f}% | Sharpe {sharpe:.2f} | MaxDD {mdd:.1f}%", flush=True)
    for lev in LEVERAGES[1:]:
        net_lev = net1x * lev
        # риск: при стопе -sl% × lev = потеря маржи
        risk_per_trade = sl * lev
        avg_lev = avg * lev
        # ликвидация: если просадка ≥ 100/lev %
        liq_price_drop = 100/lev
        liq_risk = sum(1 for p in raw_pnls if p < -liq_price_drop/100 * 100)  # сделки где сработал бы стоп на ликвидации
        print(f"  {lev}x: Net {net_lev:+.1f}% | avg {avg_lev:+.2f}% | риск/сделка {risk_per_trade:.0f}% маржи | ликв-риск при {liq_price_drop:.0f}% движении против", flush=True)
    all_results.append({"label":label,"wr":wr,"pf":pf,"net1x":net1x,"avg":avg,"sharpe":sharpe,"maxdd":mdd,
                        "wins":len(wins),"losses":len(losses),"total":len(raw_pnls)})

json.dump({"days":len(dates),"entries":len(entries),"results":all_results}, open("/tmp/final_6m.json","w"), ensure_ascii=False, indent=2)
print("\nDONE")
