"""Account-specific entry outcomes, grouped by the shared market signal."""

import hashlib
import json
import time

from .models import pair_enabled
from .notifications import emit

LABELS = {
    "pending": "等待审核",
    "approved": "审核允许，尚未成交",
    "blocked": "未开仓",
    "filled": "已成交",
    "protected": "已成交，保护单已确认",
    "unknown": "成交状态待核对",
}


def report(w, aid, op, status, reason, action="open"):
    if not op or not op.get("instrument_id"):
        return
    key = op["instrument_id"]
    group = hashlib.sha256(
        f"{op.get('exchange')}:{op.get('symbol')}:{op.get('id')}:{action}".encode()
    ).hexdigest()
    with w.store.transaction() as db:
        accounts = w.store.read(db, "v2_accounts")
        if aid not in accounts or not accounts[aid].get("ai_action_enabled"):
            return
        groups = w.store.read(db, "v2_entry_status")
        record = groups.setdefault(group, {"at": time.time(), "instrument_id": key, "rows": {}})
        previous = record["rows"].get(aid)
        row = {"status": status, "reason": reason[:700]}
        # Repeated loops must not downgrade a completed order to a pending review.
        if previous == row or (
            previous and previous["status"] in ("filled", "protected") and status in ("approved", "pending")
        ):
            return
        record["rows"][aid] = row
        groups = dict(sorted(groups.items(), key=lambda p: p[1]["at"])[-100:])
        w.store.write(db, "v2_entry_status", groups)
        if status == "pending":
            return
        lines = [
            f"QuantPilot · 账户交易审核\n{op.get('exchange', '').upper()} {op.get('symbol', key)} · {'做多' if op.get('direction') == 'long' else '做空'} · {'加仓' if action == 'add' else '开仓'}"
        ]
        for account in accounts.values():
            if not pair_enabled(account, key) or not account["auto"]:
                continue
            entry = record["rows"].get(account["id"])
            if entry:
                lines.append(
                    f"{account['name']}（{'模拟' if account['kind'] == 'paper' else account['kind']}）：{LABELS.get(entry['status'], entry['status'])}；{entry['reason']}"
                )
            elif account.get("ai_action_enabled"):
                lines.append(f"{account['name']}：尚未取得本账户审核结果，不能视为已获准开仓")
            else:
                lines.append(f"{account['name']}：未开启交易前 AI 审核；仍须通过独立资金及风控检查")
        lines.append("行情与点位结论共用；资金、强平风险和成交状态按账户分别判断。")
        digest = hashlib.sha256(json.dumps(record["rows"], sort_keys=True).encode()).hexdigest()
        # Keep every account visible: Telegram truncates oversized single messages.
        chunks, current = [], lines[0]
        for line in lines[1:]:
            if len(current) + len(line) + 1 > 3000:
                chunks.append(current)
                current = lines[0]
            current += "\n" + line
        chunks.append(current)
        for index, message in enumerate(chunks):
            emit(db, "entry-status:" + group + ":" + digest + ":" + str(index), "ai", message)
