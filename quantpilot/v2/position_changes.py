"""Only publish actual, material position-plan changes, never repeated reviews."""

import json
import time

from .notifications import emit


def levels(p):
    return {
        "stop": float(p["stop"]) if p.get("stop") is not None else None,
        "target_basis_qty": float(p.get("target_basis_qty", p.get("original_qty", p["qty"]))),
        "targets": [
            {"price": float(t["price"]), "ratio": float(t["ratio"]), "enabled": t.get("enabled", True)}
            for t in p.get("targets", [])
        ],
    }


def describe(key, value):
    if value is None:
        return "未计划"
    if key == "targets":
        return "；".join(
            f"止盈 {i + 1}：{t['price']:,.8g} / {t['ratio'] * 100:g}%"
            if t["enabled"]
            else f"止盈 {i + 1}：停用"
            for i, t in enumerate(value)
        )
    return f"{float(value):,.8g}"


def record_changes(store, db, aid, key, before, after, reason, reviewed_at, stage="protection"):
    changes = {k: {"before": before.get(k), "after": v} for k, v in after.items() if before.get(k) != v}
    if not changes:
        return
    now = time.time()
    row = db.execute(
        "INSERT INTO v2_position_changes(account_id,instrument_id,created,reviewed_at,reason,stage,changes) VALUES (?,?,?,?,?,?,?)",
        (aid, key, now, reviewed_at, reason[:1000], stage, json.dumps(changes)),
    )
    names = {
        "stop": "止损",
        "targets": "止盈安排",
        "target_basis_qty": "止盈比例基准数量",
        "add_at": "加仓计划",
        "reduce_at": "减仓计划",
    }
    item = next((i for i in store.read(db, "v2_watchlist") if i["id"] == key), {})
    account = store.read(db, "v2_accounts").get(aid, {})
    detail = "\n".join(
        f"{names.get(k, k)}：{describe(k, v['before'])} → {describe(k, v['after'])}"
        for k, v in changes.items()
    )
    emit(
        db,
        f"position-change:{row.lastrowid}",
        "ai",
        f"QuantPilot · 持仓 AI 调整\n{account.get('name', aid)} · {item.get('symbol', key)}\n{detail}\n原因：{reason[:800]}\n{'保护点位已更新' if stage == 'protection' else '计划已更新'}，不代表已成交。",
    )
    db.execute(
        "DELETE FROM v2_position_changes WHERE id NOT IN (SELECT id FROM v2_position_changes ORDER BY id DESC LIMIT 500)"
    )
    store.audit(
        db,
        "position_plan_changed",
        {"account_id": aid, "instrument_id": key, "changes": changes, "stage": stage},
    )


def position_guard(p):
    # Existing protective tightening must not invalidate the plan that applied it.
    return {
        k: p.get(k)
        for k in (
            "side",
            "qty",
            "entry",
            "opened_at",
            "initial_qty",
            "adds",
            "tp_done",
            "margin_mode",
            "leverage",
            "logic_version",
            "external_qty",
            "external_side",
            "risk_reduced",
        )
    } | {"identity": p.get("signal_id") or p.get("original_qty")}
