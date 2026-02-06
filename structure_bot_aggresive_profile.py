import time
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict
import math

# ================ Binance для свечей ================
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException

# ================ BingX клиент ================
from bingx_client import BingxClient

# ================== CONFIG ==================
BINANCE_API_KEY = ""        # я подтягиваю с бинанса свечи здесь, можно оставлять пустыми! это из публичного API 
BINANCE_API_SECRET = ""

BINGX_API_KEY = ""
BINGX_API_SECRET = ""

SYMBOL_BINANCE = "LTCUSDT"                  # без слеша
SYMBOL_BINGX = "LTC-USDT"                   # с дефисом
TIMEFRAME = "5m"
LOOKBACK = 300
FRACTAL_N = 2                               # 5-барный фрактал

TODAY_DIRECTION = "long"                    # "long", "short" - ПОДТЯНУТЬ ИЗ МАРКЕТ КЛИМАТА
LEVERAGE = 5
POSITION_USDT = 50

# Минимальное расстояние для BOS и новых экстремумов
MIN_PRICE_MOVE = 0.0001

# Параметры для вычисления экстремумов при установке SL/TP
SWING_EXTREMA_LOOKBACK = 8   # сколько последних свингов учитывать при поиске экстремума
CANDLE_EXTREMA_LOOKBACK = 20 # запас: если мало свингов, берём максимум/минимум среди последних свечей

# минимальное расстояние SL/TP от цены (в пунктах или процент)
MIN_SL_DISTANCE_PCT = 0.002  # 0.2%
MIN_TP_DISTANCE_PCT = 0.002  # 0.2%

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("BingX_StructBot")

# ================ Clients =================
binance = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)
bingx = BingxClient(BINGX_API_KEY, BINGX_API_SECRET, symbol=SYMBOL_BINGX)

# ================ Data classes =================
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class Swing:
    index: int
    type: str   # "high" или "low"
    price: float
    time: int

def get_bingx_position(symbol: str):
    """
    Возвращает:
        None — если позиции нет
        {"side": "long"/"short", "qty": float}
    """
    data = bingx.is_position_open(symbol)

    if not data or data.get("code") != 0:
        return None
    
    pos_list = data.get("data", [])
    if not pos_list:
        return None

    pos = pos_list[0]
    qty = float(pos["positionAmt"])

    if qty == 0:
        return None

    side = pos["positionSide"].lower()  # LONG / SHORT

    if side == "long":
        return {"side": "long", "qty": qty}
    if side == "short":
        return {"side": "short", "qty": qty}

    return None

# ================ Binance candles =================
def get_binance_klines() -> List[Candle]:
    try:
        raw = binance.get_klines(
            symbol=SYMBOL_BINANCE,
            interval=TIMEFRAME,
            limit=LOOKBACK
        )
        return [
            Candle(
                open_time=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5])
            ) for k in raw
        ]
    except BinanceAPIException as e:
        log.error(f"Binance error: {e}")
        return []

# ================ Fractals =================
def detect_fractals(candles: List[Candle], n: int = 2) -> List[Swing]:
    swings = []
    for i in range(n, len(candles) - n):
        c = candles[i]
        left = candles[i-n:i]
        right = candles[i+1:i+n+1]

        is_high = all(c.high >= x.high for x in left + right)
        is_low = all(c.low <= x.low for x in left + right)

        if is_high:
            swings.append(Swing(i, "high", c.high, c.open_time))
        if is_low:
            swings.append(Swing(i, "low", c.low, c.open_time))
    return swings

# ================ Structure & BOS =================
def get_structure(swings: List[Swing]) -> str:
    if len(swings) < 4:
        return "undefined"

    recent = swings[-6:]
    highs = [s for s in recent if s.type == "high"]
    lows = [s for s in recent if s.type == "low"]

    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
            return "long"
        if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
            return "short"
    return "flat"

# Последние свинги
def get_last_swing(swings: List[Swing], type_: str) -> Optional[Swing]:
    for s in reversed(swings):
        if s.type == type_:
            return s
    return None

# ================ Helpers для экстремумов SL/TP =================
def compute_extrema_for_sl_tp(swings: List[Swing], candles: List[Candle]):
    """
    Возвращает (extremum_high, extremum_low) — числа или None.
    Берём максимум/минимум среди последних SWING_EXTREMA_LOOKBACK свингов; если свингов мало - используем свечи.
    """
    recent_swings = swings[-SWING_EXTREMA_LOOKBACK:]
    highs = [s.price for s in recent_swings if s.type == "high"]
    lows = [s.price for s in recent_swings if s.type == "low"]

    if highs and lows:
        return max(highs), min(lows)

    # fallback на свечи
    recent_candles = candles[-CANDLE_EXTREMA_LOOKBACK:]
    highs = [c.high for c in recent_candles]
    lows = [c.low for c in recent_candles]
    return (max(highs) if highs else None, min(lows) if lows else None)

# ================ BingX helpers =================
def get_mark_price() -> float:
    price = bingx.get_mark_price(SYMBOL_BINGX)
    if not price:
        raise ValueError("Не удалось получить mark price с BingX")
    return float(price)

def calculate_qty(usdt: float, price: float) -> float:
    qty = (usdt * LEVERAGE) / price
    # округляем с запасом: точность 3 знака — настройте под пару
    return round(qty, 3)

def enforce_min_distance(price: float, target: float, min_pct: float):
    """
    Убедиться, что расстояние между price и target >= min_pct*price.
    Если меньше — отодвинуть target дальше.
    """
    min_dist = price * min_pct
    if price < target:
        # target выше price (TP для лонга или SL для шорта)
        if (target - price) < min_dist:
            return price + min_dist
    else:
        # target ниже price (SL для лонга или TP для шорта)
        if (price - target) < min_dist:
            return price - min_dist
    return target

# ================ State =================
class TradingState:
    def __init__(self):
        self.position: Optional[Dict] = None  # None или {"side": "long"/"short", "entry": float, "qty": float, "sl": float, "tp": float}

state = TradingState()

# ================ Order helper (использует stop/tp если есть) =================
def place_entry_with_sl_tp(side: str, qty: float, sl_price: float, tp_price: float, symbol: str):
    """
    Пробуем создать ордер с полями stop и tp. Если API не поддерживает — логируем и возвращаем None.
    Адаптируйте под ваш bingx sdk: возможно есть отдельный метод для OCO/TP/SL.
    """
    try:
        resp = bingx.place_market_order(
            side=side,
            qty=qty,
            symbol=symbol,
            stop=sl_price,
            tp=tp_price
        )
        return resp
    except Exception as e:
        log.exception(f"Ошибка при выставлении ордера с SL/TP: {e}")
        # попытка fallback: выставить market и отдельно лог о том, что SL/TP не поставлены
        try:
            resp_market = bingx.place_market_order(side=side, qty=qty, symbol=symbol)
            log.warning("Fallback: выставлен market ордер без SL/TP. Нужно вручную поставить SL/TP через API.")
            return resp_market
        except Exception as e2:
            log.exception(f"Не удалось даже выставить market при fallback: {e2}")
            return None

# ================ Main Logic =================
def run_strategy():
    candles = get_binance_klines()
    if len(candles) < 50:
        log.warning("Недостаточно свечей")
        return

    swings = detect_fractals(candles, FRACTAL_N)
    if len(swings) < 2:
        log.info("Недостаточно свингов")
        return

    structure = get_structure(swings)
    last_candle = candles[-1]
    current_price = get_mark_price()

    last_high = get_last_swing(swings, "high")
    last_low = get_last_swing(swings, "low")

    # Фактический статус позиции на бирже
    pos = get_bingx_position(SYMBOL_BINGX)
    in_position = pos is not None

    log.info(f"Структура: {structure} | Цена: {current_price:.6f} | Биржевая позиция: {pos}")

    # === BOS: закрытие позиции, если структура ломается ===
    bos_down = last_low and last_candle.close < last_low.price - MIN_PRICE_MOVE
    bos_up = last_high and last_candle.close > last_high.price + MIN_PRICE_MOVE

    if pos:
        side = pos["side"]
        qty = pos["qty"]

        if (side == "long" and bos_down) or (side == "short" and bos_up):
            log.info("BOS! Немедленное закрытие позиции маркетом.")

            try:
                close_side = "short" if side == "long" else "long"
                bingx.place_market_order(
                    side=close_side,
                    qty=qty,
                    symbol=SYMBOL_BINGX
                )
            except Exception:
                log.exception("Ошибка закрытия по BOS")

            return  # ждём следующую свечу

    # === Если направление дня не совпадает со структурой — не торгуем
    if TODAY_DIRECTION != structure:
        log.info("Сегодняшнее направление не совпадает со структурой")
        return

    # === Вход только если действительно НЕТ позиции на бирже
    if in_position:
        return  # биржа говорит, что сделка открыта — новых не ставим

    # === Рассчёт экстремумов
    extrema_high, extrema_low = compute_extrema_for_sl_tp(swings, candles)
    if extrema_high is None or extrema_low is None:
        log.warning("Не удалось вычислить экстремумы")
        return

    # ===================== ВХОД LONG =====================
    if structure == "long" and last_high and last_low and last_high.index > last_low.index:

        impulse = last_high.price - last_low.price
        retrace = last_high.price - impulse * 0.5

        if current_price <= retrace:

            qty = calculate_qty(POSITION_USDT, current_price)

            sl_price = enforce_min_distance(current_price, extrema_low * 0.999, MIN_SL_DISTANCE_PCT)
            tp_price = enforce_min_distance(current_price, extrema_high * 1.001, MIN_TP_DISTANCE_PCT)

            log.info(f"ВХОД LONG: SL={sl_price:.4f} TP={tp_price:.4f} QTY={qty}")

            resp = place_entry_with_sl_tp(
                side="long",
                qty=qty,
                sl_price=sl_price,
                tp_price=tp_price,
                symbol=SYMBOL_BINGX
            )

            if resp and resp.get("code") == 0:
                log.info("Лонг открыт на бирже")
            else:
                log.error(f"Ошибка входа LONG: {resp}")

    # ===================== ВХОД SHORT =====================
    elif structure == "short" and last_low and last_high and last_low.index > last_high.index:

        impulse = last_high.price - last_low.price
        retrace = last_low.price + impulse * 0.5

        if current_price >= retrace:

            qty = calculate_qty(POSITION_USDT, current_price)

            sl_price = enforce_min_distance(current_price, extrema_high * 1.001, MIN_SL_DISTANCE_PCT)
            tp_price = enforce_min_distance(current_price, extrema_low * 0.999, MIN_TP_DISTANCE_PCT)

            log.info(f"ВХОД SHORT: SL={sl_price:.4f} TP={tp_price:.4f} QTY={qty}")

            resp = place_entry_with_sl_tp(
                side="short",
                qty=qty,
                sl_price=sl_price,
                tp_price=tp_price,
                symbol=SYMBOL_BINGX
            )

            if resp and resp.get("code") == 0:
                log.info("Шорт открыт на бирже")
            else:
                log.error(f"Ошибка входа SHORT: {resp}")

# ================ Runner =================
if __name__ == "__main__":
    log.info("Запуск BingX + Binance Structure Bot (SL/TP при входе)")
    log.info(f"Символ: {SYMBOL_BINGX} | Таймфрейм: {TIMEFRAME} | Направление дня: {TODAY_DIRECTION}")

    while True:
        try:
            run_strategy()
        except Exception as e:
            log.exception(f"Критическая ошибка: {e}")
        time.sleep(60)  # можно уменьшить, если хотите чаще проверять