"""Settlement conservation, exchange routing and authenticated realtime recovery."""

import asyncio
import copy
import time
from decimal import Decimal as D

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from quantpilot.v2.accounts import ledger, size_order
from quantpilot.v2.contracts import (
    base_amount,
    client_name,
    contract_amount,
    native_notional,
    pnl,
    validate_market,
)
from quantpilot.v2.fund_events import ingest
from quantpilot.v2.funds import mark_valuation, snapshot
from quantpilot.v2.margin import estimates, liquidation
from quantpilot.v2.market import FuturesMarket
from quantpilot.v2.models import Account, Instrument, Sizing
from quantpilot.v2.position_review import profit_context
from quantpilot.v2.risk_context import account_context


def market(inverse=True):
    return {
        "symbol": "BTC/USD:BTC" if inverse else "BTC/USDT:USDT",
        "base": "BTC",
        "quote": "USD" if inverse else "USDT",
        "settle": "BTC" if inverse else "USDT",
        "swap": True,
        "linear": not inverse,
        "inverse": inverse,
        "active": True,
        "contractSize": 100 if inverse else 0.001,
        "precision": {"amount": 1},
        "limits": {"amount": {"min": 1}, "leverage": {"max": 100}},
    }


def position(side="long", mode="isolated", qty="1", entry="100", margin=".1"):
    return {
        "inverse": True,
        "settle": "BTC",
        "side": side,
        "qty": qty,
        "entry": entry,
        "margin": margin,
        "margin_mode": mode,
    }


def coin_setup(w, op, quote, side="long", mode="isolated"):
    item = Instrument(id="coinbtc", label="BTC", symbol="BTC/USD:BTC").model_dump()
    w.save_watchlist([*w.store.get("v2_watchlist"), item])
    a = w.save_account(
        Account(
            name="BTC wallet",
            settle="BTC",
            capital=10,
            auto=True,
            sizing=[
                Sizing(
                    instrument_id=item["id"],
                    mode="fixed",
                    quantity=1,
                    leverage=1,
                    margin_mode=mode,
                    add_count=0,
                )
            ],
        ).model_dump()
    )
    signal = copy.deepcopy(op)
    signal.update(
        instrument_id=item["id"], symbol=item["symbol"], direction=side, logic_version=w.logic.current()["id"]
    )
    if side == "short":
        signal["plan"].update(stop=104, targets=[{"price": 94, "ratio": 0.5}, {"price": 88, "ratio": 0.5}])
    q = {
        **quote,
        "inverse": True,
        "settle": "BTC",
        "contract_size": 1,
        "contract_step": 1,
        "min_contracts": 1,
    }
    return a, signal, q, {i["id"]: i for i in w.store.get("v2_watchlist")}


@pytest.mark.parametrize("side", ["long", "short"])
def test_inverse_liquidation_solves_native_margin_equation(side):
    p = position(side)
    x = liquidation(p, D(".1"))
    assert x > 0
    assert abs(D(".1") + pnl(p, x) - native_notional(p, x) * D(".0055")) < D("1e-25")


def test_inverse_cross_pool_excludes_other_isolated_collateral():
    b = ledger("1.5", "BTC")
    b["positions"] = {"a": position("short", "cross"), "b": position(margin="1")}
    marks = {"a": D(100), "b": D(100)}
    first = estimates(b, marks)["a"]
    marks["b"] = D(300)
    assert estimates(b, marks)["a"] == first
    b["cash"] = "1.3"
    assert estimates(b, marks)["a"] < first


@pytest.mark.parametrize("inverse", [True, False])
def test_contract_base_conversion_round_trip(inverse):
    m = market(inverse)
    base = base_amount(m, 20, 50000)
    assert contract_amount(m, base, 50000) == 20
    assert base == (D(".04") if inverse else D(".020"))


@pytest.mark.parametrize(
    "changes",
    [
        {"swap": False},
        {"inverse": True, "linear": False, "settle": "ETH"},
        {"contractSize": 0},
        {"active": False},
    ],
)
def test_unsupported_contract_cannot_be_mistaken_for_supported(changes):
    with pytest.raises(ValueError):
        validate_market({**market(), **changes})


def test_currency_model_and_legacy_defaults():
    assert Account(name="legacy").settle == "USDT"
    assert Account(name="coin", settle="BTC").capital == 1
    assert Account(name="real", kind="live", settle="BTC", capital=3).capital is None
    for symbol in ["BTC/USDT:BTC", "ETH/USD:BTC", "BTC/USD:BTC-261225"]:
        with pytest.raises(ValidationError):
            Instrument(label="bad", symbol=symbol)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("mode", ["isolated", "cross"])
def test_paper_inverse_partial_and_full_close_conserve_coins(workspace, opportunity, quote, side, mode):
    a, op, q, items = coin_setup(workspace, opportunity, quote, side, mode)
    result = workspace.paper.enter(a, op, {"coinbtc": q}, items, "balanced")
    assert result["settle"] == "BTC"
    before = workspace.store.get("v2_ledgers")[a["id"]]
    p = before["positions"]["coinbtc"]
    assert D(p["qty"]) * D(p["entry"]) == D(result["contracts"])
    assert D(p["margin"]) == D(p["qty"])
    assert D(before["cash"]) == D(10) - D(result["fee"])
    exitq = {**q, "bid": 110, "ask": 110.02, "mid": 110.01}
    if side == "short":
        exitq.update(bid=89.98, ask=90, mid=89.99)
    partial = workspace.paper.close(a["id"], "coinbtc", exitq, 0.5)
    last = workspace.paper.close(a["id"], "coinbtc", exitq)
    after = workspace.store.get("v2_ledgers")[a["id"]]
    assert not after["positions"]
    expected = D(before["cash"]) + D(partial["pnl"]) + D(last["pnl"])
    assert abs(D(after["cash"]) - expected) < D("1e-25")
    assert expected > D(10)
    # Reported trading PnL excludes the value change of the initial ten BTC collateral.
    assert D(after["realized_reporting"]) < D(100)


def test_inverse_wallet_isolation_and_metadata_guard(workspace, opportunity, quote):
    a, op, q, items = coin_setup(workspace, opportunity, quote)
    b = workspace.store.get("v2_ledgers")[a["id"]]
    with pytest.raises(ValueError, match="结算币种"):
        size_order({**a, "settle": "USDT"}, b, op, {"coinbtc": q}, items, "balanced", time.time())
    with pytest.raises(ValueError, match="精度"):
        size_order(a, b, op, {"coinbtc": {**q, "contract_step": None}}, items, "balanced", time.time())
    with pytest.raises(ValueError, match="钱包"):
        workspace.save_account({**a, "settle": "ETH"})
    context = account_context(workspace, a, b, "coinbtc", q, op, "open")
    assert context["settlement_currency"] == "BTC"
    import json

    json.dumps(context)  # All proposed-order Decimal fields must be serialized.


def test_native_snapshot_never_mix_currency_positions():
    a = {"exchange": "binance", "settle": "BTC"}
    balance = {
        "BTC": {"total": "1", "free": ".8"},
        "USDT": {"total": 100000, "free": 90000},
        "info": {
            "assets": [
                {"asset": "BTC", "walletBalance": "1", "unrealizedProfit": ".1", "availableBalance": ".7"},
                {"asset": "USDT", "walletBalance": 100000, "unrealizedProfit": 10},
            ]
        },
    }
    positions = [
        {
            "symbol": "BTC/USD:BTC",
            "contracts": 1,
            "contractSize": 100,
            "entryPrice": 50000,
            "unrealizedPnl": ".1",
        },
        {"symbol": "ETH/USDT:USDT", "contracts": 20, "unrealizedPnl": 1000},
    ]
    s = snapshot(a, balance, positions)
    assert s["equity"] == "1.1" and s["available"] == "0.7"
    assert len(s["positions"]) == 1 and s["positions"][0]["inverse"]
    assert s["balance_scope"] == "BTC"
    event = ingest(
        {},
        "binance",
        "watch_balance",
        {"info": {"a": {"B": [{"a": "BTC", "wb": "2"}, {"a": "USDT", "wb": 99999}]}}},
        time.time() * 1000,
        "BTC",
    )
    assert event["balance"]["wallet"]["value"] == "2"


def test_inverse_mark_estimate_does_not_invent_available_funds():
    p = {
        "symbol": "BTC/USD:BTC",
        "side": "long",
        "contracts": 100,
        "contractSize": 100,
        "entryPrice": 50000,
        "markPrice": 50000,
        "unrealizedPnl": 0,
    }
    s = {"equity": "1", "available": ".7", "unrealized": "0", "native_equity": True, "positions": [p]}
    after = mark_valuation(s, {p["symbol"]: (D(55000), time.time())})
    expected = D(10000) * (1 / D(50000) - 1 / D(55000))
    assert abs(D(after["unrealized"]) - expected) < D("1e-25")
    assert after["available"] == s["available"] and s["positions"][0]["markPrice"] == 50000


def test_inverse_profit_stop_breakeven_covers_native_fees():
    for side in ("long", "short"):
        p = position(side)
        q = {"bid": 110, "ask": 90}
        ctx = profit_context(p, q)
        sign = 1 if side == "long" else -1
        execution = D(ctx["breakeven"]) * (1 - sign * D(".0002"))
        result = pnl(p, execution) - D(".0005") - native_notional(p, execution) * D(".0005")
        assert abs(result) < D("1e-14")


def test_catalog_filters_and_routes_binance_coinm():
    calls = []

    class Catalog:
        def load_markets(self):
            return {
                "coin": market(),
                "linear": market(False),
                "expired": {**market(), "symbol": "BTC/USD:BTC-261225", "swap": False},
            }

    def factory(name, config):
        calls.append((name, config))
        return Catalog()

    m = FuturesMarket(factory=factory)
    result = m.markets("binance", "BTC", "inverse", "BTC", 0, 10)
    assert result["total"] == 1 and result["items"][0]["symbol"] == "BTC/USD:BTC"
    assert calls[0][0] == "binancecoinm"
    assert client_name("binance", "USDT") == "binanceusdm"
    assert m.markets("binance", "BTC", "inverse", "ETH")["total"] == 0


def test_public_stream_ignores_stale_reordered_and_invalid_quotes(workspace):
    item = workspace.store.get("v2_watchlist")[0]
    stream = workspace.streams
    now = time.time() * 1000
    assert stream.update_quote(item, {"bid": 100, "ask": 101, "timestamp": now}, {})
    assert not stream.update_quote(item, {"bid": 90, "ask": 91, "timestamp": now - 1000}, {})
    assert not stream.update_quote(item, {"bid": float("nan"), "ask": 91, "timestamp": now + 1}, {})
    assert not stream.update_quote(item, {"bid": 90, "ask": 91, "timestamp": now - 20000}, {})
    assert workspace.quotes[item["id"]]["mid"] == 100.5
    assert stream.status(item)["state"] == "live"
    assert workspace.quote_wake.is_set()


def test_candle_increment_updates_current_bar_without_hiding_a_gap(workspace):
    item = workspace.store.get("v2_watchlist")[0]
    stream = workspace.streams
    now = int(time.time() // 3600) * 3600000
    base = [[now - 7200000, 1, 3, 1, 2, 10]]
    stream.update_candles(item, "1h", [[now, 2, 4, 1, 3, 20]])
    out = stream.merge_candles(item, "1h", base)
    assert out[1][0] - out[0][0] == 7200000  # Missing period remains visible, not invented.
    stream.update_candles(item, "1h", [[now, 2, 4, 1, 3.5, 30]])
    assert stream.merge_candles(item, "1h", base)[-1][4] == 3.5


def test_browser_ws_requires_auth_and_exact_origin(client, logged):
    with pytest.raises(WebSocketDisconnect):
        with logged.websocket_connect(
            "ws://127.0.0.1:8080/api/v2/realtime", headers={"origin": "https://evil.example"}
        ):
            pass
    # Same cookie and exact origin are required; a control/order message is never accepted.
    with logged.websocket_connect("ws://127.0.0.1:8080/api/v2/realtime") as ws:
        packet = ws.receive_json()
        assert packet["type"] == "snapshot" and packet["accounts"]
        ws.send_json({"type": "order", "side": "buy"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_browser_ws_initial_snapshot_delta_and_resubscribe(logged, workspace, quote):
    with logged.websocket_connect("ws://127.0.0.1:8080/api/v2/realtime") as ws:
        first = ws.receive_json()
        ws.send_json({"type": "subscribe", "instrument_id": "btc", "frame": "1h"})
        with workspace.lock:
            workspace.quotes["btc"] = quote
        workspace.funds.publish()
        packet = ws.receive_json()
        assert packet["sequence"] > first["sequence"] and packet["epoch"] == first["epoch"]
        while "quotes" not in packet:
            packet = ws.receive_json()
        assert packet["quotes"]["btc"]["mid"] == 100
    with logged.websocket_connect("ws://127.0.0.1:8080/api/v2/realtime") as ws:
        snapshot_packet = ws.receive_json()
        assert snapshot_packet["type"] == "snapshot"
        assert snapshot_packet["quotes"]["btc"]["mid"] == 100
    assert not workspace.funds.subscribers


def test_browser_ws_logged_out_denied(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1:8080/api/v2/realtime"):
            pass


def test_stream_queue_remains_bounded(workspace):
    async def check():
        queue = workspace.funds.subscribe()
        for _ in range(100):
            workspace.funds.publish()
        await asyncio.sleep(0.01)
        assert queue.qsize() == 1
        workspace.funds.unsubscribe(queue)

    asyncio.run(check())


def test_clock_calibration_separates_skew_from_transport_age(workspace):
    stream = workspace.streams
    stream.clocks["gate"] = {"offset_ms": 600, "uncertainty_ms": 10, "checked_at": time.time()}
    timing = stream.timing("gate", 1000, 450)
    assert timing["exchange_to_server_ms"] == 50
    assert timing["clock_delta_ms"] == -550
    assert timing["clock_uncertainty_ms"] == 10
    assert stream.timing("unknown", 1000, 450)["exchange_to_server_ms"] is None
    stream.clocks["gate"]["checked_at"] -= 601
    assert stream.timing("gate", 1000, 450)["exchange_to_server_ms"] is None


def test_deleted_instrument_not_resurrected_by_late_market_event(workspace):
    item = workspace.store.get("v2_watchlist")[0]
    workspace.streams.active = {}
    workspace.streams.update_quote(item, {"bid": 100, "ask": 101, "timestamp": time.time() * 1000}, {})
    assert item["id"] not in workspace.quotes


def test_gate_native_balance_never_substitutes_other_currency():
    a = Account(name="coin", exchange="gate", kind="live", settle="BTC").model_dump()
    s = snapshot(
        a,
        {"info": [{"currency": "USDT", "total": "90000", "available": "80000", "unrealised_pnl": "10"}]},
        [],
    )
    assert s["equity"] is None and s["available"] is None and not s["native_equity"]


@pytest.mark.parametrize("exchange,upper", [("binance", 10), ("okx", 10), ("gate", 0.02)])
def test_inverse_leverage_tier_uses_exchange_native_units(exchange, upper):
    from quantpilot.v2.limits import contract_limits

    class Client:
        precisionMode = 4
        has = {"fetchMarketLeverageTiers": True}

        def load_markets(self):
            pass

        def market(self, symbol):
            return market()

        def fetch_market_leverage_tiers(self, symbol):
            return [{"minNotional": 0, "maxNotional": upper, "maxLeverage": 20}]

    assert contract_limits(Client(), exchange, "BTC/USD:BTC", 1000, 0.02)["max_leverage"] == 20
    with pytest.raises(ValueError, match="档位"):
        contract_limits(Client(), exchange, "BTC/USD:BTC", 2000, 0.04)


@pytest.mark.parametrize("exchange", ["binance", "okx", "gate"])
def test_inverse_private_read_routes_native_wallet(workspace, exchange):
    calls = []

    class Client:
        def fetch_balance(self, params):
            calls.append(("balance", params))
            return {"BTC": {"total": 1, "free": 0.5}}

        def fetch_positions(self, params):
            calls.append(("positions", params))
            return []

        def privateFuturesGetSettleAccounts(self, params):
            calls.append(("gate", params))
            return {"currency": "BTC", "total": 1, "available": 0.5, "unrealised_pnl": 0}

    def factory(name, config):
        calls.append(("factory", name, config["options"]))
        return Client()

    workspace.exchange.factory = factory
    a = workspace.save_account(
        Account(name="coin", kind="live", exchange=exchange, settle="BTC").model_dump()
    )
    workspace.save_credentials(a["id"], {"apiKey": "fixture", "secret": "fixture", "password": "fixture"})
    result = workspace.exchange.read(a)
    assert result["balance_scope"] == "BTC" and D(result["equity"]) == 1
    assert calls[0][1] == ("binancecoinm" if exchange == "binance" else exchange)
    assert calls[0][2]["defaultSubType"] == "inverse"
    assert all(p["settle"] == "btc" for kind, p in calls[1:] if kind in ("balance", "positions", "gate"))
    assert not result["execution"]["ready"]


def test_ai_size_cap_preserves_fractional_contract_step(workspace):
    review = workspace.action_review
    review.jobs["cap"] = {
        "proposed_order": {"qty": ".0007", "notional": ".0007", "risk": {"estimated_loss_at_stop": ".00007"}}
    }
    sizing = {
        "qty": D(".001"),
        "price": D(50000),
        "notional": D(".001"),
        "inverse": True,
        "contract_size": D(100),
        "contract_step": D(".1"),
        "risk": {
            "sizing_mode": "maximum",
            "estimated_loss_at_stop": ".0001",
            "initial_margin": ".001",
            "estimated_entry_fee": ".000001",
            "available_before": "1",
        },
    }
    review.check_size({"action_review_ticket": "cap"}, sizing)
    assert sizing["contracts"] == D(".3")
    assert sizing["qty"] == D(".0006")
    assert sizing["qty"] <= D(".0007")


def test_rapid_browser_resubscribe_does_not_disconnect(logged):
    with logged.websocket_connect("ws://127.0.0.1:8080/api/v2/realtime") as ws:
        ws.receive_json()
        for frame in ["1h", "4h", "1d"]:
            ws.send_json({"type": "subscribe", "instrument_id": "btc", "frame": frame})
        while True:
            packet = ws.receive_json()
            if packet.get("candles", {}).get("frame") == "1d":
                break
