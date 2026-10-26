from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .accounts import PaperBroker, ledger, valuation
from .ai import apply_revision, review
from .execution import ExchangeBroker
from .feeds import Feeds
from .market import FuturesMarket
from .models import Account, Preferences, defaults
from .signals import evaluate


class Workspace:
    def __init__(self, store, vault, settings, market=None, feeds=None):
        self.store, self.vault, self.settings = store, vault, settings
        self.market = market or FuturesMarket(settings.market_mode == "demo")
        self.feeds = feeds or Feeds(vault, store)
        self.paper, self.exchange = PaperBroker(store), ExchangeBroker(store, vault)
        self.quotes, self.opportunities, self.frames, self.errors = {}, {}, {}, {}
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.thread = None
        self.quote_thread = None
        self.last_evaluation, self.last_ai = 0, 0
        self.ai_status = "未启用"
        self.ai_cache = {}
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS v2_orders(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL)"
            )
            initial = {
                "v2_watchlist": defaults(),
                "v2_preferences": Preferences().model_dump(),
                "v2_accounts": {
                    "paper-default": Account(id="paper-default", name="我的模拟账户").model_dump()
                },
                "v2_ledgers": {"paper-default": ledger(10000)},
                "v2_external": {},
                "v2_evidence": [],
            }
            for key, value in initial.items():
                db.execute("INSERT OR IGNORE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def save_watchlist(self, items):
        with self.store.transaction() as db:
            old = {i["id"]: i for i in self.store.read(db, "v2_watchlist")}
            for item in items:
                item["id"] = item["id"] or secrets.token_hex(6)
            ids = [i["id"] for i in items]
            if len(ids) != len(set(ids)) or len({(i["exchange"], i["symbol"]) for i in items}) != len(items):
                raise ValueError("监测列表存在重复交易对")
            changed = {
                key
                for key, item in old.items()
                if key not in ids
                or any(
                    new["id"] == key
                    and (new["symbol"], new["exchange"]) != (item["symbol"], item["exchange"])
                    for new in items
                )
            }
            books = self.store.read(db, "v2_ledgers")
            if any(changed & set(book["positions"]) for book in books.values()):
                raise ValueError("持仓中的交易对不能删除或更换市场")
            accounts = self.store.read(db, "v2_accounts")
            if any(changed & set(a["allowed"]) for a in accounts.values() if a["kind"] != "paper"):
                raise ValueError("请先移除账户中的交易对授权")
            self.store.write(db, "v2_watchlist", items)
            self.store.audit(db, "v2_watchlist_saved", {"count": len(items)})
        with self.lock:
            for key in changed:
                for state in (self.quotes, self.opportunities, self.frames):
                    state.pop(key, None)
            self.last_evaluation = 0
        return items

    def save_account(self, account):
        account["id"] = account["id"] or secrets.token_hex(8)
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            old = accounts.get(account["id"])
            if len(accounts) >= 20 and not old:
                raise ValueError("最多支持 20 个账户")
            watch = {i["id"]: i for i in self.store.read(db, "v2_watchlist")}
            if any(k not in watch for k in account["allowed"]) or any(
                s["instrument_id"] not in watch for s in account["sizing"]
            ):
                raise ValueError("请选择监测列表中的交易对")
            if account["kind"] != "paper" and any(
                watch[k]["exchange"] != account["exchange"] for k in account["allowed"]
            ):
                raise ValueError("账户只能授权同一交易所的交易对")
            if old and (account["kind"], account["exchange"]) != (old["kind"], old["exchange"]):
                raise ValueError("现有账户不能更换类型或交易所，请新建账户")
            if account["auto"] and account["kind"] != "paper":
                gate = self.exchange.availability(account)
                if not gate["ready"]:
                    raise ValueError(gate["reason"])
                if not account["allowed"]:
                    raise ValueError("请先授权自动交易的交易对")
            books = self.store.read(db, "v2_ledgers")
            if old and old["capital"] != account["capital"]:
                raise ValueError("修改初始资金请使用重置账户")
            accounts[account["id"]] = account
            if account["kind"] == "paper" and account["id"] not in books:
                books[account["id"]] = ledger(account["capital"])
            self.store.write(db, "v2_accounts", accounts)
            self.store.write(db, "v2_ledgers", books)
            self.store.audit(
                db,
                "v2_account_saved",
                {"account_id": account["id"], "kind": account["kind"], "auto": account["auto"]},
            )
        return account

    def reset(self, aid, capital):
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            a = accounts.get(aid)
            if not a or a["kind"] != "paper":
                raise ValueError("只能重置本地模拟账户")
            books = self.store.read(db, "v2_ledgers")
            generation = books[aid]["generation"] + 1
            books[aid] = {**ledger(capital), "generation": generation}
            a["capital"], a["auto"] = capital, False
            self.store.write(db, "v2_accounts", accounts)
            self.store.write(db, "v2_ledgers", books)
            self.store.audit(db, "v2_account_reset", {"account_id": aid, "capital": capital})

    def preferences(self, prefs):
        if prefs["ai_enabled"] and (not prefs["ai_model"] or not self.vault.get("v2:ai:openai")):
            raise ValueError("先保存 OpenAI 密钥并填写模型")
        with self.store.transaction() as db:
            previous = self.store.read(db, "v2_preferences")
            self.store.write(db, "v2_preferences", prefs)
            self.store.write(db, "kill", prefs["halted"])
            if prefs["halted"]:
                strategy = self.store.read(db, "strategy")
                strategy["enabled"] = False
                self.store.write(db, "strategy", strategy)
            if prefs["halted"]:
                accounts = self.store.read(db, "v2_accounts")
                for account in accounts.values():
                    account["auto"] = False
                self.store.write(db, "v2_accounts", accounts)
            self.store.audit(
                db, "v2_preferences_saved", {"profile": prefs["profile"], "halted": prefs["halted"]}
            )
        with self.lock:
            if previous["profile"] != prefs["profile"] or any(
                previous[k] != prefs[k] for k in ("ai_enabled", "ai_model", "ai_intervene")
            ):
                for op in self.opportunities.values():
                    op["valid"] = False
                    op["blockers"] = ["风险配置已变更，等待重新评估"]
                self.ai_cache.clear()
        self.last_evaluation = 0
        return prefs

    def scan(self, item, analyze, prefs):
        key = item["id"]
        try:
            quote = self.market.quote(item)
        except Exception:
            with self.lock:
                self.quotes[key] = {
                    "error": "行情源不可用，请检查网络、地区限制或合约是否上市",
                    "received_at": time.time(),
                }
                if key in self.opportunities:
                    self.opportunities[key]["valid"] = False
                    self.opportunities[key]["blockers"] = ["行情源不可用"]
            return
        frames, op = None, None
        if analyze:
            frames = {}
            for frame in ("1w", "1d", "4h", "1h"):
                try:
                    frames[frame] = self.market.candles(item, frame, closed=True)
                except Exception:
                    frames[frame] = []
            evidence = (
                self.feeds.evidence(item)
                if not self.market.demo
                else [
                    {
                        "category": category,
                        "status": "missing",
                        "source": "演示模式不伪造外部证据",
                        "note": "未连接真实数据服务",
                    }
                    for category in ("etf", "options", "liquidation", "macro")
                ]
            )
            # Fetch again after slower data providers; never stamp stale quotes with receipt time.
            try:
                quote = self.market.quote(item)
            except Exception:
                quote = {**quote, "timestamp": 0}
            op = evaluate(item, frames, quote, evidence, prefs["profile"], time.time())
        with self.lock:
            current_item = next((i for i in self.store.get("v2_watchlist") if i["id"] == key), None)
            if not current_item or (item["symbol"], item["exchange"]) != (
                current_item["symbol"],
                current_item["exchange"],
            ):
                return
            self.quotes[key] = quote
            if frames is not None:
                self.frames[key] = frames
            if op:
                cached = self.ai_cache.get(key)
                if (
                    cached
                    and cached["signal_id"] == op.get("id")
                    and time.time() - cached["at"] < min(prefs["ai_interval"], cached["revision"]["ttl"])
                ):
                    op = apply_revision(op, cached["revision"], cached["at"], prefs["ai_intervene"])
                self.opportunities[key] = op

    def tick(self):
        items = self.store.get("v2_watchlist")
        prefs = self.store.get("v2_preferences")
        do_analysis = time.time() - self.last_evaluation > 30
        books = self.store.get("v2_ledgers")
        held = {k for b in books.values() for k in b["positions"]}
        held.update(k for positions in self.store.get("v2_managed").values() for k in positions)
        selected = [i for i in items if i["enabled"] or i["id"] in held]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.scan, item, do_analysis, prefs) for item in selected]
            for future in futures:
                future.result()
        if do_analysis:
            self.last_evaluation = time.time()
        if prefs["ai_enabled"] and time.time() - self.last_ai >= prefs["ai_interval"]:
            try:
                self.review_ai(prefs)
            except ValueError:
                self.ai_status = "AI 复核失败；已有仓位保护继续运行"

    def fast_tick(self):
        items = self.store.get("v2_watchlist")
        books = self.store.get("v2_ledgers")
        held = {k for book in books.values() for k in book["positions"]}
        held.update(k for positions in self.store.get("v2_managed").values() for k in positions)
        prefs = self.store.get("v2_preferences")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(self.scan, item, False, prefs)
                for item in items
                if item["enabled"] or item["id"] in held
            ]
            for future in futures:
                future.result()
        self.execute()

    def review_ai(self, prefs=None):
        prefs = prefs or self.store.get("v2_preferences")
        if not prefs["ai_enabled"]:
            raise ValueError("AI 尚未启用")
        with self.lock:
            if time.time() - self.last_ai < 60:
                raise ValueError("AI 请求至少间隔 60 秒")
            self.last_ai = time.time()
            opportunities = copy.deepcopy(list(self.opportunities.values()))
            reviewed_ids = {op["instrument_id"]: op.get("id") for op in opportunities}
        try:
            risk = []
            for index, (_aid, book) in enumerate(self.store.get("v2_ledgers").items()):
                snap = valuation(book, self.quotes, time.time())
                risk.append(
                    {
                        "account": "paper-" + str(index),
                        "equity": snap["equity"],
                        "available": snap["available"],
                        "exposure": snap["exposure"],
                        "positions": snap["positions"],
                    }
                )
            for index, snap in enumerate(self.store.get("v2_external").values()):
                risk.append(
                    {
                        "account": "exchange-" + str(index),
                        "equity": snap.get("equity"),
                        "available": snap.get("available"),
                        "positions": snap.get("positions", []),
                    }
                )
            revisions = review(prefs, self.vault, opportunities, risk=risk)
            with self.lock:
                for revision in revisions:
                    key = revision["instrument_id"]
                    if (
                        key in self.opportunities
                        and self.opportunities[key].get("id") == reviewed_ids.get(key)
                        and self.store.get("v2_preferences") == prefs
                    ):
                        stamp = time.time()
                        self.ai_cache[key] = {
                            "revision": revision,
                            "at": stamp,
                            "signal_id": self.opportunities[key].get("id"),
                        }
                        self.opportunities[key] = apply_revision(
                            self.opportunities[key], revision, stamp, prefs["ai_intervene"]
                        )
            self.ai_status = "已完成风险复核"
            self.store.record("v2_ai_review", {"count": len(revisions)})
        except ValueError:
            self.ai_status = "AI 不可用；新开仓等待复核，已有保护继续"
            raise
        return {"status": self.ai_status}

    def execute(self):
        with self.lock:
            quotes, ops = copy.deepcopy(self.quotes), copy.deepcopy(self.opportunities)
        items = {i["id"]: i for i in self.store.get("v2_watchlist")}
        prefs = self.store.get("v2_preferences")
        accounts = self.store.get("v2_accounts")
        for account in accounts.values():
            aid = account["id"]
            try:
                if account["kind"] == "paper":
                    self.paper.protect(account, quotes, ops)
                else:
                    self.exchange.manage(account, quotes, ops, items)
                if not account["auto"] or prefs["halted"]:
                    continue
                for key, op in ops.items():
                    if (
                        key not in items
                        or not items[key]["enabled"]
                        or not op.get("valid")
                        or op["profile"] != prefs["profile"]
                    ):
                        continue
                    if prefs["ai_enabled"] and (
                        not op.get("ai") or time.time() - op["ai"]["checked_at"] > prefs["ai_interval"]
                    ):
                        continue
                    try:
                        if account["kind"] == "paper":
                            book = self.store.get("v2_ledgers")[aid]
                            position = book["positions"].get(key)
                            adding = bool(position)
                            if adding:
                                sign = 1 if position["side"] == "long" else -1
                                trigger = (
                                    float(position["entry"])
                                    + (float(position["entry"]) - float(position["stop"])) * 0.75
                                )
                                if (quotes[key]["mid"] - trigger) * sign < 0:
                                    continue
                            self.paper.enter(account, op, quotes, items, prefs["profile"], adding)
                        else:
                            self.exchange.enter(account, op, quotes, items, prefs["profile"])
                        self.errors.pop(aid, None)
                    except ValueError as exc:
                        self.errors[aid] = str(exc)
            except Exception:
                self.errors[aid] = "账户执行暂停，请检查数据与审计记录"

    def state(self):
        now = time.time()
        with self.lock:
            quotes, ops = copy.deepcopy(self.quotes), copy.deepcopy(self.opportunities)
        for key, op in ops.items():
            q = quotes.get(key, {})
            if (
                op.get("expires_at", 0) <= now
                or q.get("error")
                or not -5 <= now - q.get("timestamp", 0) <= 15
            ):
                op["valid"] = False
                if "行情或机会已过期" not in op["blockers"]:
                    op["blockers"].append("行情或机会已过期")
        prefs = self.store.get("v2_preferences")
        books, external = self.store.get("v2_ledgers"), self.store.get("v2_external")
        accounts = []
        for a in self.store.get("v2_accounts").values():
            summary = (
                valuation(books[a["id"]], quotes, now) if a["kind"] == "paper" else external.get(a["id"], {})
            )
            accounts.append(
                {
                    **a,
                    "summary": summary,
                    "execution": self.exchange.availability(a),
                    "credential_saved": bool(self.vault.get("v2:account:" + a["id"])),
                    "last_error": self.errors.get(a["id"]),
                }
            )
        with self.store.connect() as db:
            orders = [
                {**dict(r), "result": json.loads(r["result"])}
                for r in db.execute("SELECT * FROM v2_orders ORDER BY created DESC LIMIT 100")
            ]
        return {
            "version": "0.2.0",
            "now": now,
            "demo": self.market.demo,
            "watchlist": self.store.get("v2_watchlist"),
            "preferences": prefs,
            "quotes": quotes,
            "opportunities": ops,
            "accounts": accounts,
            "orders": orders,
            "ai_status": self.ai_status,
            "last_evaluation": self.last_evaluation,
            "providers": {name: bool(self.vault.get("v2:feed:" + name)) for name in ("coinglass", "finnhub")},
            "ai_key_saved": bool(self.vault.get("v2:ai:openai")),
        }

    def start(self):
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            for account in accounts.values():
                account["auto"] = False
            self.store.write(db, "v2_accounts", accounts)
            self.store.audit(db, "v2_startup", {"all_accounts_paused": True})

        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    self.ai_status = "监测任务异常，自动开仓等待数据恢复"
                self.stop_event.wait(5)

        def fast_loop():
            while not self.stop_event.is_set():
                try:
                    self.fast_tick()
                except Exception:
                    self.errors["system"] = "行情或账户执行异常，等待重新核对"
                self.stop_event.wait(3)

        self.quote_thread = threading.Thread(target=fast_loop, name="quote-protection", daemon=True)
        self.quote_thread.start()
        self.thread = threading.Thread(target=loop, name="mission-control", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=25)
        if self.quote_thread:
            self.quote_thread.join(timeout=25)
