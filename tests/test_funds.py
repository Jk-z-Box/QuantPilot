import asyncio
import copy
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from quantpilot.v2.funds import AccountStreams, gate_info, mark_valuation, number, snapshot
from quantpilot.v2.models import Account


def gate_balance(wallet="1000", pnl="50", free="700"):
    return {
        "USDT": {"total": wallet, "free": free},
        "info": [
            {
                "user": "123",
                "currency": "USDT",
                "total": wallet,
                "unrealised_pnl": pnl,
                "available": free,
                "in_dual_mode": False,
            }
        ],
    }


def position():
    return {
        "symbol": "SOL/USDT:USDT",
        "contracts": 10,
        "contractSize": 1,
        "entryPrice": 100,
        "markPrice": 105,
        "unrealizedPnl": 50,
        "side": "long",
    }


def real_account(w, exchange="gate"):
    a = w.save_account(Account(name="Live read only", kind="live", exchange=exchange).model_dump())
    w.save_credentials(a["id"], {"apiKey": "test-only-key", "secret": "test-only-secret", "password": ""})
    return a


@pytest.mark.parametrize("value", [None, "", "nan", "Infinity", {}, []])
def test_invalid_numeric_data_stays_unknown(value):
    assert number(value) is None


def test_gate_wallet_and_equity_are_not_confused():
    result = snapshot({"exchange": "gate"}, gate_balance(), [position()])
    assert result["equity"] == "1050" and result["unrealized"] == "50"
    assert result["wallet"] == "1000" and result["available"] == "700"
    assert result["positions"][0]["hedged"] is False
    assert gate_info({"info": {}}) == {}


@pytest.mark.parametrize(
    "exchange,raw",
    [
        (
            "binance",
            {
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "1000",
                        "unrealizedProfit": "-20",
                        "availableBalance": "700",
                    }
                ]
            },
        ),
        (
            "okx",
            {
                "data": [
                    {
                        "details": [
                            {"ccy": "USDT", "eq": "980", "upl": "-20", "cashBal": "1000", "availEq": "700"}
                        ]
                    }
                ]
            },
        ),
    ],
)
def test_native_equity_for_other_exchanges(exchange, raw):
    result = snapshot({"exchange": exchange}, {"info": raw, "USDT": {"total": 1000, "free": 500}}, [])
    assert result["equity"] == "980" and result["available"] == "700" and result["unrealized"] == "-20"


def test_missing_balance_not_fabricated_as_zero():
    s = snapshot({"exchange": "gate"}, {}, [])
    assert s["equity"] is None and s["available"] is None
    s = snapshot({"exchange": "gate"}, gate_balance(), [{**position(), "unrealizedPnl": None}])
    assert s["unrealized"] == "50"


def test_mark_estimates_do_not_change_verified_snapshot_or_available():
    base = snapshot({"exchange": "gate"}, gate_balance(), [position()])
    base["checked_at"] = 99
    original = copy.deepcopy(base)
    display = mark_valuation(base, {"SOL/USDT:USDT": (Decimal("106"), 100)}, now=101)
    assert display["equity"] == "1060" and display["unrealized"] == "60"
    assert display["available"] == "700" and display["valuation_source"] == "mark_estimate"
    assert base == original
    assert mark_valuation(base, {"SOL/USDT:USDT": (Decimal("106"), 100)}, now=111) == base


def test_paused_account_stream_independent_of_execution_gate(workspace, monkeypatch):
    monkeypatch.delenv("QP_LIVE_EXECUTION", raising=False)
    a = real_account(workspace)
    assert not a["auto"] and not workspace.exchange.availability(a)["ready"]
    assert workspace.funds.identity(a)
    assert workspace.funds.identity({**a, "balance_stream_enabled": False}) is None
    assert workspace.funds.identity({**a, "kind": "paper"}) is None


def test_stream_write_preserves_locks_and_rejects_old_identity(workspace):
    a = real_account(workspace)
    f = workspace.funds
    identity = f.identity(a)
    with workspace.store.transaction() as db:
        workspace.store.write(
            db, "v2_external", {a["id"]: {"reconciliation_required": True, "error": "unknown_order"}}
        )
    result = snapshot(a, gate_balance(), [])
    assert f.persist(a, identity, result)
    stored = workspace.store.get("v2_external")[a["id"]]
    assert stored["reconciliation_required"] and stored["error"] == "unknown_order"
    assert not f.persist(a, identity, {**result, "checked_at": result["checked_at"] - 1})
    workspace.save_credentials(
        a["id"], {"apiKey": "new-test-key", "secret": "new-test-secret", "password": ""}
    )
    assert not f.persist(a, identity, result)
    workspace.delete_account(a["id"])
    assert not f.persist(a, identity, result)


def test_live_status_requires_current_heartbeat_and_enabled_identity(workspace):
    a = real_account(workspace)
    f = workspace.funds
    identity = f.identity(a)
    f.status(a["id"], identity, state="live", heartbeat_at=time.time() - 16)
    assert f.display(a, {})["stream"]["state"] == "reconnecting"
    f.status(a["id"], identity, state="live", heartbeat_at=time.time())
    assert f.display(a, {})["stream"]["state"] == "live"
    assert f.display({**a, "balance_stream_enabled": False}, {})["stream"]["state"] == "disabled"


def test_bounded_subscriber_keeps_latest(workspace):
    async def scenario():
        f = workspace.funds
        q = f.subscribe()
        for _ in range(20):
            f.publish()
        await asyncio.sleep(0)
        assert q.qsize() == 1
        assert (await q.get())["sequence"] == f.sequence
        f.unsubscribe(q)
        assert not f.subscribers

    asyncio.run(scenario())


class FakeStream:
    def __init__(self, name, config):
        self.name, self.config = name, config
        self.ws = SimpleNamespace(isConnected=True)
        # CCXT clients are identity-hashable; SimpleNamespace is not.
        self.ws = type("Socket", (), {"isConnected": True, "lastPong": None})()
        self.balance = gate_balance()
        self.positions = []
        self.closed = False
        self.acks = False
        self.reads = 0
        self.clients = {}
        self.events = {name: asyncio.Queue() for name in ("balances", "positions", "orders")}
        self.mark_queue = asyncio.Queue()
        self.handle_message = lambda ws, data: None

    async def fetch_balance(self, params):
        self.reads += 1
        return copy.deepcopy(self.balance)

    async def fetch_positions(self):
        return copy.deepcopy(self.positions)

    async def watch(self, channel):
        assert self.uid == "123"
        if self.acks:
            self.handle_message(
                self.ws,
                {"event": "subscribe", "channel": "futures." + channel, "result": {"status": "success"}},
            )
        return await self.events[channel].get()

    async def watch_balance(self):
        return await self.watch("balances")

    async def watch_positions(self):
        return await self.watch("positions")

    async def watch_orders(self):
        return await self.watch("orders")

    async def watch_tickers(self, symbols):
        return await self.mark_queue.get()

    async def close(self):
        self.closed = True


def test_authenticated_stream_reconciles_events_coalesces_and_closes(workspace):
    async def scenario():
        a = real_account(workspace)
        made = []

        def factory(name, config):
            c = FakeStream(name, config)
            c.acks = True
            made.append(c)
            return c

        f = AccountStreams(workspace, factory)
        ident = f.identity(a)
        task = asyncio.create_task(f.session(a, ident))
        await asyncio.sleep(1.1)
        c = made[0]
        assert f.statuses[a["id"]]["state"] == "live"
        assert set(f.statuses[a["id"]]["channels"]) == {
            "futures.balances",
            "futures.positions",
            "futures.orders",
        }
        assert c.config["verbose"] is False
        c.balance = gate_balance(wallet="1200", pnl="0", free="1100")
        for _ in range(20):
            c.events["orders"].put_nowait({})  # Margin reservation changes without a wallet event.
        await asyncio.sleep(1.2)
        assert workspace.store.get("v2_external")[a["id"]]["available"] == "1100"
        assert c.reads <= 4
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert c.closed
        assert not workspace.store.get("v2_managed")

    asyncio.run(scenario())


def test_initial_rest_snapshot_does_not_claim_live_before_subscription_ack(workspace):
    async def scenario():
        a = real_account(workspace)
        made = []

        def factory(name, config):
            c = FakeStream(name, config)
            made.append(c)
            return c

        f = AccountStreams(workspace, factory)
        task = asyncio.create_task(f.session(a, f.identity(a)))
        await asyncio.sleep(0.1)
        assert f.statuses[a["id"]]["state"] == "connecting"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert made[0].closed

    asyncio.run(scenario())


def test_sse_requires_auth(client):
    assert client.get("/api/v2/account-events").status_code == 401


def test_sse_initial_push_revocation_and_cleanup(logged, app, workspace):
    async def scenario():
        cookie = logged.cookies.get("qp_session")
        req = Request({"type": "http", "headers": [(b"cookie", ("qp_session=" + cookie).encode())]})

        async def connected():
            return False

        req.is_disconnected = connected
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/v2/account-events")
        response = await endpoint(req)
        assert response.headers["x-accel-buffering"] == "no"
        iterator = response.body_iterator
        event = await anext(iterator)
        assert "event: accounts" in event and "published_at" in event
        with workspace.store.connect() as db:
            db.execute("DELETE FROM sessions")
        assert "event: expired" in await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        assert not workspace.funds.subscribers

    asyncio.run(scenario())


def test_manager_cancels_on_disable_and_deletion(workspace):
    async def scenario():
        a = real_account(workspace)
        f = workspace.funds
        started, closed = [], []

        async def worker(account, identity):
            started.append(account["id"])
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(account["id"])

        f.run_account = worker
        task = asyncio.create_task(f.run())
        await asyncio.sleep(0.05)
        assert started == [a["id"]]
        workspace.save_account({**a, "balance_stream_enabled": False})
        await asyncio.sleep(0.55)
        assert closed == started
        workspace.save_account(a)
        await asyncio.sleep(0.55)
        workspace.delete_account(a["id"])
        await asyncio.sleep(0.55)
        assert len(started) == len(closed) == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_socket_failure_reconnect_status_does_not_leak_exception(workspace):
    async def scenario():
        a = real_account(workspace)
        f = workspace.funds

        async def failed(*_args):
            raise RuntimeError("private-diagnostic-must-not-appear")

        f.session = failed
        task = asyncio.create_task(f.run_account(a, f.identity(a)))
        await asyncio.sleep(0.05)
        status = f.display(a, {})["stream"]
        assert status["state"] == "reconnecting"
        assert "private-diagnostic" not in str(status)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_gate_unified_account_never_reuses_classic_wallet_equity():
    balance = gate_balance()
    balance["info"][0]["margin_mode"] = 2
    result = snapshot({"exchange": "gate"}, balance, [position()])
    assert result["equity"] is None and not result["native_equity"]
    assert result["balance_warning"] and result["available"] == "700"


def test_binance_authenticates_exact_private_url_not_public_market_socket(workspace):
    async def scenario():
        a = real_account(workspace, "binance")

        class BinanceStream(FakeStream):
            def __init__(self, name, config):
                super().__init__(name, config)
                self.options = {"linear": {"listenKey": "test-listen-key"}}
                self.clients = {self.get_private_ws_url("linear", "test-listen-key"): self.ws}

            def get_private_ws_url(self, kind, listen_key):
                return "wss://example.invalid/private?listenKey=" + listen_key

            async def watch(self, channel):
                return await self.events[channel].get()

        f = AccountStreams(workspace, BinanceStream)
        task = asyncio.create_task(f.session(a, f.identity(a)))
        await asyncio.sleep(1.1)
        assert f.statuses[a["id"]]["state"] == "live"
        assert f.statuses[a["id"]]["channels"] == ["user_data"]
        assert "test-listen-key" not in str(f.packets())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
