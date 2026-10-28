from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import ccxt

from ..market import decimal as D
from .accounts import ledger, size_order
from .limits import contract_limits
from .logic import check_version
from .models import policy_matches


class ExchangeBroker:
    """Private clients are isolated per account. Uncertain writes are never retried."""

    def __init__(self, store, vault, factory=None):
        self.store, self.vault = store, vault
        self.factory = factory or (lambda name, config: getattr(ccxt, name)(config))
        self.clients, self.locks = {}, {}
        self.protection_checks = {}
        self.guard = threading.RLock()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_managed','{}')")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_live_days','{}')")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('v2_execution_blocks','{}')")

    def client(self, account):
        credentials = self.vault.get("v2:account:" + account["id"])
        if not credentials:
            raise ValueError("请先保存账户 API 密钥")
        fingerprint = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()
        key = (account["id"], account["exchange"], account["kind"], fingerprint)
        with self.guard:
            if key not in self.clients:
                client = self.factory(
                    "binanceusdm" if account["exchange"] == "binance" else account["exchange"],
                    {
                        "enableRateLimit": True,
                        "timeout": 8000,
                        "options": {"defaultType": "swap"},
                        **credentials,
                    },
                )
                if account["kind"] == "demo":
                    client.set_sandbox_mode(True)
                self.clients[key] = client
            self.locks.setdefault(account["id"], threading.RLock())
            return self.clients[key]

    def availability(self, account):
        if account["kind"] == "paper":
            return {"ready": True, "reason": "本地模拟账户"}
        if self.store.get("v2_execution_blocks").get(account["id"]):
            return {"ready": False, "reason": "执行异常已锁定；需要人工核对后由管理员解除"}
        if not self.vault.get("v2:account:" + account["id"]):
            return {"ready": False, "reason": "未配置账户密钥"}
        if account["kind"] == "demo":
            ready = os.getenv("QP_DEMO_EXECUTION") == "1"
            return {"ready": ready, "reason": "交易所测试环境" if ready else "测试网下单开关未启用"}
        if os.getenv("QP_LIVE_EXECUTION") != "1":
            return {"ready": False, "reason": "实盘总开关关闭；可读取账户"}
        path = self.store.directory / "certifications" / f"{account['id']}.json"
        try:
            cert = json.loads(path.read_text())
            credentials = self.vault.get("v2:account:" + account["id"])
            fingerprint = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()
            required = (
                "sandbox_fill",
                "native_stop",
                "partial_take_profit",
                "reduce_only",
                "reconnect_reconcile",
                "timeout_no_duplicate",
            )
            ready = (
                cert["version"] == "0.5.0"
                and cert["exchange"] == account["exchange"]
                and cert["credential_fingerprint"] == fingerprint
                and all(cert["checks"].get(k) is True for k in required)
                and 0 <= time.time() - cert["verified_at"] < 7 * 86400
            )
            if path.stat().st_mode & 0o077:
                ready = False
        except (OSError, ValueError, KeyError, TypeError):
            ready = False
        return {"ready": ready, "reason": "验收记录有效" if ready else "缺少与当前账户匹配的实盘验收记录"}

    def read(self, account):
        client = self.client(account)
        with self.locks[account["id"]]:
            bal = client.fetch_balance({"type": "swap"})
            positions = client.fetch_positions()
            gate_mode = (
                client.privateFuturesGetSettleAccounts({"settle": "usdt"}).get("in_dual_mode")
                if account["exchange"] == "gate"
                else None
            )
        out = []
        for p in positions:
            if not p.get("contracts"):
                continue
            if account["exchange"] == "gate":
                p = {**p, "hedged": gate_mode}
            out.append(
                {
                    k: p.get(k)
                    for k in (
                        "symbol",
                        "side",
                        "contracts",
                        "contractSize",
                        "entryPrice",
                        "markPrice",
                        "notional",
                        "unrealizedPnl",
                        "liquidationPrice",
                        "leverage",
                        "marginMode",
                        "hedged",
                        "initialMargin",
                    )
                }
            )
        usdt = bal.get("USDT", {})
        return {
            "equity": str(D(usdt.get("total") or 0)),
            "available": str(D(usdt.get("free") or 0)),
            "positions": out,
            "checked_at": time.time(),
            "source": "exchange_account",
            "execution": self.availability(account),
        }

    def order(self, account, client, symbol, kind, side, amount, price, params):
        params = dict(params)
        if account["exchange"] == "gate":
            if params.get("clientOrderId"):
                params["clientOrderId"] = "t-" + params["clientOrderId"]
            params.pop("hedged", None)
            params.pop("marginMode", None)
        return client.create_order(symbol, kind, side, amount, price, params)

    def persist(self, identity, state, result):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE v2_orders SET state=?,result=? WHERE id=?", (state, json.dumps(result), identity)
            )

    def freeze(self, aid, code):
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            if aid in accounts:
                accounts[aid]["auto"] = False
                self.store.write(db, "v2_accounts", accounts)
            blocks = self.store.read(db, "v2_execution_blocks")
            blocks[aid] = {"reason": code, "at": time.time()}
            self.store.write(db, "v2_execution_blocks", blocks)
            statuses = self.store.read(db, "v2_external")
            statuses[aid] = {"error": code, "checked_at": time.time(), "reconciliation_required": True}
            self.store.write(db, "v2_external", statuses)
            self.store.audit(db, "v2_execution_paused", {"account_id": aid, "reason": code})

    def enter(self, account, opportunity, quotes, instruments, profile, adding=False):
        if not self.availability(account)["ready"]:
            raise ValueError("账户尚未通过执行门槛")
        if account["kind"] == "paper":
            raise ValueError("模拟账户不能使用交易所执行通道")
        aid, key = account["id"], opportunity["instrument_id"]
        item = instruments[key]
        if key not in account["allowed"] or item["exchange"] != account["exchange"]:
            raise ValueError("交易对未授权")
        client = self.client(account)
        with self.locks[aid]:
            snapshot = self.read(account)
            mode = (
                {"hedged": client.privateFuturesGetSettleAccounts({"settle": "usdt"}).get("in_dual_mode")}
                if account["exchange"] == "gate"
                else client.fetch_position_mode(item["symbol"])
            )
            if mode.get("hedged") is not False:
                raise ValueError("仅支持单向持仓，请先确认账户持仓模式")
            book = self.risk_book(account, snapshot, instruments)
            existing = book["positions"].get(key)
            adding = bool(existing)
            if adding:
                sign = 1 if existing["side"] == "long" else -1
                trigger = D(existing["entry"]) + (D(existing["entry"]) - D(existing["stop"])) * D(".75")
                if (D(quotes[key]["mid"]) - trigger) * sign < 0:
                    raise ValueError("未到加仓观察价")
            min_score = self.store.get("v2_preferences")["min_score"]
            sizing = size_order(
                account, book, opportunity, quotes, instruments, profile, time.time(), adding, min_score
            )
            client.load_markets()
            market = client.market(item["symbol"])
            if not market.get("linear") or not market.get("swap") or market.get("settle") != "USDT":
                raise ValueError("非受支持的线性永续合约")
            contract_size = D(market["contractSize"])
            amount = float(client.amount_to_precision(item["symbol"], str(sizing["qty"] / contract_size)))
            setting = next(s for s in account["sizing"] if s["instrument_id"] == key)
            if setting["mode"] == "fixed" and abs(D(amount) * contract_size - sizing["qty"]) > D(
                ".000000000001"
            ):
                raise ValueError("固定币数量不符合交易所数量步长，请调整仓位设置")
            if sizing["leverage"] > 1:
                total_qty = float(D(amount) * contract_size + (D(existing["qty"]) if existing else 0))
                limits = contract_limits(
                    client, account["exchange"], item["symbol"], float(sizing["price"]) * total_qty, total_qty
                )
                if sizing["leverage"] > limits["max_leverage"]:
                    raise ValueError("该仓位数量对应的交易所档位不支持所选杠杆")
            if amount <= 0:
                raise ValueError("低于最小合约数量")
            minimum = market.get("limits", {}).get("amount", {}).get("min")
            min_cost = market.get("limits", {}).get("cost", {}).get("min")
            if (minimum and amount < minimum) or (
                min_cost and D(amount) * contract_size * sizing["price"] < D(min_cost)
            ):
                raise ValueError("低于交易所最小订单")
            identity = (
                "qp"
                + hashlib.sha256(
                    f"{aid}:{opportunity['id']}:{existing['adds'] + 1 if adding else 0}".encode()
                ).hexdigest()[:24]
            )
            with self.store.transaction() as db:
                current = self.store.read(db, "v2_accounts").get(aid)
                check_version(self.store, db, opportunity, existing)
                if (
                    current != account
                    or not current["auto"]
                    or self.store.read(db, "v2_preferences")["halted"]
                    or self.store.read(db, "v2_preferences")["profile"] != profile
                    or self.store.read(db, "v2_preferences")["min_score"] != min_score
                    or not policy_matches(opportunity, self.store.read(db, "v2_preferences"))
                ):
                    raise ValueError("账户已停止或配置已变化")
                if db.execute("SELECT 1 FROM v2_orders WHERE id=?", (identity,)).fetchone():
                    return {"status": "duplicate_or_pending"}
                unresolved = db.execute(
                    "SELECT 1 FROM v2_orders WHERE account_id=? AND state IN ('sending','unknown')",
                    (aid,),
                ).fetchone()
                if unresolved:
                    raise ValueError("存在待核对订单")
                db.execute(
                    "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
                    (identity, aid, "open", "sending", "{}", time.time()),
                )
            filled, stop, targets, order = 0, {}, [], {}
            try:
                # Configure only the specifically authorized symbol, never global account mode.
                if not adding:
                    try:
                        if account["exchange"] != "gate":
                            client.set_margin_mode("isolated", item["symbol"])
                    except ccxt.MarginModeAlreadySet:
                        pass
                    client.set_leverage(sizing["leverage"], item["symbol"], {"marginMode": "isolated"})
                side = "buy" if opportunity["direction"] == "long" else "sell"
                params = {
                    "clientOrderId": identity,
                    "timeInForce": "IOC",
                    "marginMode": "isolated",
                    "hedged": False,
                }
                price = float(client.price_to_precision(item["symbol"], str(sizing["price"])))
                order = self.order(account, client, item["symbol"], "limit", side, amount, price, params)
                if not order.get("id"):
                    raise ValueError("缺少订单 ID")
                self.persist(
                    identity,
                    "sending",
                    {"order_id": order["id"], "symbol": item["symbol"], "client_id": identity},
                )
                order = client.fetch_order(order["id"], item["symbol"])
                if (
                    order.get("status") not in ("closed", "canceled", "rejected", "expired")
                    or order.get("filled") is None
                ):
                    raise ValueError("订单状态未确定")
                filled = float(order["filled"])
                if filled == 0:
                    self.persist(identity, "unfilled", {"order_id": order["id"], "symbol": item["symbol"]})
                    return {"status": "unfilled"}
                opposite = "sell" if side == "buy" else "buy"
                stop = self.order(
                    account,
                    client,
                    item["symbol"],
                    "market",
                    opposite,
                    filled,
                    None,
                    {
                        "stopLossPrice": float(
                            client.price_to_precision(item["symbol"], str(sizing["stop"]))
                        ),
                        "reduceOnly": True,
                        "marginMode": "isolated",
                        "clientOrderId": identity + "s",
                    },
                )
                if not stop.get("id"):
                    raise ValueError("保护止损未确认")
                targets = []
                remaining = D(filled)
                for i, t in enumerate(opportunity["plan"]["targets"]):
                    qty = (
                        D(client.amount_to_precision(item["symbol"], str(D(filled) * D(t["ratio"]))))
                        if i == 0
                        else remaining
                    )
                    if qty <= 0:
                        raise ValueError("仓位不足以分批止盈")
                    tp = self.order(
                        account,
                        client,
                        item["symbol"],
                        "limit",
                        opposite,
                        float(qty),
                        float(client.price_to_precision(item["symbol"], str(t["price"]))),
                        {
                            "reduceOnly": True,
                            "marginMode": "isolated",
                            "clientOrderId": identity + "t" + str(i),
                        },
                    )
                    if not tp.get("id"):
                        raise ValueError("止盈单未确认")
                    targets.append(tp["id"])
                    remaining -= qty
                result = {
                    "stop_price": str(sizing["stop"]),
                    "order_id": order["id"],
                    "symbol": item["symbol"],
                    "instrument_id": key,
                    "filled": filled,
                    "stop_id": stop["id"],
                    "target_ids": targets,
                    "client_id": identity,
                }
                actual = self.read(account)["positions"]
                position = next(
                    (p for p in actual if p.get("symbol") == item["symbol"] and p.get("contracts")), None
                )
                if (
                    not position
                    or not position.get("liquidationPrice")
                    or position.get("hedged") is not False
                    or position.get("marginMode") != "isolated"
                ):
                    raise ValueError("实际持仓模式或强平线不可验证")
                liquidation = D(position["liquidationPrice"])
                mark = D(position["markPrice"])
                sign = 1 if opportunity["direction"] == "long" else -1
                if (mark - liquidation) * sign / mark < D(".05") or (
                    sizing["stop"] - liquidation
                ) * sign / mark < D(".03"):
                    raise ValueError("实际强平距离不足")
                with self.store.transaction() as db:
                    managed = self.store.read(db, "v2_managed")
                    positions = managed.setdefault(aid, {})
                    old = positions.get(key)
                    base_qty = D(filled) * contract_size
                    initial = (
                        old["initial_notional"]
                        if old
                        else str(base_qty * D(order.get("average") or sizing["price"]))
                    )
                    positions[key] = {
                        "logic_version": old.get("logic_version") if old else opportunity.get("logic_version"),
                        "side": opportunity["direction"],
                        "qty": str(D(position["contracts"]) * contract_size),
                        "entry": str(position["entryPrice"]),
                        "stop": str(sizing["stop"]),
                        "original_qty": str(D(old["original_qty"]) + base_qty if old else base_qty),
                        "targets": opportunity["plan"]["targets"],
                        "margin": str(position.get("initialMargin") or 0),
                        "leverage": sizing["leverage"],
                        "liquidation": str(liquidation),
                        "adds": old["adds"] + 1 if old else 0,
                        "tp_done": old["tp_done"] if old else [],
                        "initial_notional": initial,
                        "initial_qty": old.get("initial_qty") if old else str(base_qty),
                        "legs": (old["legs"] if old else []) + [result],
                        "risk_reduced": old["risk_reduced"] if old else False,
                        "symbol": item["symbol"],
                    }
                    self.store.write(db, "v2_managed", managed)
                    db.execute(
                        "UPDATE v2_orders SET state='protected',result=? WHERE id=?",
                        (json.dumps(result), identity),
                    )
                return {"status": "protected", **result}
            except Exception:
                if filled > 0:
                    # Known fills with missing protection trigger one reduce-only emergency exit.
                    # If its response is lost, freeze; never retry and risk a duplicate transaction.
                    try:
                        emergency = self.order(
                            account,
                            client,
                            item["symbol"],
                            "market",
                            "sell" if opportunity["direction"] == "long" else "buy",
                            filled,
                            None,
                            {"reduceOnly": True, "marginMode": "isolated", "clientOrderId": identity + "e"},
                        )
                        if not emergency.get("id"):
                            raise ValueError("紧急退出未确认")
                        closed = client.fetch_order(emergency["id"], item["symbol"])
                        if closed.get("status") != "closed" or float(closed.get("filled") or 0) < filled:
                            raise ValueError("紧急退出尚未全部成交")
                        self.persist(
                            identity,
                            "emergency_closed",
                            {
                                "order_id": order.get("id"),
                                "exit_id": emergency["id"],
                                "symbol": item["symbol"],
                                "instrument_id": key,
                            },
                        )
                        self.cancel_protection(client, item["symbol"], stop.get("id"), targets)
                        self.freeze(aid, "保护失败，已确认紧急减仓；账户暂停，需重新核对")
                        return {"status": "emergency_closed"}
                    except Exception:
                        self.freeze(aid, "紧急退出结果未确认；请立即核对交易所仓位")
                # A lost response can mean an order WAS accepted. Keep the durable ID, block the account.
                self.freeze(aid, "订单结果或保护单未确认，请核对交易所；系统不会重复发送")
                with self.store.transaction() as db:
                    db.execute("UPDATE v2_orders SET state='unknown' WHERE id=?", (identity,))
                raise ValueError("执行状态不确定，账户已暂停；请在交易所核对仓位和保护单") from None

    def risk_book(self, account, snapshot, instruments):
        aid = account["id"]
        managed = self.store.get("v2_managed").get(aid, {})
        book = ledger(snapshot["equity"])
        unreal, margin = D(0), D(0)
        for position in snapshot["positions"]:
            key = next(
                (
                    k
                    for k, i in instruments.items()
                    if i["exchange"] == account["exchange"] and i["symbol"] == position["symbol"]
                ),
                None,
            )
            original = managed.get(key)
            if not original or key not in account["allowed"]:
                raise ValueError("发现外部或未授权仓位，暂停新增风险并要求人工核对")
            if (
                position["side"] != original["side"]
                or position.get("hedged") is not False
                or position.get("marginMode") != "isolated"
            ):
                raise ValueError("账户持仓方向或保证金模式不一致")
            qty = D(position["contracts"]) * D(position["contractSize"])
            if qty > D(original["qty"]) * D("1.000001"):
                raise ValueError("发现未登记加仓，暂停自动交易")
            liq, mark = D(position["liquidationPrice"] or 0), D(position["markPrice"] or 0)
            sign = 1 if position["side"] == "long" else -1
            if liq <= 0 or mark <= 0 or (mark - liq) * sign / mark < D(".05"):
                raise ValueError("实际强平距离不足或不可验证")
            initial = D(position.get("initialMargin") or 0)
            if initial <= 0:
                raise ValueError("保证金数据不可验证")
            p = {
                **original,
                "qty": str(qty),
                "entry": str(position["entryPrice"]),
                "liquidation": str(liq),
                "margin": str(initial),
            }
            if qty < D(original["qty"]):
                p["tp_done"] = [0]
            book["positions"][key] = p
            unreal += D(position.get("unrealizedPnl") or 0)
            margin += initial
        # Use the lower of wallet-based and available-margin-based equity estimates.
        conservative_equity = min(D(snapshot["equity"]), D(snapshot["available"]) + margin)
        book["cash"] = str(conservative_equity - unreal)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.store.transaction() as db:
            days = self.store.read(db, "v2_live_days")
            value = days.get(aid)
            if not value or value["day"] != day:
                value = {"day": day, "equity": str(conservative_equity)}
                days[aid] = value
                self.store.write(db, "v2_live_days", days)
            book["day"], book["day_equity"] = day, value["equity"]
        return book

    def cancel_protection(self, client, symbol, stop_id, target_ids):
        failed = []
        for oid in ([stop_id] if stop_id else []) + list(target_ids):
            try:
                client.cancel_order(oid, symbol, {"stop": oid == stop_id})
            except ccxt.OrderNotFound:
                continue
            except Exception:
                failed.append(oid)
        return failed

    def manage(self, account, quotes, opportunities, instruments):
        aid = account["id"]
        managed = self.store.get("v2_managed").get(aid, {})
        if not managed or not self.availability(account)["ready"]:
            return
        client = self.client(account)
        with self.locks[aid]:
            snapshot = self.read(account)
            with self.store.transaction() as db:
                external = self.store.read(db, "v2_external")
                external[aid] = snapshot
                self.store.write(db, "v2_external", external)
            for key, p in list(managed.items()):
                if key not in account["allowed"] or key not in instruments:
                    self.freeze(aid, "持仓交易对授权已移除，原生保护单保留，请人工核对")
                    continue
                current = next((x for x in snapshot["positions"] if x["symbol"] == p["symbol"]), None)
                if not current:
                    all_failed = []
                    for leg in p["legs"]:
                        all_failed += self.cancel_protection(
                            client, p["symbol"], leg["stop_id"], leg["target_ids"]
                        )
                    if all_failed:
                        self.freeze(aid, "仓位已退出，但保护单清理未确认")
                        continue
                    with self.store.transaction() as db:
                        m = self.store.read(db, "v2_managed")
                        m[aid].pop(key, None)
                        self.store.write(db, "v2_managed", m)
                        db.execute(
                            "UPDATE v2_orders SET state='closed' WHERE account_id=? AND state='protected' AND json_extract(result,'$.instrument_id')=?",
                            (aid, key),
                        )
                    continue
                protection_missing = False
                if time.time() - self.protection_checks.get((aid, key), 0) > 60:
                    try:
                        for oid in {leg["stop_id"] for leg in p["legs"]}:
                            stop_state = client.fetch_order(oid, p["symbol"], {"stop": True})
                            if stop_state.get("status") != "open":
                                protection_missing = True
                        self.protection_checks[(aid, key)] = time.time()
                    except Exception:
                        self.freeze(aid, "无法核验原生止损单，停止新增风险；请在交易所核对保护")
                        continue
                q, op = quotes.get(key), opportunities.get(key)
                if not q or q.get("error") or not -5 <= time.time() - q.get("timestamp", 0) <= 15:
                    continue  # Keep exchange-hosted protection active through data outages.
                qty = D(current["contracts"])
                cs = D(current["contractSize"])
                if qty * cs > D(p["qty"]) * D("1.000001") or current["side"] != p["side"]:
                    self.freeze(aid, "仓位数量或方向发生外部变化，停止自动干预")
                    continue
                if not op and not protection_missing:
                    continue
                op = op or {}
                sign = 1 if p["side"] == "long" else -1
                price = D(q["mid"])
                liq = D(current.get("liquidationPrice") or 0)
                close = (
                    protection_missing
                    or op.get("direction") not in (p["side"], "wait", "flat")
                    or any("事件风险" in b for b in op.get("blockers", []))
                    or op.get("ai", {}).get("action") == "close"
                    or (liq > 0 and (price - liq) * sign / price < D(".05"))
                    or (price - D(p["stop"])) * sign <= 0
                )
                reduce = (
                    not op.get("valid")
                    or op.get("expires_at", 0) <= time.time()
                    or op.get("ai", {}).get("action") == "reduce"
                ) and not p["risk_reduced"]
                if close or reduce:
                    identity = (
                        "qp"
                        + hashlib.sha256(
                            f"{aid}:{key}:{p['legs'][0]['client_id']}:{'exit' if close else 'reduce'}".encode()
                        ).hexdigest()[:24]
                    )
                    with self.store.transaction() as db:
                        if db.execute("SELECT 1 FROM v2_orders WHERE id=?", (identity,)).fetchone():
                            continue
                        db.execute(
                            "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
                            (identity, aid, "close", "sending", "{}", time.time()),
                        )
                    try:
                        amount = float(
                            client.amount_to_precision(p["symbol"], str(qty if close else qty / 2))
                        )
                        if amount <= 0:
                            raise ValueError("减仓量低于精度")
                        result = self.order(
                            account,
                            client,
                            p["symbol"],
                            "market",
                            "sell" if sign == 1 else "buy",
                            amount,
                            None,
                            {"reduceOnly": True, "marginMode": "isolated", "clientOrderId": identity},
                        )
                        confirmed = client.fetch_order(result["id"], p["symbol"])
                        if (
                            confirmed.get("status") != "closed"
                            or float(confirmed.get("filled") or 0) < amount
                        ):
                            raise ValueError("减仓结果待核对")
                        self.persist(
                            identity,
                            "filled",
                            {
                                "instrument_id": key,
                                "qty": str(D(amount) * cs),
                                "price": confirmed.get("average"),
                                "reason": "risk_exit" if close else "risk_reduce",
                            },
                        )
                        with self.store.transaction() as db:
                            m = self.store.read(db, "v2_managed")
                            m[aid][key]["risk_reduced"] = True
                            self.store.write(db, "v2_managed", m)
                        if protection_missing:
                            self.freeze(aid, "原生止损异常，已确认保护性退出；请人工核对")
                    except Exception:
                        self.freeze(aid, "减仓订单结果不确定，原生保护单保留；请核对")
                        with self.store.transaction() as db:
                            db.execute("UPDATE v2_orders SET state='unknown' WHERE id=?", (identity,))
                elif (
                    op.get("ai", {}).get("intervention_enabled")
                    and op.get("ai", {}).get("action") == "tighten"
                    and op.get("ai", {}).get("stop") is not None
                ):
                    target = D(op["ai"]["stop"])
                    if (target - D(p["stop"])) * sign <= 0 or (price - target) * sign <= 0:
                        continue
                    try:
                        # Install replacement before removing old stops. Both are reduce-only.
                        new = self.order(
                            account,
                            client,
                            p["symbol"],
                            "market",
                            "sell" if sign == 1 else "buy",
                            float(qty),
                            None,
                            {
                                "reduceOnly": True,
                                "marginMode": "isolated",
                                "stopLossPrice": float(client.price_to_precision(p["symbol"], str(target))),
                            },
                        )
                        if not new.get("id"):
                            raise ValueError("新止损单未确认")
                        for leg in p["legs"]:
                            if self.cancel_protection(client, p["symbol"], leg["stop_id"], []):
                                raise ValueError("旧止损单清理未确认")
                        with self.store.transaction() as db:
                            m = self.store.read(db, "v2_managed")
                            m[aid][key]["stop"] = str(target)
                            for leg in m[aid][key]["legs"]:
                                leg["stop_id"] = new["id"]
                            self.store.write(db, "v2_managed", m)
                    except Exception:
                        self.freeze(aid, "止损调整结果待核对，账户暂停")

                ai_targets = op.get("ai", {}).get("targets")
                if (
                    not close
                    and not reduce
                    and op.get("ai", {}).get("intervention_enabled")
                    and ai_targets
                    and ai_targets != p["targets"]
                    and qty * cs == D(p["original_qty"])
                    and not self.store.get("v2_execution_blocks").get(aid)
                    and all(
                        (D(t["price"]) - D(p["entry"])) * sign > 0
                        and (D(old["price"]) - D(t["price"])) * sign >= 0
                        and (D(t["price"]) - price) * sign > 0
                        for t, old in zip(ai_targets, p["targets"], strict=True)
                    )
                ):
                    try:
                        new_ids, remaining = [], qty
                        for index, target in enumerate(ai_targets):
                            amount = (
                                D(client.amount_to_precision(p["symbol"], str(qty * D(target["ratio"]))))
                                if index == 0
                                else remaining
                            )
                            if amount <= 0:
                                raise ValueError("分批数量不足")
                            order = self.order(
                                account,
                                client,
                                p["symbol"],
                                "limit",
                                "sell" if sign == 1 else "buy",
                                float(amount),
                                float(client.price_to_precision(p["symbol"], str(target["price"]))),
                                {"reduceOnly": True, "marginMode": "isolated"},
                            )
                            if not order.get("id"):
                                raise ValueError("止盈调整未确认")
                            new_ids.append(order["id"])
                            remaining -= amount
                        for leg in p["legs"]:
                            if self.cancel_protection(client, p["symbol"], None, leg["target_ids"]):
                                raise ValueError("原止盈单未完成清理")
                        with self.store.transaction() as db:
                            m = self.store.read(db, "v2_managed")
                            m[aid][key]["targets"] = ai_targets
                            for index, leg in enumerate(m[aid][key]["legs"]):
                                leg["target_ids"] = new_ids if index == 0 else []
                            self.store.write(db, "v2_managed", m)
                            self.store.audit(
                                db, "v2_targets_tightened", {"account_id": aid, "instrument_id": key}
                            )
                    except Exception:
                        self.freeze(aid, "止盈调整结果待核对；保留原生止损并暂停账户")

    def reconcile(self, account):
        """Read-only reconciliation; no blind retry or automatic adoption of unknown positions."""
        snapshot = self.read(account)
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM v2_orders WHERE account_id=? ORDER BY created DESC LIMIT 100",
                    (account["id"],),
                )
            ]
        snapshot["pending"] = [
            {"id": r["id"], "state": r["state"], "detail": json.loads(r["result"])}
            for r in rows
            if r["state"] in ("sending", "unknown", "protected")
        ]
        return snapshot
