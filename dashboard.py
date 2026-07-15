#!/usr/bin/env python3
"""Short Bot #2 — dashboard. aiohttp.web, один файл, без зависимостей кроме aiohttp.

Порт 8077. Читает logs/ бота (trades.jsonl, positions.json, equity.jsonl).
График сделки: candlestick (lightweight-charts) + маркеры entry/exit/SL/TP/trail,
свечи тянет с Bybit REST /v5/market/kline.
"""
import asyncio, json, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

import aiohttp
from aiohttp import web

BASE = Path(__file__).resolve().parent
LOG = BASE / "logs"
TRADES_F = LOG / "trades.jsonl"
POS_F = LOG / "positions.json"
EQ_F = LOG / "equity.jsonl"
CFG_F = BASE / "config.yaml"

BYBIT = "https://api.bybit.com"


def load_jsonl(p):
    out = []
    if not p.exists():
        return out
    with open(p) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def load_json(p):
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def load_cfg():
    cfg = {}
    if not CFG_F.exists():
        return cfg
    for ln in CFG_F.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        v = v.split("#")[0].strip()
        try:
            fv = float(v)
            cfg[k.strip()] = int(fv) if fv == int(fv) else fv
        except Exception:
            cfg[k.strip()] = v
    return cfg


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def fmt_dur(sec):
    try:
        sec = float(sec)
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
        if h:
            return f"{h}ч {m}м"
        if m:
            return f"{m}м {s}с"
        return f"{s}с"
    except Exception:
        return str(sec)


def pct(a, b):
    try:
        return ((float(a) - float(b)) / float(b)) * 100
    except Exception:
        return 0.0


# ── API handlers ──

async def api_overview(req):
    pos = load_json(POS_F)
    trades = load_jsonl(TRADES_F)
    cfg = load_cfg()
    closed = len(trades)
    wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
    total_pnl = sum(t.get("net_pnl", 0) for t in trades)
    eq = load_jsonl(EQ_F)
    # full-history equity curve with light downsampling for scalability
    MAX_PTS = 1500
    if len(eq) > MAX_PTS:
        stride = max(1, len(eq) // MAX_PTS)
        eq_sample = eq[::stride]
        # always keep the last point
        if eq_sample[-1] is not eq[-1]:
            eq_sample = eq_sample + [eq[-1]]
    else:
        eq_sample = eq
    eq_pts = [{"t": e["ts"], "eq": round(e.get("equity", 0), 2),
               "bal": round(e.get("balance", 0), 2)}
              for e in eq_sample]
    # full-history metrics (computed on ALL points, not the sample)
    eq_all = [float(e.get("equity", 0)) for e in eq]
    bal_all = [float(e.get("balance", 0)) for e in eq]
    init = float(cfg.get("initial_balance", 1000))
    cur_eq = eq_all[-1] if eq_all else init
    peak = cur_eq
    max_dd = 0.0
    for v in eq_all:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    ret_pct = (cur_eq / init - 1) * 100 if init else 0
    losses = [t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) < 0]
    gross_loss = abs(sum(losses))
    pf = round(sum(t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) > 0) / gross_loss, 2) if gross_loss > 0 else None
    period_days = round((eq[-1]["ts"] - eq[0]["ts"]) / 86400, 1) if len(eq) >= 2 else 0
    return web.json_response({
        "balance": round(pos.get("balance", 0), 2),
        "equity": round(pos.get("equity", 0), 2),
        "open_count": len(pos.get("open", [])),
        "closed_today": pos.get("closed_today", 0),
        "closed_total": closed,
        "wins": wins,
        "losses": closed - wins,
        "wr": round(wins / closed * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "updated": pos.get("updated"),
        "config": {k: cfg[k] for k in ("sl_pct", "tp_pct", "trail_pct", "activation_pct",
                                       "risk_pct", "max_open_positions", "max_daily_entries",
                                       "initial_balance") if k in cfg},
        "equity_curve": eq_pts,
        "eq_metrics": {
            "initial": round(init, 2),
            "current": round(cur_eq, 2),
            "return_pct": round(ret_pct, 2),
            "max_dd_pct": round(max_dd, 2),
            "profit_factor": pf,
            "period_days": period_days,
            "points": len(eq),
        },
    })


async def _fetch_prices(symbols):
    """Текущие цены по Bybit /v5/market/tickers (один запрос на все)."""
    out = {}
    if not symbols:
        return out
    url = f"{BYBIT}/v5/market/tickers?category=linear"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        for t in data.get("result", {}).get("list", []):
            s = t.get("symbol")
            if s in symbols:
                try:
                    out[s] = float(t.get("lastPrice", 0))
                except Exception:
                    pass
    except Exception:
        pass
    return out


async def api_open(req):
    pos = load_json(POS_F)
    openp = pos.get("open", [])
    syms = {p.get("symbol") for p in openp}
    prices = await _fetch_prices(syms)
    rows = []
    for p in openp:
        sym = p.get("symbol")
        cur_price = prices.get(sym)
        # шорт: uPnL = (entry - current) * qty
        u_pnl = None
        u_pnl_pct = None
        if cur_price:
            u_pnl = (float(p["entry_price"]) - cur_price) * float(p["qty"])
            u_pnl_pct = pct(cur_price, p["entry_price"])
        rows.append({
            "id": p.get("id"),
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "entry_price": p.get("entry_price"),
            "qty": p.get("qty"),
            "notional": round(p.get("notional", 0), 2),
            "entry_ts": p.get("entry_ts"),
            "entry_str": fmt_ts(p.get("entry_ts")),
            "sl_price": p.get("sl_price"),
            "tp_price": p.get("tp_price"),
            "act_price": p.get("act_price"),
            "trail_pct": p.get("trail_pct"),
            "activated": p.get("activated"),
            "min_price": p.get("min_price"),
            "commission_paid": round(p.get("commission_paid", 0), 4),
            "funding_paid": round(p.get("funding_paid", 0), 4),
            "funding_rate_at_open": p.get("funding_rate_at_open"),
            "hold_sec": round(time.time() - float(p.get("entry_ts", 0)), 0),
            "hold_str": fmt_dur(time.time() - float(p.get("entry_ts", 0))),
            "last_price": cur_price,
            "u_pnl": round(u_pnl, 4) if u_pnl is not None else None,
            "u_pnl_pct": round(u_pnl_pct, 2) if u_pnl_pct is not None else None,
        })
    return web.json_response({"open": rows})


async def api_trades(req):
    trades = load_jsonl(TRADES_F)
    rows = []
    for t in reversed(trades):  # новые сверху
        rows.append({
            "id": t.get("id"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "qty": t.get("qty"),
            "notional": round(t.get("qty", 0) * t.get("entry_price", 0), 2),
            "pnl": round(t.get("pnl", 0), 4),
            "commission": round(t.get("commission", 0), 4),
            "funding": round(t.get("funding", 0), 4),
            "net_pnl": round(t.get("net_pnl", 0), 4),
            "reason": t.get("reason"),
            "entry_ts": t.get("entry_ts"),
            "exit_ts": t.get("exit_ts"),
            "entry_str": fmt_ts(t.get("entry_ts")),
            "exit_str": fmt_ts(t.get("exit_ts")),
            "hold_sec": t.get("hold_sec"),
            "hold_str": fmt_dur(t.get("hold_sec")),
            "move_pct": round(pct(t.get("exit_price"), t.get("entry_price")), 2),
            "entry_price_pct": t.get("entry_price_pct"),
            "balance_after": round(t.get("balance_after", 0), 2),
            "date": t.get("date"),
        })
    return web.json_response({"trades": rows, "total": len(rows)})


async def api_chart(req):
    """Свечи Bybit + маркеры сделки. ?id=TRADE_ID или ?symbol=XXX&entry=TS&exit=TS."""
    sym = req.query.get("symbol")
    entry_ts = req.query.get("entry")
    exit_ts = req.query.get("exit")
    trade_id = req.query.get("id")

    # если есть id — найдём сделку
    trade = None
    if trade_id:
        for t in load_jsonl(TRADES_F):
            if t.get("id") == trade_id:
                trade = t; break
        if not trade:
            # может быть открытая позиция
            pos = load_json(POS_F)
            for p in pos.get("open", []):
                if p.get("id") == trade_id:
                    trade = {"id": p["id"], "symbol": p["symbol"], "side": p["side"],
                             "entry_price": p["entry_price"], "entry_ts": p["entry_ts"],
                             "sl_price": p.get("sl_price"), "tp_price": p.get("tp_price"),
                             "act_price": p.get("act_price"), "trail_pct": p.get("trail_pct"),
                             "min_price": p.get("min_price"), "activated": p.get("activated"),
                             "exit_price": None, "exit_ts": None, "reason": "open"}
                    break
    # для закрытой сделки достаём SL/TP/Trail из конфига (в логе их нет)
    if trade and trade.get("exit_ts") and "sl_price" not in trade:
        cfg = load_cfg()
        e = float(trade["entry_price"])
        sl_pct = float(cfg.get("sl_pct", 30))
        tp_pct = float(cfg.get("tp_pct", 15))
        trail_pct = float(cfg.get("trail_pct", 10))
        act_pct = float(cfg.get("activation_pct", 1))
        # шорт: SL выше входа, TP ниже
        trade["sl_price"] = e * (1 + sl_pct / 100)
        trade["tp_price"] = e * (1 - tp_pct / 100)
        trade["act_price"] = e * (1 - act_pct / 100)
        trade["trail_pct"] = trail_pct
        trade["min_price"] = None
        trade["activated"] = None
    # добавим строковые поля для фронтенда
    if trade:
        if trade.get("qty") is not None and trade.get("entry_price") is not None and "notional" not in trade:
            trade["notional"] = round(float(trade["qty"]) * float(trade["entry_price"]), 2)
        trade.setdefault("entry_str", fmt_ts(trade.get("entry_ts")))
        if trade.get("exit_ts"):
            trade["exit_str"] = fmt_ts(trade.get("exit_ts"))
            trade.setdefault("hold_str", fmt_dur(trade.get("hold_sec")))
        if "move_pct" not in trade and trade.get("exit_price"):
            trade["move_pct"] = round(pct(trade["exit_price"], trade["entry_price"]), 2)
    if trade:
        sym = trade["symbol"]
        entry_ts = trade["entry_ts"]
        exit_ts = trade.get("exit_ts") or time.time()

    if not sym or not entry_ts:
        return web.json_response({"error": "need id or symbol+entry"}, status=400)

    entry_ts = float(entry_ts)
    exit_ts = float(exit_ts) if exit_ts else time.time()
    # окно: 30 мин до входа, 30 мин после выхода (или сейчас)
    start = int((entry_ts - 1800) * 1000)
    end = int((exit_ts + 1800) * 1000)
    # Bybit kline: interval 1m, limit до 1000, start/end
    url = f"{BYBIT}/v5/market/kline?category=linear&symbol={sym}&interval=1&start={start}&end={end}&limit=1000"
    candles = []
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
            for k in data.get("result", {}).get("list", []):
                # bybit: [start, open, high, low, close, volume, turnover]
                candles.append({
                    "t": int(k[0]) / 1000,
                    "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                })
            candles.sort(key=lambda x: x["t"])
        except Exception as e:
            return web.json_response({"error": f"bybit: {e}"}, status=502)

    return web.json_response({
        "symbol": sym,
        "candles": candles,
        "trade": trade,
    })


# ── HTML ──

HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Bot #2 — Dashboard</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b0e11; color: #eaecef; font: 14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
a { color: #4a9eff; }
.container { max-width: 1100px; margin: 0 auto; padding: 12px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1e2329; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
header h1 { font-size: 18px; color: #f0b90b; }
header h1 small { color: #848e9c; font-weight: normal; font-size: 12px; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.tab { padding: 6px 14px; background: #1e2329; border-radius: 6px; cursor: pointer; color: #848e9c; font-size: 13px; }
.tab.active { background: #f0b90b; color: #0b0e11; font-weight: 600; }
.card { background: #181a20; border: 1px solid #1e2329; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.stat { display: flex; flex-direction: column; }
.stat .lbl { color: #848e9c; font-size: 11px; text-transform: uppercase; }
.stat .val { font-size: 20px; font-weight: 600; }
.pos { color: #0ecb81; } .neg { color: #f6465d; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 8px; text-align: left; border-bottom: 1px solid #1e2329; }
th { color: #848e9c; font-weight: 600; font-size: 11px; text-transform: uppercase; }
tr:hover { background: #1e2329; cursor: pointer; }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge.tp { background: #0ecb8133; color: #0ecb81; }
.badge.sl { background: #f6465d33; color: #f6465d; }
.badge.trail { background: #4a9eff33; color: #4a9eff; }
.badge.open { background: #f0b90b33; color: #f0b90b; }
.badge.short { background: #f6465d22; color: #f6465d; }
#chart-box { height: 420px; background: #0b0e11; border-radius: 8px; margin: 10px 0; }
.trade-info { display: grid; grid-template-columns: repeat(auto-fit,minmax(120px,1fr)); gap: 10px; margin-top: 10px; }
.trade-info .lbl { color: #848e9c; font-size: 11px; }
.trade-info .val { font-size: 16px; font-weight: 600; }
.back { cursor: pointer; color: #4a9eff; margin-bottom: 8px; display: inline-block; }
.hidden { display: none; }
.muted { color: #848e9c; }
.eq-chart { height: 320px; margin-top: 8px; }
.eq-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin-bottom: 10px; }
.eq-metrics .stat { background: #1e2329; padding: 6px 10px; border-radius: 6px; }
.eq-metrics .lbl { color: #848e9c; font-size: 10px; display: block; }
.eq-metrics .val { font-size: 15px; font-weight: 600; }
.eq-legend { display: flex; gap: 14px; font-size: 11px; color: #848e9c; margin-top: 6px; }
.eq-legend span { display: inline-flex; align-items: center; gap: 5px; }
.eq-legend i { width: 14px; height: 2px; display: inline-block; border-radius: 1px; }
.chart-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.tool-btn { padding: 5px 12px; background: #1e2329; color: #eaecef; border: 1px solid #2b3139; border-radius: 5px; cursor: pointer; font-size: 13px; }
.tool-btn:hover { background: #2b3139; }
.tool-btn.active { background: #f0b90b; color: #0b0e11; border-color: #f0b90b; }
#ruler-hint { font-size: 12px; }
#tv-chart { height: 500px; width: 100%; border-radius: 8px; overflow: hidden; }
#tv-chart iframe { border-radius: 8px; }
#chart-box { height: 500px; background: #0b0e11; border-radius: 8px; }
.chart-tabs { display: flex; gap: 4px; margin-bottom: 8px; }
.ctab { padding: 6px 14px; background: #1e2329; border-radius: 6px 6px 0 0; cursor: pointer; color: #848e9c; font-size: 13px; }
.ctab.active { background: #f0b90b; color: #0b0e11; font-weight: 600; }
.ctab-panel { min-height: 500px; }
@media (max-width: 600px) { .stat .val { font-size: 16px; } #tv-chart { height: 380px; } #chart-box { height: 380px; } .eq-chart { height: 240px; } }
</style></head><body><div class="container">
<header>
  <h1>Short Bot #2 <small id="upd"></small></h1>
  <div class="tabs">
    <div class="tab active" data-view="overview">Обзор</div>
    <div class="tab" data-view="open">Открытые</div>
    <div class="tab" data-view="trades">История</div>
  </div>
</header>

<div id="view-overview">
  <div class="card">
    <div class="grid">
      <div class="stat"><span class="lbl">Баланс</span><span class="val" id="s-balance">—</span></div>
      <div class="stat"><span class="lbl">Equity</span><span class="val" id="s-equity">—</span></div>
      <div class="stat"><span class="lbl">PnL всего</span><span class="val" id="s-pnl">—</span></div>
      <div class="stat"><span class="lbl">Win Rate</span><span class="val" id="s-wr">—</span></div>
      <div class="stat"><span class="lbl">Открыто</span><span class="val" id="s-open">—</span></div>
      <div class="stat"><span class="lbl">Закрыто</span><span class="val" id="s-closed">—</span></div>
    </div>
  </div>
  <div class="card">
    <div class="lbl muted" style="margin-bottom:6px">EQUITY КРИВАЯ <small id="eq-period" style="color:#5e6673"></small></div>
    <div id="eq-metrics" class="eq-metrics"></div>
    <div id="eq-chart" class="eq-chart"></div>
    <div class="eq-legend">
      <span><i style="background:#f0b90b"></i>Equity</span>
      <span><i style="background:#848e9c"></i>Balance</span>
    </div>
  </div>
  <div class="card">
    <div class="lbl muted" style="margin-bottom:6px">КОНФИГ</div>
    <div id="cfg" class="grid"></div>
  </div>
</div>

<div id="view-open" class="hidden">
  <div class="card">
    <table id="open-table"><thead>
      <tr><th>Symbol</th><th>Вход</th><th>Цена</th><th>Размер $</th><th>SL</th><th>TP</th><th>Trail</th><th>Hold</th><th>uPnL</th><th></th></tr>
    </thead><tbody></tbody></table>
    <div class="muted" style="font-size:11px;margin-top:6px">uPnL — нереализованный PnL по текущей цене Bybit (real-time). Клик по строке — график.</div>
  </div>
</div>

<div id="view-trades" class="hidden">
  <div class="card">
    <table id="trades-table"><thead>
      <tr><th>Symbol</th><th>Side</th><th>Размер $</th><th>Вход</th><th>Выход</th><th>Move%</th><th>Hold</th><th>Net PnL</th><th>Reason</th><th>Дата</th></tr>
    </thead><tbody></tbody></table>
  </div>
</div>

<div id="view-detail" class="hidden">
  <span class="back" onclick="showView('trades')">← назад</span>
  <div class="card">
    <h2 id="d-title" style="margin-bottom:6px">—</h2>
    <div id="d-info" class="trade-info" style="margin-bottom:10px"></div>
    <div class="chart-tabs">
      <div class="ctab active" data-ctab="deal">📊 Сделка</div>
      <div class="ctab" data-ctab="tv">📈 Анализ (TradingView)</div>
    </div>
    <div id="ctab-deal" class="ctab-panel">
      <div id="chart-box"></div>
    </div>
    <div id="ctab-tv" class="ctab-panel hidden">
      <div id="tv-chart"></div>
    </div>
  </div>
</div>

</div>
<script>
const $ = s => document.querySelector(s);
let chart = null, series = null, eqSeries = null, balSeries = null;

async function api(p) { const r = await fetch(p); return r.json(); }

function fmt(n, d=2) { if (n==null) return '—'; return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function pct(n) { return (n>=0?'+':'') + fmt(n,2) + '%'; }
function pnlCls(n) { return n>=0?'pos':'neg'; }
function badge(r) {
  const m = {take_profit:'tp',stop_loss:'sl',trail:'trail',open:'open'};
  const lbl = {take_profit:'TP',stop_loss:'SL',trail:'TRAIL',open:'OPEN'};
  return `<span class="badge ${m[r]||''}">${lbl[r]||r}</span>`;
}

async function loadOverview() {
  const d = await api('/api/overview');
  $('#s-balance').textContent = '$' + fmt(d.balance);
  $('#s-equity').textContent = '$' + fmt(d.equity);
  const pnl = d.equity - (d.config?.initial_balance||1000);
  $('#s-pnl').innerHTML = `<span class="${pnlCls(pnl)}">${pnl>=0?'+':''}$${fmt(pnl)}</span>`;
  $('#s-wr').textContent = d.wr + '% (' + d.wins + '/' + d.closed_total + ')';
  $('#s-open').textContent = d.open_count;
  $('#s-closed').textContent = d.closed_total + ' / сегодня ' + d.closed_today;
  $('#upd').textContent = 'обновлено ' + (d.updated||'')?.slice(11,19);
  const cfg = d.config||{};
  $('#cfg').innerHTML = Object.entries(cfg).map(([k,v])=>`<div class="stat"><span class="lbl">${k}</span><span class="val" style="font-size:15px">${v}</span></div>`).join('');
  // equity curve metrics
  const m = d.eq_metrics;
  if (m) {
    $('#eq-period').textContent = `· ${m.period_days} дн · ${m.points} точек`;
    const pfTxt = m.profit_factor!=null ? m.profit_factor.toFixed(2) : '—';
    const retCls = m.return_pct>=0 ? 'pos' : 'neg';
    const ddCls = m.max_dd_pct>=0 ? 'neg' : 'pos';
    $('#eq-metrics').innerHTML = [
      ['Return', `<span class="${retCls}">${m.return_pct>=0?'+':''}${m.return_pct}%</span>`, 'ret'],
      ['Max DD', `<span class="${ddCls}">-${m.max_dd_pct}%</span>`, 'dd'],
      ['Profit Factor', pfTxt, 'pf'],
      ['Start', '$'+fmt(m.initial), 'init'],
      ['Current', '$'+fmt(m.current), 'cur'],
    ].map(([k,v])=>`<div class="stat"><span class="lbl">${k}</span><span class="val">${v}</span></div>`).join('');
  }
  // equity curve
  drawEq(d.equity_curve);
}

function drawEq(pts) {
  if (!pts || !pts.length) return;
  if (!eqSeries) {
    const c = LightweightCharts.createChart($('#eq-chart'), {height:320,layout:{background:{color:'#0b0e11'},textColor:'#848e9c'},grid:{vertLines:{color:'#1e2329'},horzLines:{color:'#1e2329'}},rightPriceScale:{visible:true},timeScale:{timeVisible:true,secondsVisible:false},crosshair:{mode:0}});
    eqSeries = c.addAreaSeries({lineColor:'#f0b90b',topColor:'#f0b90b33',bottomColor:'#f0b90b00',priceFormat:{type:'price',precision:2}});
    balSeries = c.addLineSeries({color:'#848e9c',lineWidth:1,priceFormat:{type:'price',precision:2}});
    window._eqC = c;
  }
  eqSeries.setData(pts.map(p=>({time:Math.floor(p.t), value:p.eq})));
  balSeries.setData(pts.map(p=>({time:Math.floor(p.t), value:p.bal})));
  window._eqC.timeScale().fitContent();
}

async function loadOpen() {
  const d = await api('/api/open');
  const tb = $('#open-table tbody'); tb.innerHTML = '';
  if (!d.open.length) { tb.innerHTML = '<tr><td colspan="9" class="muted">нет открытых позиций</td></tr>'; return; }
  for (const p of d.open) {
    const tr = document.createElement('tr');
    tr.onclick = () => openDetail(p.id);
    tr.innerHTML = `<td><b>${p.symbol}</b> <span class="badge short">${p.side}</span></td>
      <td>${p.entry_str}</td>
      <td>${fmt(p.entry_price, 6)}${p.last_price?` → <span class="muted">${fmt(p.last_price,6)}</span>`:''}</td>
      <td><b>$${fmt(p.notional,0)}</b> <span class="muted">${fmt(p.qty,2)}</span></td>
      <td class="neg">${fmt(p.sl_price,6)}</td>
      <td class="pos">${fmt(p.tp_price,6)}</td>
      <td>${p.activated?'✅':'⏳'} ${p.trail_pct}% ${p.min_price?fmt(p.min_price,6):''}</td>
      <td>${p.hold_str}</td>
      <td class="${pnlCls(p.u_pnl||0)}">${p.u_pnl!=null?(p.u_pnl>=0?'+':'')+'$'+fmt(p.u_pnl)+' <span class="muted">('+pct(p.u_pnl_pct)+')</span>':'—'}</td>
      <td><span class="badge open">OPEN</span></td>`;
    tb.appendChild(tr);
  }
}

async function loadTrades() {
  const d = await api('/api/trades');
  const tb = $('#trades-table tbody'); tb.innerHTML = '';
  if (!d.trades.length) { tb.innerHTML = '<tr><td colspan="10" class="muted">нет закрытых сделок</td></tr>'; return; }
  for (const t of d.trades) {
    const tr = document.createElement('tr');
    tr.onclick = () => openDetail(t.id);
    tr.innerHTML = `<td><b>${t.symbol}</b></td>
      <td><span class="badge short">${t.side}</span></td>
      <td><b>$${fmt(t.notional,0)}</b></td>
      <td>${fmt(t.entry_price,6)}</td>
      <td>${fmt(t.exit_price,6)}</td>
      <td>${pct(t.move_pct)}</td>
      <td>${t.hold_str}</td>
      <td class="${pnlCls(t.net_pnl)}">${t.net_pnl>=0?'+':''}$${fmt(t.net_pnl)}</td>
      <td>${badge(t.reason)}</td>
      <td class="muted">${t.date||''}</td>`;
    tb.appendChild(tr);
  }
}

async function openDetail(id) {
  showView('detail');
  const d = await api('/api/chart?id=' + encodeURIComponent(id));
  if (d.error) { $('#d-title').textContent = 'Ошибка: ' + d.error; return; }
  const t = d.trade, sym = d.symbol;
  $('#d-title').innerHTML = `${sym} <span class="badge short">${t.side}</span> ${badge(t.reason)}`;
  window._currentTrade = t;
  drawChart(d.candles, t);
  // info
  const move = t.exit_price ? pct(((t.exit_price - t.entry_price)/t.entry_price)*100) : '—';
  const net = t.net_pnl!=null ? `<span class="${pnlCls(t.net_pnl)}">${t.net_pnl>=0?'+':''}$${fmt(t.net_pnl)}</span>` : '—';
  $('#d-info').innerHTML = [
    ['Вход', fmt(t.entry_price,6)], ['Выход', t.exit_price?fmt(t.exit_price,6):'—'],
    ['SL', fmt(t.sl_price,6)], ['TP', fmt(t.tp_price,6)],
    ['Trail min', t.min_price?fmt(t.min_price,6):'—'], ['Activation', t.activated?'✅ '+fmt(t.act_price,6):'⏳'],
    ['Move', move], ['Net PnL', net],
    ['Время входа', t.entry_str||fmt_ts(t.entry_ts)], ['Время выхода', t.exit_str||'—'],
    ['Hold', t.hold_str||'—'], ['Комиссия', t.commission!=null?'$'+fmt(t.commission):'—'],
    ['Фандинг', t.funding!=null?'$'+fmt(t.funding):'—'], ['Размер позиции', (t.notional!=null?'$'+fmt(t.notional,0):'—') + (t.qty!=null?' · '+fmt(t.qty,4)+' ед':'')],
  ].map(([k,v])=>`<div class="stat"><span class="lbl">${k}</span><span class="val" style="font-size:14px">${v}</span></div>`).join('');
}

function fmt_ts(ts){ if(!ts) return '—'; const d=new Date(ts*1000); return d.toISOString().slice(0,16).replace('T',' ')+' UTC'; }

function drawChart(candles, t) {
  drawDealChart(candles, t);
  drawTVChart(t);
}

// ── Вкладка "Сделка": lightweight-charts с маркерами entry/exit и линиями SL/TP ──
function drawDealChart(candles, t) {
  if (chart) { chart.remove(); chart=null; series=null; }
  const box = $('#chart-box');
  box.innerHTML = '';
  chart = LightweightCharts.createChart(box, {
    height: 500, layout: {background:{color:'#0b0e11'},textColor:'#848e9c'},
    grid: {vertLines:{color:'#1e2329'},horzLines:{color:'#1e2329'}},
    rightPriceScale: {visible:true}, timeScale: {timeVisible:true, secondsVisible:true},
    crosshair: {mode: 0},
  });
  series = chart.addCandlestickSeries({
    upColor:'#0ecb81',downColor:'#f6465d',borderUpColor:'#0ecb81',borderDownColor:'#f6465d',
    wickUpColor:'#0ecb81',wickDownColor:'#f6465d', priceFormat:{type:'price',precision:6,minMove:0.000001},
  });
  series.setData(candles.map(c=>({time:Math.floor(c.t),open:c.o,high:c.h,low:c.l,close:c.c})));

  // горизонтальные линии: entry / SL / TP / exit / min
  const entryT = Math.floor(t.entry_ts);
  const exitT = t.exit_ts ? Math.floor(t.exit_ts) : Math.floor(Date.now()/1000);
  const lines = [
    {price: t.entry_price, color: '#f0b90b', title: 'ENTRY', dashed: false},
    {price: t.sl_price, color: '#f6465d', title: 'SL', dashed: true},
    {price: t.tp_price, color: '#0ecb81', title: 'TP', dashed: true},
  ];
  if (t.exit_price) lines.push({price: t.exit_price, color: '#4a9eff', title: 'EXIT', dashed: false});
  if (t.min_price) lines.push({price: t.min_price, color: '#848e9c', title: 'MIN', dashed: true});
  for (const L of lines) {
    if (L.price==null) continue;
    series.createPriceLine({price: L.price, color: L.color, lineWidth: 1, lineStyle: L.dashed?2:0, axisLabelVisible: true, title: L.title});
  }
  // маркеры entry/exit на оси времени
  const qtyStr = t.qty != null ? ' · ' + fmt(t.qty, 4) : '';
  const notionalStr = t.notional != null ? ' · $' + fmt(t.notional, 0) : '';
  const markers = [
    {time: entryT, position: 'aboveBar', color: '#f0b90b', shape: 'arrowDown', text: 'ENTRY' + notionalStr + qtyStr},
  ];
  if (t.exit_ts) markers.push({time: exitT, position: 'belowBar', color: '#4a9eff', shape: 'arrowUp', text: t.reason?.toUpperCase()||'EXIT'});
  try { series.setMarkers(markers); } catch(e) {}
  chart.timeScale().fitContent();
}

// ── Вкладка "Анализ": TradingView со всеми инструментами ──
function drawTVChart(t) {
  const box = $('#tv-chart');
  box.innerHTML = '';
  const sym = t.symbol;
  const tvSym = 'BYBIT:' + sym.replace('USDT','') + 'USDT.P';
  const holdSec = t.exit_ts ? (t.exit_ts - t.entry_ts) : (Date.now()/1000 - t.entry_ts);
  let interval = '1';
  if (holdSec > 86400*3) interval = '60';
  else if (holdSec > 86400) interval = '15';
  else if (holdSec > 3600*6) interval = '5';
  else interval = '1';

  new TradingView.widget({
    autosize: true,
    symbol: tvSym,
    interval: interval,
    timezone: 'UTC',
    theme: 'dark',
    style: '1',
    locale: 'ru',
    toolbar_bg: '#0b0e11',
    enable_publishing: false,
    allow_symbol_change: true,
    hide_side_toolbar: false,
    withdateranges: true,
    details: false,
    studies: [],
    container_id: 'tv-chart',
  });
}

function fmtDur(sec) {
  sec = Math.abs(sec);
  if (sec < 60) return sec + 'с';
  const m = Math.floor(sec/60), s = sec%60;
  if (m < 60) return m + 'м ' + s + 'с';
  const h = Math.floor(m/60), mm = m%60;
  if (h < 24) return h + 'ч ' + mm + 'м';
  const d = Math.floor(h/24), hh = h%24;
  return d + 'д ' + hh + 'ч';
}

function showView(v) {
  document.querySelectorAll('[id^="view-"]').forEach(e=>e.classList.add('hidden'));
  $('#view-'+v).classList.remove('hidden');
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.view===v));
  if (v==='overview') loadOverview();
  if (v==='open') loadOpen();
  if (v==='trades') loadTrades();
}

// переключение вкладок графика (Сделка / Анализ)
document.addEventListener('click', e => {
  const c = e.target.closest('.ctab');
  if (!c) return;
  document.querySelectorAll('.ctab').forEach(t=>t.classList.toggle('active', t===c));
  document.querySelectorAll('.ctab-panel').forEach(p=>p.classList.add('hidden'));
  $('#ctab-'+c.dataset.ctab).classList.remove('hidden');
  // при переключении на lightweight-charts вкладку — resize (график мог быть скрыт)
  if (c.dataset.ctab === 'deal' && chart) {
    setTimeout(() => { try { chart.applyOptions({width: $('#chart-box').clientWidth, height: 500}); chart.timeScale().fitContent(); } catch(e){} }, 50);
  }
  // при переключении на TradingView — пересоздать виджет (иначе iframe 0 высоты из-за скрытого контейнера)
  if (c.dataset.ctab === 'tv' && window._currentTrade) {
    setTimeout(() => drawTVChart(window._currentTrade), 50);
  }
});

document.querySelectorAll('.tab').forEach(t => t.onclick = () => showView(t.dataset.view));
showView('overview');
setInterval(() => { if (!$('#view-overview').classList.contains('hidden')) loadOverview();
  if (!$('#view-open').classList.contains('hidden')) loadOpen(); }, 15000);
</script>
</body></html>
"""


async def index(req):
    return web.Response(text=HTML, content_type="text/html")


async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/open", api_open)
    app.router.add_get("/api/trades", api_trades)
    app.router.add_get("/api/chart", api_chart)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8077)
    await site.start()
    print("dashboard on :8077", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
