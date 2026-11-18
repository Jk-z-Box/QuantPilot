from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import time

from ..market import decimal as D
from . import journal
from .contracts import entry_position, inverse, native_notional, settlement
from .contracts import pnl as position_pnl
from .logic import check_version
from .margin import CLOSE_FEE_RATE, MAINTENANCE_RATE, estimates, mode
from .models import pair_enabled, policy_matches
from .notifications import trade_event
from .position_automation import loss_add_ready, update_trailing
from .position_changes import levels, position_guard, record_changes
from .risk_budget import BudgetExceeded, capacity
from .risk_policy import fraction, policy, protection_fraction
from .rules import active, effective

FEE = D(".0005")
SLIP = D(".0002")


def ledger(capital, settle="USDT"):
    return {
        "settle": settle,
        "cash": str(capital),
        "initial": str(capital),
        "positions": {},
        "fees": "0",
        "realized": "0",
        "realized_reporting": "0",
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
        qty = D(p["qty"])
        if fresh and q.get("mark") and -5 <= now - q.get("mark_timestamp", 0) <= 15 and D(q["mark"]) > 0:
            price = D(q["mark"])
        pnl = position_pnl(p, price)
        marks[key] = price
        if mode(p) == "cross":
            cross_unreal += pnl
        elif book.get("source") != "exchange":
            pnl = max(pnl, -D(p["margin"]))
        unreal += pnl
        margin += D(p["margin"])
        exposure += abs(native_notional(p, price))
        positions.append(
            {
                **p,
                "instrument_id": key,
                "unrealized": str(pnl),
                "mark": str(price),
                "settle": book.get("settle", "USDT"),
                "current_base_qty": str(qty * D(p["entry"]) / price if inverse(p) else qty),
                "quote_face": str(qty * D(p["entry"])) if inverse(p) else None,
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
        "settle": book.get("settle", "USDT"),
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
    coin = inverse(item)
    settle = settlement(item["symbol"])
    if settle != account.get("settle", "USDT") or settle != book.get("settle", "USDT"):
        raise ValueError("账户、账本与合约的结算币种不一致")
    if coin and item["exchange"] != account["exchange"]:
        raise ValueError("币本位账户只能交易同一交易所的合约，避免合约面值混用")
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
    parameters = effective(opportunity.get("parameters"))
    if not parameters["entry_enabled"] or (adding and not parameters["add_enabled"]):
        raise ValueError("量化逻辑已关闭此开仓或加仓动作")
    equity, free = D(snap["equity"]), D(snap["available"])
    if equity <= 0:
        raise ValueError("账户权益不足")
    day = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat()
    if book["day"] != day:
        book["day"], book["day_equity"] = day, str(equity)
    daily = fraction(account, "daily_loss_pct")
    if daily is not None and equity <= D(book["day_equity"]) * (1 - daily):
        raise ValueError(
            f"达到本账户每日亏损上限（按权益） {daily * 100}%（UTC 日初权益 {book['day_equity']}，当前 {equity}）"
        )
    plan, side = opportunity["plan"], opportunity["direction"]
    price = D(q["ask"] if side == "long" else q["bid"]) * (1 + SLIP if side == "long" else 1 - SLIP)
    if (
        not adding
        and active(parameters, "entry_band_r")
        and not D(plan["entry_low"]) * D(".999") <= price <= D(plan["entry_high"]) * D("1.001")
    ):
        raise ValueError("价格已离开入场区间")
    unit = D(1) if coin else price
    sizing = next((s for s in account["sizing"] if s["instrument_id"] == key), None)
    if not sizing or (sizing.get("mode") != "system" and not sizing.get("quantity")):
        raise ValueError("请先为此交易对设置币数量和杠杆")
    system = sizing["mode"] == "system"
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
    add_count = (
        sizing["add_count"] if sizing["add_count"] is not None else policy(account)["default_add_count"]
    )
    ratios = sizing["add_ratios"] or (
        [parameters["add_default_ratio"]] * add_count
        if active(parameters, "add_default_ratio") and parameters["add_enabled"]
        else []
    )
    if adding and not ratios:
        raise ValueError("未启用加仓比例规则")
    cap = (
        None
        if system
        else D(sizing["quantity"]) * (1 + sum(map(D, ratios)))
        if sizing["mode"] == "fixed"
        else D(sizing["quantity"])
    )
    existing = D(old["qty"]) if old else D(0)
    stop_unit_loss = -position_pnl(entry_position(1, price, side, coin), stop)
    risk_fraction = stop_unit_loss / unit + (
        (FEE + SLIP) * (1 + price / stop) if coin else 2 * FEE + 2 * SLIP
    )
    headroom, budget_rows, correlated, margin_fraction = capacity(
        account, sizing, book, snap, quotes, instruments, item["group"], leverage, risk_fraction
    )
    if cap is not None:
        pair_room = max(D(0), (cap - existing) * unit)
        budget_rows.append(
            {
                "key": "quantity",
                "label": "交易对数量上限",
                "used": str(existing * unit),
                "limit": str(cap * unit),
                "order_headroom": str(pair_room),
            }
        )
        headroom = min(headroom, pair_room)
    # Reserve room for planned additions when the user sets a total maximum.
    target = headroom if system else D(sizing["quantity"]) * unit
    if sizing["mode"] in ("maximum", "system") and ratios:
        target /= 1 + sum(map(D, ratios))
    if adding:
        if (
            not old
            or old["adds"] >= add_count
            or old["tp_done"]
            or opportunity["score"]
            < min(
                100,
                min_score + (parameters["add_score_bonus"] if active(parameters, "add_score_bonus") else 0),
            )
        ):
            raise ValueError("未满足加仓条件")
        if not old.get("initial_qty"):
            raise ValueError("旧仓位缺少初始币数量，不允许自动加仓")
        target = D(old["initial_qty"]) * D(ratios[old["adds"]]) * unit
        if (price - D(old["entry"])) * (1 if side == "long" else -1) <= 0:
            if parameters["profitable_add_only"] and not loss_add_ready(
                account, key, old, quotes[key], opportunity, now
            ):
                raise ValueError("浮亏加仓未启用、未到区间或仍在冷却；不新增风险")
    elif old:
        raise ValueError("已有仓位")
    amount = min(target, headroom)
    if sizing["mode"] == "fixed" and amount < target:
        raise BudgetExceeded(budget_rows, target, settle, "固定仓位超过额度（不自动缩量）")
    if amount * (price if coin else 1) < 5:
        raise BudgetExceeded(budget_rows, D(5) / (price if coin else 1), settle)
    qty = (
        amount / unit
        if sizing["mode"] == "fixed"
        else (amount / unit).quantize(D(".00000001"), rounding="ROUND_DOWN")
    )

    requested_qty = qty
    contract_size = D(q.get("contract_size") or 0)
    step = D(q.get("contract_step") or 0)
    min_contracts = D(q.get("min_contracts") or 1)

    def rounded(quantity):
        if not coin:
            return quantity
        if q.get("inverse") is not True or q.get("settle") != settle or contract_size <= 0 or step <= 0:
            raise ValueError("币本位合约面值或数量精度未核验，暂停新增风险")
        contracts = (quantity * price / contract_size / step).to_integral_value(rounding="ROUND_DOWN") * step
        if contracts < min_contracts:
            raise ValueError("币数量不足一档最小合约张数")
        return contracts * contract_size / price

    qty = rounded(qty)

    def projected_risk(quantity):
        projected = copy.deepcopy(book)
        projected.pop("source", None)
        projected["cash"] = str(D(book["cash"]) - quantity * unit * FEE)
        total = quantity + existing
        position = {
            "side": side,
            "inverse": coin,
            "settle": settle,
            "qty": str(total),
            "entry": str((price * quantity + (D(old["entry"]) * existing if old else 0)) / total),
            "margin": str(quantity * unit / leverage + (D(old["margin"]) if old else 0)),
            "margin_mode": margin_mode,
        }
        projected["positions"][key] = position
        marks = {
            k: D(quotes[k]["bid"] if p["side"] == "long" else quotes[k]["ask"])
            for k, p in projected["positions"].items()
        }
        liq = estimates(projected, marks)[key]
        gap = (stop - liq) * (1 if side == "long" else -1) / price if liq is not None else None
        return liq, gap

    stop_buffer = fraction(account, "stop_liq_pct")
    mark_buffer = fraction(account, "mark_liq_pct")
    if old:
        mark_buffer = max(mark_buffer or D(0), protection_fraction(account, old))
    # Even a disabled optional buffer never permits liquidation before the stop.
    minimum_gap = max(stop_buffer or D(0), (mark_buffer or D(0)) - distance / price)
    liquidation, gap = projected_risk(qty)
    if gap is not None and gap <= minimum_gap:
        if sizing["mode"] in ("system", "maximum") and margin_mode == "cross":
            low, high = D(0), qty
            for _ in range(40):
                mid = (low + high) / 2
                _, trial = projected_risk(mid)
                if trial is None or trial > minimum_gap:
                    low = mid
                else:
                    high = mid
            qty = rounded(low.quantize(D(".00000001"), rounding="ROUND_DOWN"))
            if qty * price < 5:
                raise ValueError("强平风险约束下没有足够的可开仓数量")
            liquidation, gap = projected_risk(qty)
        if gap is not None and gap <= minimum_gap:
            raise ValueError("止损距离预估强平线过近")
    return {
        "notional": qty * unit,
        "quote_notional": qty * price,
        "inverse": coin,
        "settle": settle,
        "requested_qty": requested_qty,
        "contracts": qty * price / contract_size if coin else None,
        "contract_size": contract_size if coin else None,
        "contract_step": step if coin else None,
        "qty": qty,
        "price": price,
        "stop": stop,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "liquidation": str(liquidation) if liquidation is not None else None,
        "risk": {
            "account_equity": str(equity),
            "available_before": str(free),
            "initial_margin": str(qty * unit / leverage),
            "estimated_entry_fee": str(qty * unit * FEE),
            "available_after": str(free - qty * unit / leverage - qty * unit * FEE),
            "estimated_loss_at_stop": str(qty * unit * risk_fraction),
            "existing_exposure": str(snap["exposure"]),
            "correlated_exposure": str(correlated),
            "maintenance_rate_estimate": str(MAINTENANCE_RATE),
            "close_fee_rate_estimate": str(CLOSE_FEE_RATE),
            "maintenance_margin_estimate": str((qty + existing) * unit * MAINTENANCE_RATE),
            "stop_to_liquidation_pct": float(gap * 100) if gap is not None else None,
            "minimum_stop_to_liquidation_pct": float((stop_buffer or D(0)) * 100),
            "minimum_mark_to_liquidation_pct": float((mark_buffer or D(0)) * 100),
            "budget_breakdown": budget_rows,
            "liquidation_status": "estimated" if liquidation is not None else "no_positive_price",
            "settlement_currency": settle,
            "model": ("inverse coin" if coin else "USDT linear")
            + " approximation; other position prices held constant; not exchange risk-tier certification",
            "sizing_mode": sizing["mode"],
            "system_level": sizing.get("system_level") if system else None,
            "available_margin_budget_fraction": float(margin_fraction),
        },
    }


class PaperBroker:
    def __init__(self, store):
        self.store = store
        self.action_review = None

    def record(self, db, identity, account_id, action, detail):
        db.execute(
            "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
            (identity, account_id, action, "filled", json.dumps(detail), time.time()),
        )
        self.store.audit(db, "v2_" + action, {"account_id": account_id, **detail})
        trade_event(self.store, db, identity, account_id, action, detail)

    def enter(self, account, opportunity, quotes, instruments, profile, adding=False):
        now, aid, key = time.time(), account["id"], opportunity["instrument_id"]
        if not pair_enabled(account, key):
            raise ValueError("交易对未在此账户启用")
        if getattr(self, "risk_reviews", None):
            reason = self.risk_reviews.gate(account)
            if reason:
                raise ValueError(reason)
        if self.action_review and account.get("ai_action_enabled"):
            with self.store.connect() as db:
                book = self.store.read(db, "v2_ledgers")[aid]
                old = book["positions"].get(key)
                n = old["adds"] + 1 if adding and old else 0
                identity = hashlib.sha256(
                    f"{aid}:{book['generation']}:{opportunity['id']}:{n}".encode()
                ).hexdigest()
                if db.execute("SELECT 1 FROM v2_orders WHERE id=?", (identity,)).fetchone():
                    return {"status": "duplicate"}
                check_version(self.store, db, opportunity, old)
            # Reject ineligible sizing before spending an AI request; recheck after approval.
            size_order(
                account,
                book,
                opportunity,
                quotes,
                instruments,
                profile,
                now,
                adding,
                self.store.get("v2_preferences")["min_score"],
            )
        if self.action_review:
            verdict = self.action_review.inspect(
                aid, key, "add" if adding else "open", quotes.get(key), opportunity
            )
            if verdict["state"] != "approved":
                raise ValueError(verdict["reason"])
            opportunity = verdict["opportunity"]
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            current = accounts.get(aid)
            prefs = self.store.read(db, "v2_preferences")
            if not policy_matches(opportunity, prefs):
                raise ValueError("机会的风险或 AI 配置已变更，等待重新评估")
            if current != account or not current["auto"] or prefs["halted"] or prefs["profile"] != profile:
                raise ValueError("账户已停用或配置已变更")
            if getattr(self, "risk_reviews", None):
                self.risk_reviews.commit_check(db, account)
            books = self.store.read(db, "v2_ledgers")
            book = books[aid]
            old = book["positions"].get(key)
            check_version(self.store, db, opportunity, old)
            if self.action_review:
                self.action_review.commit_check(db, aid, opportunity)
            n = old["adds"] + 1 if adding and old else 0
            identity = hashlib.sha256(
                f"{aid}:{book['generation']}:{opportunity['id']}:{n}".encode()
            ).hexdigest()
            if db.execute("SELECT 1 FROM v2_orders WHERE id=?", (identity,)).fetchone():
                return {"status": "duplicate"}
            sizing = size_order(
                account, book, opportunity, quotes, instruments, profile, now, adding, prefs["min_score"]
            )
            if self.action_review:
                self.action_review.check_size(opportunity, sizing)
            before_snapshot = valuation(book, quotes, now)
            qty, price = sizing["qty"], sizing["price"]
            fee = sizing["notional"] * FEE
            book["cash"] = str(D(book["cash"]) - fee)
            book["fees"] = str(D(book["fees"]) + fee)
            if old:
                total = qty + D(old["qty"])
                old["entry"] = str((D(old["entry"]) * D(old["qty"]) + price * qty) / total)
                old["qty"] = str(total)
                if "target_basis_qty" in old:
                    old["target_basis_qty"] = str(D(old["target_basis_qty"]) + qty)
                old["original_qty"] = str(D(old["original_qty"]) + qty)
                old["margin"] = str(D(old["margin"]) + sizing["notional"] / sizing["leverage"])
                old["adds"] += 1
                old["last_add_at"] = now
                old["logic_reporting_net"] = str(
                    D(old.get("logic_reporting_net", old.get("logic_net", 0)))
                    - fee * (price if inverse(old) else 1)
                )
                old["logic_net"] = str(D(old.get("logic_net", 0)) - fee)
                old["liquidation"] = sizing["liquidation"]
                old["liquidation_buffer_pct"] = sizing["risk"]["minimum_mark_to_liquidation_pct"]
            else:
                book["positions"][key] = {
                    "parameters": opportunity.get("parameters", {}),
                    "logic_version": opportunity.get("logic_version"),
                    "logic_trade_id": identity,
                    "logic_symbol": opportunity["symbol"],
                    "logic_net": str(-fee),
                    "logic_reporting_net": str(-fee * (price if sizing["inverse"] else 1)),
                    "logic_demo": opportunity.get("demo", True),
                    "logic_policy": json.dumps(
                        {"profile": prefs["profile"], "min_score": prefs["min_score"]}, sort_keys=True
                    ),
                    "inverse": sizing["inverse"],
                    "settle": sizing["settle"],
                    "contract_size": str(sizing["contract_size"]) if sizing["inverse"] else None,
                    "contract_step": str(sizing["contract_step"]) if sizing["inverse"] else None,
                    "side": opportunity["direction"],
                    "qty": str(qty),
                    "original_qty": str(qty),
                    "entry": str(price),
                    "stop": str(sizing["stop"]),
                    "targets": opportunity["plan"]["targets"],
                    "margin": str(sizing["notional"] / sizing["leverage"]),
                    "leverage": sizing["leverage"],
                    "margin_mode": sizing["margin_mode"],
                    "liquidation": sizing["liquidation"],
                    "liquidation_buffer_pct": sizing["risk"]["minimum_mark_to_liquidation_pct"],
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
            trade_id = old.get("logic_trade_id", identity) if old else identity
            journal.entry(
                db,
                aid,
                key,
                trade_id,
                account,
                opportunity,
                quotes[key],
                sizing,
                before_snapshot,
                valuation(book, quotes, now),
                adding,
                getattr(self, "signal_frames", {}).get(opportunity.get("id")),
            )
            detail = {
                "logic_version": opportunity.get("logic_version"),
                "instrument_id": key,
                "side": opportunity["direction"],
                "qty": str(qty),
                "price": str(price),
                "fee": str(fee),
                "margin_mode": sizing["margin_mode"],
                "settle": sizing["settle"],
                "contracts": str(sizing["contracts"]) if sizing["inverse"] else None,
            }
            self.record(db, identity, aid, "add" if adding else "open", detail)
            return {"status": "filled", **detail}

    def close(self, aid, key, quote, ratio=1, reason="manual", target=None):
        if self.action_review:
            verdict = self.action_review.inspect(aid, key, reason, quote, ratio=ratio)
            if verdict["state"] not in ("approved", "fallback"):
                return {"status": verdict["state"], "reason": verdict["reason"]}
            ratio = verdict["ratio"]
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
            qty = min(
                D(p["qty"]),
                D(p.get("target_basis_qty", p["original_qty"]) if target is not None else p["qty"])
                * D(ratio),
            )
            if inverse(p) and qty < D(p["qty"]):
                cs, step = D(p["contract_size"]), D(p.get("contract_step") or 1)
                contracts = (qty * D(p["entry"]) / cs / step).to_integral_value(
                    rounding="ROUND_CEILING"
                ) * step
                qty = min(D(p["qty"]), contracts * cs / D(p["entry"]))
            sign = 1 if p["side"] == "long" else -1
            price = D(quote["bid"] if sign == 1 else quote["ask"]) * (1 - SLIP if sign == 1 else 1 + SLIP)
            fee = native_notional(p, price) * qty / D(p["qty"]) * FEE
            pnl = position_pnl(p, price, qty)
            # Isolated paper collateral is ring-fenced, including a price gap beyond bankruptcy.
            allocated = D(p["margin"]) * qty / D(p["qty"])
            adjustment = max(D(0), -allocated - (pnl - fee)) if mode(p) == "isolated" else D(0)
            pnl += adjustment
            book["cash"] = str(D(book["cash"]) + pnl - fee)
            book["realized_reporting"] = str(
                D(book.get("realized_reporting", book["realized"]))
                + (pnl - fee) * (price if inverse(p) else 1)
            )
            book["realized"] = str(D(book["realized"]) + pnl - fee)
            book["fees"] = str(D(book["fees"]) + fee)
            before = D(p["qty"])
            p["qty"], p["margin"] = str(before - qty), str(D(p["margin"]) * (before - qty) / before)
            if target is not None:
                p["tp_done"].append(target)
                # A first take profit moves protection to breakeven, never loosens it.
                if effective(p.get("parameters"))["breakeven_enabled"]:
                    p["stop"] = str(
                        max(D(p["stop"]), D(p["entry"])) if sign == 1 else min(D(p["stop"]), D(p["entry"]))
                    )
            if reason == "trailing_take_profit":
                p.setdefault("trailing", {})["done"] = True
            if reason == "risk_reduce":
                p["risk_reduced"] = True
            p["logic_reporting_net"] = str(
                D(p.get("logic_reporting_net", p.get("logic_net", 0)))
                + (pnl - fee) * (price if inverse(p) else 1)
            )
            p["logic_net"] = str(D(p.get("logic_net", 0)) + pnl - fee)
            if D(p["qty"]) <= D(".000000001"):
                if p.get("logic_version") and p.get("logic_trade_id"):
                    db.execute(
                        "INSERT OR IGNORE INTO logic_trades VALUES (?,?,?,?,?,?)",
                        (
                            p["logic_trade_id"],
                            p.get("entry_logic_version", p["logic_version"]),
                            time.time(),
                            float(p.get("logic_reporting_net", p["logic_net"])),
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
                        "net_pnl": float(p.get("logic_reporting_net", p["logic_net"])),
                        "partial_targets": len(p["tp_done"]),
                        "entry_logic_version": p.get("entry_logic_version", p["logic_version"]),
                        "final_logic_version": p["logic_version"],
                        "mixed_versions": p.get("entry_logic_version", p["logic_version"])
                        != p["logic_version"],
                        "evidence_id": p["logic_trade_id"],
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
                "settle": book.get("settle", "USDT"),
                "reason": reason,
                "margin_mode": mode(p),
                "isolated_loss_cap_adjustment": str(adjustment),
                "fee": str(fee),
                "quote": quote,
                "planned_stop": p["stop"],
                "trade_net_pnl": p.get("logic_net"),
                "trade_reporting_net_pnl": p.get("logic_reporting_net", p.get("logic_net")),
                "reporting_currency": "USD reference (USDT at par)",
                "entry_logic_version": p.get("entry_logic_version", p.get("logic_version")),
            }
            journal.close(db, aid, key, p, detail, D(p["qty"]) <= D(".000000001"))
            self.record(db, identity, aid, "close", detail)
            return {"status": "filled", **detail}

    def protect(self, account, quotes, opportunities):
        aid = account["id"]
        book = self.store.get("v2_ledgers")[aid]
        for key, p in list(book["positions"].items()):
            parameters = effective(p.get("parameters"))
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
                and (price - D(marked["liquidation"])) * sign <= price * protection_fraction(account, p)
            ):
                self.close(aid, key, q, reason="liquidation_distance")
                continue
            trail_hit, trail_ratio = update_trailing(self.store, account, key, q, opportunities.get(key))
            if trail_hit:
                self.close(aid, key, q, ratio=trail_ratio, reason="trailing_take_profit")
                continue
            for i, target in enumerate(p["targets"]):
                if (
                    target.get("enabled", True)
                    and i not in p["tp_done"]
                    and (price - D(target["price"])) * sign >= 0
                ):
                    self.close(aid, key, q, target["ratio"], "take_profit", i)
            opportunity = opportunities.get(key)
            if not opportunity:
                continue
            if (
                not opportunity.get("position_reviewed")
                and parameters["trend_exit_enabled"]
                and opportunity.get("direction")
                not in (
                    p["side"],
                    "wait",
                    "flat",
                )
            ):
                self.close(aid, key, q, reason="trend_reversal")
            elif (
                not opportunity.get("position_reviewed")
                and parameters["event_exit_enabled"]
                and any("事件风险" in b for b in opportunity.get("blockers", []))
            ) or opportunity.get("ai", {}).get("action") == "close":
                self.close(aid, key, q, reason="event_risk")
            elif (
                (
                    not opportunity.get("position_reviewed")
                    and parameters["invalid_signal_reduce_enabled"]
                    and (not opportunity.get("valid") or opportunity.get("expires_at", 0) <= time.time())
                )
                or (
                    parameters["price_reduce_enabled"]
                    and not account.get("position_review_enabled")
                    and (price - D(p["entry"])) * sign
                    <= -abs(D(p["entry"]) - D(p["stop"])) * D(parameters["reduce_trigger_r"])
                )
                or opportunity.get("ai", {}).get("action") == "reduce"
            ) and not p["risk_reduced"]:
                self.close(
                    aid,
                    key,
                    q,
                    opportunity.get("ai", {}).get("reduce_ratio", parameters["risk_reduce_ratio"]),
                    "risk_reduce",
                )
            ai = opportunity.get("ai", {})
            if ai.get("intervention_enabled"):
                with self.store.transaction() as db:
                    books = self.store.read(db, "v2_ledgers")
                    current = books[aid]["positions"].get(key)
                    if not current or (
                        ai.get("position_guard") is not None
                        and position_guard(current) != ai["position_guard"]
                    ):
                        continue
                    before_levels = levels(current)
                    if ai.get("action") == "tighten" and ai.get("stop") is not None:
                        target = D(ai["stop"])
                        if (target - D(current["stop"])) * sign > 0 and (price - target) * sign > 0:
                            current["stop"] = str(target)
                            self.store.audit(
                                db,
                                "v2_stop_tightened",
                                {"account_id": aid, "instrument_id": key, "stop": str(target)},
                            )
                    targets = ai.get("targets")
                    if (
                        targets
                        and len(targets) == len(current["targets"])
                        and all(
                            (
                                t == old
                                if i in current["tp_done"]
                                else (
                                    (D(t["price"]) - D(current["entry"])) * sign > 0
                                    and (D(old["price"]) - D(t["price"])) * sign >= 0
                                    and (not t.get("enabled", True) or (D(t["price"]) - price) * sign > 0)
                                )
                            )
                            for i, (t, old) in enumerate(zip(targets, current["targets"], strict=True))
                        )
                    ):
                        allocation_changed = any(
                            (t.get("enabled", True), t["ratio"]) != (old.get("enabled", True), old["ratio"])
                            for t, old in zip(targets, current["targets"], strict=True)
                        )
                        if not allocation_changed or not current["tp_done"]:
                            current["targets"] = copy.deepcopy(targets)
                            if allocation_changed or (
                                ai.get("take_profit_mode", "keep") != "keep" and not current["tp_done"]
                            ):
                                current["target_basis_qty"] = current["qty"]
                    self.store.write(db, "v2_ledgers", books)
                    if ai.get("position_review"):
                        record_changes(
                            self.store,
                            db,
                            aid,
                            key,
                            before_levels,
                            levels(current),
                            ai["reason"],
                            ai["reviewed_at"],
                        )
            # Funding simulation is explicitly an 8-hour approximation, not an exchange settlement replica.
            boundary = int(time.time() // 28800) * 28800
            if q.get("funding_rate") is not None and boundary > p["funding_at"]:
                with self.store.transaction() as db:
                    books = self.store.read(db, "v2_ledgers")
                    current = books[aid]["positions"].get(key)
                    if current and current["funding_at"] < boundary:
                        fee = native_notional(current, price) * D(q["funding_rate"]) * sign
                        if mode(current) == "isolated":
                            fee = min(fee, D(current["margin"]))
                            current["margin"] = str(D(current["margin"]) - fee)
                        books[aid]["cash"] = str(D(books[aid]["cash"]) - fee)
                        current["funding_at"] = boundary
                        current["logic_reporting_net"] = str(
                            D(current.get("logic_reporting_net", current.get("logic_net", 0)))
                            - fee * (price if inverse(current) else 1)
                        )
                        current["logic_net"] = str(D(current.get("logic_net", 0)) - fee)
                        journal.event(
                            db,
                            aid,
                            key,
                            "funding",
                            {"fee": str(fee), "quote": q},
                            current.get("logic_trade_id"),
                        )
                        self.store.write(db, "v2_ledgers", books)
                        self.store.audit(
                            db,
                            "v2_funding_estimate",
                            {"account_id": aid, "instrument_id": key, "amount": str(fee)},
                        )
