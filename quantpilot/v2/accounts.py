from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import time

from ..market import decimal as D
from .logic import check_version
from .margin import estimates, mode
from .models import PROFILES, policy_matches

FEE = D(".0005")
SLIP = D(".0002")


def ledger(capital):
    return {
        "cash": str(capital),
        "initial": str(capital),
        "positions": {},
        "fees": "0",
        "realized": "0",
        "day": "",
        "day_equity": str(capital),
        "generation": 0,
        "halted": False,
        "note": "",
    }


def valuation(book, quotes, now):
    cash, unreal, margin, exposure = D(book["cash"]), D(0), D(0), D(0)
    valid = True
    positions = []
    cross_unreal = D(0)
    marks = {}
    for key, p in book["positions"].items():
        q = quotes.get(key)
        fresh = bool(q and not q.get("error") and -5 <= now - q.get("timestamp", 0) <= 15)
        valid = valid and fresh
        price = D(q["bid"] if p["side"] == "long" else q["ask"]) if fresh else D(p["entry"])
        qty, entry = D(p["qty"]), D(p["entry"])
        pnl = (price - entry) * qty * (1 if p["side"] == "long" else -1)
        marks[key] = price
        if mode(p) == "cross":
            cross_unreal += pnl
        elif book.get("source") != "exchange":
            pnl = max(pnl, -D(p["margin"]))
        unreal += pnl
        margin += D(p["margin"])
        exposure += abs(qty * price)
        positions.append(
            {
                **p,
                "instrument_id": key,
                "unrealized": str(pnl),
                "mark": str(price),
                "price_verified": fresh,
                "margin_mode": mode(p),
            }
        )
    liquidations = (
        estimates(book, marks)
        if book.get("source") != "exchange"
        else {k: D(p["liquidation"]) for k, p in book["positions"].items()}
    )
    for p in positions:
        liq = liquidations[p["instrument_id"]]
        price = D(p["mark"])
        p["liquidation"] = str(liq) if liq is not None else None
        p["liquidation_status"] = "estimated" if liq is not None else "no_positive_price"
        p["liquidation_distance"] = (
            str((price - liq) * (1 if p["side"] == "long" else -1) / price) if liq is not None else None
        )
        p["liquidation_verified"] = p["price_verified"] and (valid or mode(p) == "isolated")
    return {
        "equity": str(cash + unreal),
        "available": str(cash + cross_unreal - margin),
        "exposure": str(exposure),
        "margin": str(margin),
        "unrealized": str(unreal),
        "cash": str(cash),
        "fees": book["fees"],
        "realized": book["realized"],
        "positions": positions,
        "valuation_valid": valid,
        "halted": book["halted"],
        "note": book["note"],
    }


def size_order(account, book, opportunity, quotes, instruments, profile, now, adding=False, min_score=80):
    if opportunity.get("min_score", 80) != min_score:
        raise ValueError("有效分数已变更，等待重新评估")
    if (
        not opportunity.get("valid")
        or opportunity.get("score", 0) < min_score
        or opportunity.get("expires_at", 0) <= now
    ):
        raise ValueError("交易机会无效或已过期")
    key = opportunity["instrument_id"]
    item = instruments[key]
    if (opportunity.get("exchange"), opportunity.get("symbol")) != (item["exchange"], item["symbol"]):
        raise ValueError("交易对配置已变化，等待重新评估")
    if account["kind"] != "paper" and (
        key not in account["allowed"] or item["exchange"] != account["exchange"]
    ):
        raise ValueError("交易对未授权给此账户")
    q = quotes.get(key)
    if not q or q.get("error") or not -5 <= now - q.get("timestamp", 0) <= 15:
        raise ValueError("报价已过期")
    if q["ask"] < q["bid"] or (q["ask"] - q["bid"]) / q["mid"] > 0.003:
        raise ValueError("价差过大")
    snap = valuation(book, quotes, now)
    if not snap["valuation_valid"] or book["halted"]:
        raise ValueError("账户估值失败或已暂停")
    rule = PROFILES[profile]
    equity, free = D(snap["equity"]), D(snap["available"])
    if equity <= 0:
        raise ValueError("账户权益不足")
    day = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat()
    if book["day"] != day:
        book["day"], book["day_equity"] = day, str(equity)
    if equity < D(book["day_equity"]) * (1 - D(rule["risk"]) * 5):
        raise ValueError("达到每日亏损上限")
    plan, side = opportunity["plan"], opportunity["direction"]
    price = D(q["ask"] if side == "long" else q["bid"]) * (1 + SLIP if side == "long" else 1 - SLIP)
    if not adding and not D(plan["entry_low"]) * D(".999") <= price <= D(plan["entry_high"]) * D("1.001"):
        raise ValueError("价格已离开入场区间")
    sizing = next((s for s in account["sizing"] if s["instrument_id"] == key), None)
    if not sizing or not sizing.get("quantity"):
        raise ValueError("请先为此交易对设置币数量和杠杆")
    leverage = sizing["leverage"]
    old = book["positions"].get(key)
    margin_mode = mode(sizing)
    if old and mode(old) != margin_mode:
        raise ValueError("持仓期间不能更改该交易对保证金模式")
    if old and leverage != old["leverage"]:
        raise ValueError("持仓期间不能更改该交易对杠杆")
    if old and old["side"] != side:
        raise ValueError("不能将相反方向的机会用于加仓")
    stop = D(old["stop"] if adding and old else plan["stop"])
    distance = (price - stop) * (1 if side == "long" else -1)
    if distance <= 0:
        raise ValueError("止损方向无效")
    add_count = sizing["add_count"] if sizing["add_count"] is not None else rule["adds"]
    ratios = sizing["add_ratios"] or [0.25] * add_count
    cap = (
        D(sizing["quantity"]) * (1 + sum(map(D, ratios)))
        if sizing["mode"] == "fixed"
        else D(sizing["quantity"])
    )
    existing = D(old["qty"]) if old else D(0)
    risk_used = sum(
        max(D(0), (D(p["entry"]) - D(p["stop"])) * (1 if p["side"] == "long" else -1)) * D(p["qty"])
        for p in book["positions"].values()
    )
    risk_budget = max(D(0), equity * D(rule["risk"]) * 3 - risk_used)
    risk_amount = min(equity * D(rule["risk"]), risk_budget) / (distance / price + 2 * FEE + 2 * SLIP)
    correlated = sum(
        D(p["qty"]) * D(quotes.get(k, {}).get("mid", p["entry"]))
        for k, p in book["positions"].items()
        if instruments.get(k, {}).get("group") == item["group"]
    )
    headroom = min(
        (cap - existing) * price,
        equity * D(rule["exposure"]) - D(snap["exposure"]),
        equity * D(rule["exposure"]) * D(".75") - correlated,
        free * leverage * D(".95"),
        risk_amount,
    )
    # Reserve room for planned additions when the user sets a total maximum.
    target = D(sizing["quantity"]) * price
    if sizing["mode"] == "maximum" and ratios:
        target /= 1 + sum(map(D, ratios))
    if adding:
        if (
            not old
            or old["adds"] >= add_count
            or old["tp_done"]
            or opportunity["score"] < min(100, min_score + 5)
        ):
            raise ValueError("未满足加仓条件")
        if not old.get("initial_qty"):
            raise ValueError("旧仓位缺少初始币数量，不允许自动加仓")
        target = D(old["initial_qty"]) * D(ratios[old["adds"]]) * price
        if price * (1 if side == "long" else -1) <= D(old["entry"]) * (1 if side == "long" else -1):
            raise ValueError("不允许亏损加仓")
    elif old:
        raise ValueError("已有仓位")
    amount = min(target, headroom)
    if sizing["mode"] == "fixed" and amount < target:
        raise ValueError("固定仓位超过可用资金或风险额度")
    if amount < 5:
        raise ValueError("可用仓位额度不足")
    qty = (
        amount / price
        if sizing["mode"] == "fixed"
        else (amount / price).quantize(D(".00000001"), rounding="ROUND_DOWN")
    )
    projected = copy.deepcopy(book)
    projected.pop("source", None)
    projected["cash"] = str(D(book["cash"]) - qty * price * FEE)
    total = qty + existing
    position = {
        "side": side,
        "qty": str(total),
        "entry": str((price * qty + (D(old["entry"]) * existing if old else 0)) / total),
        "margin": str(qty * price / leverage + (D(old["margin"]) if old else 0)),
        "margin_mode": margin_mode,
    }
    projected["positions"][key] = position
    marks = {
        k: D(quotes[k]["bid"] if p["side"] == "long" else quotes[k]["ask"])
        for k, p in projected["positions"].items()
    }
    liquidation = estimates(projected, marks)[key]
    sign = 1 if side == "long" else -1
    if liquidation is not None and (stop - liquidation) * sign / price < D(".03"):
        raise ValueError("止损距离预估强平线过近")
    return {
        "notional": qty * price,
        "qty": qty,
        "price": price,
        "stop": stop,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "liquidation": str(liquidation) if liquidation is not None else None,
    }


class PaperBroker:
    def __init__(self, store):
        self.store = store

    def record(self, db, identity, account_id, action, detail):
        db.execute(
            "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
            (identity, account_id, action, "filled", json.dumps(detail), time.time()),
        )
        self.store.audit(db, "v2_" + action, {"account_id": account_id, **detail})

    def enter(self, account, opportunity, quotes, instruments, profile, adding=False):
        now, aid, key = time.time(), account["id"], opportunity["instrument_id"]
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            current = accounts.get(aid)
            prefs = self.store.read(db, "v2_preferences")
            if not policy_matches(opportunity, prefs):
                raise ValueError("机会的风险或 AI 配置已变更，等待重新评估")
            if current != account or not current["auto"] or prefs["halted"] or prefs["profile"] != profile:
                raise ValueError("账户已停用或配置已变更")
            books = self.store.read(db, "v2_ledgers")
            book = books[aid]
            old = book["positions"].get(key)
            check_version(self.store, db, opportunity, old)
            n = old["adds"] + 1 if adding and old else 0
            identity = hashlib.sha256(
                f"{aid}:{book['generation']}:{opportunity['id']}:{n}".encode()
            ).hexdigest()
            if db.execute("SELECT 1 FROM v2_orders WHERE id=?", (identity,)).fetchone():
                return {"status": "duplicate"}
            sizing = size_order(
                account, book, opportunity, quotes, instruments, profile, now, adding, prefs["min_score"]
            )
            qty, price = sizing["qty"], sizing["price"]
            fee = qty * price * FEE
            book["cash"] = str(D(book["cash"]) - fee)
            book["fees"] = str(D(book["fees"]) + fee)
            if old:
                total = qty + D(old["qty"])
                old["entry"] = str((D(old["entry"]) * D(old["qty"]) + price * qty) / total)
                old["qty"] = str(total)
                old["original_qty"] = str(D(old["original_qty"]) + qty)
                old["margin"] = str(D(old["margin"]) + qty * price / sizing["leverage"])
                old["adds"] += 1
                old["logic_net"] = str(D(old.get("logic_net", 0)) - fee)
                old["liquidation"] = sizing["liquidation"]
            else:
                book["positions"][key] = {
                    "logic_version": opportunity.get("logic_version"),
                    "logic_trade_id": identity,
                    "logic_symbol": opportunity["symbol"],
                    "logic_net": str(-fee),
                    "logic_demo": opportunity.get("demo", True),
                    "logic_policy": json.dumps(
                        {"profile": prefs["profile"], "min_score": prefs["min_score"]}, sort_keys=True
                    ),
                    "side": opportunity["direction"],
                    "qty": str(qty),
                    "original_qty": str(qty),
                    "entry": str(price),
                    "stop": str(sizing["stop"]),
                    "targets": opportunity["plan"]["targets"],
                    "margin": str(qty * price / sizing["leverage"]),
                    "leverage": sizing["leverage"],
                    "margin_mode": sizing["margin_mode"],
                    "liquidation": sizing["liquidation"],
                    "adds": 0,
                    "tp_done": [],
                    "risk_reduced": False,
                    "initial_notional": str(qty * price),
                    "initial_qty": str(qty),
                    "opened_at": now,
                    "signal_id": opportunity["id"],
                    "funding_at": int(now // 28800) * 28800,
                }
            self.store.write(db, "v2_ledgers", books)
            detail = {
                "logic_version": opportunity.get("logic_version"),
                "instrument_id": key,
                "side": opportunity["direction"],
                "qty": str(qty),
                "price": str(price),
                "fee": str(fee),
                "margin_mode": sizing["margin_mode"],
            }
            self.record(db, identity, aid, "add" if adding else "open", detail)
            return {"status": "filled", **detail}

    def close(self, aid, key, quote, ratio=1, reason="manual", target=None):
        if not quote or quote.get("error") or not -5 <= time.time() - quote.get("timestamp", 0) <= 15:
            raise ValueError("缺少新鲜报价，无法按旧价模拟平仓")
        if not 0 < ratio <= 1:
            raise ValueError("减仓比例无效")
        with self.store.transaction() as db:
            books = self.store.read(db, "v2_ledgers")
            book = books[aid]
            p = book["positions"].get(key)
            if not p or (target is not None and target in p["tp_done"]):
                return {"status": "duplicate"}
            qty = min(D(p["qty"]), D(p["original_qty"] if target is not None else p["qty"]) * D(ratio))
            sign = 1 if p["side"] == "long" else -1
            price = D(quote["bid"] if sign == 1 else quote["ask"]) * (1 - SLIP if sign == 1 else 1 + SLIP)
            fee = qty * price * FEE
            pnl = (price - D(p["entry"])) * qty * sign
            # Isolated paper collateral is ring-fenced, including a price gap beyond bankruptcy.
            allocated = D(p["margin"]) * qty / D(p["qty"])
            adjustment = max(D(0), -allocated - (pnl - fee)) if mode(p) == "isolated" else D(0)
            pnl += adjustment
            book["cash"] = str(D(book["cash"]) + pnl - fee)
            book["realized"] = str(D(book["realized"]) + pnl - fee)
            book["fees"] = str(D(book["fees"]) + fee)
            before = D(p["qty"])
            p["qty"], p["margin"] = str(before - qty), str(D(p["margin"]) * (before - qty) / before)
            if target is not None:
                p["tp_done"].append(target)
                # A first take profit moves protection to breakeven, never loosens it.
                p["stop"] = str(
                    max(D(p["stop"]), D(p["entry"])) if sign == 1 else min(D(p["stop"]), D(p["entry"]))
                )
            if reason == "risk_reduce":
                p["risk_reduced"] = True
            p["logic_net"] = str(D(p.get("logic_net", 0)) + pnl - fee)
            if D(p["qty"]) <= D(".000000001"):
                if p.get("logic_version") and p.get("logic_trade_id"):
                    db.execute(
                        "INSERT OR IGNORE INTO logic_trades VALUES (?,?,?,?,?,?)",
                        (
                            p["logic_trade_id"],
                            p["logic_version"],
                            time.time(),
                            float(p["logic_net"]),
                            int(p.get("logic_demo", True)),
                            p.get("logic_policy", "{}"),
                        ),
                    )
                if p.get("logic_version") and p.get("logic_trade_id"):
                    detail = {
                        "symbol": p.get("logic_symbol", key),
                        "side": p["side"],
                        "opened_at": p["opened_at"],
                        "closed_at": time.time(),
                        "average_entry": float(p["entry"]),
                        "final_exit": float(price),
                        "quantity": float(p["original_qty"]),
                        "additions": p["adds"],
                        "exit_reason": reason,
                        "net_pnl": float(p["logic_net"]),
                        "partial_targets": len(p["tp_done"]),
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO logic_trade_details VALUES (?,?)",
                        (p["logic_trade_id"], json.dumps(detail)),
                    )
                del book["positions"][key]
            self.store.write(db, "v2_ledgers", books)
            identity = hashlib.sha256(f"{aid}:{key}:{time.time_ns()}".encode()).hexdigest()
            detail = {
                "logic_version": p.get("logic_version"),
                "instrument_id": key,
                "qty": str(qty),
                "price": str(price),
                "pnl": str(pnl - fee),
                "reason": reason,
                "margin_mode": mode(p),
                "isolated_loss_cap_adjustment": str(adjustment),
            }
            self.record(db, identity, aid, "close", detail)
            return {"status": "filled", **detail}

    def protect(self, account, quotes, opportunities):
        aid = account["id"]
        book = self.store.get("v2_ledgers")[aid]
        for key, p in list(book["positions"].items()):
            q = quotes.get(key)
            if not q or q.get("error") or not -5 <= time.time() - q.get("timestamp", 0) <= 15:
                continue
            sign = 1 if p["side"] == "long" else -1
            price = D(q["bid"] if sign == 1 else q["ask"])
            if (price - D(p["stop"])) * sign <= 0:
                self.close(aid, key, q, reason="stop_loss")
                continue
            # Recalculate shared collateral after each fill/close/funding event.
            current_book = self.store.get("v2_ledgers")[aid]
            snapshot = valuation(current_book, quotes, time.time())
            marked = next((x for x in snapshot["positions"] if x["instrument_id"] == key), None)
            if not marked:
                continue
            if (
                marked["liquidation_verified"]
                and marked["liquidation"] is not None
                and (price - D(marked["liquidation"])) * sign <= price * D(".03")
            ):
                self.close(aid, key, q, reason="liquidation_distance")
                continue
            for i, target in enumerate(p["targets"]):
                if i not in p["tp_done"] and (price - D(target["price"])) * sign >= 0:
                    self.close(aid, key, q, target["ratio"], "take_profit", i)
            opportunity = opportunities.get(key)
            if not opportunity:
                continue
            if opportunity.get("direction") not in (p["side"], "wait", "flat"):
                self.close(aid, key, q, reason="trend_reversal")
            elif (
                any("事件风险" in b for b in opportunity.get("blockers", []))
                or opportunity.get("ai", {}).get("action") == "close"
            ):
                self.close(aid, key, q, reason="event_risk")
            elif (
                not opportunity.get("valid")
                or opportunity.get("expires_at", 0) <= time.time()
                or opportunity.get("ai", {}).get("action") == "reduce"
            ) and not p["risk_reduced"]:
                self.close(aid, key, q, 0.5, "risk_reduce")
            ai = opportunity.get("ai", {})
            if (
                ai.get("intervention_enabled")
                and ai.get("action") == "tighten"
                and ai.get("stop") is not None
            ):
                target = D(ai["stop"])
                if (target - D(p["stop"])) * sign > 0 and (price - target) * sign > 0:
                    with self.store.transaction() as db:
                        books = self.store.read(db, "v2_ledgers")
                        current = books[aid]["positions"].get(key)
                        if current and (target - D(current["stop"])) * sign > 0:
                            current["stop"] = str(target)
                            self.store.write(db, "v2_ledgers", books)
                            self.store.audit(
                                db,
                                "v2_stop_tightened",
                                {"account_id": aid, "instrument_id": key, "stop": str(target)},
                            )
            if ai.get("intervention_enabled") and ai.get("targets") and not p["tp_done"]:
                targets = ai["targets"]
                if all(
                    (D(t["price"]) - D(p["entry"])) * sign > 0
                    and (D(old["price"]) - D(t["price"])) * sign >= 0
                    for t, old in zip(targets, p["targets"], strict=True)
                ):
                    with self.store.transaction() as db:
                        books = self.store.read(db, "v2_ledgers")
                        current = books[aid]["positions"].get(key)
                        if current and not current["tp_done"]:
                            current["targets"] = targets
                            self.store.write(db, "v2_ledgers", books)
            # Funding simulation is explicitly an 8-hour approximation, not an exchange settlement replica.
            boundary = int(time.time() // 28800) * 28800
            if q.get("funding_rate") is not None and boundary > p["funding_at"]:
                with self.store.transaction() as db:
                    books = self.store.read(db, "v2_ledgers")
                    current = books[aid]["positions"].get(key)
                    if current and current["funding_at"] < boundary:
                        fee = D(current["qty"]) * price * D(q["funding_rate"]) * sign
                        if mode(current) == "isolated":
                            fee = min(fee, D(current["margin"]))
                            current["margin"] = str(D(current["margin"]) - fee)
                        books[aid]["cash"] = str(D(books[aid]["cash"]) - fee)
                        current["funding_at"] = boundary
                        current["logic_net"] = str(D(current.get("logic_net", 0)) - fee)
                        self.store.write(db, "v2_ledgers", books)
                        self.store.audit(
                            db,
                            "v2_funding_estimate",
                            {"account_id": aid, "instrument_id": key, "amount": str(fee)},
                        )
