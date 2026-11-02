"""Partial private WebSocket events for display; never used as execution snapshots."""

from __future__ import annotations

import copy
from decimal import Decimal


def numeric(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def event_ms(payload, received_ms):
    if not isinstance(payload, dict):
        return received_ms
    value = next(
        (
            numeric(payload.get(k))
            for k in ("timestamp", "time_ms", "timestamp_ms", "E", "uTime", "time")
            if numeric(payload.get(k)) is not None
        ),
        None,
    )
    if value is None:
        return received_ms
    value = float(value)
    return value * 1000 if value < 100000000000 else value


def balance_fields(exchange, payload):
    info = payload.get("info", {})
    fields = {}
    if exchange == "gate":
        rows = info if isinstance(info, list) else [info]
        row = next((r for r in rows if r.get("currency", "USDT") == "USDT"), {})
        fields = {"wallet": row.get("balance", row.get("total")), "available": row.get("available")}
    elif exchange == "okx":
        rows = info.get("data", []) if isinstance(info, dict) else info
        row = next((d for r in rows for d in r.get("details", []) if d.get("ccy") == "USDT"), {})
        fields = {
            "equity": row.get("eq"),
            "unrealized": row.get("upl"),
            "wallet": row.get("cashBal"),
            "available": row.get("availEq"),
        }
    elif exchange == "binance" and isinstance(info, dict):
        row = next((b for b in info.get("a", {}).get("B", []) if b.get("a") == "USDT"), {})
        fields = {"wallet": row.get("wb")}
    return {k: str(numeric(v)) for k, v in fields.items() if numeric(v) is not None}


def ingest(events, exchange, method, payload, received_ms):
    events = copy.deepcopy(events)
    if method == "watch_balance" and isinstance(payload, dict):
        values = balance_fields(exchange, payload)
        source_ms = event_ms(payload, received_ms)
        for key, value in values.items():
            old = events.setdefault("balance", {}).get(key, {})
            if source_ms >= old.get("source_ms", 0):
                events["balance"][key] = {"value": value, "received_ms": received_ms, "source_ms": source_ms}
    elif method == "watch_positions" and isinstance(payload, list):
        for p in payload:
            if not p.get("symbol") or numeric(p.get("contracts")) is None:
                continue
            raw = p.get("info") or {}
            if numeric(p.get("contracts")) == 0 and raw.get("mode") in ("dual_long", "dual_short"):
                p = {**p, "side": raw["mode"].removeprefix("dual_")}
            flat_all = numeric(p.get("contracts")) == 0 and (
                raw.get("mode") == "single" or raw.get("ps") == "BOTH" or raw.get("posSide") == "net"
            )
            key = p["symbol"] + ":" + ("all" if flat_all else str(p.get("side")))
            source_ms = event_ms(p, event_ms(raw, received_ms))
            old = events.setdefault("positions", {}).get(key, {})
            if source_ms < old.get("source_ms", 0):
                continue
            fields = (
                "symbol",
                "side",
                "contracts",
                "contractSize",
                "entryPrice",
                "markPrice",
                "unrealizedPnl",
                "liquidationPrice",
                "leverage",
                "marginMode",
                "hedged",
                "notional",
                "initialMargin",
                "maintenanceMargin",
            )
            value = {k: p[k] for k in fields if p.get(k) is not None}
            events["positions"][key] = {
                "value": value,
                "flat_all": flat_all,
                "received_ms": received_ms,
                "source_ms": source_ms,
            }
        # An account can never accumulate an unbounded history of closed symbols.
        if len(events.get("positions", {})) > 256:
            events["positions"] = dict(
                sorted(events["positions"].items(), key=lambda x: x[1]["received_ms"])[-256:]
            )
    return events


def apply(base, events):
    result = copy.deepcopy(base)
    boundary = base.get("read_started_at", base.get("checked_at", 0)) * 1000
    times = {
        "funds_ms": base.get("checked_at", 0) * 1000,
        "available_ms": base.get("checked_at", 0) * 1000,
        "positions_ms": base.get("checked_at", 0) * 1000,
    }
    active_positions = [e for e in events.get("positions", {}).values() if e["received_ms"] >= boundary]
    active_positions.sort(key=lambda e: e["source_ms"])
    for event in active_positions:
        p = event["value"]
        rows = result.setdefault("positions", [])
        matched = [
            r
            for r in rows
            if r.get("symbol") == p["symbol"] and (event["flat_all"] or r.get("side") == p.get("side"))
        ]
        if numeric(p.get("contracts")) == 0:
            result["positions"] = [r for r in rows if r not in matched]
        elif matched:
            previous = matched[0]
            if previous.get("contracts") != p["contracts"] and "unrealizedPnl" not in p:
                previous["unrealizedPnl"] = None
            if previous.get("contracts") != p["contracts"] and "liquidationPrice" not in p:
                previous["liquidationPrice"] = None
            previous.update(p)
            previous["stream_pending"] = True
        elif p.get("side") in ("long", "short"):
            rows.append({**p, "stream_pending": True})
        times["positions_ms"] = max(times["positions_ms"], event["received_ms"])
    result["pending_reconcile"] = bool(active_positions)
    for key, event in events.get("balance", {}).items():
        if event["received_ms"] < boundary:
            continue
        if key == "wallet" and base.get("native_equity") and not active_positions:
            before, after, equity = (
                numeric(result.get("wallet")),
                numeric(event["value"]),
                numeric(result.get("equity")),
            )
            if None not in (before, after, equity):
                result["equity"] = str(equity + after - before)
                result["valuation_source"] = "mark_estimate"
        result[key] = event["value"]
        times["funds_ms"] = max(times["funds_ms"], event["received_ms"])
        if key == "available":
            times["available_ms"] = event["received_ms"]
    result["data_times"] = times
    return result
