"""Share account-independent market/level review, never account approvals."""

import copy
import hashlib
import json
import time

from . import ai_client


class MarketReview:
    def __init__(self, owner):
        self.owner = owner
        self.jobs = {}
        self.last_requests = {}

    def inspect(self, op, quote, prefs):
        owner, now = self.owner, time.time()
        # Exclude account-specific add triggers, balances and order quantities.
        market = {
            k: op.get(k)
            for k in (
                "exchange",
                "symbol",
                "direction",
                "id",
                "score",
                "valid",
                "plan",
                "profile",
                "min_score",
                "blockers",
                "reasons",
                "indicators",
                "evidence",
                "parameters",
                "logic_version",
            )
        }
        identity = hashlib.sha256(
            json.dumps(
                {
                    "market": {k: v for k, v in market.items() if k != "plan"},
                    "prefs": prefs,
                    "logic": owner.workspace.logic.current()["id"],
                    "auth": ai_client.fingerprint(prefs, owner.workspace.vault),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        interval = prefs["evaluation_interval"]
        with owner.lock:
            self.jobs = {k: j for k, j in self.jobs.items() if now - j["at"] < max(120, interval * 2 + 45)}
            job = self.jobs.get(identity)

            def plan_changed(previous):
                def levels(plan):
                    return [plan[k] for k in ("entry", "entry_low", "entry_high", "stop")] + [
                        t["price"] for t in plan["targets"]
                    ]

                return [t.get("ratio") for t in market["plan"]["targets"]] != [
                    t.get("ratio") for t in previous["targets"]
                ] or any(
                    abs(a - b) / quote["mid"] > prefs["ai_reaudit_threshold_pct"] / 100
                    for a, b in zip(levels(market["plan"]), levels(previous), strict=True)
                )

            if job and (
                plan_changed(job["plan"])
                or (job["state"] == "error" and now - job["at"] >= interval)
                or (
                    job["state"] == "ready"
                    and (
                        now - job["completed_at"] > interval
                        or abs(quote["mid"] / job["reference"] - 1) > prefs["ai_reaudit_threshold_pct"] / 100
                    )
                )
            ):
                self.jobs.pop(identity, None)
                job = None
            if job is None:
                bucket = (op["exchange"], op["symbol"], op["direction"])
                if now - self.last_requests.get(bucket, 0) < interval:
                    return {"state": "pending", "reason": "等待共享交易对 AI 审核周期"}
                self.last_requests[bucket] = now
                job = {
                    "at": now,
                    "state": "pending",
                    "deadline": now + 45,
                    "reference": quote["mid"],
                    "plan": copy.deepcopy(market["plan"]),
                }
                self.jobs[identity] = job
                if (
                    len(self.jobs) > 40
                    or not prefs["ai_model"]
                    or not ai_client.ready(prefs, owner.workspace.vault)
                ):
                    job.update(state="error", reason="AI 未连接、模型未设置或审核队列已满")
                else:
                    owner.pool.submit(
                        self.run,
                        identity,
                        prefs,
                        {"stage": "shared_market", "opportunity": market, "quote": quote},
                    )
            if job["state"] == "pending" and now > job["deadline"]:
                job.update(state="error", reason="共享交易对 AI 审核超时")
            if job["state"] == "error":
                raise ValueError("AI 交易审核不可用：暂停开仓和加仓；保护继续运行")
            if job["state"] != "ready":
                return {"state": "pending", "reason": "等待共享交易对行情与点位审核"}
            result = job["result"]
            if result["decision"] == "reject":
                return {"state": "rejected", "reason": "AI 暂不执行（共享行情）：" + result["reason"]}
            adjusted = copy.deepcopy(op)
            adjusted["plan"] = copy.deepcopy(job["plan"])
            if result["decision"] == "adjust":
                owner.adjust(adjusted, result, quote)
            adjusted["market_review_ticket"] = identity
            return {"state": "approved", "opportunity": adjusted, "result": result}

    def run(self, identity, prefs, data):
        try:
            result = self.owner.request_decision(prefs, data)
            with self.owner.lock:
                job = self.jobs.get(identity)
                if (
                    not job
                    or job["state"] != "pending"
                    or time.time() > job["deadline"]
                    or self.owner.workspace.stop_event.is_set()
                ):
                    return
                job.update(state="ready", result=result, completed_at=time.time())
        except Exception:
            with self.owner.lock:
                job = self.jobs.get(identity)
                if job and job["state"] == "pending":
                    job.update(state="error", reason="共享交易对 AI 审核不可用")
