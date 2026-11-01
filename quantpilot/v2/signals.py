from __future__ import annotations

import hashlib
import math
import statistics

from ..strategy import ema, rsi
from .models import PROFILES
from .rules import effective


def required_bars(parameters=None):
    p = effective(parameters)
    return max(
        60,
        p["ema_slow"] + 1 if p["ema_enabled"] else 0,
        p["macd_slow"] + p["macd_signal"] + 1 if p["macd_enabled"] else 0,
        p["rsi_period"] + 1 if p["rsi_enabled"] else 0,
        p["atr_period"] + 1,
        p["volume_lookback"] + 1 if p["volume_enabled"] or p["volume_confirm_enabled"] else 0,
        p["volatility_lookback"] + 1 if p["volatility_enabled"] else 0,
    )


def indicators(rows, parameters=None):
    parameters = effective(parameters)
    required = required_bars(parameters)
    if len(rows) < required:
        raise ValueError(f"至少需要 {required} 根已完成 K 线")
    close = [float(r[4]) for r in rows]
    fast, slow = (
        (ema(close, parameters["ema_fast"]), ema(close, parameters["ema_slow"]))
        if parameters["ema_enabled"]
        else (close[-1], close[-2])
    )
    series = (
        [
            ema(close[:i], parameters["macd_fast"]) - ema(close[:i], parameters["macd_slow"])
            for i in range(parameters["macd_slow"] + parameters["macd_signal"], len(close) + 1)
        ]
        if parameters["macd_enabled"]
        else [0.0]
    )
    macd = series[-1]
    signal = ema(series, parameters["macd_signal"])
    tr = [max(r[2] - r[3], abs(r[2] - p[4]), abs(r[3] - p[4])) for p, r in zip(rows, rows[1:], strict=False)]
    n = parameters["atr_period"]
    atr = sum(tr[:n]) / n
    for value in tr[n:]:
        atr = ((n - 1) * atr + value) / n
    vol = statistics.mean(r[5] for r in rows[-parameters["volume_lookback"] - 1 : -1])
    returns = (
        [
            math.log(b / a)
            for a, b in zip(
                close[-parameters["volatility_lookback"] - 1 : -1],
                close[-parameters["volatility_lookback"] :],
                strict=True,
            )
        ]
        if parameters["volatility_enabled"]
        else [0.0]
    )
    trend_fast, trend_slow = (fast, slow) if parameters["ema_enabled"] else (close[-1], close[-2])
    return {
        "ema20": fast,
        "ema50": slow,
        "rsi": rsi(close, parameters["rsi_period"]) if parameters["rsi_enabled"] else 50.0,
        "ema_fast_period": parameters["ema_fast"],
        "ema_slow_period": parameters["ema_slow"],
        "macd": macd,
        "macd_signal": signal,
        "macd_hist": macd - signal,
        "atr": atr,
        "volatility": statistics.pstdev(returns) * 100,
        "volume_ratio": rows[-1][5] / vol if vol else 0,
        "trend": "long" if trend_fast > trend_slow else "short" if trend_fast < trend_slow else "flat",
        "close": close[-1],
        "timestamp": rows[-1][0] / 1000,
    }


def evaluate(item, frames, quote, evidence, profile, now, min_score=80, parameters=None):
    parameters = effective(parameters)
    rule = PROFILES[profile]
    stats, blockers, reasons = {}, [], []
    active_frames = [f for f in ("1w", "1d", "4h", "1h") if parameters["frame_" + f + "_enabled"]]
    for frame in dict.fromkeys(active_frames + ["1h"]):
        try:
            stats[frame] = indicators(frames[frame], parameters)
        except (KeyError, ValueError, TypeError):
            blockers.append(frame + " 趋势数据不足")
    base = {
        "instrument_id": item["id"],
        "symbol": item["symbol"],
        "exchange": item["exchange"],
        "evaluated_at": now,
        "expires_at": now + (parameters["ttl_override"] or rule["ttl"]),
        "profile": profile,
        "min_score": min_score,
        "indicators": stats,
        "evidence": evidence,
        "parameters": parameters,
        "blockers": blockers,
        "reasons": reasons,
    }
    base_frame = active_frames[-1]
    if not stats.get("1h") or not stats.get(base_frame) or not quote or quote.get("error"):
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
    direction = stats[base_frame]["trend"]
    sign = 1 if direction == "long" else -1
    if direction == "flat":
        blockers.append("趋势方向不明确")
    if now - quote.get("timestamp", 0) > 15 or quote.get("timestamp", 0) > now + 5:
        blockers.append("报价时间过期或未验证")
    if quote["ask"] < quote["bid"] or (quote["ask"] - quote["bid"]) / quote["mid"] > 0.003:
        blockers.append("买卖价差过大")
    breakdown = []

    def points(name, value, maximum, available=True):
        if maximum <= 0:
            return
        breakdown.append({"name": name, "points": round(value, 1), "max": maximum, "available": available})

    aligned = sum(stats[f]["trend"] == direction for f in active_frames if f in stats)
    frame_total = sum(parameters["frame_" + f + "_weight"] for f in active_frames)
    frame_aligned = sum(
        parameters["frame_" + f + "_weight"]
        for f, v in stats.items()
        if f in active_frames and v["trend"] == direction
    )
    trend_weight = parameters["weight_trend"]
    points(
        "多周期趋势",
        trend_weight * frame_aligned / frame_total if frame_total else 0,
        trend_weight,
        all(f in stats for f in active_frames),
    )
    if (
        parameters["alignment_4h_1h_enabled"]
        and "4h" in active_frames
        and "1h" in active_frames
        and stats.get("4h", {}).get("trend") != direction
    ):
        blockers.append("4 小时与 1 小时趋势冲突")
    if (
        profile == "conservative"
        and parameters["conservative_alignment_enabled"]
        and aligned < len(active_frames)
    ):
        blockers.append("保守模式要求所有启用周期方向一致")
    momentum = (
        parameters["rsi_long_min"] <= hour["rsi"] <= parameters["rsi_long_max"]
        if sign == 1
        else parameters["rsi_short_min"] <= hour["rsi"] <= parameters["rsi_short_max"]
    )
    for key, name, ok in [("rsi", "RSI 动量", momentum), ("macd", "MACD 动量", hour["macd_hist"] * sign > 0)]:
        if parameters[key + "_enabled"]:
            points(name, parameters["weight_" + key] if ok else 0, parameters["weight_" + key])
    volume_ok = hour["volume_ratio"] >= rule["volume"] * parameters["volume_multiplier"]
    if parameters["volume_enabled"]:
        points("成交量确认", parameters["weight_volume"] if volume_ok else 0, parameters["weight_volume"])
    if parameters["volume_confirm_enabled"] and not volume_ok:
        blockers.append("成交量不支持当前方向")
    atr_fraction = hour["atr"] / hour["close"]
    if parameters["volatility_enabled"]:
        points(
            "波动率与入场位置",
            parameters["weight_volatility"]
            if parameters["atr_score_min"] <= atr_fraction <= parameters["atr_score_max"]
            else 0,
            parameters["weight_volatility"],
        )
        if atr_fraction > parameters["max_atr_pct"]:
            blockers.append("波动风险超过设定上限")
    if hour["atr"] <= 0:
        blockers.append("ATR 无效")
    funding, oi = quote.get("funding_rate"), quote.get("open_interest")
    if parameters["funding_enabled"]:
        points(
            "资金费率",
            parameters["weight_funding"]
            if funding is not None and sign * funding <= parameters["funding_limit"]
            else 0,
            parameters["weight_funding"],
            funding is not None,
        )
    if parameters["oi_enabled"]:
        points(
            "合约持仓量",
            parameters["weight_oi"] if oi is not None and oi > 0 else 0,
            parameters["weight_oi"],
            oi is not None,
        )
    for category, name, weight in [
        ("options", "期权 / 交割", 8),
        ("etf", "已披露资金流", 6),
        ("liquidation", "清算数据", 8),
        ("macro", "宏观 / 新闻", 8),
    ]:
        if not parameters[category + "_enabled"]:
            continue
        weight = parameters["weight_" + category]
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
    total = sum(b["max"] for b in breakdown)
    if total <= 0:
        blockers.append("至少需要一项有效评分权重")
    score = round(100 * sum(b["points"] for b in breakdown) / total) if total else 0
    if not parameters["entry_enabled"]:
        blockers.append("量化逻辑已关闭策略开仓")
    entry = float(quote["ask"] if sign == 1 else quote["bid"])
    distance = max(
        hour["atr"] * parameters["atr_stop"]
        if parameters["atr_stop_enabled"]
        else entry * parameters["fixed_stop_pct"],
        entry * parameters["stop_floor_pct"],
    )
    stop = entry - sign * distance
    if stop <= 0:
        blockers.append("止损价格无效")
    targets = [
        {"price": entry + sign * distance * parameters["target_1"], "ratio": parameters["target_1_ratio"]},
        {
            "price": entry + sign * distance * parameters["target_2"],
            "ratio": 1 - parameters["target_1_ratio"],
        },
    ]
    for index, target in enumerate(targets, 1):
        target["enabled"] = parameters[f"target_{index}_enabled"]
    if parameters["target_1_enabled"] != parameters["target_2_enabled"]:
        for target in targets:
            target["ratio"] = 1.0 if target["enabled"] else 0.0
    # Account for estimated round trip fees and slippage in the displayed R/R.
    costs = entry * 0.0014
    reward_multiple = sum(
        parameters[f"target_{i + 1}"] * t["ratio"] for i, t in enumerate(targets) if t["enabled"]
    )
    rr = (distance * reward_multiple - costs) / (distance + costs)
    if rr < (parameters["min_rr_override"] or rule["min_rr"]):
        blockers.append("扣除预估费用后的收益风险比不足")
    if score < min_score:
        reasons.append(f"尚未达到 {min_score} 分有效阈值")
    plan = {
        "entry": entry,
        "entry_low": entry - distance * parameters["entry_band_r"],
        "entry_high": entry + distance * parameters["entry_band_r"],
        "stop": stop,
        "targets": targets,
        "rr": round(rr, 2),
        "add_at": entry + sign * distance * parameters["add_trigger_r"],
        "reduce_at": entry - sign * distance * parameters["reduce_trigger_r"],
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
        "coverage": round(100 * sum(b["max"] for b in breakdown if b["available"]) / total) if total else 0,
        "breakdown": breakdown,
        "plan": plan,
    }
