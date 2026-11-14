"""Point-in-time trade evidence. Missing historical facts are never reconstructed by AI."""

from __future__ import annotations

import hashlib
import json
import time

from .rules import effective_view


def initialize(store):
    with store.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS v2_trade_journal (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, instrument_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, opened REAL NOT NULL, closed REAL, detail TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS v2_trade_events (id INTEGER PRIMARY KEY, trade_id TEXT, account_id TEXT NOT NULL, instrument_id TEXT NOT NULL, at REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS journal_events_trade ON v2_trade_events(trade_id,at)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS v2_equity_curve (account_id TEXT NOT NULL, at REAL NOT NULL, equity REAL, available REAL, unrealized REAL, exposure REAL, source TEXT NOT NULL, PRIMARY KEY(account_id,at))"
        )
        db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_journal_clock','0')")


def event(db, aid, key, action, detail, trade_id=None):
    db.execute(
        "INSERT INTO v2_trade_events(trade_id,account_id,instrument_id,at,action,detail) VALUES (?,?,?,?,?,?)",
        (trade_id, aid, key, time.time(), action, json.dumps(detail, default=str)),
    )


def entry(db, aid, key, trade_id, account, op, quote, sizing, before, after, adding=False, bars=None):
    if not adding:
        detail = {
            "schema": 1,
            "bars": bars or {},
            "history_sources": op.get("history", {}),
            "signal": {
                k: op.get(k)
                for k in (
                    "id",
                    "symbol",
                    "exchange",
                    "direction",
                    "score",
                    "min_score",
                    "profile",
                    "evaluated_at",
                    "expires_at",
                    "coverage",
                    "indicators",
                    "breakdown",
                    "blockers",
                    "evidence",
                    "plan",
                    "ai",
                    "parameters",
                )
            },
            "effective_parameters": effective_view(op.get("parameters")),
            "entry_quote": quote,
            "risk_and_sizing": sizing,
            "account_before": {
                k: before.get(k) for k in ("equity", "available", "unrealized", "exposure", "positions")
            },
            "account_after": {
                k: after.get(k) for k in ("equity", "available", "unrealized", "exposure", "positions")
            },
            "demo": bool(op.get("demo")),
            "versions": [op.get("logic_version")],
            "mfe": None,
            "mae": None,
            "mark_samples": 0,
            "missing": [
                k
                for k, v in {
                    "entry indicators": op.get("indicators"),
                    "source candles": bars,
                    "risk snapshot": sizing.get("risk"),
                }.items()
                if not v
            ],
            "cost_basis": "paper estimates"
            if account["kind"] == "paper"
            else "exchange fills; unavailable fees explicitly unknown",
        }
        db.execute(
            "INSERT OR IGNORE INTO v2_trade_journal VALUES (?,?,?,?,?,?,NULL,?)",
            (
                trade_id,
                aid,
                key,
                op.get("logic_version", "unknown"),
                account["kind"],
                time.time(),
                json.dumps(detail, default=str),
            ),
        )
    event(
        db,
        aid,
        key,
        "add" if adding else "open",
        {"quote": quote, "sizing": sizing, "version": op.get("logic_version"), "signal_id": op.get("id")},
        trade_id,
    )


def close(db, aid, key, p, detail, complete):
    tid = p.get("logic_trade_id") or p.get("journal_id")
    event(db, aid, key, "close", detail, tid)
    if complete and tid:
        row = db.execute("SELECT detail FROM v2_trade_journal WHERE id=?", (tid,)).fetchone()
        if row:
            saved = json.loads(row[0])
            saved.update(
                outcome=detail,
                final_position={
                    k: p.get(k)
                    for k in (
                        "side",
                        "entry",
                        "stop",
                        "targets",
                        "adds",
                        "tp_done",
                        "logic_net",
                        "parameters",
                        "logic_version",
                    )
                },
            )
            db.execute(
                "UPDATE v2_trade_journal SET closed=?,detail=? WHERE id=?",
                (time.time(), json.dumps(saved, default=str), tid),
            )


def transition(db, aid, key, p, version, parameters, reason):
    before = p.get("logic_version")
    if before == version:
        return
    p.setdefault("entry_logic_version", before)
    tid = p.get("logic_trade_id") or p.get("journal_id")
    event(
        db,
        aid,
        key,
        "strategy_transition",
        {
            "before": before,
            "after": version,
            "reason": reason,
            "effective_parameters": effective_view(parameters),
        },
        tid,
    )
    if tid:
        row = db.execute("SELECT detail FROM v2_trade_journal WHERE id=?", (tid,)).fetchone()
        if row:
            data = json.loads(row[0])
            data["versions"] = list(dict.fromkeys(data.get("versions", [before]) + [version]))
            db.execute("UPDATE v2_trade_journal SET detail=? WHERE id=?", (json.dumps(data), tid))
    p["logic_version"], p["parameters"] = version, parameters


def sample(workspace):
    """Five-second marked evidence; avoid turning every quote into a database write."""
    from .accounts import valuation

    now = time.time()
    if now - workspace.store.get("v2_journal_clock") < 5:
        return
    with workspace.lock:
        quotes = dict(workspace.quotes)
    with workspace.store.transaction() as db:
        if now - workspace.store.read(db, "v2_journal_clock") < 5:
            return
        workspace.store.write(db, "v2_journal_clock", now)
        accounts = workspace.store.read(db, "v2_accounts")
        books = workspace.store.read(db, "v2_ledgers")
        external = workspace.store.read(db, "v2_external")
        for aid, a in accounts.items():
            if a["kind"] == "paper":
                if aid not in books:
                    continue
                s = valuation(books[aid], quotes, now)
                if not s.get("valuation_valid"):
                    continue
            else:
                s = external.get(aid, {})
                if s.get("error") or now - s.get("checked_at", 0) > 30:
                    continue
            db.execute(
                "INSERT OR IGNORE INTO v2_equity_curve VALUES (?,?,?,?,?,?,?)",
                (
                    aid,
                    now,
                    s.get("equity"),
                    s.get("available"),
                    s.get("unrealized"),
                    s.get("exposure"),
                    a["kind"],
                ),
            )
            positions = (
                books.get(aid, {}).get("positions", {})
                if a["kind"] == "paper"
                else workspace.store.read(db, "v2_managed").get(aid, {})
            )
            for key, p in positions.items():
                q = quotes.get(key, {})
                if q.get("error") or not -5 <= now - q.get("timestamp", 0) <= 15:
                    continue
                tid = p.get("logic_trade_id") or p.get("journal_id")
                row = (
                    db.execute("SELECT detail FROM v2_trade_journal WHERE id=?", (tid,)).fetchone()
                    if tid
                    else None
                )
                if not row:
                    continue
                data = json.loads(row[0])
                sign = 1 if p["side"] == "long" else -1
                unreal = (float(q["mid"]) - float(p["entry"])) * float(p["qty"]) * sign
                data["mfe"] = max(data["mfe"] if data["mfe"] is not None else unreal, unreal)
                data["mae"] = min(data["mae"] if data["mae"] is not None else unreal, unreal)
                data["mark_samples"] += 1
                data["last_mark"] = {"at": now, "quote": q, "unrealized": unreal, "qty": p["qty"]}
                db.execute("UPDATE v2_trade_journal SET detail=? WHERE id=?", (json.dumps(data), tid))
        db.execute("DELETE FROM v2_equity_curve WHERE at<?", (now - 180 * 86400,))


def review_evidence(store, start, end):
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM v2_trade_journal WHERE closed>=? AND closed<=? ORDER BY closed DESC LIMIT 100",
            (start, end),
        ).fetchall()
        trades = []
        groups = {}
        for r in rows:
            d = json.loads(r["detail"])
            if d.get("demo"):
                continue
            if db.execute("SELECT 1 FROM logic_sample_exclusions WHERE trade_id=?", (r["id"],)).fetchone():
                continue
            alias = hashlib.sha256(r["account_id"].encode()).hexdigest()[:8]
            events = [
                {"at": e["at"], "action": e["action"], "detail": json.loads(e["detail"])}
                for e in db.execute(
                    "SELECT * FROM v2_trade_events WHERE trade_id=? ORDER BY at LIMIT 300", (r["id"],)
                )
            ]
            d.update(account=alias, kind=r["kind"], opened=r["opened"], closed=r["closed"], events=events)
            d.pop("bars", None)
            observed = d.get("signal", {}).get("indicators") or {}
            directions = {s.get("trend") for s in observed.values() if s.get("trend")}
            regime = (
                "trend_long"
                if directions == {"long"}
                else "trend_short"
                if directions == {"short"}
                else "mixed_or_flat"
                if directions
                else "unknown"
            )
            d["entry_regime"] = regime
            trades.append(d)
            key = (
                alias,
                r["kind"],
                d.get("signal", {}).get("symbol"),
                d.get("signal", {}).get("direction"),
                r["version"],
                "mixed" if len(d.get("versions", [])) > 1 else "single",
                regime,
            )
            net = d.get("outcome", {}).get("trade_net_pnl")
            if net is not None:
                groups.setdefault(key, []).append(float(net))
        grouped = []
        for key, vals in groups.items():
            grouped.append(
                {
                    "account": key[0],
                    "kind": key[1],
                    "symbol": key[2],
                    "side": key[3],
                    "entry_version": key[4],
                    "attribution": key[5],
                    "entry_regime": key[6],
                    "trades": len(vals),
                    "net_pnl": sum(vals),
                    "wins": sum(v > 0 for v in vals),
                    "largest_profit_share": max([0, *vals]) / sum(v for v in vals if v > 0)
                    if any(v > 0 for v in vals)
                    else None,
                }
            )
        curves = []
        for r in db.execute(
            "SELECT DISTINCT account_id FROM v2_equity_curve WHERE at>=? AND at<=?", (start, end)
        ):
            aid = r[0]
            points = db.execute(
                "SELECT at,equity FROM v2_equity_curve WHERE account_id=? AND at>=? AND at<=? AND equity IS NOT NULL ORDER BY at",
                (aid, start, end),
            ).fetchall()
            peak = 0
            drawdown = 0
            for x in points:
                peak = max(peak, x["equity"])
                if peak > 0:
                    drawdown = max(drawdown, (peak - x["equity"]) / peak)
            curves.append(
                {
                    "account": hashlib.sha256(aid.encode()).hexdigest()[:8],
                    "samples": len(points),
                    "marked_drawdown_fraction": drawdown,
                    "first_at": points[0]["at"] if points else None,
                    "last_at": points[-1]["at"] if points else None,
                    "cash_flow_adjusted": False,
                    "points": [dict(p) for p in points[:: max(1, len(points) // 120)]][-121:],
                }
            )
        rejected = [
            {"at": r["at"], "instrument_id": r["instrument_id"], "detail": json.loads(r["detail"])}
            for r in db.execute(
                "SELECT * FROM v2_trade_events WHERE action='rejected' AND at>=? AND at<=? ORDER BY at DESC LIMIT 50",
                (start, end),
            )
        ]
    return {
        "closed_trade_evidence": trades,
        "groups": grouped,
        "equity_curves": curves,
        "rejected_opportunities": rejected,
        "limitations": [
            "MFE/MAE based on five-second samples, not tick-perfect extrema.",
            "Equity drawdown includes deposits/withdrawals; do not call it cash-flow-adjusted return.",
            "Mixed-version trades are labelled; do not attribute all PnL to the last version.",
            "Legacy entries without point-in-time evidence remain missing.",
        ],
    }


def backfill_legacy(store):
    """Recover only fields already present in immutable order records, once per trade."""
    with store.transaction() as db:
        rows = db.execute(
            "SELECT t.*,d.detail,o.account_id,o.result FROM logic_trades t JOIN logic_trade_details d ON d.id=t.id JOIN v2_orders o ON o.id=t.id WHERE t.id NOT IN (SELECT id FROM v2_trade_journal) ORDER BY t.closed_at LIMIT 2000"
        ).fetchall()
        for r in rows:
            trade = json.loads(r["detail"])
            order = json.loads(r["result"])
            key = order.get("instrument_id")
            if not key:
                continue
            opened = trade.get("opened_at", r["closed_at"])
            data = {
                "schema": 0,
                "recovered_from": "stored order records",
                "signal": {"symbol": trade.get("symbol"), "direction": trade.get("side")},
                "versions": list(
                    dict.fromkeys([r["version"], trade.get("final_logic_version", r["version"])])
                ),
                "demo": bool(r["demo"]),
                "outcome": {"trade_net_pnl": r["net_pnl"], "reason": trade.get("exit_reason")},
                "missing": [
                    "entry indicators and evidence",
                    "initial portfolio and liquidation risk",
                    "original protection plan",
                    "tick-level excursion history",
                ],
                "cost_basis": "only recorded costs, no inferred fees",
            }
            db.execute(
                "INSERT OR IGNORE INTO v2_trade_journal VALUES (?,?,?,?,?,?,?,?)",
                (
                    r["id"],
                    r["account_id"],
                    key,
                    r["version"],
                    "paper",
                    opened,
                    r["closed_at"],
                    json.dumps(data),
                ),
            )
            orders = db.execute(
                "SELECT created,action,result FROM v2_orders WHERE account_id=? AND created>=? AND created<=? AND json_extract(result,'$.instrument_id')=? ORDER BY created",
                (r["account_id"], opened - 0.01, r["closed_at"] + 0.01, key),
            ).fetchall()
            for o in orders:
                db.execute(
                    "INSERT INTO v2_trade_events(trade_id,account_id,instrument_id,at,action,detail) VALUES (?,?,?,?,?,?)",
                    (r["id"], r["account_id"], key, o["created"], o["action"], o["result"]),
                )


def compare_candidate(store, parameters, start, end):
    """Replay historical entry decisions only, never advertise it as a profit backtest."""
    from .signals import evaluate

    comparisons = []
    missing = 0
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM v2_trade_journal WHERE closed>=? AND closed<=? ORDER BY closed DESC LIMIT 20",
            (start, end),
        ).fetchall()
        for row in rows:
            d = json.loads(row["detail"])
            sig = d.get("signal", {})
            bars = d.get("bars")
            quote = d.get("entry_quote")
            if d.get("demo") or not bars or not quote or not sig.get("parameters"):
                missing += 1
                continue
            if db.execute("SELECT 1 FROM logic_sample_exclusions WHERE trade_id=?", (row["id"],)).fetchone():
                continue
            item = {"id": row["instrument_id"], "symbol": sig["symbol"], "exchange": sig["exchange"]}
            at = quote.get("received_at") or quote.get("timestamp")
            try:
                old = evaluate(
                    item,
                    bars,
                    quote,
                    sig.get("evidence") or [],
                    sig["profile"],
                    at,
                    sig.get("min_score") or 80,
                    sig["parameters"],
                )
                new = evaluate(
                    item,
                    bars,
                    quote,
                    sig.get("evidence") or [],
                    sig["profile"],
                    at,
                    sig.get("min_score") or 80,
                    parameters,
                )
                comparisons.append(
                    {
                        "symbol": sig["symbol"],
                        "at": at,
                        "before": {
                            "valid": old["valid"],
                            "score": old["score"],
                            "direction": old["direction"],
                            "stop": (old.get("plan") or {}).get("stop"),
                        },
                        "after": {
                            "valid": new["valid"],
                            "score": new["score"],
                            "direction": new["direction"],
                            "stop": (new.get("plan") or {}).get("stop"),
                            "blockers": new["blockers"],
                        },
                    }
                )
            except (ValueError, TypeError, KeyError):
                missing += 1
    return {
        "kind": "entry_signal_replay_only",
        "samples": len(comparisons),
        "missing": missing,
        "changed_decisions": sum(
            any(r["before"][k] != r["after"][k] for k in r["before"]) for r in comparisons
        ),
        "comparisons": comparisons,
        "profit_backtest": False,
        "note": "仅复算历史入场信号；未模拟后续价格路径与成交，不证明收益改善。不阻塞通过参数和执行保护检查后的立即生效。",
    }
