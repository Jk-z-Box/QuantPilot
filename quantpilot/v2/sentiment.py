"""Published BTC sentiment, explicitly labelled as a crypto market proxy."""

import math

URL = "https://alternative.me/crypto/fear-and-greed-index/"
API = "https://api.alternative.me/fng/"


def observation(payload, now):
    if payload.get("metadata", {}).get("error"):
        raise ValueError("情绪来源返回错误")
    rows = []
    for row in payload.get("data", [])[:30]:
        value, timestamp = float(row["value"]), float(row["timestamp"])
        if (
            not math.isfinite(value)
            or not 0 <= value <= 100
            or not math.isfinite(timestamp)
            or not 0 < timestamp <= now + 5
        ):
            raise ValueError("情绪数据格式或时间无效")
        rows.append({"value": value, "timestamp": timestamp})
    if not rows or len({r["timestamp"] for r in rows}) != len(rows):
        raise ValueError("情绪数据不足或重复")
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    latest = rows[0]
    return {
        "category": "sentiment",
        "source": "Alternative.me · BTC 恐惧与贪婪指数",
        "url": URL,
        "published_at": latest["timestamp"],
        "expires_at": latest["timestamp"] + 129600,
        "status": "fresh" if now - latest["timestamp"] <= 129600 else "stale",
        "value": latest["value"],
        "bias": "neutral",
        "note": f"{latest['value']:g}/100 · 日度 BTC 情绪，仅作加密市场参考；不是每个币种的独立指数。",
        "details": {"history": rows, "scope": "BTC market proxy", "fetched_at": now},
    }
