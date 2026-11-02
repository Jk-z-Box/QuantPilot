"""Read-only private streams and browser fan-out. Never submits exchange orders."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from contextlib import suppress
from decimal import Decimal

import ccxt.pro as ccxtpro


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def gate_info(balance):
    raw = balance.get("info", {})
    if isinstance(raw, list):
        return next((r for r in raw if r.get("currency") == "USDT"), raw[0] if raw else {})
    return raw if isinstance(raw, dict) else {}


def snapshot(account, balance, positions, metadata=None):
    """Normalize native USDT account equity, not CCXT wallet `total` alone."""
    fields = (
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
        "maintenanceMargin",
        "maintenanceMarginPercentage",
    )
    out = [{k: p.get(k) for k in fields} for p in positions if number(p.get("contracts"))]
    usdt = balance.get("USDT", {})
    equity, available = number(usdt.get("total")), number(usdt.get("free"))
    pnls = [number(p.get("unrealizedPnl")) for p in out]
    unrealized = sum(pnls, Decimal(0)) if all(p is not None for p in pnls) else None
    wallet, native, warning = None, False, None
    if account["exchange"] == "gate":
        raw = metadata or gate_info(balance)
        for p in out:
            p["hedged"] = raw.get("in_dual_mode", p.get("hedged"))
        wallet = number(raw.get("total"))
        pnl = number(raw.get("unrealised_pnl"))
        if wallet is not None and pnl is not None:
            equity, unrealized, native = wallet + pnl, pnl, True
        available = number(raw.get("available")) if "available" in raw else available
        if str(raw.get("margin_mode", 0)) != "0":
            # Gate documents futures `total` only for classic accounts. Never label unified
            # multi-currency collateral as USDT equity or pass it to the trading risk engine.
            equity, wallet, native = None, None, False
            warning = "统一账户：此合约接口不提供完整 USDT 权益；权益暂不估算"
    elif account["exchange"] == "binance":
        raw = balance.get("info", {})
        assets = raw.get("assets", []) if isinstance(raw, dict) else []
        usd = next((a for a in assets if a.get("asset") == "USDT"), {})
        wallet, pnl = number(usd.get("walletBalance")), number(usd.get("unrealizedProfit"))
        if wallet is not None and pnl is not None:
            equity, unrealized, native = wallet + pnl, pnl, True
        if "availableBalance" in usd:
            available = number(usd["availableBalance"])
    elif account["exchange"] == "okx":
        raw = balance.get("info", {})
        data = raw.get("data", []) if isinstance(raw, dict) else []
        details = [d for r in data for d in r.get("details", [])]
        usd = next((d for d in details if d.get("ccy") == "USDT"), {})
        if number(usd.get("eq")) is not None:
            equity, unrealized, native = number(usd["eq"]), number(usd.get("upl")), True
            wallet = number(usd.get("cashBal"))
        if number(usd.get("availEq")) is not None:
            available = number(usd["availEq"])
    return {
        "equity": str(equity) if equity is not None else None,
        "available": str(available) if available is not None else None,
        "unrealized": str(unrealized) if unrealized is not None else None,
        "wallet": str(wallet) if wallet is not None else None,
        "native_equity": native,
        "positions": out,
        "checked_at": time.time(),
        "source": "exchange_account",
        "valuation_source": "exchange",
        "balance_scope": "USDT",
        "balance_warning": warning,
    }


def mark_valuation(base, marks, now=None):
    """Display estimate only. The persisted snapshot used by risk checks is untouched."""
    now = time.time() if now is None else now
    result, delta, changed = copy.deepcopy(base), Decimal(0), False
    for p in result.get("positions", []):
        mark = marks.get(p.get("symbol"))
        if not mark or now - mark[1] > 10 or not str(p.get("symbol", "")).endswith(":USDT"):
            continue
        qty, size, entry, previous = (
            number(p.get(k)) for k in ("contracts", "contractSize", "entryPrice", "unrealizedPnl")
        )
        if None in (qty, size, entry, previous) or p.get("side") not in ("long", "short"):
            continue
        pnl = (mark[0] - entry) * abs(qty) * size * (1 if p["side"] == "long" else -1)
        delta += pnl - previous
        p.update(markPrice=str(mark[0]), unrealizedPnl=str(pnl))
        changed = True
    if changed:
        for field in ("equity", "unrealized"):
            value = number(base.get(field))
            if value is not None and (field != "equity" or base.get("native_equity")):
                result[field] = str(value + delta)
        result.update(valuation_source="mark_estimate", valued_at=now)
    return result


class AccountStreams:
    def __init__(self, workspace, factory=None):
        self.w = workspace
        self.factory = factory or (lambda name, config: getattr(ccxtpro, name)(config))
        self.lock = threading.RLock()
        self.statuses, self.marks, self.identities = {}, {}, {}
        self.subscribers, self.sequence = {}, 0
        self.thread, self.loop, self.task = None, None, None

    def identity(self, account):
        credentials = self.w.vault.get("v2:account:" + account["id"])
        if account["kind"] == "paper" or not account.get("balance_stream_enabled", True) or not credentials:
            return None
        return hashlib.sha256(
            json.dumps([account["exchange"], account["kind"], credentials], sort_keys=True).encode()
        ).hexdigest()

    def current(self, aid, identity):
        account = self.w.store.get("v2_accounts").get(aid)
        return bool(account and self.identity(account) == identity)

    def display(self, account, base):
        aid = account["id"]
        identity = self.identity(account)
        with self.lock:
            status = copy.deepcopy(self.statuses.get(aid, {})) if self.identities.get(aid) == identity else {}
            marks = dict(self.marks.get(aid, {})) if status.get("state") == "live" else {}
        if not identity:
            status = {
                "state": "disabled" if not account.get("balance_stream_enabled", True) else "unconfigured"
            }
        if not status:
            status = {"state": "connecting"}
        if status.get("state") == "live" and time.time() - status.get("heartbeat_at", 0) > 15:
            status["state"] = "reconnecting"
        result = mark_valuation(base, marks)
        result["stream"] = status
        return result

    def packets(self):
        external = self.w.store.get("v2_external")
        return [
            {"id": a["id"], "summary": self.display(a, external.get(a["id"], {}))}
            for a in self.w.store.get("v2_accounts").values()
            if a["kind"] != "paper"
        ]

    def publish(self):
        with self.lock:
            self.sequence += 1
            packet = {"sequence": self.sequence, "published_at": time.time(), "accounts": self.packets()}
            for loop, queue in list(self.subscribers.values()):

                def deliver(q=queue, p=packet):
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(p)

                if not loop.is_closed():
                    loop.call_soon_threadsafe(deliver)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        with self.lock:
            if len(self.subscribers) >= 32:
                raise ValueError("实时连接过多，请关闭多余的网页")
            self.subscribers[id(queue)] = (asyncio.get_running_loop(), queue)
        self.publish()
        return queue

    def unsubscribe(self, queue):
        with self.lock:
            self.subscribers.pop(id(queue), None)

    def status(self, aid, identity, **values):
        if not self.current(aid, identity):
            return
        with self.lock:
            if self.identities.get(aid) != identity:
                self.statuses[aid], self.marks[aid] = {}, {}
            self.identities[aid] = identity
            self.statuses.setdefault(aid, {}).update(values)
        self.publish()

    def persist(self, account, identity, result):
        aid = account["id"]
        # Share the credential mutation lock; never resurrect a removed/rekeyed account.
        with self.w.exchange.guard:
            guard = self.w.exchange.locks.setdefault(aid, threading.RLock())
        with guard:
            if not self.current(aid, identity):
                return False
            with self.w.store.transaction() as db:
                external = self.w.store.read(db, "v2_external")
                old = external.get(aid, {})
                if old.get("checked_at", 0) > result["checked_at"]:
                    return False
                external[aid] = {**old, **result}  # execution/reconciliation locks survive read-only sync
                self.w.store.write(db, "v2_external", external)
        return True

    async def session(self, account, identity):
        aid = account["id"]
        credentials = self.w.vault.get("v2:account:" + aid)
        client = self.factory(
            "binanceusdm" if account["exchange"] == "binance" else account["exchange"],
            {
                **credentials,
                "enableRateLimit": True,
                "timeout": 8000,
                "newUpdates": True,
                "verbose": False,
                "options": {
                    "defaultType": "swap",
                    "defaultSubType": "linear",
                    "watchPositions": {"fetchPositionsSnapshot": False},
                },
            },
        )
        tasks, channels, marks = [], set(), {}
        dirty, subscribed = asyncio.Event(), asyncio.Event()
        required = {
            "gate": {"futures.balances", "futures.positions", "futures.orders"},
            "okx": {"account", "positions", "orders"},
            "binance": set(),
        }[account["exchange"]]
        private_clients = set()
        original = client.handle_message

        def message(ws, data):
            original(ws, data)
            if (
                data.get("event") == "subscribe"
                and not data.get("error")
                and str(data.get("code", "0")) == "0"
                and data.get("result", {}).get("status", "success") == "success"
            ):
                channel = data.get("channel") or data.get("arg", {}).get("channel")
                if channel in required:
                    channels.add(channel)
                    private_clients.add(ws)
            if required and required <= channels:
                subscribed.set()

        client.handle_message = message
        if account["kind"] == "demo":
            client.set_sandbox_mode(True)
        state = {"base": {}, "symbols": (), "last_read": 0.0}

        read_lock = asyncio.Lock()

        async def read():
            async with read_lock:
                await read_locked()

        async def read_locked():
            balance = await client.fetch_balance({"type": "swap"})
            positions = await client.fetch_positions()
            if account["exchange"] == "gate":
                raw = gate_info(balance)
                if not raw.get("user"):
                    raw = await client.privateFuturesGetSettleAccounts({"settle": "usdt"})
                client.uid = str(raw["user"])
            else:
                raw = None
            result = snapshot(account, balance, positions, raw)
            if await asyncio.to_thread(self.persist, account, identity, result):
                state["base"] = result
                state["symbols"] = tuple(
                    sorted(
                        {p["symbol"] for p in result["positions"] if p.get("symbol", "").endswith(":USDT")}
                    )
                )
                state["last_read"] = time.monotonic()
                self.status(aid, identity, snapshot_at=result["checked_at"])

        async def watch(method):
            while True:
                await getattr(client, method)()
                marks.clear()
                with self.lock:
                    self.marks[aid] = {}
                dirty.set()
                self.status(aid, identity, event_at=time.time())

        async def watch_marks():
            while True:
                symbols = state["symbols"]
                if not symbols:
                    await asyncio.sleep(0.5)
                    continue
                if account["exchange"] == "gate":
                    tickers = await client.watch_tickers(list(symbols))
                else:
                    tickers = await client.watch_mark_prices(list(symbols))
                for symbol, ticker in tickers.items():
                    raw = ticker.get("info", {})
                    value = number(ticker.get("markPrice") or raw.get("mark_price"))
                    ts = (ticker.get("timestamp") or time.time() * 1000) / 1000
                    if symbol in symbols and value is not None and value > 0 and abs(time.time() - ts) < 10:
                        if ts >= marks.get(symbol, (None, 0))[1]:
                            marks[symbol] = (value, ts)
                if self.current(aid, identity):
                    with self.lock:
                        self.marks[aid] = dict(marks)
                    self.publish()
                # Available margin is exchange-specific: refresh its native value, never infer it from cash.
                dirty.set()

        async def reconcile():
            while True:
                try:
                    await asyncio.wait_for(dirty.wait(), 60)
                except TimeoutError:
                    pass
                await asyncio.sleep(max(0.15, 1 - (time.monotonic() - state["last_read"])))
                dirty.clear()
                await read()

        async def health():
            began, ready = time.monotonic(), False
            while True:
                await asyncio.sleep(1)
                if account["exchange"] == "binance":
                    # A user-data socket is created only after the authenticated listen-key request.
                    listen_key = client.options.get("linear", {}).get("listenKey")
                    if listen_key:
                        private = client.clients.get(client.get_private_ws_url("linear", listen_key))
                        if private and private.isConnected:
                            private_clients.add(private)
                            channels.add("user_data")
                            subscribed.set()
                if not subscribed.is_set():
                    if time.monotonic() - began > 35:
                        raise TimeoutError("subscription_timeout")
                    continue
                if not private_clients or not all(
                    c.isConnected and (not c.lastPong or time.time() * 1000 - c.lastPong < 15000)
                    for c in private_clients
                ):
                    raise ConnectionError("private_stream_disconnected")
                if not ready:
                    await read()  # Close the bootstrap/subscription gap before claiming realtime.
                    ready = True
                self.status(
                    aid,
                    identity,
                    state="live",
                    heartbeat_at=time.time(),
                    channels=sorted(channels),
                    message="交易所实时连接正常",
                    snapshot_at=state["base"]["checked_at"],
                )

        try:
            self.status(aid, identity, state="connecting", message="正在建立交易所只读连接")
            await read()
            tasks = [
                asyncio.create_task(watch(m)) for m in ("watch_balance", "watch_positions", "watch_orders")
            ]
            tasks += [asyncio.create_task(f()) for f in (reconcile, watch_marks, health)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                await client.close()

    async def run_account(self, account, identity):
        delay = 1
        while self.current(account["id"], identity):
            try:
                await self.session(account, identity)
            except Exception:
                # Exchange exceptions can contain API keys/listen keys. Never log their contents.
                self.status(
                    account["id"],
                    identity,
                    state="reconnecting",
                    message="连接中断，旧数据待核对；自动重连中。请检查 API 读取权限、IP 白名单和网络",
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def run(self):
        tasks = {}
        try:
            while True:
                accounts = self.w.store.get("v2_accounts")
                wanted = {a["id"]: (a, self.identity(a)) for a in accounts.values() if a["kind"] != "paper"}
                for aid, (identity, task) in list(tasks.items()):
                    if wanted.get(aid, (None, None))[1] != identity:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del tasks[aid]
                        with self.lock:
                            self.statuses.pop(aid, None)
                            self.marks.pop(aid, None)
                            self.identities.pop(aid, None)
                        self.publish()
                for aid, (account, identity) in wanted.items():
                    if identity and aid not in tasks:
                        tasks[aid] = (identity, asyncio.create_task(self.run_account(account, identity)))
                await asyncio.sleep(0.5)
        finally:
            for _, task in tasks.values():
                task.cancel()
            await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)

    def start(self):
        def main():
            async def run():
                self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
                await self.run()

            with suppress(asyncio.CancelledError):
                asyncio.run(run())

        self.thread = threading.Thread(target=main, name="account-websockets", daemon=True)
        self.thread.start()

    def stop(self):
        if self.loop and not self.loop.is_closed() and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:
            self.thread.join(timeout=10)
