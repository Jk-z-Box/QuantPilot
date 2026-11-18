import asyncio
import time

from quantpilot.v2.fund_events import apply, balance_fields, ingest
from quantpilot.v2.funds import AccountStreams, snapshot
from tests.test_funds import FakeStream, gate_balance, position, real_account


def base():
    return {
        **snapshot({"exchange": "gate"}, gate_balance(), [position()]),
        "read_started_at": 1,
        "checked_at": 1.1,
    }


def test_partial_balance_keeps_missing_available_and_skips_stale_cached_free():
    payload = {
        "timestamp": 1200,
        "USDT": {"total": 1200, "free": 9999},
        "info": [{"balance": "1200", "currency": "USDT"}],
    }
    events = ingest({}, "gate", "watch_balance", payload, 1300)
    result = apply(base(), events)
    assert result["wallet"] == "1200" and result["equity"] == "1250"
    assert result["available"] == "700"
    assert result["data_times"]["funds_ms"] == 1300
    assert result["data_times"]["available_ms"] == 1100


def test_position_quantity_and_close_render_before_rest():
    updated = {**position(), "contracts": 20, "timestamp": 1200, "unrealizedPnl": 100}
    events = ingest({}, "gate", "watch_positions", [updated], 1300)
    result = apply(base(), events)
    assert result["positions"][0]["contracts"] == 20
    assert result["positions"][0]["unrealizedPnl"] == 100
    assert result["pending_reconcile"]
    events = ingest(events, "gate", "watch_positions", [{**updated, "contracts": 0, "timestamp": 1400}], 1500)
    assert apply(base(), events)["positions"] == []


def test_late_rest_response_cannot_erase_newer_event():
    events = ingest({}, "gate", "watch_positions", [{**position(), "contracts": 25}], 1500)
    stale = {**base(), "read_started_at": 1.2, "checked_at": 1.8}
    assert apply(stale, events)["positions"][0]["contracts"] == 25
    reconciled = {**base(), "read_started_at": 1.6, "checked_at": 1.9}
    assert apply(reconciled, events)["positions"][0]["contracts"] == 10
    assert not apply(reconciled, events)["pending_reconcile"]


def test_out_of_order_position_event_is_ignored():
    p = {**position(), "timestamp": 1500000000500, "contracts": 25}
    events = ingest({}, "gate", "watch_positions", [p], 1500)
    events = ingest(
        events, "gate", "watch_positions", [{**p, "timestamp": 1500000000100, "contracts": 2}], 1600
    )
    assert apply(base(), events)["positions"][0]["contracts"] == 25


def test_gate_net_flat_clears_short_even_when_ccxt_cache_had_no_previous_side():
    b = base()
    b["positions"][0]["side"] = "short"
    events = ingest(
        {}, "gate", "watch_positions", [{**position(), "contracts": 0, "info": {"mode": "single"}}], 1500
    )
    assert apply(b, events)["positions"] == []


def test_gate_dual_short_flat_does_not_remove_long():
    b = base()
    b["positions"].append({**position(), "side": "short"})
    events = ingest(
        {}, "gate", "watch_positions", [{**position(), "contracts": 0, "info": {"mode": "dual_short"}}], 1500
    )
    assert [p["side"] for p in apply(b, events)["positions"]] == ["long"]


def test_native_other_exchange_balance_fields_only():
    assert balance_fields(
        "binance", {"USDT": {"free": 999}, "info": {"a": {"B": [{"a": "USDT", "wb": "1000", "cw": "400"}]}}}
    ) == {"wallet": "1000"}
    assert balance_fields(
        "okx",
        {
            "info": {
                "data": [
                    {
                        "details": [
                            {"ccy": "USDT", "eq": "1200", "cashBal": "1000", "upl": "200", "availEq": "600"}
                        ]
                    }
                ]
            }
        },
    ) == {"equity": "1200", "wallet": "1000", "unrealized": "200", "available": "600"}


def test_native_position_event_does_not_wait_for_slow_rest(workspace):
    async def scenario():
        a = real_account(workspace)
        clients = []

        class SlowRead(FakeStream):
            def __init__(self, name, config):
                super().__init__(name, config)
                self.acks = True
                self.positions = [position()]
                self.slow = False

            async def fetch_balance(self, params):
                if self.slow:
                    await asyncio.sleep(0.5)
                return await super().fetch_balance(params)

        def factory(name, config):
            c = SlowRead(name, config)
            clients.append(c)
            return c

        f = AccountStreams(workspace, factory)
        task = asyncio.create_task(f.session(a, f.identity(a)))
        await asyncio.sleep(1.1)
        c = clients[0]
        c.slow = True
        update = {**position(), "contracts": 25, "timestamp": time.time() * 1000}
        c.events["positions"].put_nowait([update])
        await asyncio.sleep(0.03)
        stored = workspace.store.get("v2_external")[a["id"]]
        assert stored["positions"][0]["contracts"] == 10
        assert f.display(a, stored)["positions"][0]["contracts"] == 25
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_millisecond_clock_requires_auth(client, logged):
    # Logged fixture shares client: use a header-free anonymous request by clearing cookies.
    assert isinstance(logged.get("/api/v2/clock").json()["server_ms"], float)
    client.cookies.clear()
    assert client.get("/api/v2/clock").status_code == 401


def test_paper_accounts_share_event_delivery_without_private_credentials(workspace):
    packets = workspace.funds.packets()
    paper = next(p for p in packets if p["id"] == "paper-default")
    assert paper["summary"]["equity"] == "10000"
    assert not paper["summary"]["positions"]
