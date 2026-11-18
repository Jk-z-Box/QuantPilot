from __future__ import annotations


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
