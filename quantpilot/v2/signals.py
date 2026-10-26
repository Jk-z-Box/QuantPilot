from __future__ import annotations

import hashlib
import math
import statistics

from ..strategy import ema, rsi
from .models import PROFILES


def indicators(rows):
    if len(rows) < 60:
        raise ValueError("至少需要 60 根已完成 K 线")
    close = [float(r[4]) for r in rows]
    fast, slow = ema(close, 20), ema(close, 50)
    series = [ema(close[:i], 12) - ema(close[:i], 26) for i in range(35, len(close) + 1)]
    macd = series[-1]
    signal = ema(series, 9)
    tr = [max(r[2] - r[3], abs(r[2] - p[4]), abs(r[3] - p[4])) for p, r in zip(rows, rows[1:], strict=False)]
    atr = sum(tr[:14]) / 14
    for value in tr[14:]:
        atr = (13 * atr + value) / 14
    vol = statistics.mean(r[5] for r in rows[-21:-1])
    returns = [math.log(b / a) for a, b in zip(close[-31:-1], close[-30:], strict=True)]
    return {
        "ema20": fast,
        "ema50": slow,
        "rsi": rsi(close),
        "macd": macd,
        "macd_signal": signal,
        "macd_hist": macd - signal,
        "atr": atr,
        "volatility": statistics.pstdev(returns) * 100,
        "volume_ratio": rows[-1][5] / vol if vol else 0,
        "trend": "long" if fast > slow else "short" if fast < slow else "flat",
        "close": close[-1],
        "timestamp": rows[-1][0] / 1000,
    }


def evaluate(item, frames, quote, evidence, profile, now, min_score=80):
    rule = PROFILES[profile]
    stats, blockers, reasons = {}, [], []
    for frame in ("1w", "1d", "4h", "1h"):
        try:
            stats[frame] = indicators(frames[frame])
        except (KeyError, ValueError, TypeError):
            blockers.append(frame + " 趋势数据不足")
    base = {
        "instrument_id": item["id"],
        "symbol": item["symbol"],
        "exchange": item["exchange"],
        "evaluated_at": now,
        "expires_at": now + rule["ttl"],
        "profile": profile,
        "min_score": min_score,
        "indicators": stats,
        "evidence": evidence,
        "blockers": blockers,
        "reasons": reasons,
    }
    if not stats.get("1h") or not quote or quote.get("error"):
        return {
            **base,
            "score": 0,
            "direction": "wait",
            "valid": False,
            "coverage": 0,
            "breakdown": [],
            "plan": None,
        }
    hour = stats["1h"]
    direction = hour["trend"]
    sign = 1 if direction == "long" else -1
    if direction == "flat":
        blockers.append("趋势方向不明确")
    if now - quote.get("timestamp", 0) > 15 or quote.get("timestamp", 0) > now + 5:
        blockers.append("报价时间过期或未验证")
    if quote["ask"] < quote["bid"] or (quote["ask"] - quote["bid"]) / quote["mid"] > 0.003:
        blockers.append("买卖价差过大")
    breakdown = []

    def points(name, value, maximum, available=True):
        breakdown.append({"name": name, "points": round(value, 1), "max": maximum, "available": available})

    aligned = sum(s["trend"] == direction for s in stats.values())
    points("多周期趋势", aligned * 6, 24, len(stats) == 4)
    if stats.get("4h", {}).get("trend") != direction:
        blockers.append("4 小时与 1 小时趋势冲突")
    if profile == "conservative" and aligned < 4:
        blockers.append("保守模式要求四个周期方向一致")
    momentum = 43 <= hour["rsi"] <= 70 if sign == 1 else 30 <= hour["rsi"] <= 57
    points("动量 EMA / RSI / MACD", (9 if momentum else 0) + (9 if hour["macd_hist"] * sign > 0 else 0), 18)
    volume_ok = hour["volume_ratio"] >= rule["volume"]
    points("成交量确认", 12 if volume_ok else 0, 12)
    if not volume_ok:
        blockers.append("成交量不支持当前方向")
    atr_fraction = hour["atr"] / hour["close"]
    points("波动率与入场位置", 6 if 0.001 <= atr_fraction <= 0.06 else 0, 6)
    if atr_fraction > 0.10 or hour["atr"] <= 0:
        blockers.append("波动风险过高或 ATR 无效")
    funding = quote.get("funding_rate")
    points("资金费率", 6 if funding is not None and sign * funding <= 0.0003 else 0, 6, funding is not None)
    oi = quote.get("open_interest")
    points("合约持仓量", 4 if oi is not None and oi > 0 else 0, 4, oi is not None)
    for category, name, weight in [
        ("options", "期权 / 交割", 8),
        ("etf", "已披露资金流", 6),
        ("liquidation", "清算数据", 8),
        ("macro", "宏观 / 新闻", 8),
    ]:
        sources = [
            e
            for e in evidence
            if e["category"] == category
            and e.get("status") == "fresh"
            and e.get("published_at", now + 1) <= now
            and e.get("expires_at", 0) > now
        ]
        if not sources:
            points(name, 0, weight, False)
            continue
        base["expires_at"] = min(base["expires_at"], *(e["expires_at"] for e in sources))
        risk = any(
            e.get("bias") == "risk" or (e.get("event_at") and -3600 < e["event_at"] - now < 7200)
            for e in sources
        )
        if risk:
            blockers.append(name + " 存在事件风险")
        favorable = any(e.get("bias") == ("bullish" if sign == 1 else "bearish") for e in sources)
        adverse = any(e.get("bias") == ("bearish" if sign == 1 else "bullish") for e in sources)
        # Neutral observations demonstrate coverage, not directional proof.
        points(name, 0 if risk or adverse else weight if favorable else weight / 2, weight)
    score = round(sum(b["points"] for b in breakdown))
    entry = float(quote["ask"] if sign == 1 else quote["bid"])
    distance = max(hour["atr"] * 2, entry * 0.003)
    stop = entry - sign * distance
    if stop <= 0:
        blockers.append("止损价格无效")
    targets = [
        {"price": entry + sign * distance * 1.5, "ratio": 0.5},
        {"price": entry + sign * distance * 3, "ratio": 0.5},
    ]
    # Account for estimated round trip fees and slippage in the displayed R/R.
    costs = entry * 0.0014
    rr = (distance * 2.25 - costs) / (distance + costs)
    if rr < rule["min_rr"]:
        blockers.append("扣除预估费用后的收益风险比不足")
    if score < min_score:
        reasons.append(f"尚未达到 {min_score} 分有效阈值")
    plan = {
        "entry": entry,
        "entry_low": entry - distance * 0.12,
        "entry_high": entry + distance * 0.12,
        "stop": stop,
        "targets": targets,
        "rr": round(rr, 2),
        "add_at": entry + sign * distance * 0.75,
        "reduce_at": entry - sign * distance * 0.5,
        "risk_per_unit": distance,
        "fee_assumption": 0.001,
        "slippage_assumption": 0.0004,
    }
    identity = f"{item['id']}:{hour['timestamp']}:{direction}"
    return {
        **base,
        "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "score": score,
        "direction": direction,
        "valid": score >= min_score and not blockers,
        "coverage": sum(b["max"] for b in breakdown if b["available"]),
        "breakdown": breakdown,
        "plan": plan,
    }
