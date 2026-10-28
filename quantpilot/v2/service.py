from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .accounts import PaperBroker, ledger, valuation
from .ai import apply_revision, review
from .ai_client import fingerprint, ready
from .ai_errors import AIError
from .chatgpt import ChatGPTConnection
from .codex_client import CodexConnection
from .execution import ExchangeBroker
from .feeds import Feeds
from .limits import contract_limits
from .logic import LogicEngine
from .market import FuturesMarket
from .models import Account, Preferences, defaults, policy_matches, signal_policy
from .research import ResearchEngine
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
        self.ai_error = None
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
                "v2_archived_accounts": {},
                "v2_sizing_migration": {},
            }
            for key, value in initial.items():
                db.execute("INSERT OR IGNORE INTO kv VALUES (?,?)", (key, json.dumps(value)))
            prefs = {**Preferences().model_dump(), **self.store.read(db, "v2_preferences")}
            self.store.write(db, "v2_preferences", Preferences.model_validate(prefs).model_dump())
            accounts = self.store.read(db, "v2_accounts")
            migrated = self.store.read(db, "v2_sizing_migration")
            for aid, account in accounts.items():
                if "leverage" in account or any("notional" in s for s in account["sizing"]):
                    migrated.setdefault(aid, copy.deepcopy(account))
                    leverage = account.pop("leverage", 1)
                    for sizing in account["sizing"]:
                        sizing.pop("notional", None)
                        sizing.setdefault("quantity", None)
                        sizing.setdefault("leverage", leverage)
                    account["auto"] = False
                if account["kind"] != "paper":
                    account["capital"] = None
            self.store.write(db, "v2_accounts", accounts)
            self.store.write(db, "v2_sizing_migration", migrated)
        self.chatgpt = ChatGPTConnection(vault)
        self.codex = CodexConnection()
        self.research = ResearchEngine(self)
        self.logic = LogicEngine(self)

    def codex_action(self, action):
        if action == "activate":
            if not self.codex.status()["ready"]:
                raise ValueError("请先完成官方设备码授权")
            result = {"ready": True}
        else:
            if action not in ("login", "cancel", "logout"):
                raise ValueError("无效的连接操作")
            result = self.codex.call(action)
        # A new identity must never inherit enabled automated trading or AI tasks.
        with self.store.transaction() as db:
            prefs = self.store.read(db, "v2_preferences")
            if action == "activate" or prefs["ai_provider"] == "codex":
                if action == "activate":
                    prefs["ai_provider"] = "codex"
                prefs.update(ai_enabled=False, ai_intervene=False, ai_data_enabled=False, ai_model="")
                self.store.write(db, "v2_preferences", prefs)
            accounts = self.store.read(db, "v2_accounts")
            for account in accounts.values():
                account["auto"] = False
            self.store.write(db, "v2_accounts", accounts)
            self.store.audit(db, "codex_connection_changed", {"action": action})
        self.auth_changed()
        return result

    def auth_changed(self):
        if hasattr(self, "logic"):
            self.logic.settings({**self.store.get("v2_logic")["settings"], "enabled": False, "auto_optimize": False})
        with self.lock:
            self.ai_cache.clear()
            self.ai_error = None
            self.last_evaluation = 0
            self.ai_status = "连接已更新，请选择模型并保存 AI 设置"
            for op in self.opportunities.values():
                op["valid"] = False
                op.pop("ai", None)
                op["blockers"] = ["AI 连接已变更，等待重新评估"]
        self.research.invalidate()

    def limits(self, instrument_id, exchange, aid=""):
        item = next((i for i in self.store.get("v2_watchlist") if i["id"] == instrument_id), None)
        if not item or exchange not in ("binance", "okx", "gate"):
            raise ValueError("请选择有效交易对及交易所")
        account = self.store.get("v2_accounts").get(aid)
        try:
            if (
                account
                and account["kind"] != "paper"
                and account["exchange"] == exchange
                and self.vault.get("v2:account:" + aid)
            ):
                client = self.exchange.client(account)
                with self.exchange.locks[aid]:
                    return contract_limits(client, exchange, item["symbol"])
            with self.market.locks[exchange]:
                return contract_limits(self.market.client(exchange), exchange, item["symbol"])
        except Exception:
            return {
                "max_leverage": 1,
                "verified": False,
                "note": "合约规格暂无法核验，仅允许 1 倍；请检查网络、合约是否上市及账户 API。",
            }

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
        account = Account.model_validate(account).model_dump()
        account["id"] = account["id"] or secrets.token_hex(8)
        for sizing in account["sizing"]:
            if sizing["leverage"] > 1:
                limits = self.limits(sizing["instrument_id"], account["exchange"], account["id"])
                if not limits["verified"] or sizing["leverage"] > limits["max_leverage"]:
                    raise ValueError(f"{sizing['instrument_id']} 杠杆未通过交易所上限校验：{limits['note']}")
        with self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            old = accounts.get(account["id"])
            if account["id"] in self.store.read(db, "v2_archived_accounts"):
                raise ValueError("账户已删除，请新建账户")
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
            positions = (
                books.get(account["id"], {}).get("positions", {})
                if account["kind"] == "paper"
                else self.store.read(db, "v2_managed").get(account["id"], {})
            )
            by_id = {s["instrument_id"]: s for s in account["sizing"]}
            if any(k in by_id and by_id[k]["leverage"] != p["leverage"] for k, p in positions.items()):
                raise ValueError("请先平仓，再更改该交易对杠杆")
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

    def delete_account(self, aid):
        with self.exchange.guard:
            guard = self.exchange.locks.setdefault(aid, threading.RLock())
        with guard, self.store.transaction() as db:
            accounts = self.store.read(db, "v2_accounts")
            account = accounts.get(aid)
            if not account:
                raise ValueError("账户不存在或已删除")
            if account["auto"]:
                raise ValueError("请先暂停该账户自动交易")
            books = self.store.read(db, "v2_ledgers")
            if books.get(aid, {}).get("positions") or self.store.read(db, "v2_managed").get(aid):
                raise ValueError("账户仍有持仓，请先平仓或重置模拟账户")
            if (
                self.store.read(db, "v2_execution_blocks").get(aid)
                or db.execute(
                    "SELECT 1 FROM v2_orders WHERE account_id=? AND state IN ('sending','unknown')", (aid,)
                ).fetchone()
            ):
                raise ValueError("账户存在待核对订单，不能删除")
            archives = self.store.read(db, "v2_archived_accounts")
            archives[aid] = {"account": account, "ledger": books.pop(aid, None), "deleted_at": time.time()}
            accounts.pop(aid)
            db.execute("DELETE FROM credentials WHERE name=?", ("v2:account:" + aid,))
            self.store.write(db, "v2_accounts", accounts)
            self.store.write(db, "v2_ledgers", books)
            self.store.write(db, "v2_archived_accounts", archives)
            self.store.audit(db, "v2_account_deleted", {"account_id": aid})
        with self.exchange.guard:
            self.exchange.clients = {k: v for k, v in self.exchange.clients.items() if k[0] != aid}
        return {"deleted": True, "history_preserved": True}

    def save_credentials(self, aid, credentials):
        with self.exchange.guard:
            guard = self.exchange.locks.setdefault(aid, threading.RLock())
        with guard, self.store.transaction() as db:
            account = self.store.read(db, "v2_accounts").get(aid)
            if not account or account["kind"] == "paper":
                raise ValueError("请选择有效的交易所账户")
            if account["auto"]:
                raise ValueError("请先停止该账户自动交易")
            if self.store.read(db, "v2_managed").get(aid) or self.store.read(db, "v2_execution_blocks").get(
                aid
            ):
                raise ValueError("账户仍有受管理仓位或待核对状态，请先处理再更换密钥")
            if db.execute(
                "SELECT 1 FROM v2_orders WHERE account_id=? AND state IN ('sending','unknown')", (aid,)
            ).fetchone():
                raise ValueError("账户有待核对订单，不能更换密钥")
            if account["exchange"] == "okx" and not credentials["password"]:
                raise ValueError("OKX 账户需要填写 Passphrase")
            encrypted = self.vault.fernet.encrypt(json.dumps(credentials).encode())
            db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)", ("v2:account:" + aid, encrypted))
            self.store.audit(db, "credential_saved", {"name": "v2:account:" + aid})
            # Clear clients before releasing the execution lock so an old client cannot persist.
            with self.exchange.guard:
                self.exchange.clients = {k: v for k, v in self.exchange.clients.items() if k[0] != aid}
        return {"saved": True}

    def delete_key(self, provider):
        if provider == "openai" and self.store.get("v2_preferences")["ai_provider"] == "api_key":
            prefs = self.store.get("v2_preferences")
            self.preferences({**prefs, "ai_enabled": False, "ai_intervene": False, "ai_data_enabled": False})
            self.ai_status = "未启用 · 密钥已删除"
        self.vault.delete("v2:" + ("ai:" if provider == "openai" else "feed:") + provider)
        self.feeds.cache.clear()
        self.last_evaluation = 0
        with self.lock:
            for op in self.opportunities.values():
                op["valid"] = False
                op["blockers"] = ["数据配置已变更，等待重新评估"]
            self.ai_cache.clear()

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
        prefs = Preferences.model_validate(prefs).model_dump()
        if (prefs["ai_enabled"] or prefs["ai_data_enabled"]) and (
            not prefs["ai_model"] or not ready(prefs, self.vault)
        ):
            raise ValueError("请先完成所选 AI 连接并选择模型；官方登录需获准使用 ChatGPT 计划")
        with self.store.transaction() as db:
            previous = self.store.read(db, "v2_preferences")
            self.store.write(db, "v2_preferences", prefs)
            if previous["ai_provider"] != prefs["ai_provider"]:
                accounts = self.store.read(db, "v2_accounts")
                for account in accounts.values():
                    account["auto"] = False
                self.store.write(db, "v2_accounts", accounts)
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
                previous[k] != prefs[k]
                for k in (
                    "ai_provider",
                    "ai_enabled",
                    "ai_model",
                    "ai_intervene",
                    "min_score",
                    "ai_data_enabled",
                )
            ):
                for op in self.opportunities.values():
                    op["valid"] = False
                    op["blockers"] = ["风险配置已变更，等待重新评估"]
                    op["reasons"] = []
                self.ai_cache.clear()
                self.ai_error = None
                self.ai_status = "等待复核" if prefs["ai_enabled"] else "未启用"
        if any(previous[k] != prefs[k] for k in ("ai_provider", "ai_model", "ai_data_enabled")):
            self.research.invalidate()
        self.last_evaluation = 0
        return prefs

    def scan(self, item, analyze, prefs):
        key = item["id"]
        logic = self.logic.current()
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
            if not self.market.demo:
                evidence = self.research.supplement(item, evidence)
            # Fetch again after slower data providers; never stamp stale quotes with receipt time.
            try:
                quote = self.market.quote(item)
            except Exception:
                quote = {**quote, "timestamp": 0}
            op = evaluate(item, frames, quote, evidence, prefs["profile"], time.time(), prefs["min_score"], logic["parameters"])
            op["logic_version"] = logic["id"]
            op["demo"] = self.market.demo
            if op.get("id"):
                op["id"] += ":" + logic["id"]
            op["policy"] = signal_policy(prefs)
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
                if self.logic.current()["id"] != logic["id"]:
                    return
                current_prefs = self.store.get("v2_preferences")
                if any(
                    current_prefs[k] != prefs[k]
                    for k in (
                        "profile",
                        "min_score",
                        "ai_provider",
                        "ai_enabled",
                        "ai_model",
                        "ai_intervene",
                        "ai_data_enabled",
                    )
                ):
                    return
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
                if self.ai_error is None:
                    self.ai_status = "AI 复核暂未完成；已有仓位保护继续运行"

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
        context = fingerprint(prefs, self.vault)
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
                        and context == fingerprint(prefs, self.vault)
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
            if self.store.get("v2_preferences") == prefs and context == fingerprint(prefs, self.vault):
                self.ai_status = "已完成风险复核"
                self.ai_error = None
            self.store.record("v2_ai_review", {"count": len(revisions)})
        except ValueError as exc:
            error = exc if isinstance(exc, AIError) else AIError("invalid_response")
            # Ignore failures from a request whose settings were changed while in flight.
            if self.store.get("v2_preferences") == prefs:
                with self.lock:
                    self.ai_cache.clear()
                    for op in self.opportunities.values():
                        op.pop("ai", None)
                self.ai_error = {**error.public(), "checked_at": time.time()}
                self.ai_status = error.title
                self.store.record(
                    "v2_ai_review_failed", {"kind": error.kind, "http_status": error.http_status}
                )
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
                        or op.get("min_score", 80) != prefs["min_score"]
                        or not policy_matches(op, prefs)
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
            "version": "0.5.0",
            "now": now,
            "demo": self.market.demo,
            "watchlist": self.store.get("v2_watchlist"),
            "preferences": prefs,
            "quotes": quotes,
            "opportunities": ops,
            "accounts": accounts,
            "orders": orders,
            "archived_accounts": {
                k: v["account"]["name"] for k, v in self.store.get("v2_archived_accounts").items()
            },
            "ai_status": self.ai_status,
            "ai_error": copy.deepcopy(self.ai_error),
            "ai_data_status": self.store.get("v2_ai_data_status"),
            "ai_data_evidence": self.research.observations(),
            "last_evaluation": self.last_evaluation,
            "providers": {name: bool(self.vault.get("v2:feed:" + name)) for name in ("coinglass", "finnhub")},
            "chatgpt": self.chatgpt.status(),
            "codex": self.codex.status(),
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
        self.research.start()

        def logic_loop():
            while not self.stop_event.wait(15):
                try:
                    self.logic.scheduled()
                except Exception:
                    self.store.record("logic_scheduler_failed", {})

        threading.Thread(target=logic_loop, name="logic-scheduler", daemon=True).start()

    def stop(self):
        self.stop_event.set()
        if self.research.thread:
            self.research.thread.join(timeout=3)
        if self.thread:
            self.thread.join(timeout=25)
        if self.quote_thread:
            self.quote_thread.join(timeout=25)
