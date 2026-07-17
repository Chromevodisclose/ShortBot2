#!/usr/bin/env python3
"""
Bybit V5 executor — реальные ордера на бирже.

Функции:
  - place_market_short(symbol, qty) → вход в шорт
  - set_trading_stop(symbol, sl, tp, trailing, active) → SL/TP/Trail на позицию
  - place_dca_limit(symbol, qty, price) → лимитка DCA (buy — докупка шорта)
  - cancel_order(symbol, order_id) → отмена
  - cancel_all(symbol) → отмена всех ордеров по символу
  - get_open_orders(symbol) → открытые ордера
  - get_position(symbol) → реальная позиция
  - get_balance() → баланс USDT
  - get_instrument(symbol) → minQty, qtyStep, minNotional, tickSize

SL/TP/trail — position-level (через /v5/position/trading-stop):
  - биржа сама исполняет, не зависит от бота
  - при закрытии позиции ордера снимаются автоматически
  - при вызове с новыми ценами — старые заменяются

DCA лимитки — обычные ордера (через /v5/order/create):
  - ставим по одной за раз или все сразу
  - при TP/SL нужно отменять неисполненные
"""
import hashlib, hmac, json, time, urllib.request, urllib.error
from typing import Optional

# ── Ключи из ~/.bybit_api (работает и локально и на VPS) ──
import os as _os
_api_file = _os.path.expanduser("~/.bybit_api")
if not _os.path.exists(_api_file):
    # fallback для VPS где home = /root
    _api_file = "/root/.bybit_api"
_api_key, _api_secret = open(_api_file).read().strip().split("\n")
_BASE = "https://api.bybit.com"


def _sign(params: str, body: str = "") -> dict:
    """Подписать запрос. params — query string для GET, body — JSON для POST."""
    ts = str(int(time.time() * 1000))
    recv = "5000"
    payload = f"{ts}{_api_key}{recv}{params}{body}"
    sign = hmac.new(_api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-SIGN-TYPE": "SHA256",
        "X-BAPI-SIGN": sign,
        "X-BAPI-API-KEY": _api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }


def _get(path: str, params: str = "") -> dict:
    """GET запрос к Bybit V5."""
    url = f"{_BASE}{path}?{params}" if params else f"{_BASE}{path}"
    headers = _sign(params)
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"retCode": e.code, "retMsg": f"HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}


def _post(path: str, body: dict) -> dict:
    """POST запрос к Bybit V5."""
    body_str = json.dumps(body, separators=(",", ":"))
    headers = _sign("", body_str)
    url = f"{_BASE}{path}"
    req = urllib.request.Request(url, data=body_str.encode(), headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"retCode": e.code, "retMsg": f"HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}


# ═══════════════════════════════════════════
# ЧТЕНИЕ СОСТОЯНИЯ
# ═══════════════════════════════════════════

def get_balance() -> Optional[float]:
    """Баланс USDT на Unified аккаунте."""
    d = _get("/v5/account/wallet-balance", "accountType=UNIFIED&coin=USDT")
    if d.get("retCode") != 0:
        print(f"[get_balance] {d.get('retMsg')}")
        return None
    coins = d["result"]["list"][0].get("coin", [])
    if coins:
        return float(coins[0].get("walletBalance", 0))
    return 0.0


def get_position(symbol: str) -> Optional[dict]:
    """Реальная позиция по символу. Возвращает {size, side, avgPrice, takeProfit, stopLoss} или None."""
    d = _get("/v5/position/list", f"category=linear&symbol={symbol}")
    if d.get("retCode") != 0:
        print(f"[get_position] {d.get('retMsg')}")
        return None
    for p in d["result"]["list"]:
        if float(p["size"]) > 0:
            return {
                "symbol": p["symbol"],
                "size": float(p["size"]),
                "side": p["side"],
                "avgPrice": float(p.get("avgPrice", 0)),
                "takeProfit": p.get("takeProfit", ""),
                "stopLoss": p.get("stopLoss", ""),
                "trailingStop": p.get("trailingStop", ""),
                "unrealisedPnl": float(p.get("unrealisedPnl", 0)),
                "leverage": p.get("leverage", ""),
            }
    return None


def get_open_orders(symbol: str) -> list:
    """Открытые ордера по символу (не исполненные)."""
    d = _get("/v5/order/realtime", f"category=linear&symbol={symbol}")
    if d.get("retCode") != 0:
        print(f"[get_open_orders] {d.get('retMsg')}")
        return []
    return d["result"]["list"]


def get_instrument(symbol: str) -> Optional[dict]:
    """Параметры инструмента: minQty, qtyStep, minNotional, tickSize, maxLev."""
    d = _get("/v5/market/instruments-info", f"category=linear&symbol={symbol}")
    if d.get("retCode") != 0 or not d["result"]["list"]:
        return None
    li = d["result"]["list"][0]
    lot = li["lotSizeFilter"]
    price = li["priceFilter"]
    return {
        "symbol": symbol,
        "minQty": float(lot["minOrderQty"]),
        "qtyStep": float(lot["qtyStep"]),
        "minNotional": float(lot["minNotionalValue"]),
        "tickSize": float(price["tickSize"]),
        "maxLeverage": float(li["leverageFilter"]["maxLeverage"]),
        "priceScale": int(li["priceScale"]),
    }


def get_ticker(symbol: str) -> Optional[float]:
    """Текущая цена (lastPrice)."""
    d = _get("/v5/market/tickers", f"category=linear&symbol={symbol}")
    if d.get("retCode") != 0 or not d["result"]["list"]:
        return None
    return float(d["result"]["list"][0]["lastPrice"])


# ═══════════════════════════════════════════
# УСТАНОВКА ОРДЕРОВ
# ═══════════════════════════════════════════

def place_market_short(symbol: str, qty: float, leverage: float = 0) -> Optional[str]:
    """
    Открыть шорт маркет-ордером.
    qty — количество монет.
    leverage — если > 0, сначала выставить плечо.
    Возвращает orderId или None.
    """
    if leverage > 0:
        set_leverage(symbol, leverage)

    inst = get_instrument(symbol)
    if not inst:
        return None
    qty = round(qty / inst["qtyStep"]) * inst["qtyStep"]
    if qty < inst["minQty"]:
        print(f"[place_market_short] qty {qty} < minQty {inst['minQty']}")
        return None

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",            # Sell = шорт
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "positionIdx": 0,          # one-way mode
        "reduceOnly": False,
    }
    d = _post("/v5/order/create", body)
    if d.get("retCode") != 0:
        print(f"[place_market_short] {d.get('retMsg')} code={d.get('retCode')}")
        return None
    return d["result"].get("orderId")


def place_dca_limit(symbol: str, qty: float, price: float) -> Optional[str]:
    """
    Лимитный ордер на докупку шорта (Buy — закрыть часть / Sell — открыть ещё шорт).
    DCA при росте → докупаем шорт = Sell limit по цене выше текущей.
    Возвращает orderId или None.
    """
    inst = get_instrument(symbol)
    if not inst:
        return None
    qty = round(qty / inst["qtyStep"]) * inst["qtyStep"]
    if qty < inst["minQty"]:
        print(f"[place_dca_limit] qty {qty} < minQty {inst['minQty']}")
        return None
    tick = inst["tickSize"]
    price = round(price / tick) * tick

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",            # шорт = Sell, докупка шорта тоже Sell
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC",      # Good Till Cancelled
        "positionIdx": 0,
        "reduceOnly": False,
    }
    d = _post("/v5/order/create", body)
    if d.get("retCode") != 0:
        print(f"[place_dca_limit] {d.get('retMsg')} code={d.get('retCode')}")
        return None
    return d["result"].get("orderId")


def set_stop_loss_order(symbol, qty, trigger_price, order_link_id=""):
    """
    Поставить SL как отдельный conditional order (не position-level).
    triggerDirection=1 (risingPrice) — для шорта (закрытие при росте цены).
    Этот ордер можно двигать через modify (в отличие от position SL).
    Возвращает orderId или None.
    """
    inst = get_instrument(symbol)
    tick = inst["tickSize"] if inst else 0.0001
    trigger_price = round(trigger_price / tick) * tick

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy",            # закрыть шорт = Buy
        "orderType": "Market",
        "qty": str(qty),
        "reduceOnly": True,
        "triggerPrice": str(trigger_price),
        "triggerBy": "LastPrice",
        "triggerDirection": "1",  # risingPrice
        "timeInForce": "GTC",
        "positionIdx": 0,
    }
    if order_link_id:
        body["orderLinkId"] = order_link_id

    d = _post("/v5/order/create", body)
    if d.get("retCode") != 0:
        print(f"[set_stop_loss_order] {d.get('retMsg')} code={d.get('retCode')}")
        return None
    return d["result"].get("orderId")


def cancel_stop_loss_order(symbol, order_link_id=""):
    """Отменить SL conditional order по orderLinkId."""
    body = {
        "category": "linear",
        "symbol": symbol,
    }
    if order_link_id:
        body["orderLinkId"] = order_link_id
    d = _post("/v5/order/cancel", body)
    return d.get("retCode") == 0


def get_stop_loss_orders(symbol):
    """Найти SL conditional orders по символу (через orderLinkId или stopOrderType)."""
    d = _get("/v5/order/realtime", f"category=linear&symbol={symbol}")
    if d.get("retCode") != 0:
        return []
    result = []
    for o in d["result"]["list"]:
        # SL conditional: stopOrderType=Stop или triggerDirection=1 + reduceOnly
        if o.get("stopOrderType") in ("Stop", "StopLoss") or (
            o.get("triggerDirection") == "1" and o.get("reduceOnly") and o.get("orderType") == "Market"
        ):
            result.append({
                "orderId": o["orderId"],
                "orderLinkId": o.get("orderLinkId", ""),
                "triggerPrice": float(o.get("triggerPrice", 0)),
                "qty": float(o.get("qty", 0)),
            })
    return result


def set_trading_stop(
    symbol: str,
    take_profit: float = 0,
    stop_loss: float = 0,
    trailing_stop: float = 0,
    active_price: float = 0,
    tpsl_mode: str = "Full",
) -> bool:
    """
    Установить TP/SL/Trailing на ПОЗИЦИЮ (не ордер).
    0 = отменить соответствующий ордер.
    trailing_stop — РАССТОЯНИЕ в цене (не %).
    active_price — цена активации trailing.
    Биржа сама исполнит, не зависит от бота.
    При вызове с новыми ценами — старые заменяются.
    """
    inst = get_instrument(symbol)
    tick = inst["tickSize"] if inst else 0.0001

    body = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": tpsl_mode,
        "positionIdx": 0,
    }
    if take_profit > 0:
        body["takeProfit"] = str(round(take_profit / tick) * tick)
        body["tpTriggerBy"] = "LastPrice"
    else:
        body["takeProfit"] = "0"
    if stop_loss > 0:
        body["stopLoss"] = str(round(stop_loss / tick) * tick)
        body["slTriggerBy"] = "LastPrice"
    else:
        body["stopLoss"] = "0"
    if trailing_stop > 0:
        body["trailingStop"] = str(round(trailing_stop / tick) * tick)
    else:
        body["trailingStop"] = "0"
    if active_price > 0:
        body["activePrice"] = str(round(active_price / tick) * tick)

    d = _post("/v5/position/trading-stop", body)
    if d.get("retCode") != 0:
        print(f"[set_trading_stop] {d.get('retMsg')} code={d.get('retCode')}")
        return False
    return True


def cancel_order(symbol: str, order_id: str) -> bool:
    """Отменить ордер по ID."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "orderId": order_id,
    }
    d = _post("/v5/order/cancel", body)
    if d.get("retCode") != 0:
        print(f"[cancel_order] {d.get('retMsg')}")
        return False
    return True


def cancel_all(symbol: str) -> int:
    """Отменить все открытые ордера по символу. Возвращает кол-во отменённых."""
    orders = get_open_orders(symbol)
    cancelled = 0
    for o in orders:
        if cancel_order(symbol, o["orderId"]):
            cancelled += 1
    return cancelled


def close_market(symbol: str, qty: float) -> Optional[str]:
    """Закрыть позицию маркет-ордером (Buy для шорта)."""
    inst = get_instrument(symbol)
    if not inst:
        return None
    qty = round(qty / inst["qtyStep"]) * inst["qtyStep"]

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy",            # закрыть шорт = Buy
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "positionIdx": 0,
        "reduceOnly": True,
    }
    d = _post("/v5/order/create", body)
    if d.get("retCode") != 0:
        print(f"[close_market] {d.get('retMsg')} code={d.get('retCode')}")
        return None
    return d["result"].get("orderId")


def set_leverage(symbol: str, leverage: float) -> bool:
    """Установить плечо для символа."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    d = _post("/v5/position/set-leverage", body)
    if d.get("retCode") != 0:
        # ig-5001: leverage not modified — норма
        if d.get("retCode") != 110043:
            print(f"[set_leverage] {d.get('retMsg')}")
        return False
    return True


# ═══════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import sys

    print("=== Bybit Executor Self-Test ===\n")

    # 1. Баланс
    bal = get_balance()
    print(f"1. Баланс USDT: ${bal}")

    # 2. Инструмент
    sym = "EPICUSDT"
    inst = get_instrument(sym)
    print(f"2. {sym}: minQty={inst['minQty']} qtyStep={inst['qtyStep']} "
          f"minNotional=${inst['minNotional']} tickSize={inst['tickSize']}")

    # 3. Тикер
    price = get_ticker(sym)
    print(f"3. {sym} цена: ${price}")

    # 4. Позиция
    pos = get_position(sym)
    print(f"4. Позиция {sym}: {pos}")

    # 5. Открытые ордера
    orders = get_open_orders(sym)
    print(f"5. Открытые ордера {sym}: {len(orders)}")

    print("\n✅ Executor готов. Баланс $0 — пополните для тестов.")
    print("Тестовые команды:")
    print(f"  python3 {sys.argv[0]} test   # микро-тест ордеров (нужен баланс)")
