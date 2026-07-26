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
        if eq_sample[-1] is not eq[-1]:
            eq_sample = eq_sample + [eq[-1]]
    else:
        eq_sample = eq
    eq_pts = [{"t": e["ts"], "eq": round(e.get("equity", 0), 2),
               "bal": round(e.get("balance", 0), 2)}
              for e in eq_sample]
    # full-history metrics (computed on ALL points, not the sample)
    eq_all = [float(e.get("equity", 0)) for e in eq]
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
                                       "initial_balance",
                                       "dca_enabled", "dca_max_count", "dca_trigger_pct",
                                       "dca_qty_multiplier", "commission_taker_pct",
                                       "slippage_pct", "ranking_interval_min",
                                       "min_volume_usd") if k in cfg},
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
            # unrealized net PnL = uPnL - commission - funding
            "u_net_pnl": round((u_pnl or 0) - p.get("commission_paid", 0) - p.get("funding_paid", 0), 4) if u_pnl is not None else None,
            # distances to key levels (% from current price)
            "dist_to_tp": round((p.get("tp_price",0) / cur_price - 1) * 100, 2) if cur_price and p.get("tp_price") else None,
            "dist_to_dca": round((p.get("dca_trigger_price",0) / cur_price - 1) * 100, 2) if cur_price and p.get("dca_trigger_price") and p.get("dca_count",0) < p.get("dca_max_count",0) else None,
            "dist_to_sl": round((p.get("sl_price",0) / cur_price - 1) * 100, 2) if cur_price and p.get("sl_price") else None,
            "hold_sec": round(time.time() - float(p.get("entry_ts", 0)), 0),
            "hold_str": fmt_dur(time.time() - float(p.get("entry_ts", 0))),
            "last_price": cur_price,
            "u_pnl": round(u_pnl, 4) if u_pnl is not None else None,
            "u_pnl_pct": round(u_pnl_pct, 2) if u_pnl_pct is not None else None,
            # DCA fields
            "dca_count": p.get("dca_count", 0),
            "dca_max_count": p.get("dca_max_count", 0),
            "dca_trigger_price": p.get("dca_trigger_price"),
            "dca_trigger_pct": round((p.get("dca_trigger_price",0)/p.get("entry_price",1)-1)*100, 1) if p.get("dca_trigger_price") and p.get("entry_price") else None,
            "original_entry": p.get("original_entry"),
            "original_qty": p.get("original_qty"),
            "dca_events": p.get("dca_events", []),
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
            "dca_count": t.get("dca_count", 0),
            "original_entry": t.get("original_entry"),
            "dca_events": t.get("dca_events", []),
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
                             "exit_price": None, "exit_ts": None, "reason": "open",
                             "qty": p.get("qty"), "dca_count": p.get("dca_count", 0),
                             "dca_trigger_price": p.get("dca_trigger_price"),
                             "original_entry": p.get("original_entry"),
                             "original_qty": p.get("original_qty"),
                             "dca_events": p.get("dca_events", []),
                             "avg_entry": p["entry_price"]}
                    break
    # для закрытой сделки достаём SL/TP/Trail из конфига (в логе их нет)
    if trade and trade.get("exit_ts") and "sl_price" not in trade:
        cfg = load_cfg()
        e = float(trade["entry_price"])
        sl_pct = float(cfg.get("sl_pct", 30))
        tp_pct = float(cfg.get("tp_pct", 15))
        trail_pct = float(cfg.get("trail_pct", 10))
        act_pct = float(cfg.get("activation_pct", 1))
        # шорт: SL выше входа, TP ниже (SL=0 → без стопа)
        trade["sl_price"] = e * (1 + sl_pct / 100) if sl_pct > 0 else None
        trade["tp_price"] = e * (1 - tp_pct / 100)
        trade["act_price"] = e * (1 - act_pct / 100)
        trade["trail_pct"] = trail_pct
        trade["min_price"] = None
        trade["activated"] = None
    # добавим строковые поля для фронтенда
    if trade:
        if trade.get("qty") is not None and trade.get("entry_price") is not None and "notional" not in trade:
            trade["notional"] = round(float(trade["qty"]) * float(trade["entry_price"]), 2)
        trade.setdefault("dca_count", 0)
        trade.setdefault("dca_events", [])
        trade.setdefault("avg_entry", trade.get("entry_price"))
        trade.setdefault("original_entry", trade.get("entry_price"))
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
    # окно: 12ч до входа, 12ч после выхода (или сейчас) — полный контекст
    # Bybit limit=1000 свечей, при 12ч+12ч=24ч=1440 мин нужно >1000 → тянем батчами
    pre_sec = 43200   # 12 часов до входа
    post_sec = 43200  # 12 часов после выхода
    start = int((entry_ts - pre_sec) * 1000)
    end = int((exit_ts + post_sec) * 1000)
    # Bybit kline: interval 1m, limit до 1000, start/end
    # Bybit отдаёт max 1000 свечей за запрос. 24ч = 1440 мин → тянем в 2 батча
    candles = []
    async with aiohttp.ClientSession() as sess:
        try:
            # Батч 1: от start до start+1000min
            mid = start + 1000 * 60 * 1000
            url1 = f"{BYBIT}/v5/market/kline?category=linear&symbol={sym}&interval=1&start={start}&end={mid}&limit=1000"
            async with sess.get(url1, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
            for k in data.get("result", {}).get("list", []):
                candles.append({"t": int(k[0]) / 1000, "o": float(k[1]), "h": float(k[2]),
                                "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
            # Батч 2: от mid до end (если end > mid)
            if end > mid:
                url2 = f"{BYBIT}/v5/market/kline?category=linear&symbol={sym}&interval=1&start={mid}&end={end}&limit=1000"
                async with sess.get(url2, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                for k in data.get("result", {}).get("list", []):
                    candles.append({"t": int(k[0]) / 1000, "o": float(k[1]), "h": float(k[2]),
                                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
            candles.sort(key=lambda x: x["t"])
        except Exception as e:
            return web.json_response({"error": f"bybit: {e}"}, status=502)


    return web.json_response({
        "symbol": sym,
        "candles": candles,
        "trade": trade,
    })


# ═══════════════════════════════════════════════════════
# LIVE API — реальные данные с Bybit через executor
# ═══════════════════════════════════════════════════════

def _bybit_signed_get(path, params=""):
    """Signed GET к Bybit (для private endpoints)."""
    import hashlib, hmac, urllib.request
    api_file = os.path.expanduser("~/.bybit_api")
    if not os.path.exists(api_file):
        api_file = "/root/.bybit_api"
    if not os.path.exists(api_file):
        return {"retCode": -1, "retMsg": "no api keys"}
    key, secret = open(api_file).read().strip().split("\n")
    ts = str(int(time.time() * 1000))
    recv = "5000"
    q = f"{ts}{key}{recv}{params}"
    sign = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.bybit.com{path}?{params}"
    req = urllib.request.Request(url, headers={
        "X-BAPI-SIGN-TYPE": "SHA256", "X-BAPI-SIGN": sign,
        "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv,
    })
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# ═══════════════════════════════════════════════════════
# MANAGEMENT PANEL API — config / bot control / orders
# ═══════════════════════════════════════════════════════

import subprocess, re

# Keys managed by the config panel
CFG_KEYS_SCALAR = {
    "min_gain_pct": float,
    "trail_pct": float,
    "sl_pct": float,
    "dca_max_count": int,
    "max_open_positions": int,
    "risk_notional_pct": float,
    "leverage": float,
    "max_daily_entries": int,
    "ranking_interval_min": int,
    "min_volume_usd": float,
}
CFG_KEYS_LIST = {"tp_chain", "dca_trigger_pct"}


def _bybit_signed_post(path, body):
    """Signed POST to Bybit (JSON body). Mirror of _bybit_signed_get."""
    import hashlib, hmac, urllib.request
    api_file = os.path.expanduser("~/.bybit_api")
    if not os.path.exists(api_file):
        api_file = "/root/.bybit_api"
    if not os.path.exists(api_file):
        return {"retCode": -1, "retMsg": "no api keys"}
    key, secret = open(api_file).read().strip().split("\n")
    ts = str(int(time.time() * 1000))
    recv = "5000"
    json_body = json.dumps(body, separators=(",", ":"))
    sign_str = f"{ts}{key}{recv}{json_body}"
    sign = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.bybit.com{path}"
    req = urllib.request.Request(url, data=json_body.encode(), headers={
        "X-BAPI-SIGN-TYPE": "SHA256", "X-BAPI-SIGN": sign,
        "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return {"retCode": e.code, "retMsg": f"HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}


def _save_cfg_partial(updates):
    """Update only changed keys in config.yaml, preserving comments/formatting.
    Line-by-line replacement for scalar keys; list keys replace the whole list inline.
    Returns (ok, msg, updated_keys)."""
    if not CFG_F.exists():
        return False, "config.yaml not found", []
    lines = CFG_F.read_text().splitlines()
    updated = []
    for key, val in updates.items():
        if key in CFG_KEYS_LIST:
            # list value — format as [v1, v2, ...]
            if isinstance(val, str):
                parts = [p.strip() for p in val.strip("[]").split(",") if p.strip()]
            else:
                parts = list(val)
            list_str = "[" + ", ".join(str(p) for p in parts) + "]"
            # find existing line with key: [...]
            found = False
            for i, ln in enumerate(lines):
                stripped = ln.lstrip()
                if stripped.startswith(f"{key}:") or stripped.startswith(f"{key} :"):
                    # preserve leading whitespace
                    indent = ln[:len(ln) - len(stripped)]
                    # keep inline comment if present after the list
                    lines[i] = f"{indent}{key}: {list_str}"
                    found = True
                    updated.append(key)
                    break
            if not found:
                lines.append(f"{key}: {list_str}")
                updated.append(key)
        elif key in CFG_KEYS_SCALAR:
            found = False
            for i, ln in enumerate(lines):
                stripped = ln.lstrip()
                if stripped.startswith(f"{key}:") or stripped.startswith(f"{key} :"):
                    indent = ln[:len(ln) - len(stripped)]
                    lines[i] = f"{indent}{key}: {val}"
                    found = True
                    updated.append(key)
                    break
            if not found:
                lines.append(f"{key}: {val}")
                updated.append(key)
        else:
            # unknown key — skip
            continue
    CFG_F.write_text("\n".join(lines) + "\n")
    return True, f"updated {len(updated)} keys: {', '.join(updated)}", updated


async def api_config_get(req):
    """GET /api/config — return current config values."""
    try:
        cfg = load_cfg()
        # also return raw list keys (load_cfg parses them as strings if not float)
        out = {}
        for k in CFG_KEYS_SCALAR:
            out[k] = cfg.get(k)
        for k in CFG_KEYS_LIST:
            v = cfg.get(k)
            # load_cfg may return a string like "[10.0, 15.0, 20.0, 25.0, 30.0]"
            if isinstance(v, str):
                out[k] = [p.strip() for p in v.strip("[]").split(",") if p.strip()]
            elif isinstance(v, list):
                out[k] = v
            else:
                out[k] = v
        return web.json_response({"config": out})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_config_save(req):
    """POST /api/config — save config.yaml parameters (partial update)."""
    try:
        body = await req.json()
    except Exception:
        try:
            body = dict(req.query)
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
    updates = {}
    for k in CFG_KEYS_SCALAR:
        if k in body and body[k] is not None and str(body[k]).strip() != "":
            try:
                updates[k] = CFG_KEYS_SCALAR[k](body[k])
            except (ValueError, TypeError):
                return web.json_response({"error": f"invalid value for {k}: {body[k]}"}, status=400)
    for k in CFG_KEYS_LIST:
        if k in body and body[k] is not None:
            updates[k] = body[k]
    if not updates:
        return web.json_response({"ok": False, "msg": "no keys to update"})
    ok, msg, updated = _save_cfg_partial(updates)
    return web.json_response({"ok": ok, "msg": msg, "updated": updated})


async def api_bot_restart(req):
    """POST /api/bot/restart — restart short-bot.service via systemctl."""
    try:
        r = subprocess.run(["systemctl", "restart", "short-bot.service"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return web.json_response({"ok": True, "msg": "short-bot.service restarted"})
        else:
            return web.json_response({"ok": False, "msg": r.stderr or r.stdout or "restart failed"})
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "msg": "restart timed out (30s)"})
    except Exception as e:
        return web.json_response({"ok": False, "msg": str(e)})


async def api_bot_status(req):
    """POST /api/bot/status — bot process status (running/stopped, PID, uptime)."""
    try:
        r = subprocess.run(["systemctl", "is-active", "short-bot.service"],
                           capture_output=True, text=True, timeout=10)
        active = r.stdout.strip() == "active"
        # get PID + uptime via systemctl show
        r2 = subprocess.run(["systemctl", "show", "short-bot.service",
                             "--property=MainPID,ActiveEnterTimestamp,ExecMainStartTimestamp"],
                            capture_output=True, text=True, timeout=10)
        info = {}
        for part in r2.stdout.strip().split():
            if "=" in part:
                k, _, v = part.partition("=")
                info[k] = v
        pid = int(info.get("MainPID", 0) or 0)
        # uptime from ActiveEnterTimestamp (e.g. "Tue 2025-01-01 12:00:00 UTC")
        uptime_sec = None
        ts_str = info.get("ExecMainStartTimestamp") or info.get("ActiveEnterTimestamp")
        if ts_str:
            try:
                # parse "Tue 2025-01-01 12:00:00 UTC" → strip weekday
                # systemctl format: "Tue 2025-01-01 12:00:00 UTC"
                parts = ts_str.split(None, 1)
                dt_str = parts[1] if len(parts) > 1 else ts_str
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S UTC")
                uptime_sec = (datetime.utcnow() - dt).total_seconds()
            except Exception:
                uptime_sec = None
        return web.json_response({
            "active": active,
            "status": "running" if active else "stopped",
            "pid": pid,
            "uptime_sec": uptime_sec,
            "uptime_str": fmt_dur(uptime_sec) if uptime_sec else None,
        })
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_position_close(req):
    """POST /api/position/close — close a position manually via executor.close_market."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    symbol = body.get("symbol", "").upper().strip()
    qty = body.get("qty")
    if not symbol:
        return web.json_response({"error": "symbol required"}, status=400)
    try:
        import executor
        if qty is None or float(qty) <= 0:
            # get position size from Bybit
            pos = executor.get_position(symbol)
            if not pos or pos["size"] == 0:
                return web.json_response({"error": f"no open position for {symbol}"})
            qty = pos["size"]
        qty = float(qty)
        # cancel all open orders first (DCA limits, TP/SL conditionals)
        executor.cancel_all(symbol)
        oid = executor.close_market(symbol, qty)
        if oid:
            return web.json_response({"ok": True, "orderId": oid, "msg": f"closed {symbol} qty={qty}"})
        else:
            return web.json_response({"ok": False, "msg": f"close_market returned None for {symbol}"})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_order_cancel(req):
    """POST /api/order/cancel — cancel an order via executor.cancel_order."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    symbol = body.get("symbol", "").upper().strip()
    order_id = body.get("order_id", "").strip()
    if not symbol or not order_id:
        return web.json_response({"error": "symbol and order_id required"}, status=400)
    try:
        import executor
        ok = executor.cancel_order(symbol, order_id)
        return web.json_response({"ok": ok, "msg": "cancelled" if ok else "cancel failed"})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_tp_move(req):
    """POST /api/tp/move — move TP for a position.
    Cancel old TP conditional order, set new via live_trader._set_tp_order."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    symbol = body.get("symbol", "").upper().strip()
    trigger_price = body.get("trigger_price") or body.get("tp_price")
    qty = body.get("qty")
    if not symbol or not trigger_price:
        return web.json_response({"error": "symbol and trigger_price required"}, status=400)
    try:
        import executor, live_trader
        trigger_price = float(trigger_price)
        # get current position for qty
        pos = executor.get_position(symbol)
        if not pos or pos["size"] == 0:
            return web.json_response({"error": f"no open position for {symbol}"})
        qty = float(qty) if qty else pos["size"]
        # cancel existing conditional orders (TP) — cancel_all will also remove DCA limits
        # We only want to cancel TP, so find TP-type orders
        orders = executor.get_open_orders(symbol)
        tp_cancelled = 0
        for o in orders:
            # TP conditional orders have triggerPrice > 0 and orderType Market
            if o.get("orderType") == "Market" and o.get("triggerPrice") and float(o.get("triggerPrice", 0)) > 0:
                if executor.cancel_order(symbol, o["orderId"]):
                    tp_cancelled += 1
        # set new TP
        oid = live_trader._set_tp_order(symbol, qty, trigger_price, order_link_id=f"TP-{symbol}-{int(time.time())}")
        if oid:
            return web.json_response({"ok": True, "orderId": oid, "tp_cancelled": tp_cancelled,
                                       "msg": f"TP moved to {trigger_price} for {symbol} (cancelled {tp_cancelled} old)"})
        else:
            return web.json_response({"ok": False, "msg": f"_set_tp_order failed for {symbol}",
                                       "tp_cancelled": tp_cancelled})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_sl_move(req):
    """POST /api/sl/move — move SL for a position via executor.set_trading_stop."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    symbol = body.get("symbol", "").upper().strip()
    stop_loss = body.get("stop_loss") or body.get("sl_price")
    take_profit = body.get("take_profit") or body.get("tp_price") or 0
    if not symbol or not stop_loss:
        return web.json_response({"error": "symbol and stop_loss required"}, status=400)
    try:
        import executor
        stop_loss = float(stop_loss)
        take_profit = float(take_profit) if take_profit else 0
        ok = executor.set_trading_stop(symbol, take_profit=take_profit, stop_loss=stop_loss)
        return web.json_response({"ok": ok, "msg": f"SL set to {stop_loss}" if ok else "set_trading_stop failed"})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_position_open(req):
    """POST /api/position/open — manually open a short."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    symbol = body.get("symbol", "").upper().strip()
    mode = body.get("mode", "qty")  # "qty" or "notional"
    qty_val = body.get("qty")
    notional_pct = body.get("notional_pct")
    leverage = body.get("leverage")
    if not symbol:
        return web.json_response({"error": "symbol required"}, status=400)
    try:
        import executor, live_trader
        # get balance
        balance = executor.get_balance()
        if not balance or balance <= 0:
            return web.json_response({"error": "cannot get balance from Bybit"})
        # get current price
        price = executor.get_ticker(symbol)
        if not price or price <= 0:
            return web.json_response({"error": f"cannot get price for {symbol}"})
        # leverage from body or config
        if not leverage:
            leverage = load_cfg().get("leverage", 3)
        leverage = float(leverage)
        # qty calculation
        if mode == "notional" and notional_pct:
            notional = balance * float(notional_pct) / 100
            qty = notional / price
        elif qty_val:
            qty = float(qty_val)
        else:
            return web.json_response({"error": "qty or notional_pct required"})
        # set leverage
        executor.set_leverage(symbol, leverage)
        # place market short
        oid = executor.place_market_short(symbol, qty, leverage=leverage)
        if not oid:
            return web.json_response({"ok": False, "msg": f"place_market_short failed for {symbol}"})
        time.sleep(1)
        # get fill
        pos = executor.get_position(symbol)
        if not pos or pos["size"] == 0:
            return web.json_response({"ok": True, "orderId": oid, "msg": "order placed but position not found yet"})
        fill_price = pos["avgPrice"]
        actual_qty = pos["size"]
        # set TP from config tp_chain[0]
        cfg = load_cfg()
        tp_chain = cfg.get("tp_chain", [20.0])
        if isinstance(tp_chain, str):
            tp_chain = [float(p.strip()) for p in tp_chain.strip("[]").split(",") if p.strip()]
        tp_pct = float(tp_chain[0]) if tp_chain else 20.0
        tp = fill_price * (1 - tp_pct / 100)
        tp_oid = live_trader._set_tp_order(symbol, actual_qty, tp,
                                            order_link_id=f"TP-{symbol}-{int(time.time())}")
        # set DCA limit orders
        dca_trigger = cfg.get("dca_trigger_pct", [10.0, 15.0, 20.0, 25.0, 30.0])
        if isinstance(dca_trigger, str):
            dca_trigger = [float(p.strip()) for p in dca_trigger.strip("[]").split(",") if p.strip()]
        dca_mult = float(cfg.get("dca_qty_multiplier", 1.0))
        dca_oids = {}
        for i, pct_lvl in enumerate(dca_trigger):
            dca_price = fill_price * (1 + float(pct_lvl) / 100)
            dca_qty = actual_qty * dca_mult
            doid = executor.place_dca_limit(symbol, dca_qty, dca_price)
            if doid:
                dca_oids[i + 1] = doid
        # set SL if sl_pct > 0
        sl_pct = float(cfg.get("sl_pct", 0))
        sl_oid = None
        if sl_pct > 0:
            sl_price = fill_price * (1 + sl_pct / 100)
            sl_oid = executor.set_stop_loss_order(symbol, actual_qty, sl_price,
                                                   order_link_id=f"SL-{symbol}-{int(time.time())}")
        return web.json_response({
            "ok": True, "orderId": oid, "tp_orderId": tp_oid,
            "fill_price": fill_price, "qty": actual_qty,
            "tp": tp, "dca_orders": dca_oids, "sl_orderId": sl_oid,
            "msg": f"opened short {symbol} qty={actual_qty} @ {fill_price}, TP={tp}, DCA={len(dca_oids)} levels"
        })
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_orders(req):
    """GET /api/orders — all open orders across symbols (Bybit realtime)."""
    try:
        dor = _bybit_signed_get("/v5/order/realtime", "category=linear&settleCoin=USDT")
        orders = []
        if dor.get("retCode") == 0:
            for o in dor["result"]["list"]:
                orders.append({
                    "orderId": o.get("orderId", ""),
                    "symbol": o.get("symbol", ""),
                    "side": o.get("side", ""),
                    "orderType": o.get("orderType", ""),
                    "price": o.get("price", ""),
                    "qty": o.get("qty", ""),
                    "filledQty": o.get("cumExecQty", "0"),
                    "status": o.get("orderStatus", ""),
                    "triggerPrice": o.get("triggerPrice", ""),
                    "reduceOnly": o.get("reduceOnly", ""),
                    "timeInForce": o.get("timeInForce", ""),
                    "createdTime": o.get("createdTime", ""),
                    "orderLinkId": o.get("orderLinkId", ""),
                })
        # sort by createdTime desc
        orders.sort(key=lambda x: int(x.get("createdTime", 0) or 0), reverse=True)
        return web.json_response({"orders": orders, "count": len(orders)})
    except Exception as e:
        return web.json_response({"error": str(e)})




async def api_live_overview(req):
    """Обзор LIVE — реальные баланс/позиции с биржи."""
    try:
        d = _bybit_signed_get("/v5/account/wallet-balance", "accountType=UNIFIED&coin=USDT")
        if d.get("retCode") != 0:
            return web.json_response({"error": d.get("retMsg", "?")})
        acct = d["result"]["list"][0]
        equity = float(acct.get("totalEquity", 0))
        balance = float(acct.get("totalWalletBalance", 0))
        coin = acct.get("coin", [{}])
        usdt = coin[0] if coin else {}
        try:
            available = float(usdt.get("availableToWithdraw", 0) or usdt.get("walletBalance", 0) or 0)
        except (ValueError, TypeError):
            available = 0.0

        # Позиции
        dp = _bybit_signed_get("/v5/position/list", "category=linear&settleCoin=USDT")
        positions = []
        if dp.get("retCode") == 0:
            for p in dp["result"]["list"]:
                if float(p["size"]) > 0:
                    positions.append({
                        "symbol": p["symbol"],
                        "size": float(p["size"]),
                        "side": p["side"],
                        "avgPrice": float(p.get("avgPrice", 0)),
                        "leverage": p.get("leverage", ""),
                        "takeProfit": p.get("takeProfit", ""),
                        "stopLoss": p.get("stopLoss", ""),
                        "trailingStop": p.get("trailingStop", ""),
                        "unrealisedPnl": float(p.get("unrealisedPnl", 0)),
                    })

        # Закрытые сделки (для WR/PnL)
        dc = _bybit_signed_get("/v5/position/closed-pnl", "category=linear&limit=100")
        closed = []
        if dc.get("retCode") == 0:
            for t in dc["result"]["list"]:
                closed.append({
                    "symbol": t["symbol"], "pnl": float(t["closedPnl"]),
                    "qty": float(t["qty"]), "ts": int(t.get("createdTime", 0)),
                })
        total_pnl = sum(c["pnl"] for c in closed)
        wins = sum(1 for c in closed if c["pnl"] > 0)
        wr = wins / len(closed) * 100 if closed else 0

        # Ордера (DCA лимитки)
        dor = _bybit_signed_get("/v5/order/realtime", "category=linear&settleCoin=USDT")
        open_orders = len(dor["result"]["list"]) if dor.get("retCode") == 0 else 0

        return web.json_response({
            "mode": "LIVE",
            "balance": round(balance, 4),
            "equity": round(equity, 4),
            "available": round(available, 4),
            "total_pnl": round(total_pnl, 4),
            "open_count": len(positions),
            "open_orders": open_orders,
            "closed_count": len(closed),
            "win_rate": round(wr, 1),
            "positions": positions,
        })
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_live_open(req):
    """Открытые позиции LIVE — детально с биржи."""
    try:
        dp = _bybit_signed_get("/v5/position/list", "category=linear&settleCoin=USDT")
        positions = []
        if dp.get("retCode") == 0:
            for p in dp["result"]["list"]:
                if float(p["size"]) > 0:
                    # Подгрузим ордера по символу для DCA
                    dor = _bybit_signed_get("/v5/order/realtime", f"category=linear&symbol={p['symbol']}")
                    dca_orders = []
                    tp_trigger = ""
                    if dor.get("retCode") == 0:
                        for o in dor["result"]["list"]:
                            # Detect TP conditional order: Buy Market with triggerPrice
                            tp_raw = o.get("triggerPrice", "")
                            if o.get("side") == "Buy" and o.get("orderType") == "Market" and tp_raw and float(tp_raw) > 0:
                                tp_trigger = tp_raw
                            dca_orders.append({
                                "orderId": o["orderId"][:12],
                                "side": o["side"],
                                "price": float(o.get("price", 0)),
                                "qty": float(o.get("qty", 0)),
                                "type": o.get("orderType", ""),
                                "status": o.get("orderStatus", ""),
                                "triggerPrice": tp_raw,
                            })
                    positions.append({
                        "symbol": p["symbol"],
                        "size": float(p["size"]),
                        "side": p["side"],
                        "avgPrice": float(p.get("avgPrice", 0)),
                        "leverage": p.get("leverage", ""),
                        "takeProfit": p.get("takeProfit", ""),
                        "stopLoss": p.get("stopLoss", ""),
                        "trailingStop": p.get("trailingStop", ""),
                        "activePrice": p.get("activePrice", ""),
                        "unrealisedPnl": float(p.get("unrealisedPnl", 0)),
                        "dca_orders": dca_orders,
                    })
        return web.json_response({"positions": positions})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_live_trades(req):
    """История закрытых сделок LIVE — с биржи (closed-pnl)."""
    try:
        dc = _bybit_signed_get("/v5/position/closed-pnl", "category=linear&limit=100")
        trades = []
        if dc.get("retCode") == 0:
            for t in dc["result"]["list"]:
                entry_ts = int(t.get("createdTime", 0))  # время закрытия
                exit_ts = int(t.get("updatedTime", t.get("createdTime", 0)))
                # Bybit closed-pnl: createdTime = время закрытия, keyed для entry нет
                # берём avgEntryPrice / avgExitPrice
                trades.append({
                    "symbol": t["symbol"],
                    "side": t.get("side", ""),
                    "qty": float(t["qty"]),
                    "pnl": float(t["closedPnl"]),
                    "entry": float(t.get("avgEntryPrice", 0)),
                    "exit": float(t.get("avgExitPrice", 0)),
                    "ts": entry_ts,
                    "exitTs": exit_ts,
                    "date": datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "leverage": t.get("leverage", ""),
                })
        trades.sort(key=lambda x: x["ts"], reverse=True)
        return web.json_response({"trades": trades})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_live_chart(req):
    """График LIVE — свечи с Bybit + реальные уровни позиции + маркеры сделки."""
    sym = req.query.get("symbol", "")
    interval = req.query.get("interval", "15")  # 1, 5, 15, 60, 240, D
    limit = int(req.query.get("limit", 300))
    # Для конкретной сделки: от и до (ms)
    from_ts = req.query.get("from", "")
    to_ts = req.query.get("to", "")
    if not sym:
        return web.json_response({"error": "no symbol"})
    try:
        # Свечи (public, без auth)
        params = f"category=linear&symbol={sym}&interval={interval}&limit={limit}"
        if from_ts and to_ts:
            params += f"&start={from_ts}&end={to_ts}"
        url = f"{BYBIT}/v5/market/kline?{params}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        candles = []
        for k in data.get("result", {}).get("list", []):
            candles.append({
                "t": int(k[0]) / 1000, "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
            })
        candles.sort(key=lambda x: x["t"])

        # Реальная позиция (для уровней SL/TP/entry)
        dp = _bybit_signed_get("/v5/position/list", f"category=linear&symbol={sym}")
        pos_info = None
        if dp.get("retCode") == 0:
            for p in dp["result"]["list"]:
                if float(p["size"]) > 0:
                    pos_info = {
                        "side": p["side"], "size": float(p["size"]),
                        "avgPrice": float(p.get("avgPrice", 0)),
                        "takeProfit": p.get("takeProfit", ""),
                        "stopLoss": p.get("stopLoss", ""),
                        "trailingStop": p.get("trailingStop", ""),
                        "activePrice": p.get("activePrice", ""),
                    }
                    break

        # DCA ордера
        dor = _bybit_signed_get("/v5/order/realtime", f"category=linear&symbol={sym}")
        dca_levels = []
        if dor.get("retCode") == 0:
            for o in dor["result"]["list"]:
                if o.get("orderType") == "Limit":
                    dca_levels.append(float(o.get("price", 0)))

        # Маркеры сделки (из query: entry, exit, entryTs, exitTs, pnl)
        trade_markers = None
        entry_price = req.query.get("entry", "")
        exit_price = req.query.get("exit", "")
        entry_ts = req.query.get("entryTs", "")
        exit_ts_q = req.query.get("exitTs", "")
        pnl_val = req.query.get("pnl", "")
        if entry_price or exit_price:
            trade_markers = {
                "entry": float(entry_price) if entry_price else None,
                "exit": float(exit_price) if exit_price else None,
                "entryTs": int(entry_ts) / 1000 if entry_ts else None,
                "exitTs": int(exit_ts_q) / 1000 if exit_ts_q else None,
                "pnl": float(pnl_val) if pnl_val else None,
            }

        return web.json_response({
            "symbol": sym,
            "candles": candles,
            "position": pos_info,
            "dca_levels": dca_levels,
            "trade_markers": trade_markers,
        })
    except Exception as e:
        return web.json_response({"error": str(e)})


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
#chart-box { height: 520px; background: #0b0e11; border-radius: 8px; position: relative; }
.chart-legend { position: absolute; top: 8px; left: 8px; z-index: 10; background: rgba(11,14,17,0.85); border: 1px solid #2b3139; border-radius: 6px; padding: 8px 12px; font-size: 12px; font-family: monospace; pointer-events: none; max-width: 300px; }
.chart-legend .lg-row { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
.chart-legend .lg-dot { width: 12px; height: 2px; border-radius: 1px; flex-shrink: 0; }
.chart-legend .lg-label { color: #d1d4dc; min-width: 36px; font-weight: 600; }
.chart-legend .lg-price { color: #848e9c; margin-left: auto; }
.chart-legend .lg-pct { color: #f0b90b; font-size: 11px; margin-left: 4px; }
.ruler-btn { position: absolute; top: 8px; right: 8px; z-index: 12; padding: 5px 10px; background: #1e2329; color: #848e9c; border: 1px solid #2b3139; border-radius: 5px; cursor: pointer; font-size: 12px; font-family: monospace; user-select: none; }
.ruler-btn:hover { background: #2b3139; color: #d1d4dc; }
.ruler-btn.active { background: #f0b90b; color: #0b0e11; border-color: #f0b90b; }
.chart-info { position: absolute; top: 8px; left: 50%; transform: translateX(-50%); z-index: 10; background: rgba(11,14,17,0.85); border: 1px solid #2b3139; border-radius: 6px; padding: 6px 10px; font-size: 12px; color: #d1d4dc; font-family: monospace; pointer-events: none; }
.chart-tabs { display: flex; gap: 4px; margin-bottom: 8px; }
.ctab { padding: 6px 14px; background: #1e2329; border-radius: 6px 6px 0 0; cursor: pointer; color: #848e9c; font-size: 13px; }
.ctab.active { background: #f0b90b; color: #0b0e11; font-weight: 600; }
.ctab-panel { min-height: 500px; }
@media (max-width: 600px) { .stat .val { font-size: 16px; } #tv-chart { height: 380px; } #chart-box { height: 380px; } .eq-chart { height: 240px; } }
</style></head><body><div class="container">
<header>
  <h1>Short Bot #2 <small id="upd"></small> <span style="font-size:11px;color:#848e9c">DEMO</span></h1>
  <div style="display:flex;gap:6px">
    <a href="/live" style="padding:6px 14px;background:#f6465d;color:#fff;border-radius:6px;font-size:13px;text-decoration:none;font-weight:600">→ LIVE</a>
  </div>
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
    <div class="lbl muted" style="margin-bottom:8px">СТРАТЕГИЯ</div>
    <div id="strategy-desc" style="font-size:13px;line-height:1.6"></div>
  </div>
  <div class="card">
    <div class="lbl muted" style="margin-bottom:6px">КОНФИГ</div>
    <div id="cfg" class="grid"></div>
  </div>
</div>

<div id="view-open" class="hidden">
  <div class="card">
    <table id="open-table"><thead>
      <tr><th>Symbol</th><th>Вход</th><th>Цена →</th><th>Размер $</th><th>SL</th><th>TP</th><th>DCA</th><th>Trail</th><th>Hold</th><th>Comm</th><th>Фандинг</th><th>uPnL</th><th>uNet</th><th></th></tr>
    </thead><tbody></tbody></table>
    <div class="muted" style="font-size:11px;margin-top:6px">uPnL — нереализованный PnL по текущей цене Bybit (real-time). Клик по строке — график.</div>
  </div>
</div>

<div id="view-trades" class="hidden">
  <div class="card">
    <table id="trades-table"><thead>
      <tr><th>Symbol</th><th>Side</th><th>Размер $</th><th>Вход</th><th>Выход</th><th>Move%</th><th>Hold</th><th>PnL $</th><th>Comm</th><th>Фандинг</th><th>Net</th><th>Reason</th><th>Дата</th></tr>
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
      <div id="chart-box"><div id="chart-legend" class="chart-legend"></div><div id="chart-info" class="chart-info"></div><div id="ruler-btn" class="ruler-btn" title="Shift+drag или кликни кнопку">📏 Линейка</div></div>
    </div>
    <div id="ctab-tv" class="ctab-panel hidden">
      <div id="tv-chart"></div>
    </div>
  </div>
</div>

</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
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
  const d = await api('api/overview');
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
  renderStrategy(cfg);
  // equity curve metrics
  const m = d.eq_metrics;
  if (m) {
    $('#eq-period').textContent = `· ${m.period_days} дн · ${m.points} точек`;
    const pfTxt = m.profit_factor!=null ? m.profit_factor.toFixed(2) : '—';
    const retCls = m.return_pct>=0 ? 'pos' : 'neg';
    $('#eq-metrics').innerHTML = [
      ['Return', `<span class="${retCls}">${m.return_pct>=0?'+':''}${m.return_pct}%</span>`],
      ['Max DD', `<span class="neg">-${m.max_dd_pct}%</span>`],
      ['Profit Factor', pfTxt],
      ['Start', '$'+fmt(m.initial)],
      ['Current', '$'+fmt(m.current)],
    ].map(([k,v])=>`<div class="stat"><span class="lbl">${k}</span><span class="val">${v}</span></div>`).join('');
  }
  // equity curve
  drawEq(d.equity_curve);
}
function renderStrategy(c) {
  const dcaOn = c.dca_enabled !== false;
  const dcaMax = c.dca_max_count || 0;
  const dcaTrigRaw = c.dca_trigger_pct || 0;
  const dcaTrig = Array.isArray(dcaTrigRaw) ? dcaTrigRaw.join('/') : dcaTrigRaw;
  const dcaMult = c.dca_qty_multiplier || 1;
  const maxQty = (1 + dcaMax * dcaMult).toFixed(0);
  const html = `
    <div style="margin-bottom:10px">
      <b style="color:#f0b90b">Сценарий Б:</b> Монета впервые становится #1 по росту за день →
      откат на позицию #2 → <b>шорт #2</b>. Ловим откат после пампа.
    </div>
    <div style="margin-bottom:10px">
      <b style="color:#f0b90b">Логика входа:</b><br>
      • Ранжирование каждые ${c.ranking_interval_min||15} мин по росту за день<br>
      • Монета была #1 → стала #2 → шортим #2<br>
      • Фильтр: объём > $${(c.min_volume_usd||0).toLocaleString()}, BTC/ETH/SOL исключены<br>
      • Бан монеты до конца дня после входа (не входить повторно)<br>
      • Размер позиции: ${c.risk_pct||5}% депозита, макс ${c.max_open_positions||3} одновременно
    </div>
    <div style="margin-bottom:10px">
      <b style="color:#f0b90b">Выход:</b><br>
      • <b>TP</b> ${c.tp_pct||5}% — фиксация при падении на 5%<br>
      • <b>SL</b> ${c.sl_pct>0 ? (c.sl_pct+'% — стоп при росте') : '<span style="color:#f6465d">ОТКЛЮЧЕН</span> — ручной контроль'}<br>
      • <b>Trail</b> ${c.trail_pct||3}% отступ, активация при падении ${c.activation_pct||1}%
    </div>
    <div style="margin-bottom:10px">
      <b style="color:#f0b90b">DCA усреднение:</b> ${dcaOn ? '<span style="color:#0ecb81">ВКЛ</span>' : '<span style="color:#f6465d">ВЫКЛ</span>'}<br>
      • Рост против шорта на ${dcaTrig}% → докупаем (повышаем avg entry)<br>
      • Макс докупов: ${dcaMax} → позиция до ${maxQty}× от изначальной<br>
      • Каждый DCA: ×${dcaMult} от оригинального qty<br>
      • После DCA: SL/TP пересчитываются от новой средней<br>
      • TP = avg - 5% → нужен откат 5% от повышенной средней
    </div>
    <div style="margin-bottom:10px">
      <b style="color:#f0b90b">Исполнение:</b> Market ордера (paper trading)<br>
      • Комиссия: ${c.commission_taker_pct||0.055}% taker RT (entry + DCA + close)<br>
      • Slippage: ${c.slippage_pct||0.02}% на вход и выход<br>
      • Фандинг: реальный Bybit, интервал per-symbol (1ч/2ч/4ч/8ч)
    </div>
    <div>
      <b style="color:#f0b90b">Риск-менеджмент:</b><br>
      • Заявленный risk: ${c.risk_pct||5}% на сделку = $${((c.risk_pct||5)*(c.initial_balance||1000)/100).toFixed(2)}<br>
      ${c.sl_pct > 0
        ? `• Notional позиции: $${((c.risk_pct||5)*(c.initial_balance||1000)/100/((c.sl_pct||30)/100)).toFixed(0)}<br>
         → после DCA×${dcaMax}: $${((c.risk_pct||5)*(c.initial_balance||1000)/100/((c.sl_pct||30)/100)*(1+dcaMax*dcaMult)).toFixed(0)}<br>
        • <b style="color:#f6465d">Убыток на SL после DCA: ~$${((c.risk_pct||5)*(c.initial_balance||1000)/100*(1+dcaMax*dcaMult)).toFixed(0)}
        = ~${((c.risk_pct||5)*(1+dcaMax*dcaMult)).toFixed(0)}% депозита</b><br>
        • Два SL подряд = ~${((c.risk_pct||5)*(1+dcaMax*dcaMult)*2).toFixed(0)}% депозита`
        : `• <b>SL отключён</b> — notional = ${c.risk_notional_pct||20}% depо = $${((c.risk_notional_pct||20)*(c.initial_balance||1000)/100).toFixed(0)}<br>
        → после DCA×${dcaMax}: $${((c.risk_notional_pct||20)*(c.initial_balance||1000)/100*(1+dcaMax*dcaMult)).toFixed(0)}<br>
        • <b style="color:#f6465d">Без SL: риск не ограничен, контроль — TP + trail + ручные закрытия</b>`}
    </div>`;
  const el = $('#strategy-desc');
  if (el) el.innerHTML = html;
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
  const d = await api('api/open');
  const tb = $('#open-table tbody'); tb.innerHTML = '';
  if (!d.open.length) { tb.innerHTML = '<tr><td colspan="14" class="muted">нет открытых позиций</td></tr>'; return; }
  for (const p of d.open) {
    const tr = document.createElement('tr');
    tr.onclick = () => openDetail(p.id);
    const dcaBadge = p.dca_count > 0 ? `<span class="badge tp">DCA×${p.dca_count}</span>` : `<span class="muted">0/${p.dca_max_count||0}</span>`;
    const dcaTrig = p.dca_trigger_price ? `<div class="muted" style="font-size:11px">trig ${fmt(p.dca_trigger_price,4)}</div>` : '';
    tr.innerHTML = `<td><b>${p.symbol}</b> <span class="badge short">${p.side}</span></td>
      <td>${p.entry_str}</td>
      <td>${fmt(p.entry_price, 6)}${p.last_price?` → <span class="muted">${fmt(p.last_price,6)}</span>`:''}</td>
      <td><b>$${fmt(p.notional,0)}</b> <span class="muted">${fmt(p.qty,2)}</span></td>
      <td class="neg">${fmt(p.sl_price,6)}${p.dist_to_sl!=null?`<div class="muted" style="font-size:10px">+${pct(p.dist_to_sl)}</div>`:''} </td>
      <td class="pos">${fmt(p.tp_price,6)}${p.dist_to_tp!=null?`<div class="muted" style="font-size:10px">${pct(p.dist_to_tp)}</div>`:''} </td>
      <td>${dcaBadge}${dcaTrig}${p.dist_to_dca!=null?`<div class="muted" style="font-size:10px">→ +${pct(p.dist_to_dca)}</div>`:''} </td>
      <td>${p.activated?'✅':'⏳'} ${p.trail_pct}% ${p.min_price?fmt(p.min_price,6):''}</td>
      <td>${p.hold_str}</td>
      <td class="muted">$${fmt(p.commission_paid)}</td>
      <td class="${pnlCls(p.funding_paid||0)}">${p.funding_paid>=0?'+':''}$${fmt(p.funding_paid)}</td>
      <td class="${pnlCls(p.u_pnl||0)}">${p.u_pnl!=null?(p.u_pnl>=0?'+':'')+'$'+fmt(p.u_pnl)+' <span class="muted">('+pct(p.u_pnl_pct)+')</span>':'—'}</td>
      <td class="${pnlCls(p.u_net_pnl||0)}"><b>${p.u_net_pnl!=null?(p.u_net_pnl>=0?'+':'')+'$'+fmt(p.u_net_pnl):'—'}</b></td>
      <td><span class="badge open">OPEN</span></td>`;
    tb.appendChild(tr);
  }
}

async function loadTrades() {
  const d = await api('api/trades');
  const tb = $('#trades-table tbody'); tb.innerHTML = '';
  if (!d.trades.length) { tb.innerHTML = '<tr><td colspan="13" class="muted">нет закрытых сделок</td></tr>'; return; }
  for (const t of d.trades) {
    const tr = document.createElement('tr');
    tr.onclick = () => openDetail(t.id);
    const dcaBadge = t.dca_count > 0 ? ` <span class="badge tp" style="font-size:9px">DCA×${t.dca_count}</span>` : '';
    tr.innerHTML = `<td><b>${t.symbol}</b>${dcaBadge}</td>
      <td><span class="badge short">${t.side}</span></td>
      <td><b>$${fmt(t.notional,0)}</b></td>
      <td>${fmt(t.entry_price,6)}</td>
      <td>${fmt(t.exit_price,6)}</td>
      <td>${pct(t.move_pct)}</td>
      <td>${t.hold_str}</td>
      <td class="${pnlCls(t.pnl)}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</td>
      <td class="muted">$${fmt(t.commission)}</td>
      <td class="${pnlCls(t.funding)}">${t.funding>=0?'+':''}$${fmt(t.funding)}</td>
      <td class="${pnlCls(t.net_pnl)}"><b>${t.net_pnl>=0?'+':''}$${fmt(t.net_pnl)}</b></td>
      <td>${badge(t.reason)}</td>
      <td class="muted">${t.date||''}</td>`;
    tb.appendChild(tr);
  }
}

async function openDetail(id) {
  showView('detail');
  const d = await api('api/chart?id=' + encodeURIComponent(id));
  if (d.error) { $('#d-title').textContent = 'Ошибка: ' + d.error; return; }
  const t = d.trade, sym = d.symbol;
  $('#d-title').innerHTML = `${sym} <span class="badge short">${t.side}</span> ${badge(t.reason)}`;
  window._currentTrade = t;
  drawChart(d.candles, t);
  // info
  const move = t.exit_price ? pct(((t.exit_price - t.entry_price)/t.entry_price)*100) : '—';
  const net = t.net_pnl!=null ? `<span class="${pnlCls(t.net_pnl)}">${t.net_pnl>=0?'+':''}$${fmt(t.net_pnl)}</span>` : '—';
  const dcaInfo = t.dca_events && t.dca_events.length
    ? '<div style="margin-top:6px"><b style="color:#9c6ade">DCA усреднения:</b> ' +
      t.dca_events.map(d => 'DCA#'+d.n+' @'+fmt(d.fill,4)+' avg '+fmt(d.avg_before,4)+'→'+fmt(d.avg_after,4)+' qty='+fmt(d.qty_total,2)).join(' · ') +
      '</div>'
    : '';
  $('#d-info').innerHTML = [
    ['Вход', fmt(t.entry_price,6)], ['Выход', t.exit_price?fmt(t.exit_price,6):'—'],
    ['SL', fmt(t.sl_price,6)], ['TP', fmt(t.tp_price,6)],
    ['Trail min', t.min_price?fmt(t.min_price,6):'—'], ['Activation', t.activated?'✅ '+fmt(t.act_price,6):'⏳'],
    ['Move', move], ['Net PnL', net],
    ['Время входа', t.entry_str||fmt_ts(t.entry_ts)], ['Время выхода', t.exit_str||'—'],
    ['Hold', t.hold_str||'—'], ['Комиссия', t.commission!=null?'$'+fmt(t.commission):'—'],
    ['Фандинг', t.funding!=null?'$'+fmt(t.funding):'—'], ['Размер позиции', (t.notional!=null?'$'+fmt(t.notional,0):'—') + (t.qty!=null?' · '+fmt(t.qty,4)+' ед':'')],
  ].map(([k,v])=>`<div class="stat"><span class="lbl">${k}</span><span class="val" style="font-size:14px">${v}</span></div>`).join('') + dcaInfo;
}

function fmt_ts(ts){ if(!ts) return '—'; const d=new Date(ts*1000); return d.toISOString().slice(0,16).replace('T',' ')+' UTC'; }

function drawChart(candles, t) {
  drawDealChart(candles, t);
  // TV widget создаётся только при переключении на вкладку "Анализ"
}

// ── Measure Tool Plugin (линейка как на TradingView) ──
class MeasureToolPlugin {
  constructor() {
    this._chart = null; this._series = null;
    this._paneViews = [new MeasureToolPaneView(this)];
    this.startPoint = null; this.endPoint = null; this.isActive = false;
  }
  attached({ chart, series }) { this._chart = chart; this._series = series; }
  detached() { this._chart = null; this._series = null; }
  updateAllViews() { this._paneViews.forEach(v => v.update()); }
  paneViews() { return this._paneViews; }
  setData(start, end) {
    this.startPoint = start; this.endPoint = end;
    this.isActive = !!(start && end);
    // v4: requestUpdate для перерисовки примитивов
    if (this._chart) { try { this._chart.requestUpdate(); } catch(e) { try { this._series.applyOptions({}); } catch(e2) {} } }
  }
}

class MeasureToolPaneView {
  constructor(source) { this._source = source; this._renderer = new MeasureToolRenderer(source); }
  update() {}
  zOrder() { return 'top'; }
  renderer() { return this._source.isActive ? this._renderer : null; }
}

class MeasureToolRenderer {
  constructor(source) { this._source = source; }
  draw(ctx) {
    const { startPoint, endPoint, _series, _chart } = this._source;
    if (!startPoint || !endPoint) return;
    const ts = _chart.timeScale();
    // timeToCoordinate returns null if time not exactly in data
    // Use logicalToCoordinate: find nearest candle index via binary search
    const data = _series.data();
    const findLogical = (t) => {
      let lo = 0, hi = data.length - 1, best = 0, bestDiff = Infinity;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const diff = Math.abs(data[mid].time - t);
        if (diff < bestDiff) { bestDiff = diff; best = mid; }
        if (data[mid].time < t) lo = mid + 1;
        else hi = mid - 1;
      }
      return best;
    };
    const x1 = ts.logicalToCoordinate(findLogical(startPoint.time));
    const x2 = ts.logicalToCoordinate(findLogical(endPoint.time));
    const y1 = _series.priceToCoordinate(startPoint.price);
    const y2 = _series.priceToCoordinate(endPoint.price);
    if (x1 === null || x2 === null || y1 === null || y2 === null) return;

    ctx.save();
    // Selection rectangle
    ctx.fillStyle = 'rgba(240,185,11,0.12)';
    ctx.strokeStyle = '#f0b90b';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    const rx = Math.min(x1, x2), ry = Math.min(y1, y2);
    const rw = Math.abs(x2 - x1), rh = Math.abs(y2 - y1);
    ctx.fillRect(rx, ry, rw, rh);
    ctx.strokeRect(rx, ry, rw, rh);
    ctx.setLineDash([]);

    // Calculate stats
    const priceDiff = endPoint.price - startPoint.price;
    const pctDiff = (priceDiff / startPoint.price) * 100;
    // Bars count via logical range
    let bars = Math.abs(findLogical(endPoint.time) - findLogical(startPoint.time));
    // Time diff
    let timeStr = '';
    if (startPoint.time && endPoint.time) {
      const diffSec = Math.abs(endPoint.time - startPoint.time);
      if (diffSec < 60) timeStr = diffSec + 'с';
      else if (diffSec < 3600) timeStr = Math.floor(diffSec/60) + 'м ' + (diffSec%60) + 'с';
      else if (diffSec < 86400) timeStr = Math.floor(diffSec/3600) + 'ч ' + Math.floor((diffSec%3600)/60) + 'м';
      else timeStr = Math.floor(diffSec/86400) + 'д ' + Math.floor((diffSec%86400)/3600) + 'ч';
    }

    const sign = priceDiff > 0 ? '+' : '';
    const lines = [
      sign + priceDiff.toFixed(6) + ' (' + (pctDiff > 0 ? '+' : '') + pctDiff.toFixed(2) + '%)',
      bars + ' баров · ' + timeStr
    ];

    // Label box
    const textX = (x1 + x2) / 2;
    const textY = Math.max(y1, y2) + 8;
    const rectW = 150, rectH = 42;
    ctx.fillStyle = '#1e222d';
    ctx.strokeStyle = '#f0b90b';
    ctx.lineWidth = 1;
    ctx.beginPath();
    let bx = textX - rectW/2, by = textY;
    // Keep box in view
    if (bx < 2) bx = 2;
    if (bx + rectW > ts.width() - 60) bx = ts.width() - 60 - rectW;
    if (by + rectH > _chart.priceScale('right').height() - 2) by = Math.min(y1,y2) - rectH - 8;
    ctx.roundRect(bx, by, rectW, rectH, 5);
    ctx.fill(); ctx.stroke();

    // Text
    ctx.fillStyle = '#f0b90b';
    ctx.font = 'bold 12px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(lines[0], bx + rectW/2, by + 15);
    ctx.fillStyle = '#848e9c';
    ctx.font = '11px monospace';
    ctx.fillText(lines[1], bx + rectW/2, by + 30);
    ctx.restore();
  }
}

// ── Вкладка "Сделка": lightweight-charts с маркерами entry/exit и линиями SL/TP ──
function drawDealChart(candles, t) {
  if (chart) { chart.remove(); chart=null; series=null; }
  const box = $('#chart-box');
  const legend = box.querySelector('#chart-legend');
  const info = box.querySelector('#chart-info');
  const rbtn = box.querySelector('#ruler-btn');
  box.innerHTML = '';
  if (legend) box.appendChild(legend);
  if (info) box.appendChild(info);
  if (rbtn) box.appendChild(rbtn);

  chart = LightweightCharts.createChart(box, {
    height: 520, autoSize: true,
    layout: {background:{color:'#0b0e11'},textColor:'#d1d4dc',fontSize:11,fontFamily:'monospace'},
    grid: {vertLines:{color:'rgba(30,35,41,0.5)'},horzLines:{color:'rgba(30,35,41,0.5)'}},
    rightPriceScale: {visible:true,borderColor:'#1e2329',scaleMargins:{top:0.08,bottom:0.08}},
    timeScale: {visible:true,borderColor:'#1e2329',timeVisible:true,secondsVisible:false},
    crosshair: {
      mode: 0,
      vertLine: {color:'#848e9c',width:1,style:3,labelBackgroundColor:'#363a45'},
      horzLine: {color:'#848e9c',width:1,style:3,labelBackgroundColor:'#363a45'}
    },
  });

  // Price precision from data
  const minPrice = Math.min(...candles.map(c=>c.l));
  let precision = 2, minMove = 0.01;
  if (minPrice < 0.001) { precision = 6; minMove = 0.000001; }
  else if (minPrice < 1) { precision = 5; minMove = 0.00001; }
  else if (minPrice < 100) { precision = 4; minMove = 0.0001; }
  else { precision = 2; minMove = 0.01; }

  series = chart.addCandlestickSeries({
    upColor:'#0ecb81',downColor:'#f6465d',borderUpColor:'#0ecb81',borderDownColor:'#f6465d',
    wickUpColor:'#0ecb81',wickDownColor:'#f6465d',
    priceFormat:{type:'price',precision:precision,minMove:minMove},
  });
  series.setData(candles.map(c=>({time:Math.floor(c.t),open:c.o,high:c.h,low:c.l,close:c.c})));

  const e = t.entry_price;
  const pctV = (p) => p && e ? ((p/e - 1) * 100).toFixed(1) + '%' : '';
  const fmtP = (p) => p != null ? fmt(p, precision) : '—';

  // All price levels — ordered top (highest) to bottom (lowest)
  const levels = [
    {key:'SL', price: t.sl_price, color: '#f6465d', dashed: true, axisLabel: true, title: 'SL'},
    {key:'DCA', price: t.dca_trigger_price, color: '#9c6ade', dashed: true, axisLabel: false, title: 'D'},
    {key:'ORIG', price: (t.original_entry && Math.abs(t.original_entry - t.entry_price) > 0.000001) ? t.original_entry : null, color: '#848e9c', dashed: true, axisLabel: false, title: 'O'},
    {key:'ENTRY', price: t.entry_price, color: '#f0b90b', dashed: false, axisLabel: true, title: 'E', lw: 2},
    {key:'EXIT', price: t.exit_price, color: '#4a9eff', dashed: false, axisLabel: false, title: 'X'},
    {key:'TP', price: t.tp_price, color: '#0ecb81', dashed: true, axisLabel: true, title: 'TP'},
    {key:'MIN', price: t.min_price, color: '#5e6b85', dashed: true, axisLabel: false, title: 'M'},
  ];

  // Price lines — short titles, axisLabel only for ENTRY/SL/TP
  for (const L of levels) {
    if (L.price == null) continue;
    series.createPriceLine({
      price: L.price, color: L.color,
      lineWidth: L.lw || 1, lineStyle: L.dashed ? 2 : 0,
      axisLabelVisible: L.axisLabel, title: L.title
    });
  }

  // Legend overlay — full info with prices + percentages
  let legHtml = '';
  for (const L of levels) {
    if (L.price == null) continue;
    const p = pctV(L.price);
    const dotStyle = L.dashed ? 'border-top: 2px dashed ' + L.color + '; background: transparent;' : 'background: ' + L.color + ';';
    legHtml += '<div class="lg-row"><span class="lg-dot" style="' + dotStyle + '"></span><span class="lg-label" style="color:' + L.color + '">' + L.key + '</span><span class="lg-price">' + fmtP(L.price) + '</span><span class="lg-pct">' + p + '</span></div>';
  }
  if (legend) legend.innerHTML = legHtml;

  // Info overlay — symbol + hold + pnl
  const reasonStr = t.reason ? t.reason.toUpperCase() : 'OPEN';
  const netStr = t.net_pnl != null ? (t.net_pnl >= 0 ? '+' : '') + '$' + fmt(t.net_pnl) : '—';
  const netColor = t.net_pnl != null ? (t.net_pnl >= 0 ? '#0ecb81' : '#f6465d') : '#848e9c';
  if (info) info.innerHTML = '<b style="color:#f0b90b">' + t.symbol + '</b> · ' + (t.hold_str||'—') + ' · <span style="color:' + netColor + '">' + netStr + '</span> · ' + reasonStr;

  // Markers entry/exit/DCA
  const entryT = Math.floor(t.entry_ts);
  const exitT = t.exit_ts ? Math.floor(t.exit_ts) : Math.floor(Date.now()/1000);
  const markers = [
    {time: entryT, position: 'aboveBar', color: '#f0b90b', shape: 'arrowDown', text: 'ENTRY'},
  ];
  if (t.dca_events && t.dca_events.length) {
    for (const d of t.dca_events) {
      markers.push({time: Math.floor(d.ts), position: 'aboveBar', color: '#9c6ade', shape: 'circle', text: 'DCA#' + d.n});
    }
  }
  if (t.exit_ts) markers.push({time: exitT, position: 'belowBar', color: '#4a9eff', shape: 'arrowUp', text: t.reason?.toUpperCase()||'EXIT'});
  try { series.setMarkers(markers); } catch(e) {}

  // Crosshair tooltip — OHLC on hover
  chart.subscribeCrosshairMove(param => {
    if (!param.time || !param.seriesData) return;
    const d = param.seriesData.get(series);
    if (d && info) {
      info.innerHTML = '<b style="color:#f0b90b">' + t.symbol + '</b> O:' + fmt(d.open,precision) + ' H:' + fmt(d.high,precision) + ' L:' + fmt(d.low,precision) + ' C:' + fmt(d.close,precision);
    }
  });

  chart.timeScale().fitContent();

  // ── Линейка: HTML overlay, anchored to time/price ──
  if (window._rulerCleanup) window._rulerCleanup();

  // Store start/end as {time, price} so ruler stays anchored when chart scrolls/zooms
  let rulerStage = 0, rulerStart = null, rulerEnd = null;
  let rulerOverlay = box.querySelector('#ruler-overlay');
  if (!rulerOverlay) {
    rulerOverlay = document.createElement('div');
    rulerOverlay.id = 'ruler-overlay';
    rulerOverlay.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;z-index:15;pointer-events:none;display:none';
    box.appendChild(rulerOverlay);
  }
  const btn = box.querySelector('#ruler-btn');

  const resetRuler = () => {
    rulerStage = 0; rulerStart = null; rulerEnd = null;
    rulerOverlay.style.display = 'none';
    rulerOverlay.innerHTML = '';
    if (btn) btn.classList.remove('active');
  };

  if (btn) {
    btn.onclick = (e) => {
      e.stopPropagation();
      if (btn.classList.contains('active')) { resetRuler(); }
      else { btn.classList.add('active'); }
    };
  }
  const isActive = () => btn.classList.contains('active');

  // Convert pixel → {time, price}
  const pxToTP = (x, y) => {
    const ts = chart.timeScale();
    const time = ts.coordinateToTime(x);
    const price = series.coordinateToPrice(y);
    return (time != null && price != null) ? { time, price } : null;
  };

  // Convert {time, price} → pixel
  const tpToPx = (tp) => {
    if (!tp) return null;
    const ts = chart.timeScale();
    const x = ts.logicalToCoordinate(findLogical(tp.time));
    const y = series.priceToCoordinate(tp.price);
    return (x != null && y != null) ? { x, y } : null;
  };

  // Binary search nearest candle index
  const findLogical = (t) => {
    const data = series.data();
    let lo = 0, hi = data.length - 1, best = 0, bd = Infinity;
    while (lo <= hi) { const m = (lo+hi)>>1; const d = Math.abs(data[m].time - t); if (d < bd) { bd = d; best = m; } if (data[m].time < t) lo = m+1; else hi = m-1; }
    return best;
  };

  // Render ruler from rulerStart/rulerEnd (anchored to time/price)
  const renderRuler = () => {
    if (!rulerStart) return;
    const end = rulerEnd || rulerStart;
    const p1 = tpToPx(rulerStart), p2 = tpToPx(end);
    if (!p1 || !p2) { rulerOverlay.innerHTML = ''; return; }
    const x1 = Math.min(p1.x, p2.x), y1 = Math.min(p1.y, p2.y);
    const w = Math.abs(p2.x - p1.x), h = Math.abs(p2.y - p1.y);
    // Label
    const priceDiff = end.price - rulerStart.price;
    const pct = (priceDiff / rulerStart.price) * 100;
    const data = series.data();
    const bars = Math.abs(findLogical(end.time) - findLogical(rulerStart.time));
    const dSec = Math.abs(end.time - rulerStart.time);
    const tStr = dSec<60?dSec+'с':dSec<3600?Math.floor(dSec/60)+'м':dSec<86400?Math.floor(dSec/3600)+'ч '+Math.floor((dSec%3600)/60)+'м':Math.floor(dSec/86400)+'д';
    const labelText = (priceDiff>=0?'+':'')+priceDiff.toFixed(6)+' ('+(pct>=0?'+':'')+pct.toFixed(2)+'%) · '+bars+' баров · '+tStr;
    rulerOverlay.innerHTML = '<div style="position:absolute;left:'+x1+'px;top:'+y1+'px;width:'+w+'px;height:'+h+'px;border:1px dashed #f0b90b;background:rgba(240,185,11,0.12)"></div><div style="position:absolute;left:'+(x1+w/2-90)+'px;top:'+(y1+h+4)+'px;background:#1e222d;border:1px solid #f0b90b;border-radius:5px;padding:4px 10px;color:#f0b90b;font:bold 11px monospace;white-space:nowrap">'+labelText+'</div>';
  };

  // click — start / end
  const onClick = (e) => {
    if (!isActive() && !e.shiftKey) { resetRuler(); return; }
    e.stopPropagation();
    const rect = box.getBoundingClientRect();
    const tp = pxToTP(e.clientX - rect.left, e.clientY - rect.top);
    if (!tp) return;
    if (rulerStage === 0 || rulerStage === 2) {
      if (rulerStage === 2) { rulerOverlay.innerHTML = ''; }
      rulerStage = 1; rulerStart = tp; rulerEnd = null;
      rulerOverlay.style.display = 'block';
      renderRuler();
    } else if (rulerStage === 1) {
      rulerEnd = tp; rulerStage = 2;
      renderRuler();
    }
  };

  // mousemove — preview (stage 1)
  const onMove = (e) => {
    if (!isActive() && !e.shiftKey) return;
    if (rulerStage !== 1) return;
    const rect = box.getBoundingClientRect();
    const tp = pxToTP(e.clientX - rect.left, e.clientY - rect.top);
    if (!tp) return;
    rulerEnd = tp;
    renderRuler();
  };

  const onKey = (e) => { if (e.key === 'Escape') resetRuler(); };

  box.addEventListener('click', onClick);
  box.addEventListener('mousemove', onMove);
  window.addEventListener('keydown', onKey);

  // Re-render on chart scroll/zoom — subscribeCrosshairMove fires on pan
  const onXH = () => { if (rulerStage >= 1) renderRuler(); };
  chart.subscribeCrosshairMove(onXH);
  // Also on visible range change
  const onScale = () => { if (rulerStage >= 1) renderRuler(); };
  chart.timeScale().subscribeVisibleLogicalRangeChange(onScale);

  window._rulerCleanup = () => {
    box.removeEventListener('click', onClick);
    box.removeEventListener('mousemove', onMove);
    window.removeEventListener('keydown', onKey);
    try { chart.timeScale().unsubscribeVisibleLogicalRangeChange(onScale); } catch(e) {}
  };
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
  // при переключении на lightweight-charts — убрать TV iframe (фикс наложения графиков)
  if (c.dataset.ctab === 'deal') {
    const tvBox = $('#tv-chart');
    if (tvBox) tvBox.innerHTML = '';
    if (chart) {
      setTimeout(() => { try { chart.applyOptions({width: $('#chart-box').clientWidth, height: 500}); chart.timeScale().fitContent(); } catch(e){} }, 50);
    }
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
    """Корень — редирект на Live (демо отключено, данные устарели)."""
    return web.HTTPFound("/live")
    return web.Response(text=HTML, content_type="text/html")


async def tv_page(request):
    """TradingView графики сделок"""
    p = Path(__file__).parent / "tv_charts.html"
    if not p.exists():
        return web.Response(text="tv_charts.html not found", status=404)
    return web.FileResponse(p, headers={"Content-Type": "text/html; charset=utf-8"})

async def charts_page(request):
    """Отдать интерактивные графики сделок"""
    chart_path = Path(__file__).parent / "charts.html"
    if not chart_path.exists():
        return web.Response(text="charts.html not found", status=404)
    return web.FileResponse(chart_path, headers={"Content-Type": "text/html; charset=utf-8"})

# ── Auth по токену в query string (чтобы fetch работал) ──
import hashlib, base64
DASH_TOKEN = os.environ.get("DASH_TOKEN", "piktor2026")

@web.middleware
async def auth_middleware(req, handler):
    # Токен в query: ?token=...
    token = req.query.get("token", "")
    if token == DASH_TOKEN:
        return await handler(req)
    # Или Basic auth (fallback)
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, pw = decoded.partition(":")
            if user == "piktor" and pw == "RdnxScalp2026!":
                return await handler(req)
        except Exception:
            pass
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Short Bot"'},
        text="Authorization required. Use ?token=piktor2026",
    )


async def live_page(request):
    """Live dashboard — реальные данные с Bybit."""
    html = LIVE_HTML
    return web.Response(text=html, headers={"Content-Type": "text/html; charset=utf-8"})


PANEL_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Bot — Panel</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b0e11; color: #eaecef; font: 14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
.container { max-width: 1400px; margin: 0 auto; padding: 12px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1e2329; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
header h1 { font-size: 18px; color: #f0b90b; }
header h1 small { color: #848e9c; font-weight: normal; font-size: 12px; }
.nav { display: flex; gap: 8px; align-items: center; }
.nav a { color: #4a9eff; text-decoration: none; padding: 6px 14px; background: #1e2329; border-radius: 6px; font-size: 13px; }
.nav a:hover { background: #2b3139; }
.topbar { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 12px; }
.stat { background: #181a20; border: 1px solid #1e2329; border-radius: 8px; padding: 10px 14px; }
.stat .lbl { color: #848e9c; font-size: 11px; text-transform: uppercase; margin-bottom: 2px; }
.stat .val { font-size: 20px; font-weight: 600; }
.stat .val.pos { color: #0ecb81; }
.stat .val.neg { color: #f6465d; }
.bot-dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.bot-dot.on { background: #0ecb81; box-shadow: 0 0 6px #0ecb81; }
.bot-dot.off { background: #f6465d; box-shadow: 0 0 6px #f6465d; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 12px; }
.tab { padding: 8px 16px; background: #1e2329; border-radius: 6px 6px 0 0; cursor: pointer; color: #848e9c; font-size: 13px; border: 1px solid transparent; }
.tab.active { background: #181a20; color: #f0b90b; border-color: #1e2329; border-bottom-color: #181a20; font-weight: 600; }
.tab:hover { color: #eaecef; }
.card { background: #181a20; border: 1px solid #1e2329; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.card h3 { font-size: 13px; color: #848e9c; text-transform: uppercase; margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 8px; text-align: left; border-bottom: 1px solid #1e2329; }
th { color: #848e9c; font-size: 11px; text-transform: uppercase; }
tr:hover { background: #1e2329; }
.pos { color: #0ecb81; } .neg { color: #f6465d; }
.muted { color: #848e9c; }
input, select, textarea { background: #1e2329; border: 1px solid #2b3139; color: #eaecef; padding: 6px 10px; border-radius: 4px; font-size: 13px; }
input:focus, select:focus { border-color: #f0b90b; outline: none; }
.btn { padding: 6px 14px; background: #f0b90b; color: #0b0e11; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; font-size: 13px; }
.btn:hover { background: #d4a517; }
.btn.danger { background: #f6465d; color: #fff; }
.btn.danger:hover { background: #d63d52; }
.btn.small { padding: 3px 10px; font-size: 12px; }
.btn.green { background: #0ecb81; color: #0b0e11; }
.btn.green:hover { background: #0ab76e; }
.btn-outline { background: transparent; border: 1px solid #2b3139; color: #eaecef; }
.btn-outline:hover { border-color: #f0b90b; color: #f0b90b; }
.form-row { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
.form-row label { min-width: 180px; color: #848e9c; font-size: 13px; }
.form-row input { min-width: 120px; }
.cfg-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.cfg-item { display: flex; flex-direction: column; gap: 4px; }
.cfg-item label { color: #848e9c; font-size: 12px; }
.cfg-item input { width: 100%; }
#toast { position: fixed; bottom: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { padding: 10px 18px; border-radius: 6px; font-size: 13px; font-weight: 600; min-width: 200px; animation: slideIn 0.3s; }
.toast.ok { background: #0ecb81; color: #0b0e11; }
.toast.err { background: #f6465d; color: #fff; }
.toast.info { background: #4a9eff; color: #fff; }
@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
.modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 8000; display: none; justify-content: center; align-items: center; }
.modal { background: #181a20; border: 1px solid #2b3139; border-radius: 8px; padding: 20px; min-width: 360px; max-width: 500px; }
.modal h3 { color: #f0b90b; margin-bottom: 14px; }
.modal .form-row { margin-bottom: 12px; }
.hidden { display: none !important; }
.action-cell { white-space: nowrap; }
.action-cell button { margin-right: 4px; }
.refresh-indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #0ecb81; margin-left: 8px; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
</style><script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script></head><body><div class="container">
  <header>
    <h1>Short Bot <small>Management Panel</small></h1>
    <div class="nav">
      <a href="/live?token=piktor2026">Live Dashboard</a>
      <a href="/panel?token=piktor2026" class="active">Panel</a>
    </div>
  </header>

  <div class="topbar" id="topbar">
    <div class="stat"><div class="lbl">Balance</div><div class="val" id="s-balance">—</div></div>
    <div class="stat"><div class="lbl">Equity</div><div class="val" id="s-equity">—</div></div>
    <div class="stat"><div class="lbl">Avail Margin</div><div class="val" id="s-avail">—</div></div>
    <div class="stat"><div class="lbl">Unreal. PnL</div><div class="val" id="s-upnl">—</div></div>
    <div class="stat"><div class="lbl">Open Positions</div><div class="val" id="s-open">—</div></div>
    <div class="stat"><div class="lbl">Open Orders</div><div class="val" id="s-orders">—</div></div>
    <div class="stat"><div class="lbl">Bot Status</div><div class="val" id="s-bot" style="font-size:16px"><span class="bot-dot off" id="bot-dot"></span><span id="bot-text">—</span></div></div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="positions">Positions</div>
    <div class="tab" data-tab="orders">Orders</div>
    <div class="tab" data-tab="config">Config</div>
    <div class="tab" data-tab="trades">Trades</div>
    <div class="tab" data-tab="open">Open Position</div>
    <div class="tab" data-tab="chart">Chart</div>
  </div>

  <!-- Tab: Positions -->
  <div id="tab-positions" class="tab-content">
    <div class="card">
      <h3>Open Positions (Bybit realtime) <span class="refresh-indicator"></span></h3>
      <table id="pos-table"><thead><tr>
        <th>Symbol</th><th>Side</th><th>Size</th><th>Avg Price</th><th>Lev</th>
        <th>uPnL</th><th>TP</th><th>SL</th><th>Trail</th><th>Actions</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <!-- Tab: Orders -->
  <div id="tab-orders" class="tab-content hidden">
    <div class="card">
      <h3>Open Orders (Bybit realtime) <span class="refresh-indicator"></span></h3>
      <table id="ord-table"><thead><tr>
        <th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th>Price</th>
        <th>Trigger</th><th>Qty</th><th>Filled</th><th>ReduceOnly</th><th>Status</th><th>Action</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <!-- Tab: Config -->
  <div id="tab-config" class="tab-content hidden">
    <div class="card">
      <h3>Bot Configuration (config.yaml)</h3>
      <div class="cfg-grid" id="cfg-form">
        <div class="cfg-item"><label>min_gain_pct (%)</label><input id="cfg-min_gain_pct" type="number" step="0.1"></div>
        <div class="cfg-item"><label>tp_chain (list, comma-sep)</label><input id="cfg-tp_chain" type="text" placeholder="20.0, 20.0, 20.0"></div>
        <div class="cfg-item"><label>trail_pct (%)</label><input id="cfg-trail_pct" type="number" step="0.1"></div>
        <div class="cfg-item"><label>sl_pct (%)</label><input id="cfg-sl_pct" type="number" step="0.1"></div>
        <div class="cfg-item"><label>dca_trigger_pct (list)</label><input id="cfg-dca_trigger_pct" type="text" placeholder="10.0, 15.0, 20.0, 25.0, 30.0"></div>
        <div class="cfg-item"><label>dca_max_count</label><input id="cfg-dca_max_count" type="number" step="1"></div>
        <div class="cfg-item"><label>max_open_positions</label><input id="cfg-max_open_positions" type="number" step="1"></div>
        <div class="cfg-item"><label>risk_notional_pct (%)</label><input id="cfg-risk_notional_pct" type="number" step="0.1"></div>
        <div class="cfg-item"><label>leverage (x)</label><input id="cfg-leverage" type="number" step="1"></div>
        <div class="cfg-item"><label>max_daily_entries</label><input id="cfg-max_daily_entries" type="number" step="1"></div>
        <div class="cfg-item"><label>ranking_interval_min</label><input id="cfg-ranking_interval_min" type="number" step="1"></div>
        <div class="cfg-item"><label>min_volume_usd</label><input id="cfg-min_volume_usd" type="number" step="1000"></div>
      </div>
      <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn" onclick="saveConfig()">Save Config</button>
        <button class="btn green" onclick="applyToBot()">Apply to Bot (Restart)</button>
        <button class="btn-outline" onclick="loadConfig()">Reload from File</button>
      </div>
    </div>
  </div>

  <!-- Tab: Trades -->
  <div id="tab-trades" class="tab-content hidden">
    <div class="card">
      <h3>Closed Trade History (Bybit)</h3>
      <table id="trades-table"><thead><tr>
        <th>Date</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Lev</th><th>PnL</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <!-- Tab: Open Position -->
  <div id="tab-open" class="tab-content hidden">
    <div class="card">
      <h3>Manually Open Short Position</h3>
      <div class="form-row">
        <label>Symbol</label>
        <input id="open-symbol" type="text" placeholder="e.g. EPICUSDT" style="min-width:200px">
      </div>
      <div class="form-row">
        <label>Size mode</label>
        <select id="open-mode" onchange="toggleOpenMode()">
          <option value="qty">Qty (coins)</option>
          <option value="notional">Notional % of balance</option>
        </select>
      </div>
      <div class="form-row" id="open-qty-row">
        <label>Qty (coins)</label>
        <input id="open-qty" type="number" step="0.0001" placeholder="0.0">
      </div>
      <div class="form-row hidden" id="open-notional-row">
        <label>Notional % of balance</label>
        <input id="open-notional" type="number" step="0.1" placeholder="10.0">
      </div>
      <div class="form-row">
        <label>Leverage (from config if empty)</label>
        <input id="open-leverage" type="number" step="1" placeholder="e.g. 3">
      </div>
      <div class="form-row">
        <button class="btn danger" onclick="openPosition()">OPEN SHORT (Market)</button>
        <span class="muted">This will place a real market short order on Bybit + set TP/DCA from config.</span>
      </div>
    </div>
  </div>
  <!-- Tab: Chart -->
  <div id="tab-chart" class="tab-content hidden">
    <div class="card">
      <h3>Chart <span class="muted" style="font-size:12px">— click a position row to load chart</span></h3>
      <div class="form-row" style="margin-bottom:10px">
        <label>Symbol</label>
        <input id="chart-symbol" type="text" placeholder="e.g. EULUSDT" style="min-width:200px">
        <button class="btn" onclick="loadPanelChart(document.getElementById('chart-symbol').value)">Load</button>
      </div>
      <div id="panel-chart-info" style="margin-bottom:8px;font-size:13px"></div>
      <div id="chart-container" style="width:100%;height:450px"></div>
    </div>
  </div>
</div>

<!-- Modal for TP/SL move -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal" id="modal-box">
    <h3 id="modal-title">Move TP</h3>
    <div class="form-row">
      <label>Symbol</label>
      <span id="modal-symbol" class="muted"></span>
    </div>
    <div class="form-row">
      <label id="modal-price-label">Trigger Price</label>
      <input id="modal-price" type="number" step="0.000001">
    </div>
    <div class="form-row" style="justify-content:flex-end;margin-top:16px">
      <button class="btn-outline" onclick="closeModal()">Cancel</button>
      <button class="btn" id="modal-confirm" onclick="confirmModal()">Confirm</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || 'piktor2026';
const API = (path) => path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;
let modalAction = null;
let modalData = {};

function fmt(n, d=2) { if (n == null || n === '' || n === '') return '—'; const v = Number(n); return isNaN(v) ? n : v.toFixed(d); }
function pnlClass(n) { return Number(n) > 0 ? 'pos' : Number(n) < 0 ? 'neg' : ''; }
function $(id) { return document.getElementById(id); }

// ── Toast ──
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  $('toast').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.5s'; setTimeout(() => el.remove(), 500); }, 4000);
}

// ── Tabs ──
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
    $('tab-' + t.dataset.tab).classList.remove('hidden');
    if (t.dataset.tab === 'config') loadConfig();
    if (t.dataset.tab === 'trades') loadTrades();
    if (t.dataset.tab === 'chart' && !window._chartLoaded) { window._chartLoaded = true; }
  };
});

// ── Overview / Top bar ──
async function loadOverview() {
  try {
    const r = await fetch(API('/api/live/overview'));
    const d = await r.json();
    if (d.error) { $('s-balance').textContent = 'ERR'; return; }
    $('s-balance').textContent = '$' + fmt(d.balance);
    $('s-equity').textContent = '$' + fmt(d.equity);
    $('s-avail').textContent = '$' + fmt(d.available);
    const upnl = d.positions ? d.positions.reduce((s, p) => s + (p.unrealisedPnl || 0), 0) : 0;
    const upnlEl = $('s-upnl');
    upnlEl.textContent = '$' + fmt(upnl);
    upnlEl.className = 'val ' + pnlClass(upnl);
    $('s-open').textContent = d.open_count;
    $('s-orders').textContent = d.open_orders;
  } catch(e) { $('s-balance').textContent = 'ERR'; }
}

async function loadBotStatus() {
  try {
    const r = await fetch(API('/api/bot/status'), { method: 'POST' });
    const d = await r.json();
    if (d.error) { $('bot-text').textContent = 'ERR'; return; }
    const dot = $('bot-dot');
    const txt = $('bot-text');
    if (d.active) {
      dot.className = 'bot-dot on';
      txt.textContent = 'Running (PID ' + d.pid + (d.uptime_str ? ', ' + d.uptime_str : '') + ')';
    } else {
      dot.className = 'bot-dot off';
      txt.textContent = 'Stopped';
    }
  } catch(e) { $('bot-text').textContent = 'ERR'; }
}

// ── Positions tab ──
async function loadPositions() {
  try {
    const r = await fetch(API('/api/live/open'));
    const d = await r.json();
    if (d.error) { $('pos-table').querySelector('tbody').innerHTML = '<tr><td colspan=10 class="muted">' + d.error + '</td></tr>'; return; }
    let html = '';
    const positions = d.positions || [];
    if (positions.length === 0) {
      html = '<tr><td colspan=10 class="muted">No open positions</td></tr>';
    } else {
      for (const p of positions) {
        const pnlCls = p.unrealisedPnl > 0 ? 'pos' : 'neg';
        html += '<tr>'
          + '<td><b><a href="#" data-sym="' + p.symbol + '" onclick="chartFromPos(this.dataset.sym);return false" style="color:#4a9eff;text-decoration:none">' + p.symbol + '</a></b></td>'
          + '<td>' + p.side + '</td>'
          + '<td>' + p.size + '</td>'
          + '<td>' + fmt(p.avgPrice, 6) + '</td>'
          + '<td>' + p.leverage + 'x</td>'
          + '<td class="' + pnlCls + '">' + fmt(p.unrealisedPnl) + '</td>'
          + '<td>' + (p.takeProfit || '—') + '</td>'
          + '<td>' + (p.stopLoss || '—') + '</td>'
          + '<td>' + (p.trailingStop && p.trailingStop !== '0' ? p.trailingStop : '—') + '</td>'
          + '<td class="action-cell">'
          +   '<button class="btn danger small" onclick="closePosition(\\'' + p.symbol + '\\',' + p.size + ')">Close</button>'
          +   '<button class="btn small" onclick="openModal(\\'tp\\', \\'' + p.symbol + '\\', ' + p.size + ', ' + (p.takeProfit && p.takeProfit !== '0' ? p.takeProfit : '') + ')">Move TP</button>'
          +   '<button class="btn small" onclick="openModal(\\'sl\\', \\'' + p.symbol + '\\', ' + p.size + ', ' + (p.stopLoss && p.stopLoss !== '0' ? p.stopLoss : '') + ')">Move SL</button>'
          + '</td></tr>';
      }
    }
    $('pos-table').querySelector('tbody').innerHTML = html;
  } catch(e) { $('pos-table').querySelector('tbody').innerHTML = '<tr><td colspan=10>ERR: ' + e + '</td></tr>'; }
}

// ── Orders tab ──
async function loadOrders() {
  try {
    const r = await fetch(API('/api/orders'));
    const d = await r.json();
    if (d.error) { $('ord-table').querySelector('tbody').innerHTML = '<tr><td colspan=11 class="muted">' + d.error + '</td></tr>'; return; }
    let html = '';
    const orders = d.orders || [];
    if (orders.length === 0) {
      html = '<tr><td colspan=11 class="muted">No open orders</td></tr>';
    } else {
      for (const o of orders) {
        const ts = o.createdTime ? new Date(parseInt(o.createdTime)).toISOString().replace('T',' ').slice(0,19) : '—';
        html += '<tr>'
          + '<td>' + ts + '</td>'
          + '<td><b>' + o.symbol + '</b></td>'
          + '<td>' + o.side + '</td>'
          + '<td>' + o.orderType + '</td>'
          + '<td>' + (o.price && o.price !== '0' ? fmt(o.price, 6) : '—') + '</td>'
          + '<td>' + (o.triggerPrice && o.triggerPrice !== '0' ? fmt(o.triggerPrice, 6) : '—') + '</td>'
          + '<td>' + o.qty + '</td>'
          + '<td>' + (o.filledQty || '0') + '</td>'
          + '<td>' + (o.reduceOnly === 'true' ? '✓' : '') + '</td>'
          + '<td>' + o.status + '</td>'
          + '<td class="action-cell"><button class="btn danger small" onclick="cancelOrder(\\'' + o.symbol + '\\',\\'' + o.orderId + '\\')">Cancel</button></td>'
          + '</tr>';
      }
    }
    $('ord-table').querySelector('tbody').innerHTML = html;
  } catch(e) { $('ord-table').querySelector('tbody').innerHTML = '<tr><td colspan=11>ERR: ' + e + '</td></tr>'; }
}

// ── Trades tab ──
async function loadTrades() {
  try {
    const r = await fetch(API('/api/live/trades'));
    const d = await r.json();
    if (d.error) { $('trades-table').querySelector('tbody').innerHTML = '<tr><td colspan=8 class="muted">' + d.error + '</td></tr>'; return; }
    let html = '';
    const trades = d.trades || [];
    if (trades.length === 0) {
      html = '<tr><td colspan=8 class="muted">No closed trades</td></tr>';
    } else {
      for (const t of trades) {
        const cls = t.pnl > 0 ? 'pos' : 'neg';
        html += '<tr>'
          + '<td>' + (t.date || '—') + '</td>'
          + '<td><b>' + t.symbol + '</b></td>'
          + '<td>' + t.side + '</td>'
          + '<td>' + t.qty + '</td>'
          + '<td>' + fmt(t.entry, 6) + '</td>'
          + '<td>' + fmt(t.exit, 6) + '</td>'
          + '<td>' + (t.leverage || '—') + '</td>'
          + '<td class="' + cls + '">' + fmt(t.pnl) + '</td>'
          + '</tr>';
      }
    }
    $('trades-table').querySelector('tbody').innerHTML = html;
  } catch(e) { $('trades-table').querySelector('tbody').innerHTML = '<tr><td colspan=8>ERR: ' + e + '</td></tr>'; }
}

// ── Config tab ──
async function loadConfig() {
  try {
    const r = await fetch(API('/api/config'));
    const d = await r.json();
    if (d.error) { toast('Config load error: ' + d.error, 'err'); return; }
    const c = d.config || {};
    $('cfg-min_gain_pct').value = c.min_gain_pct ?? '';
    $('cfg-tp_chain').value = Array.isArray(c.tp_chain) ? c.tp_chain.join(', ') : (c.tp_chain || '');
    $('cfg-trail_pct').value = c.trail_pct ?? '';
    $('cfg-sl_pct').value = c.sl_pct ?? '';
    $('cfg-dca_trigger_pct').value = Array.isArray(c.dca_trigger_pct) ? c.dca_trigger_pct.join(', ') : (c.dca_trigger_pct || '');
    $('cfg-dca_max_count').value = c.dca_max_count ?? '';
    $('cfg-max_open_positions').value = c.max_open_positions ?? '';
    $('cfg-risk_notional_pct').value = c.risk_notional_pct ?? '';
    $('cfg-leverage').value = c.leverage ?? '';
    $('cfg-max_daily_entries').value = c.max_daily_entries ?? '';
    $('cfg-ranking_interval_min').value = c.ranking_interval_min ?? '';
    $('cfg-min_volume_usd').value = c.min_volume_usd ?? '';
  } catch(e) { toast('Config load error: ' + e, 'err'); }
}

async function saveConfig() {
  if (!confirm('Save config.yaml? This updates the file on disk (bot restart needed to apply).')) return;
  const body = {
    min_gain_pct: parseFloat($('cfg-min_gain_pct').value),
    tp_chain: $('cfg-tp_chain').value,
    trail_pct: parseFloat($('cfg-trail_pct').value),
    sl_pct: parseFloat($('cfg-sl_pct').value),
    dca_trigger_pct: $('cfg-dca_trigger_pct').value,
    dca_max_count: parseInt($('cfg-dca_max_count').value),
    max_open_positions: parseInt($('cfg-max_open_positions').value),
    risk_notional_pct: parseFloat($('cfg-risk_notional_pct').value),
    leverage: parseFloat($('cfg-leverage').value),
    max_daily_entries: parseInt($('cfg-max_daily_entries').value),
    ranking_interval_min: parseInt($('cfg-ranking_interval_min').value),
    min_volume_usd: parseFloat($('cfg-min_volume_usd').value),
  };
  try {
    const r = await fetch(API('/api/config'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok || d.ok === false && d.updated) {
      toast('Config saved: ' + d.msg, d.ok ? 'ok' : 'err');
    } else {
      toast('Config save: ' + (d.msg || d.error || 'unknown'), 'err');
    }
  } catch(e) { toast('Config save error: ' + e, 'err'); }
}

async function applyToBot() {
  if (!confirm('Apply config to bot? This will restart short-bot.service (systemctl restart). The bot will reload config from disk.\\n\\nMake sure you saved config first!')) return;
  try {
    const r = await fetch(API('/api/bot/restart'), { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      toast('Bot restarted: ' + d.msg, 'ok');
      setTimeout(loadBotStatus, 2000);
    } else {
      toast('Restart failed: ' + (d.msg || d.error), 'err');
    }
  } catch(e) { toast('Restart error: ' + e, 'err'); }
}

// ── Actions ──
async function closePosition(symbol, qty) {
  if (!confirm('CLOSE position ' + symbol + ' qty=' + qty + '?\\nThis will cancel all orders and place a market buy (reduce-only).')) return;
  try {
    const r = await fetch(API('/api/position/close'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol, qty }) });
    const d = await r.json();
    if (d.ok) toast('Closed ' + symbol + ': ' + d.msg, 'ok');
    else toast('Close failed: ' + (d.msg || d.error), 'err');
    setTimeout(() => { loadPositions(); loadOrders(); loadOverview(); }, 1500);
  } catch(e) { toast('Close error: ' + e, 'err'); }
}

async function cancelOrder(symbol, orderId) {
  if (!confirm('Cancel order ' + orderId.slice(0,12) + ' on ' + symbol + '?')) return;
  try {
    const r = await fetch(API('/api/order/cancel'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol, order_id: orderId }) });
    const d = await r.json();
    if (d.ok) toast('Order cancelled: ' + symbol, 'ok');
    else toast('Cancel failed: ' + (d.msg || d.error), 'err');
    setTimeout(loadOrders, 1500);
  } catch(e) { toast('Cancel error: ' + e, 'err'); }
}

function openModal(type, symbol, qty, currentVal) {
  modalAction = type;
  modalData = { symbol, qty };
  $('modal-title').textContent = type === 'tp' ? 'Move TP' : 'Move SL';
  $('modal-price-label').textContent = type === 'tp' ? 'TP Trigger Price' : 'SL Trigger Price';
  $('modal-symbol').textContent = symbol + ' (qty=' + qty + ')';
  $('modal-price').value = currentVal || '';
  $('modal-overlay').style.display = 'flex';
  $('modal-price').focus();
}

function closeModal() {
  $('modal-overlay').style.display = 'none';
  modalAction = null;
}

async function confirmModal() {
  const price = parseFloat($('modal-price').value);
  if (!price || price <= 0) { toast('Invalid price', 'err'); return; }
  const { symbol, qty } = modalData;
  const action = modalAction;
  closeModal();
  const endpoint = action === 'tp' ? '/api/tp/move' : '/api/sl/move';
  const bodyKey = action === 'tp' ? 'trigger_price' : 'stop_loss';
  if (!confirm((action === 'tp' ? 'Move TP' : 'Move SL') + ' for ' + symbol + ' to ' + price + '?')) return;
  try {
    const r = await fetch(API(endpoint), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ symbol, qty, [bodyKey]: price }) });
    const d = await r.json();
    if (d.ok) toast((action === 'tp' ? 'TP' : 'SL') + ' moved: ' + (d.msg || 'ok'), 'ok');
    else toast('Move failed: ' + (d.msg || d.error), 'err');
    setTimeout(loadPositions, 1500);
  } catch(e) { toast('Move error: ' + e, 'err'); }
}

function toggleOpenMode() {
  const mode = $('open-mode').value;
  $('open-qty-row').classList.toggle('hidden', mode === 'notional');
  $('open-notional-row').classList.toggle('hidden', mode === 'qty');
}

async function openPosition() {
  const symbol = $('open-symbol').value.toUpperCase().trim();
  const mode = $('open-mode').value;
  const leverage = $('open-leverage').value ? parseFloat($('open-leverage').value) : null;
  if (!symbol) { toast('Symbol required', 'err'); return; }
  const body = { symbol, leverage };
  if (mode === 'notional') {
    body.mode = 'notional';
    body.notional_pct = parseFloat($('open-notional').value);
    if (!body.notional_pct) { toast('Notional % required', 'err'); return; }
  } else {
    body.mode = 'qty';
    body.qty = parseFloat($('open-qty').value);
    if (!body.qty) { toast('Qty required', 'err'); return; }
  }
  if (!confirm('OPEN SHORT ' + symbol + ' on Bybit?\\nMode: ' + mode + ', ' + (mode === 'qty' ? 'qty=' + body.qty : 'notional=' + body.notional_pct + '%') + (leverage ? ', lev=' + leverage + 'x' : '') + '\\n\\nThis places a REAL market short order + sets TP/DCA from config.')) return;
  try {
    const r = await fetch(API('/api/position/open'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) toast('Short opened: ' + d.msg, 'ok');
    else toast('Open failed: ' + (d.msg || d.error), 'err');
    setTimeout(() => { loadPositions(); loadOrders(); loadOverview(); }, 2000);
  } catch(e) { toast('Open error: ' + e, 'err'); }
}

// ── Auto-refresh ──
function refreshAll() {
  loadOverview();
  loadBotStatus();
  if (!$('tab-positions').classList.contains('hidden')) loadPositions();
  if (!$('tab-orders').classList.contains('hidden')) loadOrders();
}

// Initial load
loadOverview();
loadBotStatus();
loadPositions();
function chartFromPos(symbol) {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelector('[data-tab="chart"]').classList.add('active');
  document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
  document.getElementById('tab-chart').classList.remove('hidden');
  document.getElementById('chart-symbol').value = symbol;
  setTimeout(function() { loadPanelChart(symbol); }, 50);
}

let _chart = null, _candleSeries = null, _priceLines = [];

async function loadPanelChart(symbol) {
  if (!symbol) return;
  try {
    const r = await fetch(API('/api/live/chart?symbol=' + symbol));
    const d = await r.json();
    if (d.error) { toast('Chart error: ' + d.error, 'err'); return; }
    
    if (!_chart) {
      _chart = LightweightCharts.createChart(document.getElementById("chart-container"), {
        layout: { background: { color: '#0b0e11' }, textColor: '#848e9c' },
        grid: { vertLines: { color: '#1e2329' }, horzLines: { color: '#1e2329' } },
        timeScale: { borderColor: '#2b3139', timeVisible: true, secondsVisible: false },
        priceScale: { borderColor: '#2b3139' },
        crosshair: { mode: 0 },
      });
      _candleSeries = _chart.addCandlestickSeries({
        upColor: '#0ecb81', downColor: '#f6465d',
        wickUpColor: '#0ecb81', wickDownColor: '#f6465d',
        borderVisible: false,
      });
    }
    
    // Clear old price lines
    _priceLines.forEach(pl => _candleSeries.removePriceLine(pl));
    _priceLines = [];
    
    // Set candles
    const candles = (d.candles || []).map(c => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    }));
    _candleSeries.setData(candles);
    
    // Add price lines for position
    const pos = d.position;
    const avg = pos ? pos.avgPrice : 0;
    if (pos && pos.size > 0) {
      _priceLines.push(_candleSeries.createPriceLine({ price: avg, color: '#f0b90b', lineWidth: 2, lineStyle: 0, title: 'Entry ' + avg, axisLabelVisible: true }));
      
      // TP
      if (d.dca_levels && d.dca_levels.length > 0) {
        // TP = avg * 0.8 (20% below avg)
        const tp = avg * 0.8;
        _priceLines.push(_candleSeries.createPriceLine({ price: tp, color: '#0ecb81', lineWidth: 1, lineStyle: 2, title: 'TP ' + tp.toFixed(4), axisLabelVisible: true }));
      }
      
      // DCA levels
      (d.dca_levels || []).forEach((lvl, i) => {
        _priceLines.push(_candleSeries.createPriceLine({ price: lvl, color: '#f6465d', lineWidth: 1, lineStyle: 1, title: 'DCAx' + (i+1) + ' ' + lvl.toFixed(4), axisLabelVisible: true }));
      });
    }
    
    // Info text
    if (pos && pos.size > 0) {
      const lastC = candles.length ? candles[candles.length-1].close : 0;
      const pnl = (avg - lastC) * pos.size;
      const pnlColor = pnl >= 0 ? '#0ecb81' : '#f6465d';
      document.getElementById("panel-chart-info").innerHTML = '<span class="muted">Side:</span> ' + pos.side + 
        ' | <span class="muted">Size:</span> ' + pos.size + 
        ' | <span class="muted">Avg:</span> ' + avg + 
        ' | <span class="muted">Price:</span> ' + lastC + 
        ' | <span class="muted">uPnL:</span> <span style="color:' + pnlColor + '">' + pnl.toFixed(4) + '</span>';
    } else {
      document.getElementById("panel-chart-info").innerHTML = '<span class="muted">No open position for ' + symbol + '</span>';
    }
    
    _chart.timeScale().fitContent();
  } catch(e) {
    toast('Chart load failed: ' + e.message, 'err');
  }
}

setInterval(refreshAll, 5000);
</script>
</body></html>"""




async def panel_page(request):
    """Management panel — Bybit-like control interface."""
    html = PANEL_HTML
    token = request.query.get("token", "piktor2026")
    html = html.replace("__TOKEN__", token)
    return web.Response(text=html, headers={"Content-Type": "text/html; charset=utf-8"})



LIVE_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Bot — LIVE</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b0e11; color: #eaecef; font: 14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
.container { max-width: 1200px; margin: 0 auto; padding: 12px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1e2329; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
header h1 { font-size: 18px; color: #f0b90b; }
header h1 small { color: #848e9c; font-weight: normal; font-size: 12px; }
.nav { display: flex; gap: 8px; }
.nav a { color: #4a9eff; text-decoration: none; padding: 6px 14px; background: #1e2329; border-radius: 6px; font-size: 13px; }
.nav a.active { background: #f0b90b; color: #0b0e11; font-weight: 600; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 12px; }
.tab { padding: 6px 14px; background: #1e2329; border-radius: 6px; cursor: pointer; color: #848e9c; font-size: 13px; }
.tab.active { background: #f0b90b; color: #0b0e11; font-weight: 600; }
.card { background: #181a20; border: 1px solid #1e2329; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.stat { display: flex; flex-direction: column; }
.stat .lbl { color: #848e9c; font-size: 11px; text-transform: uppercase; }
.stat .val { font-size: 20px; font-weight: 600; }
.stat .val.pos { color: #0ecb81; }
.stat .val.neg { color: #f6465d; }
.live-badge { display: inline-block; padding: 2px 8px; background: #f6465d; color: #fff; border-radius: 4px; font-size: 11px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid #1e2329; }
th { color: #848e9c; font-size: 11px; text-transform: uppercase; }
.pos { color: #0ecb81; } .neg { color: #f6465d; }
.sym-input { padding: 6px; background: #1e2329; border: 1px solid #2b3139; border-radius: 4px; color: #eaecef; font-size: 13px; }
.btn { padding: 6px 14px; background: #f0b90b; color: #0b0e11; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; }
.btn:hover { background: #d4a517; }
#chart { width: 100%; height: 450px; }
.muted { color: #848e9c; }
select, input { background: #1e2329; border: 1px solid #2b3139; color: #eaecef; padding: 6px; border-radius: 4px; }
.dca-row { color: #4a9eff; font-size: 12px; }
</style></head><body><div class="container">
  <header>
    <h1>Short Bot <small>LIVE <span class="live-badge">REAL</span></small></h1>
    <div class="nav">
      <a href="/live" class="active">Live</a>
    </div>
  </header>

  <div class="tabs">
    <div class="tab active" data-view="overview">Обзор</div>
    <div class="tab" data-view="open">Открытые</div>
    <div class="tab" data-view="trades">История</div>
    <div class="tab" data-view="chart">График</div>
  </div>

  <div id="view-overview" class="view">
    <div class="card">
      <div class="grid">
        <div class="stat"><span class="lbl">Баланс</span><span class="val" id="s-balance">—</span></div>
        <div class="stat"><span class="lbl">Equity</span><span class="val" id="s-equity">—</span></div>
        <div class="stat"><span class="lbl">Доступно</span><span class="val" id="s-avail">—</span></div>
        <div class="stat"><span class="lbl">PnL всего</span><span class="val" id="s-pnl">—</span></div>
        <div class="stat"><span class="lbl">Win Rate</span><span class="val" id="s-wr">—</span></div>
        <div class="stat"><span class="lbl">Открыто</span><span class="val" id="s-open">—</span></div>
        <div class="stat"><span class="lbl">Ордера (DCA)</span><span class="val" id="s-orders">—</span></div>
        <div class="stat"><span class="lbl">Закрыто</span><span class="val" id="s-closed">—</span></div>
      </div>
    </div>
    <div class="card">
      <div class="lbl muted" style="margin-bottom:8px">ОТКРЫТЫЕ ПОЗИЦИИ (с биржи)</div>
      <div id="open-list"></div>
    </div>
  </div>

  <div id="view-open" class="view" style="display:none">
    <div class="card">
      <div class="lbl muted" style="margin-bottom:8px">ОТКРЫТЫЕ ПОЗИЦИИ + DCA ОРДЕРА</div>
      <div id="open-detail"></div>
    </div>
  </div>

  <div id="view-trades" class="view" style="display:none">
    <div class="card">
      <div class="lbl muted" style="margin-bottom:8px">ИСТОРИЯ СДЕЛОК (с биржи)</div>
      <table id="trades-table"><thead><tr>
        <th>Дата</th><th>Символ</th><th>Сторона</th><th>Qty</th>
        <th>Вход</th><th>Выход</th><th>PnL</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div id="view-chart" class="view" style="display:none">
    <div class="card">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <input class="sym-input" id="chart-sym" placeholder="Символ (EPICUSDT)" value="EPICUSDT">
        <select id="chart-interval">
          <option value="1">1m</option>
          <option value="5">5m</option>
          <option value="15" selected>15m</option>
          <option value="60">1h</option>
          <option value="240">4h</option>
          <option value="D">1D</option>
        </select>
        <button class="btn" onclick="loadChart(document.getElementById('chart-symbol').value)">Построить</button>
        <span class="muted" id="chart-info"></span>
      </div>
      <div id="chart"></div>
      <div id="chart-levels" style="margin-top:10px"></div>
    </div>
  </div>
</div>

<script>
// Токен из URL для авторизации API
const TOKEN = new URLSearchParams(location.search).get('token') || 'piktor2026';
const API = `/api/live`;

// Переключение вкладок
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.view').forEach(v => v.style.display = 'none');
    document.getElementById('view-' + t.dataset.view).style.display = 'block';
    if (t.dataset.view === 'open') loadOpen();
    if (t.dataset.view === 'trades') loadTrades();
  };
});

function fmt(n, d=2) { return n != null ? Number(n).toFixed(d) : '—'; }
function pnlClass(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : ''; }

async function loadOverview() {
  try {
    const r = await fetch(`${API}/overview?token=${TOKEN}`, {credentials: 'include'});
    const d = await r.json();
    if (d.error) { document.getElementById('s-balance').textContent = 'ERROR: ' + d.error; return; }
    document.getElementById('s-balance').textContent = '$' + fmt(d.balance);
    document.getElementById('s-equity').textContent = '$' + fmt(d.equity);
    document.getElementById('s-avail').textContent = '$' + fmt(d.available);
    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = '$' + fmt(d.total_pnl);
    pnlEl.className = 'val ' + pnlClass(d.total_pnl);
    document.getElementById('s-wr').textContent = d.win_rate + '%';
    document.getElementById('s-open').textContent = d.open_count;
    document.getElementById('s-orders').textContent = d.open_orders;
    document.getElementById('s-closed').textContent = d.closed_count;

    // Список позиций
    let html = '';
    if (!d.positions || d.positions.length === 0) {
      html = '<p class="muted">Открытых позиций нет</p>';
    } else {
      html = '<table><thead><tr><th>Символ</th><th>Сторона</th><th>Qty</th><th>Entry</th><th>SL</th><th>TP</th><th>PnL</th></tr></thead><tbody>';
      for (const p of d.positions) {
        const pnlCls = p.unrealisedPnl > 0 ? 'pos' : 'neg';
        html += `<tr><td><b>${p.symbol}</b></td><td>${p.side}</td><td>${p.size}</td>
          <td>${fmt(p.avgPrice, 6)}</td><td>${p.stopLoss || '—'}</td><td>${p.takeProfit || '—'}</td>
          <td class="${pnlCls}">${fmt(p.unrealisedPnl)}</td></tr>`;
      }
      html += '</tbody></table>';
    }
    document.getElementById('open-list').innerHTML = html;
  } catch(e) { document.getElementById('s-balance').textContent = 'ERR: ' + e; }
}

async function loadOpen() {
  try {
    const r = await fetch(`${API}/open?token=${TOKEN}`, {credentials: 'include'});
    const d = await r.json();
    if (d.error) { document.getElementById('open-detail').innerHTML = 'ERROR: ' + d.error; return; }
    let html = '';
    if (!d.positions || d.positions.length === 0) {
      html = '<p class="muted">Открытых позиций нет</p>';
    } else {
      for (const p of d.positions) {
        const pnlCls = p.unrealisedPnl > 0 ? 'pos' : 'neg';
        html += `<div style="margin-bottom:16px;padding:10px;background:#1e2329;border-radius:6px">
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
            <b style="font-size:16px">${p.symbol}</b>
            <span class="muted">${p.side} ${p.size} @ ${fmt(p.avgPrice,6)}</span>
            <span>Lev: ${p.leverage}x</span>
            <span class="${p.pnlCls}">PnL: ${fmt(p.unrealisedPnl)}</span>
          </div>
          <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(100px,1fr));margin-bottom:8px">
            <div class="stat"><span class="lbl">SL</span><span>${p.stopLoss || '—'}</span></div>
            <div class="stat"><span class="lbl">TP</span><span>${p.takeProfit || '—'}</span></div>
            <div class="stat"><span class="lbl">Trail</span><span>${p.trailingStop || '—'}</span></div>
            <div class="stat"><span class="lbl">Active</span><span>${p.activePrice || '—'}</span></div>
          </div>`;
        if (p.dca_orders && p.dca_orders.length > 0) {
          html += '<div class="muted" style="font-size:11px;margin-bottom:4px">DCA ОРДЕРА:</div><table><thead><tr><th>Цена</th><th>Сторона</th><th>Qty</th><th>Тип</th><th>Статус</th></tr></thead><tbody>';
          for (const o of p.dca_orders) {
            html += `<tr class="dca-row"><td>${fmt(o.price, 6)}</td><td>${o.side}</td><td>${o.qty}</td><td>${o.type}</td><td>${o.status}</td></tr>`;
          }
          html += '</tbody></table>';
        } else {
          html += '<p class="muted" style="font-size:12px">DCA ордеров нет</p>';
        }
        html += '</div>';
      }
    }
    document.getElementById('open-detail').innerHTML = html;
  } catch(e) { document.getElementById('open-detail').innerHTML = 'ERR: ' + e; }
}

async function loadTrades() {
  try {
    const r = await fetch(`${API}/trades?token=${TOKEN}`, {credentials: 'include'});
    const d = await r.json();
    if (d.error) { document.querySelector('#trades-table tbody').innerHTML = '<tr><td colspan=7>ERROR: ' + d.error + '</td></tr>'; return; }
    let html = '';
    if (!d.trades || d.trades.length === 0) {
      html = '<tr><td colspan=7 class="muted">Закрытых сделок нет</td></tr>';
    } else {
      for (const t of d.trades) {
        const cls = t.pnl > 0 ? 'pos' : 'neg';
        // Клик на строку → открыть график сделки
        const onclick = `showTradeChart('${t.symbol}', ${t.entry}, ${t.exit}, ${t.ts}, ${t.exitTs}, ${t.pnl})`;
        html += `<tr style="cursor:pointer" onclick="${onclick}" title="Клик — показать на графике">
          <td>${t.date}</td><td><b>${t.symbol}</b></td><td>${t.side}</td>
          <td>${t.qty}</td><td>${fmt(t.entry,6)}</td><td>${fmt(t.exit,6)}</td>
          <td class="${cls}">${fmt(t.pnl)}</td></tr>`;
      }
    }
    document.querySelector('#trades-table tbody').innerHTML = html;
  } catch(e) { document.querySelector('#trades-table tbody').innerHTML = '<tr><td colspan=7>ERR: ' + e + '</td></tr>'; }
}

// Показать график конкретной сделки
function showTradeChart(sym, entry, exit, entryTs, exitTs, pnl) {
  // Переключить на вкладку График
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelector('.tab[data-view="chart"]').classList.add('active');
  document.querySelectorAll('.view').forEach(v => v.style.display = 'none');
  document.getElementById('view-chart').style.display = 'block';
  document.getElementById('chart-sym').value = sym;
  // Загрузить график с маркерами сделки
  loadTradeChart(sym, entry, exit, entryTs, exitTs, pnl);
}

async function loadTradeChart(sym, entry, exit, entryTs, exitTs, pnl) {
  const interval = document.getElementById('chart-interval').value;
  document.getElementById('chart-info').textContent = 'Загрузка сделки ' + sym + '...';
  try {
    // Окно вокруг сделки: -1 день до entry, +1 день после exit (или +7 дней если exit 0)
    const fromMs = entryTs - 86400000;  // -1 день
    const toMs = (exitTs || entryTs) + 86400000 * 3;  // +3 дня
    const r = await fetch(`${API}/chart?symbol=${sym}&interval=${interval}&from=${fromMs}&to=${toMs}&entry=${entry}&exit=${exit}&entryTs=${entryTs}&exitTs=${exitTs||0}&pnl=${pnl}&token=${TOKEN}`, {credentials: 'include'});
    const d = await r.json();
    if (d.error) { document.getElementById('chart-info').textContent = 'ERROR: ' + d.error; return; }

    if (!chart) {
      chart = LightweightCharts.createChart(document.getElementById('chart'), {
        layout: { background: { color: '#0b0e11' }, textColor: '#848e9c' },
        grid: { vertLines: { color: '#1e2329' }, horzLines: { color: '#1e2329' } },
        timeScale: { timeVisible: true, secondsVisible: false },
      });
      candleSeries = chart.addCandlestickSeries({
        upColor: '#0ecb81', downColor: '#f6465d',
        borderUpColor: '#0ecb81', borderDownColor: '#f6465d',
        wickUpColor: '#0ecb81', wickDownColor: '#f6465d',
      });
    }
    const candles = d.candles.map(c => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    }));
    candleSeries.setData(candles);

    let levelsHtml = '';
    // Маркеры сделки
    if (d.trade_markers) {
      const tm = d.trade_markers;
      if (tm.entry) {
        candleSeries.createPriceLine({ price: tm.entry, color: '#4a9eff', lineWidth: 2, lineStyle: 0, title: 'Entry' });
        levelsHtml += `<div>🔵 Entry: ${fmt(tm.entry,6)}</div>`;
      }
      if (tm.exit) {
        const exitColor = tm.pnl >= 0 ? '#0ecb81' : '#f6465d';
        candleSeries.createPriceLine({ price: tm.exit, color: exitColor, lineWidth: 2, lineStyle: 0, title: 'Exit' });
        const cls = tm.pnl >= 0 ? 'pos' : 'neg';
        levelsHtml += `<div class="${cls}">🔴 Exit: ${fmt(tm.exit,6)} (PnL: ${fmt(tm.pnl)})</div>`;
      }
      // Маркеры на свечах entry/exit
      const markers = [];
      if (tm.entryTs) {
        markers.push({time: tm.entryTs, position: 'belowBar', color: '#4a9eff', shape: 'arrowUp', text: 'Entry'});
      }
      if (tm.exitTs) {
        const mColor = tm.pnl >= 0 ? '#0ecb81' : '#f6465d';
        markers.push({time: tm.exitTs, position: 'aboveBar', color: mColor, shape: 'arrowDown', text: 'Exit'});
      }
      if (markers.length > 0) {
        markers.sort((a,b) => a.time - b.time);
        candleSeries.setMarkers(markers);
      }
    }
    // Текущая позиция (если есть)
    if (d.position) {
      const p = d.position;
      candleSeries.createPriceLine({ price: p.avgPrice, color: '#4a9eff', lineWidth: 1, lineStyle: 0, title: 'Current Entry' });
      if (p.stopLoss) candleSeries.createPriceLine({ price: parseFloat(p.stopLoss), color: '#f6465d', lineWidth: 1, lineStyle: 2, title: 'SL' });
      if (p.takeProfit) candleSeries.createPriceLine({ price: parseFloat(p.takeProfit), color: '#0ecb81', lineWidth: 1, lineStyle: 2, title: 'TP' });
    }
    // DCA уровни
    if (d.dca_levels && d.dca_levels.length > 0) {
      levelsHtml += '<div style="margin-top:6px"><b class="pos">DCA лимитки:</b> ' + d.dca_levels.map(l => fmt(l,6)).join(', ') + '</div>';
    }
    document.getElementById('chart-levels').innerHTML = levelsHtml;
    document.getElementById('chart-info').textContent = `Сделка ${sym} — ${candles.length} свечей`;
    chart.timeScale().fitContent();
  } catch(e) { document.getElementById('chart-info').textContent = 'ERR: ' + e; }
}

// График
let chart = null, candleSeries = null;
async function loadChart() {
  const sym = document.getElementById('chart-sym').value.toUpperCase();
  const interval = document.getElementById('chart-interval').value;
  if (!sym) return;
  document.getElementById('chart-info').textContent = 'Загрузка...';
  try {
    const r = await fetch(`${API}/chart?symbol=${sym}&interval=${interval}&limit=300&token=${TOKEN}`, {credentials: 'include'});
    const d = await r.json();
    if (d.error) { document.getElementById('chart-info').textContent = 'ERROR: ' + d.error; return; }
    
    if (!chart) {
      chart = LightweightCharts.createChart(document.getElementById('chart'), {
        layout: { background: { color: '#0b0e11' }, textColor: '#848e9c' },
        grid: { vertLines: { color: '#1e2329' }, horzLines: { color: '#1e2329' } },
        timeScale: { timeVisible: true, secondsVisible: false },
      });
      candleSeries = chart.addCandlestickSeries({
        upColor: '#0ecb81', downColor: '#f6465d',
        borderUpColor: '#0ecb81', borderDownColor: '#f6465d',
        wickUpColor: '#0ecb81', wickDownColor: '#f6465d',
      });
    }
    const candles = d.candles.map(c => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    }));
    candleSeries.setData(candles);
    
    // Уровни позиции
    let levelsHtml = '';
    if (d.position) {
      const p = d.position;
      // Entry
      candleSeries.createPriceLine({ price: p.avgPrice, color: '#4a9eff', lineWidth: 1, lineStyle: 0, title: 'Entry ' + p.side });
      levelsHtml += `<div>Entry: ${fmt(p.avgPrice,6)} (${p.side} ${p.size})</div>`;
      if (p.stopLoss) {
        candleSeries.createPriceLine({ price: parseFloat(p.stopLoss), color: '#f6465d', lineWidth: 1, lineStyle: 2, title: 'SL' });
        levelsHtml += `<div class="neg">SL: ${p.stopLoss}</div>`;
      }
      if (p.takeProfit) {
        candleSeries.createPriceLine({ price: parseFloat(p.takeProfit), color: '#0ecb81', lineWidth: 1, lineStyle: 2, title: 'TP' });
        levelsHtml += `<div class="pos">TP: ${p.takeProfit}</div>`;
      }
      if (p.activePrice) {
        candleSeries.createPriceLine({ price: parseFloat(p.activePrice), color: '#f0b90b', lineWidth: 1, lineStyle: 1, title: 'Trail active' });
        levelsHtml += `<div>Trail active: ${p.activePrice}</div>`;
      }
    } else {
      levelsHtml = '<span class="muted">Нет открытой позиции по этому символу</span>';
    }
    // DCA уровни
    if (d.dca_levels && d.dca_levels.length > 0) {
      levelsHtml += '<div style="margin-top:6px"><b class="pos">DCA лимитки:</b> ' + d.dca_levels.map(l => fmt(l,6)).join(', ') + '</div>';
    }
    document.getElementById('chart-levels').innerHTML = levelsHtml;
    document.getElementById('chart-info').textContent = `${sym} ${interval}m — ${candles.length} свечей`;
    chart.timeScale().fitContent();
  } catch(e) { document.getElementById('chart-info').textContent = 'ERR: ' + e; }
}

// Автообновление
loadOverview();

// ── Chart ──



setInterval(loadOverview, 5000);
</script>
</body></html>
"""


async def main():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/charts", charts_page)
    app.router.add_get("/tv", tv_page)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/open", api_open)
    app.router.add_get("/api/trades", api_trades)
    app.router.add_get("/api/chart", api_chart)
    # LIVE API
    app.router.add_get("/api/live/overview", api_live_overview)
    app.router.add_get("/api/live/open", api_live_open)
    app.router.add_get("/api/live/trades", api_live_trades)
    app.router.add_get("/api/live/chart", api_live_chart)
    app.router.add_get("/live", live_page)
    app.router.add_get("/panel", panel_page)
    # PANEL API — config / bot control / orders
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_save)
    app.router.add_post("/api/bot/restart", api_bot_restart)
    app.router.add_post("/api/bot/status", api_bot_status)
    app.router.add_post("/api/position/close", api_position_close)
    app.router.add_post("/api/order/cancel", api_order_cancel)
    app.router.add_post("/api/tp/move", api_tp_move)
    app.router.add_post("/api/sl/move", api_sl_move)
    app.router.add_post("/api/position/open", api_position_open)
    app.router.add_get("/api/orders", api_orders)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8077)
    await site.start()
    print("dashboard on :8077", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
