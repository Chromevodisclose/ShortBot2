#!/usr/bin/env python3
"""
Live trader v2 — лайв-логика для Short Bot.
SL/TP — через conditional orders (не position-level), можно двигать.
DCA лимитки — проверка существующих при рестарте (без дублей).
"""
import json, os, time, logging
from typing import Optional
from executor import (
    place_market_short, place_dca_limit, set_trading_stop,
    set_stop_loss_order, cancel_stop_loss_order, get_stop_loss_orders,
    cancel_order, cancel_all, close_market,
    get_position, get_open_orders, get_balance, get_ticker,
    get_instrument, set_leverage,
)

log = logging.getLogger("live_trader")

SL_PCT = 25.0
TP_CHAIN = [20.0] * 6
DCA_MAX = 5
DCA_TRIG = 15.0
DCA_MULT = 1.0
RISK_PCT = 3.0
LEVERAGE = 3
BE_PLUS_AT_DCA = 4
TRAIL_PCT = 3.0
TRAIL_AT_DCA = 4
TRAIL_ACTIVE_PCT = 5.0


def init(cfg):
    global SL_PCT, TP_CHAIN, DCA_MAX, DCA_TRIG, DCA_MULT, RISK_PCT
    global LEVERAGE, BE_PLUS_AT_DCA, TRAIL_PCT, TRAIL_AT_DCA, TRAIL_ACTIVE_PCT
    SL_PCT = cfg.get("sl_pct", 25.0)
    TP_CHAIN = cfg.get("tp_chain", [20.0] * 6)
    DCA_MAX = cfg.get("dca_max_count", 5)
    DCA_TRIG = cfg.get("dca_trigger_pct", 15.0)
    DCA_MULT = cfg.get("dca_qty_multiplier", 1.0)
    RISK_PCT = cfg.get("risk_pct", 3.0)
    LEVERAGE = cfg.get("leverage", 3)
    BE_PLUS_AT_DCA = cfg.get("be_plus_at_dca", 4)
    TRAIL_PCT = cfg.get("trail_pct", 3.0)
    TRAIL_AT_DCA = cfg.get("trail_at_dca", 4)
    TRAIL_ACTIVE_PCT = cfg.get("trail_active_pct", 5.0)


def calc_dca_levels(entry, qty_orig):
    """Все DCA уровни от entry. Возвращает list of dict."""
    levels = []
    avg = entry
    qty = qty_orig
    for i in range(DCA_MAX):
        trigger = avg * (1 + DCA_TRIG / 100)
        fill = trigger
        new_qty = qty_orig * DCA_MULT
        avg_new = (avg * qty + fill * new_qty) / (qty + new_qty)
        qty_new = qty + new_qty
        levels.append({
            "level": i + 1,
            "trigger_price": trigger,
            "fill_price": fill,
            "avg_after": avg_new,
            "qty_after": qty_new,
        })
        avg = avg_new
        qty = qty_new
    return levels


def calc_final_sl(entry, qty_orig):
    """Финальный SL от avg после всех DCA."""
    levels = calc_dca_levels(entry, qty_orig)
    if not levels:
        return entry * (1 + SL_PCT / 100)
    return levels[-1]["avg_after"] * (1 + SL_PCT / 100)


def calc_tp(dca_count, avg):
    """TP для DCA уровня. На BE_PLUS — выход в плюс."""
    if dca_count >= BE_PLUS_AT_DCA and BE_PLUS_AT_DCA > 0:
        return avg * 0.999
    tp_level = TP_CHAIN[min(dca_count, len(TP_CHAIN) - 1)]
    return avg * (1 - tp_level / 100)


def open_live_position(symbol, entry_price, balance, ts):
    """
    Открыть лайв позицию:
      1. Плечо
      2. Market short
      3. SL conditional order (не position-level!)
      4. TP conditional order
      5. DCA лимитки (проверка дублей)
    """
    set_leverage(symbol, LEVERAGE)

    inst = get_instrument(symbol)
    if not inst:
        log.error(f"[{symbol}] нет instrument info")
        return None

    risk_usd = balance * RISK_PCT / 100
    qty = risk_usd / (entry_price * SL_PCT / 100)
    qty = round(qty / inst["qtyStep"]) * inst["qtyStep"]
    notional = qty * entry_price
    if notional < inst["minNotional"]:
        qty = (int((inst["minNotional"] / entry_price) / inst["qtyStep"]) + 1) * inst["qtyStep"]
        notional = qty * entry_price
    if qty < inst["minQty"]:
        log.error(f"[{symbol}] qty {qty} < minQty {inst['minQty']}")
        return None

    log.info(f"[{symbol}] открытие: qty={qty} entry~${entry_price:.6f} notional=${notional:.2f}")

    order_id = place_market_short(symbol, qty, leverage=LEVERAGE)
    if not order_id:
        log.error(f"[{symbol}] market order не прошёл")
        return None

    time.sleep(1)
    pos_info = get_position(symbol)
    if not pos_info or pos_info["size"] == 0:
        log.error(f"[{symbol}] позиция не открылась")
        return None

    fill_price = pos_info["avgPrice"]
    actual_qty = pos_info["size"]
    log.info(f"[{symbol}] исполнен вход ${fill_price:.6f}, qty={actual_qty}")

    dca_levels = calc_dca_levels(fill_price, actual_qty)
    final_sl = calc_final_sl(fill_price, actual_qty)
    tp = calc_tp(0, fill_price)

    # SL/TP conditional на РЕАЛЬНЫЙ size (actual_qty)
    # После каждого DCA бот переставит на новый размер
    sl_oid = set_stop_loss_order(symbol, actual_qty, final_sl, order_link_id=f"SL-{symbol}-{int(ts)}")
    if sl_oid:
        log.info(f"[{symbol}] SL conditional ${final_sl:.6f} qty={actual_qty} orderId={sl_oid[:12]}...")
    else:
        log.error(f"[{symbol}] SL не поставился! Позиция без защиты!")

    tp_oid = _set_tp_order(symbol, actual_qty, tp, order_link_id=f"TP-{symbol}-{int(ts)}")
    if tp_oid:
        log.info(f"[{symbol}] TP conditional ${tp:.6f} qty={actual_qty} orderId={tp_oid[:12]}...")

    # DCA лимитки — проверка дублей
    existing = {float(o.get("price", 0)): o["orderId"] for o in get_open_orders(symbol)
                if o.get("orderType") == "Limit" and float(o.get("price", 0)) > 0}
    dca_order_ids = {}
    for lvl in dca_levels:
        dca_qty = actual_qty * DCA_MULT
        # Проверка дубля — если уже стоит лимитка по этой цене, не ставим
        already = False
        for ex_price in existing:
            if abs(ex_price - lvl["fill_price"]) < inst["tickSize"]:
                dca_order_ids[lvl["level"]] = existing[ex_price]
                already = True
                break
        if not already:
            oid = place_dca_limit(symbol, dca_qty, lvl["fill_price"])
            if oid:
                dca_order_ids[lvl["level"]] = oid
                log.info(f"[{symbol}] DCA×{lvl['level']} ${lvl['fill_price']:.6f} orderId={oid[:12]}...")

    position = {
        "id": f"{symbol}_{int(ts)}",
        "symbol": symbol,
        "side": "short",
        "entry_price": fill_price,
        "qty": actual_qty,
        "original_qty": actual_qty,
        "notional": actual_qty * fill_price,
        "entry_ts": ts,
        "original_entry": fill_price,
        "sl_price": final_sl,
        "sl_order_id": sl_oid,
        "tp_price": tp,
        "tp_order_id": tp_oid,
        "dca_count": 0,
        "dca_max_count": DCA_MAX,
        "dca_order_ids": dca_order_ids,
        "dca_levels": dca_levels,
        "final_sl_set": False,
        "sl_synced": True,  # SL уже стоит, не пересчитывать каждый раз
        "open_day": time.strftime("%Y-%m-%d", time.gmtime(ts)),
        "live": True,
    }
    return position


def _set_tp_order(symbol, qty, trigger_price, order_link_id=""):
    """TP conditional order — Buy при падении (triggerDirection=2)."""
    inst = get_instrument(symbol)
    tick = inst["tickSize"] if inst else 0.0001
    trigger_price = round(trigger_price / tick) * tick
    body = {
        "category": "linear", "symbol": symbol, "side": "Buy",
        "orderType": "Market", "qty": str(qty), "reduceOnly": True,
        "triggerPrice": str(trigger_price), "triggerBy": "LastPrice",
        "triggerDirection": "2",  # fallingPrice — для шорта TP
        "timeInForce": "GTC", "positionIdx": 0,
    }
    if order_link_id:
        body["orderLinkId"] = order_link_id
    from executor import _post
    d = _post("/v5/order/create", body)
    if d.get("retCode") != 0:
        log.error(f"[TP order] {d.get('retMsg')} code={d.get('retCode')}")
        return None
    return d["result"].get("orderId")


def restore_positions_from_exchange(existing_symbols):
    """
    Автовосстановление позиций с биржи при старте.
    Если на Bybit есть позиция, а в existing_symbols её нет — подхватываем.
    Возвращает list восстановленных позиций.
    """
    import hashlib, hmac, json, time, urllib.request
    api_file = os.path.expanduser("~/.bybit_api")
    if not os.path.exists(api_file):
        api_file = "/root/.bybit_api"
    if not os.path.exists(api_file):
        return []
    key, secret = open(api_file).read().strip().split("\n")
    ts = str(int(time.time() * 1000)); recv = "5000"
    params = "category=linear&settleCoin=USDT"
    q = f"{ts}{key}{recv}{params}"
    sign = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.bybit.com/v5/position/list?{params}"
    req = urllib.request.Request(url, headers={
        "X-BAPI-SIGN-TYPE": "SHA256", "X-BAPI-SIGN": sign,
        "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv,
    })
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        log.error(f"[restore] API ошибка: {e}")
        return []

    restored = []
    for p in d.get("result", {}).get("list", []):
        if float(p["size"]) == 0:
            continue
        sym = p["symbol"]
        if sym in existing_symbols:
            continue  # уже есть в state

        avg = float(p["avgPrice"])
        size = float(p["size"])
        # Определить сколько DCA исполнено: ratio = size / original_qty
        # original_qty = size / (dca_count + 1). Если size = orig → 0, 2×orig → 1...
        # Нужно найти orig_qty. Если позиция без DCA — orig = size.
        # Эвристика: считаем что DCA×N исполнен если size кратен с допуском
        # Проще: dca_count = round(size/orig - 1), но orig неизвестен.
        # Берём из DCA лимиток — qty лимитки = orig_qty
        orders = get_open_orders(sym)
        orig_qty = size
        dca_count = 0
        for o in orders:
            if o.get("orderType") == "Limit":
                lq = float(o.get("qty", 0))
                if lq > 0 and lq < size:
                    orig_qty = lq
                    dca_count = round(size / orig_qty) - 1
                    if dca_count < 0:
                        dca_count = 0
                    break

        final_sl = calc_final_sl(avg, orig_qty)
        tp = calc_tp(dca_count, avg)

        # Найти SL/TP order IDs
        sl_oid = tp_oid = None
        dca_order_ids = {}
        levels = calc_dca_levels(avg, orig_qty)
        for o in orders:
            if o.get("stopOrderType") == "Stop":
                dir_ = str(o.get("triggerDirection", "0"))
                if dir_ == "1":
                    sl_oid = o["orderId"]
                elif dir_ == "2":
                    tp_oid = o["orderId"]
            elif o.get("orderType") == "Limit":
                op = float(o.get("price", 0))
                for lvl in levels:
                    if lvl["level"] > dca_count and abs(op - lvl["fill_price"]) < avg * 0.002:
                        dca_order_ids[lvl["level"]] = o["orderId"]

        pos = {
            "id": f"{sym}_restore_{int(time.time())}",
            "symbol": sym, "side": "short",
            "entry_price": avg, "qty": size, "original_qty": orig_qty,
            "notional": size * avg, "entry_ts": time.time(),
            "original_entry": avg,
            "sl_price": final_sl, "sl_order_id": sl_oid,
            "tp_price": tp, "tp_order_id": tp_oid,
            "dca_count": dca_count, "dca_max_count": DCA_MAX,
            "dca_order_ids": dca_order_ids,
            "dca_levels": levels, "final_sl_set": dca_count >= DCA_MAX,
            "sl_synced": True, "open_day": time.strftime("%Y-%m-%d", time.gmtime()),
            "live": True,
        }
        restored.append(pos)
        log.info(f"[restore] подхватил {sym} size={size} dca×{dca_count} SL=${final_sl:.6f}")

    return restored


def sync_position(pos):
    """
    Синхронизация с биржей:
      - проверить закрытие (TP/SL/trail)
      - проверить исполнение DCA (по размеру позиции!)
      - после DCA: поднять TP, НЕ ТРОГАТЬ SL (он conditional, стоит)
      - на DCA×4-5: trail
    """
    sym = pos["symbol"]

    # ВАЖНО: при старте get_position может вернуть None (API задержка)
    # Делаем 2 попытки с задержкой
    pos_info = get_position(sym)
    if not pos_info:
        time.sleep(2)
        pos_info = get_position(sym)
    if not pos_info or pos_info["size"] == 0:
        # Только теперь — позиция реально закрыта
        # Но НЕ отменяем всё сразу — проверим ещё раз через 3 сек
        time.sleep(3)
        pos_info = get_position(sym)
        if not pos_info or pos_info["size"] == 0:
            log.info(f"[{sym}] позиция закрыта (size=0) — отмена ордеров")
            cancel_all(sym)
            return "closed"
        # позиция вернулась — был API глюк
        log.warning(f"[{sym}] API глюк — позиция вернулась, не отменяем")

    # Позиция жива — проверяем DCA по РАЗМЕРУ позиции
    current_qty = pos_info["size"]
    original_qty = pos["original_qty"]
    current_avg = pos_info["avgPrice"]

    if current_qty <= original_qty * 1.01:
        # DCA не было — ничего не делаем, SL/TP стоят
        return "open"

    # DCA исполнился — считаем сколько
    ratio = current_qty / original_qty
    real_dca_count = int(round(ratio) - 1)
    if real_dca_count <= pos["dca_count"]:
        return "open"  # уже обработано

    old_count = pos["dca_count"]
    pos["dca_count"] = real_dca_count
    pos["entry_price"] = current_avg
    pos["qty"] = current_qty
    log.info(f"[{sym}] DCA исполнен ×{old_count}→×{real_dca_count}, avg=${current_avg:.6f}")

    # Поднять TP (conditional — отменить старый, поставить новый на РЕАЛЬНЫЙ size)
    new_tp = calc_tp(real_dca_count, current_avg)
    if pos.get("tp_order_id"):
        cancel_order(sym, pos["tp_order_id"])
    if pos.get("sl_order_id"):
        cancel_order(sym, pos["sl_order_id"])
    # На DCA×4-5 — trail вместо TP
    if real_dca_count >= TRAIL_AT_DCA and TRAIL_PCT > 0:
        # trail через trading-stop (position-level для trail ок, он сам закроет)
        trail_dist = current_avg * TRAIL_PCT / 100
        active_price = current_avg * (1 - TRAIL_ACTIVE_PCT / 100)
        set_trading_stop(sym, take_profit=0, stop_loss=0,
                         trailing_stop=trail_dist, active_price=active_price)
        log.info(f"[{sym}] trail {TRAIL_PCT}% active=${active_price:.6f}")
        pos["tp_order_id"] = None
    else:
        tp_oid = _set_tp_order(sym, current_qty, new_tp, order_link_id=f"TP-{sym}-{int(time.time())}")
        pos["tp_order_id"] = tp_oid
        log.info(f"[{sym}] TP поднят ${new_tp:.6f} qty={current_qty}")

    # Переставить SL на новый размер (та же цена, новый qty)
    # SL цена = финальный, не меняется до DCA×5
    sl_price = pos["sl_price"]
    if real_dca_count < DCA_MAX:
        new_sl_oid = set_stop_loss_order(sym, current_qty, sl_price,
                                          order_link_id=f"SL-{sym}-{int(time.time())}")
        pos["sl_order_id"] = new_sl_oid
        log.info(f"[{sym}] SL переставлен ${sl_price:.6f} qty={current_qty}")

    # SL НЕ ТРОГАЕМ — conditional order стоит, не двигаем
    pos["tp_price"] = new_tp

    # После DCA×5 — уточнить SL (отменить старый, поставить новый на РЕАЛЬНЫЙ size)
    if real_dca_count >= DCA_MAX and not pos.get("final_sl_set"):
        real_sl = current_avg * (1 + SL_PCT / 100)
        if pos.get("sl_order_id"):
            cancel_order(sym, pos["sl_order_id"])
        new_sl_oid = set_stop_loss_order(sym, current_qty, real_sl,
                                          order_link_id=f"SL-{sym}-final-{int(time.time())}")
        pos["sl_order_id"] = new_sl_oid
        pos["sl_price"] = real_sl
        pos["final_sl_set"] = True
        log.info(f"[{sym}] финальный SL уточнён ${real_sl:.6f} qty={current_qty}")

    # Переставить оставшиеся DCA лимитки (если нужно)
    remaining = calc_dca_levels(current_avg, original_qty)
    existing_prices = {float(o.get("price", 0)) for o in get_open_orders(sym)
                       if o.get("orderType") == "Limit" and float(o.get("price", 0)) > 0}
    for lvl in remaining:
        if lvl["level"] > real_dca_count:
            # Проверить — стоит ли уже лимитка близко
            close = any(abs(p - lvl["fill_price"]) < current_avg * 0.001 for p in existing_prices)
            if not close:
                dca_qty = original_qty * DCA_MULT
                oid = place_dca_limit(sym, dca_qty, lvl["fill_price"])
                if oid:
                    pos["dca_order_ids"][lvl["level"]] = oid
                    log.info(f"[{sym}] DCA×{lvl['level']} переставлена ${lvl['fill_price']:.6f}")

    return "open"


def close_position_manual(sym):
    """Принудительно закрыть позицию + отменить всё."""
    pos_info = get_position(sym)
    if pos_info and pos_info["size"] > 0:
        cancel_all(sym)
        oid = close_market(sym, pos_info["size"])
        log.info(f"[{sym}] принудительное закрытие orderId={oid}")
        return oid
    return None
