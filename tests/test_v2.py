import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from quantpilot.v2.accounts import ledger, size_order, valuation
from quantpilot.v2.ai import apply_revision, review
from quantpilot.v2.execution import ExchangeBroker
from quantpilot.v2.feeds import fomc_events, option_summary
from quantpilot.v2.market import FuturesMarket
from quantpilot.v2.models import Account, Instrument, Preferences, Sizing
from quantpilot.v2.signals import evaluate, indicators


def universe(workspace):
    return {i["id"]: i for i in workspace.store.get("v2_watchlist")}


@pytest.mark.parametrize("path", ["state", "chart/btc", "markets"])
def test_v2_requires_session(client, path):
    assert client.get("/api/v2/" + path).status_code == 401


def test_v2_defaults_and_legacy_survive(logged):
    s = logged.get("/api/v2/state").json()
    assert [i["label"] for i in s["watchlist"]] == ["BTC", "ETH", "SOL", "AVGO", "SOXL"]
    assert all(i["exchange"] == "binance" and i["symbol"].endswith(":USDT") for i in s["watchlist"])
    assert s["accounts"][0]["summary"]["equity"] == "10000"
    assert logged.get("/api/state").status_code == 200
    assert logged.get("/legacy").status_code == 200
    assert "/static/workspace.js" in logged.get("/").text


def test_v2_csrf(logged):
    logged.headers.pop("x-csrf-token")
    assert logged.put("/api/v2/preferences", json=Preferences().model_dump()).status_code == 403


@pytest.mark.parametrize("payload", [{"name": "x", "capital": float("inf")}, {"name": "x", "leverage": 20}])
def test_model_rejects_invalid_capital_and_leverage(payload):
    with pytest.raises(ValidationError):
        Account(**payload)


def test_sizing_validation():
    with pytest.raises(ValidationError):
        Sizing(instrument_id="btc", add_count=2, add_ratios=[0.1])
    with pytest.raises(ValidationError):
        Sizing(instrument_id="btc", add_count=1, add_ratios=[float("nan")])
    with pytest.raises(ValidationError):
        Account(name="x", kind="live", allowed=[], sizing=[Sizing(instrument_id="btc")])
    with pytest.raises(ValidationError):
        Instrument(label="x", symbol="BTC/USDT")


def test_account_isolation(workspace, account, opportunity, quote):
    second = workspace.save_account(Account(name="second", capital=5000).model_dump())
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    books = workspace.store.get("v2_ledgers")
    assert books[second["id"]]["cash"] == "5000.0"
    assert not books[second["id"]]["positions"]
    assert books[account["id"]]["positions"]["btc"]["qty"]


def test_concurrent_order_dedup(workspace, account, opportunity, quote):
    def place():
        return workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: place(), range(4)))
    assert [r["status"] for r in results].count("filled") == 1
    assert len(workspace.state()["orders"]) == 1


def test_two_targets_and_stop_never_widens(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    before = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    q = {**quote, "bid": 107, "ask": 107.01, "mid": 107}
    workspace.paper.protect(account, {"btc": q}, {"btc": opportunity})
    p = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    assert Decimal(p["qty"]) == Decimal(before["qty"]) / 2
    assert p["tp_done"] == [0] and Decimal(p["stop"]) == Decimal(p["entry"])
    workspace.paper.protect(account, {"btc": q}, {"btc": opportunity})
    assert workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"] == p["qty"]
    workspace.paper.protect(
        account, {"btc": {**q, "bid": 113, "ask": 113.01, "mid": 113}}, {"btc": opportunity}
    )
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]


def test_halted_still_protects_positions(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    workspace.preferences({**Preferences().model_dump(), "halted": True})
    q = {**quote, "bid": 94, "ask": 94.01, "mid": 94}
    workspace.quotes = {"btc": q}
    workspace.opportunities = {"btc": opportunity}
    workspace.execute()
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]


def test_stale_quote_never_fills_or_closes(workspace, account, opportunity, quote):
    quotes = {"btc": {**quote, "timestamp": time.time() - 60}}
    with pytest.raises(ValueError, match="报价"):
        workspace.paper.enter(account, opportunity, quotes, universe(workspace), "balanced")
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    with pytest.raises(ValueError, match="报价"):
        workspace.paper.close(account["id"], "btc", quotes["btc"])
    assert not valuation(workspace.store.get("v2_ledgers")[account["id"]], quotes, time.time())[
        "valuation_valid"
    ]


def test_fixed_size_is_not_silently_reduced(workspace, account, opportunity, quote):
    account["sizing"] = [Sizing(instrument_id="btc", mode="fixed", quantity=500).model_dump()]
    account = workspace.save_account(account)
    with pytest.raises(ValueError, match="固定"):
        workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    account["sizing"][0]["mode"] = "maximum"
    account = workspace.save_account(account)
    result = workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert result["status"] == "filled"
    assert Decimal(result["qty"]) * Decimal(result["price"]) < 50000


def test_reset_retains_audit_and_generates_new_identity(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    workspace.reset(account["id"], 2000)
    book = workspace.store.get("v2_ledgers")[account["id"]]
    assert not book["positions"] and book["generation"] == 1 and book["cash"] == "2000"
    assert len(workspace.state()["orders"]) == 1
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]


def test_allowlist_and_cross_exchange(workspace):
    with pytest.raises(ValueError, match="同一交易所"):
        workspace.save_account(Account(name="okx", kind="live", exchange="okx", allowed=["btc"]).model_dump())
    live = workspace.save_account(Account(name="live", kind="live", allowed=["btc"]).model_dump())
    with pytest.raises(ValueError):
        workspace.reset(live["id"], 1000)
    live["auto"] = True
    with pytest.raises(ValueError):
        workspace.save_account(live)


def test_key_storage_does_not_echo(logged, workspace):
    a = logged.post("/api/v2/accounts", json=Account(name="real", kind="live").model_dump()).json()
    r = logged.post(
        "/api/v2/accounts/" + a["id"] + "/credentials",
        json={"api_key": "test-key-123", "secret": "test-secret-456"},
    )
    assert r.status_code == 200
    state = logged.get("/api/v2/state").text
    assert "test-key-123" not in state and "test-secret-456" not in state
    assert workspace.vault.get("v2:account:" + a["id"])["secret"] == "test-secret-456"


def test_watchlist_cannot_mutate_open_position(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    items = workspace.store.get("v2_watchlist")
    items[0]["symbol"] = "ETH/USDT:USDT"
    with pytest.raises(ValueError):
        workspace.save_watchlist(items)


def test_threshold_expiration_and_pause(workspace, account, opportunity, quote):
    for field, value in [("score", 79), ("expires_at", time.time() - 1), ("valid", False)]:
        bad = {**opportunity, field: value}
        with pytest.raises(ValueError):
            workspace.paper.enter(account, bad, {"btc": quote}, universe(workspace), "balanced")
    account["auto"] = False
    workspace.save_account(account)
    with pytest.raises(ValueError):
        workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")


def test_signal_no_missing_data_rescaling(monkeypatch, quote):
    import quantpilot.v2.signals as signals

    monkeypatch.setattr(
        signals,
        "indicators",
        lambda rows, parameters=None: {
            "trend": "long",
            "rsi": 55,
            "macd_hist": 1,
            "volume_ratio": 1.5,
            "atr": 2,
            "close": 100,
            "timestamp": 1000,
        },
    )
    item = Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump()
    result = evaluate(item, {f: [[1]] for f in ["1w", "1d", "4h", "1h"]}, quote, [], "balanced", time.time())
    assert result["score"] == 70 and result["coverage"] == 70 and not result["valid"]
    evidence = [
        {
            "category": c,
            "status": "fresh",
            "published_at": time.time() - 1,
            "expires_at": time.time() + 600,
            "bias": "bullish",
        }
        for c in ["etf", "options", "liquidation", "macro"]
    ]
    result = evaluate(
        item, {f: [[1]] for f in ["1w", "1d", "4h", "1h"]}, quote, evidence, "balanced", time.time()
    )
    assert result["score"] == 100 and result["valid"]
    evidence[-1]["bias"] = "risk"
    assert not evaluate(
        item, {f: [[1]] for f in ["1w", "1d", "4h", "1h"]}, quote, evidence, "balanced", time.time()
    )["valid"]


def test_ai_cannot_override_hard_blocks(opportunity):
    rev = {
        "instrument_id": "btc",
        "score_delta": 5,
        "ttl": 300,
        "action": "tighten",
        "stop": 90,
        "reason": "test",
    }
    blocked = {**opportunity, "score": 79, "blockers": ["行情已过期"]}
    r = apply_revision(blocked, rev, time.time(), True)
    assert not r["valid"] and r["plan"]["stop"] == 96
    assert r["expires_at"] <= blocked["expires_at"]
    rev["stop"] = 98
    assert apply_revision(opportunity, rev, time.time(), True)["plan"]["stop"] == 98
    assert apply_revision(opportunity, rev, time.time(), False)["plan"]["stop"] == 96


def test_ai_structured_validation_and_no_secret_in_payload(workspace, opportunity):
    workspace.vault.put("v2:ai:openai", {"apiKey": "unit-secret"})

    def handler(request):
        assert b"unit-secret" not in request.content
        payload = json.loads(request.content)
        assert payload["text"]["format"]["strict"] and payload["store"] is False
        answer = {
            "revisions": [
                {
                    "instrument_id": "btc",
                    "score_delta": -10,
                    "ttl": 60,
                    "action": "reduce",
                    "stop": None,
                    "targets": None,
                    "reason": "test",
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}
                ],
            },
        )

    prefs = Preferences(ai_enabled=True, ai_model="test").model_dump()
    assert (
        review(prefs, workspace.vault, [opportunity], httpx.MockTransport(handler))[0]["score_delta"] == -10
    )
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "incomplete"}))
    with pytest.raises(ValueError):
        review(prefs, workspace.vault, [opportunity], bad)


def test_option_max_pain_uses_nearest_expiry():
    now = time.time()
    ins = [
        {
            "instrument_name": "a",
            "strike": 100,
            "option_type": "call",
            "expiration_timestamp": (now + 10000) * 1000,
            "contract_size": 1,
        },
        {
            "instrument_name": "b",
            "strike": 110,
            "option_type": "put",
            "expiration_timestamp": (now + 10000) * 1000,
            "contract_size": 1,
        },
        {
            "instrument_name": "c",
            "strike": 500,
            "option_type": "put",
            "expiration_timestamp": (now + 20000) * 1000,
            "contract_size": 1,
        },
    ]
    books = [
        {"instrument_name": "a", "open_interest": 20},
        {"instrument_name": "b", "open_interest": 10},
        {"instrument_name": "c", "open_interest": 1000},
    ]
    r = option_summary(ins, books, now)
    assert r["max_pain"] == 100 and r["contracts"] == 2 and r["put_oi"] == 10


def test_fomc_dst_conversion():
    html = '2026 FOMC Meetings <div class="fomc-meeting__month"><strong>October</strong></div><div class="fomc-meeting__date">27-28</div>'
    r = fomc_events(html)
    assert len(r) == 1 and time.gmtime(r[0]["event_at"]).tm_hour == 18


def test_indicator_math():
    rows = [[i * 3600000, 100 + i, 102 + i, 99 + i, 101 + i, 100] for i in range(180)]
    r = indicators(rows)
    assert r["ema20"] > r["ema50"] and r["rsi"] == 100 and r["atr"] == 3
    assert r["volume_ratio"] == 1 and r["volatility"] >= 0


def test_demo_chart_includes_live_and_completed():
    m = FuturesMarket(demo=True)
    item = Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump()
    all_rows = m.candles(item, "1h")
    closed = m.candles(item, "1h", closed=True)
    assert len(all_rows) > len(closed) and all_rows[-1][0] > closed[-1][0]


def test_account_client_identity_and_sandbox(workspace):
    made = []

    class Client:
        def __init__(self, name, config):
            self.name = name
            self.config = config
            self.sandbox = False
            made.append(self)

        def set_sandbox_mode(self, value):
            self.sandbox = value

    broker = ExchangeBroker(workspace.store, workspace.vault, Client)
    a = Account(id="one", name="one", kind="demo").model_dump()
    b = {**a, "id": "two"}
    for account in (a, b):
        workspace.vault.put("v2:account:" + account["id"], {"apiKey": account["id"], "secret": "test"})
    assert broker.client(a) is not broker.client(b)
    assert broker.client(a).sandbox
    assert all(c.name == "binanceusdm" for c in made)


def test_live_gate_requires_real_acceptance(workspace, monkeypatch):
    a = Account(id="a", name="real", kind="live").model_dump()
    workspace.vault.put("v2:account:a", {"apiKey": "test", "secret": "test"})
    assert not workspace.exchange.availability(a)["ready"]
    monkeypatch.setenv("QP_LIVE_EXECUTION", "1")
    assert not workspace.exchange.availability(a)["ready"]


def test_profile_change_invalidates_previous_signal(workspace, opportunity):
    workspace.opportunities = {"btc": opportunity}
    workspace.preferences(Preferences(profile="conservative").model_dump())
    assert not workspace.opportunities["btc"]["valid"]


def test_reduce_on_invalidation_once(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    op = {**opportunity, "valid": False, "score": 60}
    workspace.paper.protect(account, {"btc": quote}, {"btc": op})
    p = copy.deepcopy(workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"])
    workspace.paper.protect(account, {"btc": quote}, {"btc": op})
    assert workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"] == p["qty"]
    assert p["risk_reduced"]


class ExecutionClient:
    def __init__(self, name, config):
        self.name, self.config = name, config
        self.calls, self.orders, self.positions = [], {}, {}
        self.failure, self.partial = "", 1
        self.has = {"fetchMarketLeverageTiers": True}
        self.precisionMode = 4

    def fetch_market_leverage_tiers(self, symbol):
        return [{"minNotional": 0, "maxNotional": 1000000, "maxLeverage": 100}]

    def set_sandbox_mode(self, enabled):
        self.sandbox = enabled

    def fetch_balance(self, params):
        return {"USDT": {"total": 10000, "free": 9900}}

    def fetch_positions(self, symbols=None):
        return [p.copy() for p in self.positions.values() if p["contracts"] > 0]

    def fetch_position_mode(self, symbol):
        return {"hedged": False}

    def privateFuturesGetSettleAccounts(self, params):
        return {"in_dual_mode": False}

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {
            "linear": True,
            "swap": True,
            "settle": "USDT",
            "contractSize": 1,
            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}},
        }

    def amount_to_precision(self, symbol, amount):
        return str(Decimal(str(amount)).quantize(Decimal(".0001"), rounding="ROUND_DOWN"))

    def price_to_precision(self, symbol, price):
        return str(Decimal(str(price)).quantize(Decimal(".01")))

    def set_margin_mode(self, *args):
        return {}

    def set_leverage(self, *args):
        return {}

    def create_order(self, symbol, type, side, amount, price, params):
        self.calls.append((symbol, type, side, amount, price, params))
        if self.failure == "entry_timeout" and not params.get("reduceOnly"):
            raise TimeoutError("test")
        if self.failure == "stop" and params.get("stopLossPrice"):
            raise ValueError("test")
        oid = str(len(self.calls))
        protection = params.get("stopLossPrice") is not None or (params.get("reduceOnly") and type == "limit")
        filled = 0 if protection else amount if params.get("reduceOnly") else amount * self.partial
        self.orders[oid] = {
            "id": oid,
            "status": "open" if protection else "closed",
            "filled": filled,
            "average": price or 100,
        }
        if not protection:
            p = self.positions.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "side": "long" if side == "buy" else "short",
                    "contracts": 0,
                    "contractSize": 1,
                    "entryPrice": 100,
                    "markPrice": 100,
                    "notional": 0,
                    "unrealizedPnl": 0,
                    "liquidationPrice": 50 if side == "buy" else 150,
                    "leverage": 2,
                    "marginMode": "isolated",
                    "hedged": False,
                    "initialMargin": 0,
                },
            )
            p["contracts"] += -filled if params.get("reduceOnly") else filled
            p["initialMargin"] = p["contracts"] * 50
            p["notional"] = p["contracts"] * 100
        return self.orders[oid].copy()

    def fetch_order(self, oid, symbol, params=None):
        return self.orders[oid].copy()

    def cancel_order(self, oid, symbol, params):
        self.orders[oid]["status"] = "canceled"
        return self.orders[oid]


def demo_account(workspace, monkeypatch, exchange="binance"):
    monkeypatch.setenv("QP_DEMO_EXECUTION", "1")
    aid = "demo-" + exchange
    workspace.vault.put("v2:account:" + aid, {"apiKey": "test-" + exchange, "secret": "test-secret"})
    items = workspace.store.get("v2_watchlist")
    items[0]["exchange"] = exchange
    workspace.save_watchlist(items)
    a = Account(
        id=aid,
        name="exchange test",
        kind="demo",
        exchange=exchange,
        allowed=["btc"],
        sizing=[Sizing(instrument_id="btc", quantity=1, leverage=2)],
        auto=True,
    ).model_dump()
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": True, "max_leverage": 100, "note": "test"}
    )
    a = workspace.save_account(a)
    broker = ExchangeBroker(workspace.store, workspace.vault, ExecutionClient)
    return a, broker


@pytest.mark.parametrize("exchange", ["binance", "okx", "gate"])
def test_external_partial_fill_has_native_protection(workspace, monkeypatch, opportunity, quote, exchange):
    a, b = demo_account(workspace, monkeypatch, exchange)
    c = b.client(a)
    opportunity = {**opportunity, "exchange": exchange}
    c.partial = 0.6
    result = b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert result["status"] == "protected"
    assert len(c.calls) == 4
    assert c.calls[0][5]["timeInForce"] == "IOC"
    assert all(call[5]["reduceOnly"] for call in c.calls[1:])
    assert c.calls[1][3] == result["filled"]
    assert sum(Decimal(str(call[3])) for call in c.calls[2:]) == Decimal(str(result["filled"]))


def test_unknown_external_order_never_retried(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    c.failure = "entry_timeout"
    with pytest.raises(ValueError, match="执行状态不确定"):
        b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert not workspace.store.get("v2_accounts")[a["id"]]["auto"]
    a = workspace.store.get("v2_accounts")[a["id"]]
    a["auto"] = True
    with pytest.raises(ValueError, match="人工核对"):
        workspace.save_account(a)
    c.failure = ""
    with pytest.raises(ValueError):
        b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert len(c.calls) == 1


def test_protection_failure_exits_known_fill(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    c.failure = "stop"
    result = b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert result["status"] == "emergency_closed"
    assert c.calls[-1][5]["reduceOnly"] and c.calls[-1][1] == "market"
    assert not c.fetch_positions() and not workspace.store.get("v2_accounts")[a["id"]]["auto"]


def test_external_exit_on_reversal_only_reduces(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    b.manage(a, {"btc": quote}, {"btc": {**opportunity, "direction": "short"}}, universe(workspace))
    assert c.calls[-1][5]["reduceOnly"] and not c.fetch_positions()
    b.manage(a, {"btc": quote}, {"btc": opportunity}, universe(workspace))
    assert not workspace.store.get("v2_managed")[a["id"]]


def test_external_foreign_position_blocks_entries(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    c.positions["ETH/USDT:USDT"] = {"symbol": "ETH/USDT:USDT", "side": "long", "contracts": 1}
    with pytest.raises(ValueError, match="外部"):
        b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert not c.calls


def test_daily_loss_blocks_more_risk(workspace, account, opportunity, quote):
    book = ledger(10000)
    book.update(day=time.strftime("%Y-%m-%d", time.gmtime()), day_equity="12000")
    with pytest.raises(ValueError, match="每日亏损"):
        size_order(account, book, opportunity, {"btc": quote}, universe(workspace), "balanced", time.time())


def test_short_profit_and_stop(workspace, account, opportunity, quote):
    op = {
        **opportunity,
        "direction": "short",
        "plan": {
            **opportunity["plan"],
            "stop": 104,
            "targets": [{"price": 94, "ratio": 0.5}, {"price": 88, "ratio": 0.5}],
        },
    }
    workspace.paper.enter(account, op, {"btc": quote}, universe(workspace), "balanced")
    workspace.paper.protect(account, {"btc": {**quote, "bid": 86.99, "ask": 87, "mid": 87}}, {"btc": op})
    book = workspace.store.get("v2_ledgers")[account["id"]]
    assert not book["positions"] and Decimal(book["cash"]) > 10000


def test_changed_market_cannot_use_old_signal(workspace, account, opportunity, quote):
    items = universe(workspace)
    items["btc"] = {**items["btc"], "exchange": "okx"}
    with pytest.raises(ValueError, match="配置已变化"):
        size_order(account, ledger(10000), opportunity, {"btc": quote}, items, "balanced", time.time())


def test_maximum_reserves_room_and_enforces_add_count(workspace, account, opportunity, quote):
    account["sizing"] = [Sizing(instrument_id="btc", quantity=3, add_count=1, add_ratios=[0.5]).model_dump()]
    account = workspace.save_account(account)
    r = workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert Decimal(r["qty"]) * Decimal(r["price"]) < 201
    q = {**quote, "bid": 103.99, "ask": 104.01, "mid": 104}
    result = workspace.paper.enter(
        account, opportunity, {"btc": q}, universe(workspace), "balanced", adding=True
    )
    assert result["status"] == "filled"
    with pytest.raises(ValueError, match="加仓"):
        workspace.paper.enter(
            account, {**opportunity, "id": "next"}, {"btc": q}, universe(workspace), "balanced", adding=True
        )


def test_fixed_add_is_not_rejected_by_unrealized_gain(workspace, account, opportunity, quote):
    account["sizing"] = [
        Sizing(instrument_id="btc", mode="fixed", quantity=1, add_count=1, add_ratios=[0.5]).model_dump()
    ]
    account = workspace.save_account(account)
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    q = {**quote, "bid": 103.99, "ask": 104.01, "mid": 104}
    result = workspace.paper.enter(
        account, opportunity, {"btc": q}, universe(workspace), "balanced", adding=True
    )
    assert result["status"] == "filled"


def test_opposite_side_never_added(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    with pytest.raises(ValueError, match="相反方向"):
        workspace.paper.enter(
            account,
            {**opportunity, "direction": "short", "id": "changed"},
            {"btc": quote},
            universe(workspace),
            "balanced",
            adding=True,
        )


def test_ai_targets_revalidate_profile_rr(opportunity):
    op = {**opportunity, "profile": "conservative"}
    rev = {
        "instrument_id": "btc",
        "score_delta": 0,
        "ttl": 60,
        "action": "tighten",
        "stop": None,
        "targets": [{"price": 105, "ratio": 0.5}, {"price": 108, "ratio": 0.5}],
        "reason": "nearer",
    }
    result = apply_revision(op, rev, time.time(), True)
    assert result["plan"]["rr"] < 2 and not result["valid"]
    assert result["plan"]["targets"] == rev["targets"]
    assert apply_revision(op, rev, time.time(), False)["plan"] == op["plan"]
    rev["targets"][0]["price"] = 120
    assert apply_revision(op, rev, time.time(), True)["ai"]["targets"] is None


def test_ai_tightens_paper_protection(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    ai = {
        "intervention_enabled": True,
        "action": "tighten",
        "stop": 98,
        "targets": [{"price": 105, "ratio": 0.6}, {"price": 110, "ratio": 0.4}],
    }
    workspace.paper.protect(account, {"btc": quote}, {"btc": {**opportunity, "ai": ai}})
    p = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    assert p["stop"] == "98" and p["targets"] == ai["targets"]


def test_deleted_native_stop_triggers_confirmed_exit(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    r = b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    c.orders[r["stop_id"]]["status"] = "canceled"
    b.manage(a, {"btc": quote}, {"btc": opportunity}, universe(workspace))
    assert not c.fetch_positions() and not b.availability(a)["ready"]


def test_native_ai_targets_and_stop_replace_reduce_only(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    r = b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    ai = {
        "intervention_enabled": True,
        "action": "tighten",
        "stop": 98,
        "targets": [{"price": 105, "ratio": 0.6}, {"price": 110, "ratio": 0.4}],
    }
    b.manage(a, {"btc": quote}, {"btc": {**opportunity, "ai": ai}}, universe(workspace))
    p = workspace.store.get("v2_managed")[a["id"]]["btc"]
    assert p["stop"] == "98" and p["targets"] == ai["targets"]
    assert c.orders[r["stop_id"]]["status"] == "canceled"
    assert all(c.orders[t]["status"] == "canceled" for t in r["target_ids"])
    assert all(x[5]["reduceOnly"] for x in c.calls[1:])


def test_execution_freeze_survives_recreate(workspace, monkeypatch):
    a, b = demo_account(workspace, monkeypatch)
    b.freeze(a["id"], "test uncertainty")
    fresh = ExchangeBroker(workspace.store, workspace.vault, ExecutionClient)
    assert not fresh.availability(a)["ready"]


def test_gate_uses_native_client_id_prefix(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch, "gate")
    b.enter(a, {**opportunity, "exchange": "gate"}, {"btc": quote}, universe(workspace), "balanced")
    for call in b.client(a).calls:
        assert call[5]["clientOrderId"].startswith("t-")
        assert "marginMode" not in call[5]


def test_public_liquidation_never_labels_as_all_exchanges(workspace):
    from quantpilot.v2.feeds import Feeds

    now = int(time.time() // 3600) * 3600

    def handler(request):
        assert request.url.host == "api.gateio.ws"
        return httpx.Response(
            200,
            json=[
                {"time": now - 3600, "long_liq_usd": 1, "short_liq_usd": 2, "long_liq_usd_new": 3},
                {"time": now, "long_liq_usd": 4, "short_liq_usd": 5, "long_liq_usd_new": 6},
            ],
        )

    f = Feeds(workspace.vault, workspace.store, httpx.MockTransport(handler))
    r = f.public_liquidation("BTC")
    assert r["status"] == "fresh" and r["details"]["long_liquidation_usd"] == 9
    assert "单市场" in r["source"] and "不代表币安" in r["note"]


def test_coinglass_expired_disclosure_not_refreshed(workspace):
    from quantpilot.v2.feeds import Feeds

    workspace.vault.put("v2:feed:coinglass", {"apiKey": "test-key"})
    stamp = time.time() - 5 * 86400

    def handler(request):
        assert request.headers["CG-API-KEY"] == "test-key"
        return httpx.Response(200, json={"code": "0", "data": [{"timestamp": stamp * 1000, "flow_usd": 123}]})

    r = Feeds(workspace.vault, workspace.store, httpx.MockTransport(handler)).coinglass("BTC")[0]
    assert r["status"] == "stale" and r["published_at"] == pytest.approx(stamp, rel=0, abs=1e-6)


def test_legacy_kill_also_pauses_v2(workspace, account):
    workspace.store.set_kill(True)
    assert workspace.store.get("v2_preferences")["halted"]
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]
    workspace.store.set_kill(False)
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]


def test_v2_halt_also_pauses_legacy(workspace):
    workspace.preferences(Preferences(halted=True).model_dump())
    assert workspace.store.get("kill") is True
    assert workspace.store.get("strategy")["enabled"] is False


def test_ai_response_for_replaced_signal_is_discarded(workspace, opportunity, monkeypatch):
    import quantpilot.v2.service as service

    prefs = Preferences(ai_enabled=True, ai_model="test").model_dump()
    workspace.vault.put("v2:ai:openai", {"apiKey": "test"})
    workspace.preferences(prefs)
    workspace.opportunities = {"btc": opportunity}

    def review(*args, **kwargs):
        workspace.opportunities = {"btc": {**opportunity, "id": "new-signal"}}
        return [
            {
                "instrument_id": "btc",
                "score_delta": -10,
                "ttl": 60,
                "action": "close",
                "stop": None,
                "targets": None,
                "reason": "old",
            }
        ]

    monkeypatch.setattr(service, "review", review)
    workspace.review_ai(prefs)
    assert "ai" not in workspace.opportunities["btc"]


def test_api_configuration_validation_and_persistence(logged, workspace):
    items = workspace.store.get("v2_watchlist")
    response = logged.put("/api/v2/watchlist", json={"items": items})
    assert response.status_code == 200
    assert logged.put("/api/v2/watchlist", json={"items": items + [items[0]]}).status_code == 409
    assert logged.get("/api/v2/chart/absent").status_code == 404
    workspace.market.demo = True
    assert len(logged.get("/api/v2/chart/btc?frame=4h").json()["candles"]) > 50
    assert logged.get("/api/v2/chart/btc?frame=bad").status_code == 503
    assert logged.get("/api/v2/markets?exchange=unsupported").status_code == 422
    assert (
        logged.put("/api/v2/preferences", json=Preferences(profile="aggressive").model_dump()).status_code
        == 200
    )
    assert logged.post("/api/v2/keys/openai", json={"api_key": "unit-test-key"}).json()["saved"]
    assert "unit-test-key" not in logged.get("/api/v2/state").text
    assert logged.request("DELETE", "/api/v2/keys/openai", json={}).status_code == 200
    assert logged.post("/api/v2/ai/review", json={}).status_code == 409
    now = time.time()
    data = {
        "instrument_id": "btc",
        "category": "macro",
        "source": "test filing",
        "url": "https://example.com/filing",
        "published_at": now - 10,
        "expires_at": now + 100,
        "bias": "risk",
        "event_at": now + 20,
        "value": 0,
        "note": "test",
    }
    assert logged.post("/api/v2/evidence", json=data).status_code == 200
    assert logged.post("/api/v2/evidence", json={**data, "published_at": now + 600}).status_code == 422
    assert logged.post("/api/v2/evidence", json={**data, "url": "http://127.0.0.1"}).status_code == 422
    assert (
        logged.post(
            "/api/v2/accounts/paper-default/reset", json={"capital": 25000, "confirmation": "RESET"}
        ).status_code
        == 200
    )
    assert (
        logged.post(
            "/api/v2/accounts/paper-default/reset", json={"capital": 25000, "confirmation": "no"}
        ).status_code
        == 422
    )
    assert logged.get("/api/v2/state").json()["accounts"][0]["summary"]["equity"] == "25000.0"


def test_cli_status_and_halt_include_workspace(workspace, settings, tmp_path, monkeypatch, capsys):
    import quantpilot.cli as cli
    from quantpilot.cli import main
    from quantpilot.config import atomic_json

    path = workspace.store.directory / "config.json"
    atomic_json(path, settings.model_dump())
    monkeypatch.setenv("QP_CONFIG", str(path))
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    assert main(["status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["workspace"]["profile"] == "balanced" and result["accounts"][0]["kind"] == "paper"
    assert main(["kill", "on"]) == 0
    assert workspace.store.get("v2_preferences")["halted"]


def test_start_pauses_all_accounts(workspace, account, monkeypatch):
    import quantpilot.v2.service as service

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def join(self, **kwargs):
            pass

    monkeypatch.setattr(service.threading, "Thread", Thread)
    workspace.start()
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]
    workspace.stop()


def test_live_receipt_is_bound_expiring_private_and_blockable(workspace, monkeypatch):
    import hashlib

    a = Account(id="cert-test", name="test", kind="live").model_dump()
    secret = {"apiKey": "unit-test", "secret": "unit-test"}
    workspace.vault.put("v2:account:" + a["id"], secret)
    monkeypatch.setenv("QP_LIVE_EXECUTION", "1")
    cert = {
        "version": "0.5.0",
        "exchange": "binance",
        "credential_fingerprint": hashlib.sha256(json.dumps(secret, sort_keys=True).encode()).hexdigest(),
        "verified_at": time.time(),
        "checks": {
            k: True
            for k in (
                "sandbox_fill",
                "native_stop",
                "partial_take_profit",
                "reduce_only",
                "reconnect_reconcile",
                "timeout_no_duplicate",
            )
        },
    }
    path = workspace.store.directory / "certifications" / (a["id"] + ".json")
    path.parent.mkdir()
    path.write_text(json.dumps(cert))
    path.chmod(0o600)
    assert workspace.exchange.availability(a)["ready"]
    path.chmod(0o644)
    assert not workspace.exchange.availability(a)["ready"]
    path.chmod(0o600)
    cert["verified_at"] -= 8 * 86400
    path.write_text(json.dumps(cert))
    assert not workspace.exchange.availability(a)["ready"]
    cert["verified_at"] = time.time()
    path.write_text(json.dumps(cert))
    workspace.vault.put("v2:account:" + a["id"], {"apiKey": "other", "secret": "other"})
    assert not workspace.exchange.availability(a)["ready"]


def test_pause_new_entries_does_not_itself_reduce_healthy_position(workspace, account, opportunity, quote):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    qty = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"]
    workspace.quotes = {"btc": quote}
    workspace.opportunities = {"btc": opportunity}
    workspace.preferences(Preferences(halted=True).model_dump())
    workspace.execute()
    assert workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"] == qty
