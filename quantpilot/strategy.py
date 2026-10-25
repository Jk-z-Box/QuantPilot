from __future__ import annotations

import math


def ema(values, period):
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for current in values[period:]:
        value += alpha * (current - value)
    return value


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:], strict=False)]
    gain = sum(max(v, 0) for v in changes[:period]) / period
    loss = sum(max(-v, 0) for v in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 100 - 100 / (1 + gain / loss) if loss else (100 if gain else 50)


def analyze(rows, timeframe_seconds):
    if len(rows) < 60:
        raise ValueError("insufficient_candles")
    closes = [r[4] for r in rows]
    if not all(math.isfinite(v) and v > 0 for v in closes):
        raise ValueError("invalid_candle")
    fast, slow, momentum = ema(closes, 20), ema(closes, 50), rsi(closes)
    tr = [max(row[2] - row[3], abs(row[2] - previous[4]), abs(row[3] - previous[4]))
          for previous, row in zip(rows, rows[1:], strict=False)]
    atr = sum(tr[-14:]) / 14
    average_volume = sum(r[5] for r in rows[-21:-1]) / 20
    volume_ratio = rows[-1][5] / average_volume if average_volume else 0
    action = "hold"
    reason = "趋势或动量未满足入场条件"
    if fast > slow and 45 <= momentum <= 70 and volume_ratio >= .8:
        action, reason = "buy", "EMA20 高于 EMA50，RSI 处于 45–70，成交量满足阈值"
    elif fast < slow:
        action, reason = "sell", "EMA20 低于 EMA50，退出已有多头仓位"
    return {"action": action, "reason": reason, "ema20": fast, "ema50": slow,
            "rsi14": momentum, "atr14": atr, "volume_ratio": volume_ratio,
            "candle_timestamp": rows[-1][0] / 1000,
            "valid_until": rows[-1][0] / 1000 + 2 * timeframe_seconds,
            "reference_close": closes[-1], "suggested_stop": max(closes[-1] - 2 * atr, 0),
            "suggested_take": closes[-1] + 4 * atr}
