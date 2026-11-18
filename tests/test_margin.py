"""Collateral conservation and mode-specific execution, using offline exchange doubles."""

import copy
import time
from decimal import Decimal as D

import ccxt
import pytest
from test_v2 import demo_account

from quantpilot.v2.accounts import ledger, size_order, valuation
from quantpilot.v2.margin import estimates, liquidation
from quantpilot.v2.models import Sizing


def position(side="short", margin_mode="isolated", qty="100", entry="100", margin="1000"):
    return dict(side=side, margin_mode=margin_mode, qty=qty, entry=entry, margin=margin)


@pytest.mark.parametrize(
    "side,expected", [("long", D("9000") / D("99.45")), ("short", D("11000") / D("100.55"))]
)
def test_isolated_threshold_satisfies_margin_equation(side, expected):
    p = position(side)
    price = liquidation(p, D("1000"))
    assert price == expected
    sign = 1 if side == "long" else -1
    equity = D("1000") + sign * D("100") * (price - D("100"))
    assert abs(equity - D("100") * price * D(".0055")) < D("1e-20")


def test_cross_uses_shared_wallet_and_excludes_isolated_collateral():
    b = ledger(10000)
    b["positions"] = {"a": position(margin_mode="cross"), "i": position(margin="2000")}
    marks = {"a": D(100), "i": D(100)}
    initial = estimates(b, marks)["a"]
    assert initial == D("18000") / D("100.55")
    marks["i"] = D(1)  # Isolated profit cannot be pledged to cross positions.
    assert estimates(b, marks)["a"] == initial
    b["positions"]["b"] = position("long", "cross", "10", "100", "100")
    marks["b"] = D(100)
    before = estimates(b, marks)["a"]
    marks["b"] = D(80)
    assert estimates(b, marks)["a"] < before
    b["cash"] = "9900"  # Fees/funding reduce shared collateral immediately.
    assert estimates(b, marks)["a"] < before


def test_no_positive_liquidation_is_not_zero_or_missing_quote():
    b = ledger(10000)
    b["positions"] = {"a": position("long", "cross", "1", "100", "10")}
    q = {"a": {"bid": 100, "ask": 100, "timestamp": time.time()}}
    s = valuation(b, q, time.time())
    assert s["positions"][0]["liquidation"] is None
    assert s["positions"][0]["liquidation_status"] == "no_positive_price"
    assert s["positions"][0]["liquidation_verified"]
    assert not valuation(b, {}, time.time())["positions"][0]["liquidation_verified"]


def test_available_excludes_isolated_unrealized_profit(quote):
    b = ledger(1000)
    b["positions"] = {
        "i": position("long", qty="1", margin="10"),
        "c": position("long", "cross", "1", "100", "10"),
    }
    quotes = {"i": {**quote, "bid": 200}, "c": {**quote, "bid": 110}}
    s = valuation(b, quotes, time.time())
    assert D(s["available"]) == 990
    assert D(s["equity"]) == 1110
    quotes["i"]["timestamp"] -= 90
    s = valuation(b, quotes, time.time())
    assert not next(p for p in s["positions"] if p["instrument_id"] == "c")["liquidation_verified"]


@pytest.mark.parametrize("selected", ["isolated", "cross"])
def test_paper_mode_persisted_and_held_mode_change_blocked(workspace, account, opportunity, quote, selected):
    account["sizing"][0]["margin_mode"] = selected
    account = workspace.save_account(account)
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, instruments, "balanced")
    b = workspace.store.get("v2_ledgers")[account["id"]]
    assert b["positions"]["btc"]["margin_mode"] == selected
    changed = copy.deepcopy(account)
    changed["sizing"][0]["margin_mode"] = "cross" if selected == "isolated" else "isolated"
    with pytest.raises(ValueError, match="先平仓"):
        workspace.save_account(changed)
    workspace.paper.close(account["id"], "btc", quote)
    workspace.save_account(changed)


def test_unknown_order_blocks_margin_switch(workspace, account):
    with workspace.store.transaction() as db:
        db.execute(
            "INSERT INTO v2_orders VALUES ('pending',?,'open','unknown','{}',?)", (account["id"], time.time())
        )
    account["sizing"][0]["margin_mode"] = "cross"
    with pytest.raises(ValueError, match="待核对"):
        workspace.save_account(account)


def test_legacy_position_remains_isolated(workspace, account, quote, opportunity):
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, instruments, "balanced")
    with workspace.store.transaction() as db:
        books = workspace.store.read(db, "v2_ledgers")
        del books[account["id"]]["positions"]["btc"]["margin_mode"]
        workspace.store.write(db, "v2_ledgers", books)
    account["sizing"][0]["margin_mode"] = "cross"
    with pytest.raises(ValueError, match="先平仓"):
        workspace.save_account(account)
    s = valuation(workspace.store.get("v2_ledgers")[account["id"]], {"btc": quote}, time.time())
    assert s["positions"][0]["margin_mode"] == "isolated"


@pytest.mark.parametrize("selected", ["isolated", "cross"])
def test_add_uses_weighted_entry_and_combined_collateral(
    workspace, account, opportunity, quote, selected, monkeypatch
):
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": True, "max_leverage": 100, "note": "test"}
    )
    account["sizing"] = [
        Sizing(
            instrument_id="btc",
            mode="fixed",
            quantity=1,
            leverage=2,
            margin_mode=selected,
            add_count=1,
            add_ratios=[0.25],
        ).model_dump()
    ]
    account = workspace.save_account(account)
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, instruments, "balanced")
    q = {**quote, "bid": 103.99, "ask": 104.01, "mid": 104}
    b = workspace.store.get("v2_ledgers")[account["id"]]
    sizing = size_order(account, b, opportunity, {"btc": q}, instruments, "balanced", time.time(), True)
    workspace.paper.enter(account, {**opportunity, "id": "add-1"}, {"btc": q}, instruments, "balanced", True)
    b = workspace.store.get("v2_ledgers")[account["id"]]
    p = b["positions"]["btc"]
    expected = estimates(b, {"btc": D("103.99")})["btc"]
    assert p["liquidation"] == (str(expected) if expected is not None else None)
    assert p["liquidation"] == sizing["liquidation"]
    assert D(p["qty"]) == D("1.25")
    assert D(p["entry"]) < sizing["price"]
    workspace.paper.close(account["id"], "btc", q, 0.5)
    b = workspace.store.get("v2_ledgers")[account["id"]]
    assert D(b["positions"]["btc"]["margin"]) == D(p["margin"]) / 2


def test_isolated_gap_cannot_spend_other_collateral(workspace, account, opportunity, quote, monkeypatch):
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": True, "max_leverage": 100, "note": "test"}
    )
    account["sizing"][0]["leverage"] = 2
    account = workspace.save_account(account)
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, instruments, "balanced")
    b = workspace.store.get("v2_ledgers")[account["id"]]
    expected = D(b["cash"]) - D(b["positions"]["btc"]["margin"])
    workspace.paper.close(account["id"], "btc", {**quote, "bid": 1, "ask": 1, "mid": 1})
    assert D(workspace.store.get("v2_ledgers")[account["id"]]["cash"]) == expected


@pytest.mark.parametrize("selected", ["isolated", "cross"])
def test_funding_collateral_and_protection_recomputed(workspace, account, opportunity, quote, selected):
    account["sizing"][0]["margin_mode"] = selected
    account = workspace.save_account(account)
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, instruments, "balanced")
    with workspace.store.transaction() as db:
        books = workspace.store.read(db, "v2_ledgers")
        books[account["id"]]["positions"]["btc"]["funding_at"] = 0
        workspace.store.write(db, "v2_ledgers", books)
    before = workspace.store.get("v2_ledgers")[account["id"]]
    workspace.paper.protect(account, {"btc": quote}, {"btc": opportunity})
    after = workspace.store.get("v2_ledgers")[account["id"]]
    paid = D(before["cash"]) - D(after["cash"])
    assert paid > 0
    assert D(before["positions"]["btc"]["margin"]) - D(after["positions"]["btc"]["margin"]) == (
        paid if selected == "isolated" else 0
    )


@pytest.mark.parametrize("exchange", ["gate", "okx", "binance"])
@pytest.mark.parametrize("selected", ["isolated", "cross"])
def test_exchange_entry_and_native_protection_use_selected_mode(
    workspace, monkeypatch, opportunity, quote, exchange, selected
):
    a, broker = demo_account(workspace, monkeypatch, exchange)
    a["sizing"][0]["margin_mode"] = selected
    a = workspace.save_account(a)
    c = broker.client(a)
    calls = []
    c.set_margin_mode = lambda *args: calls.append(("mode", args))
    c.set_leverage = lambda *args: calls.append(("leverage", args))
    original = c.create_order

    def create(*args):
        order = original(*args)
        for p in c.positions.values():
            p["marginMode"] = selected
        return order

    c.create_order = create
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    result = broker.enter(a, {**opportunity, "exchange": exchange}, {"btc": quote}, instruments, "balanced")
    assert result["status"] == "protected"
    assert workspace.store.get("v2_managed")[a["id"]]["btc"]["margin_mode"] == selected
    assert [x[1][0] for x in calls if x[0] == "mode"] == ([selected] if exchange == "binance" else [])
    assert calls[-1][1][2] == ({} if exchange == "binance" else {"marginMode": selected})
    for call in c.calls:
        assert call[-1].get("marginMode") == (selected if exchange == "okx" else None)
    broker.risk_book(a, broker.read(a), instruments)
    c.positions[opportunity["symbol"]]["marginMode"] = "cross" if selected == "isolated" else "isolated"
    with pytest.raises(ValueError, match="模式不一致"):
        broker.risk_book(a, broker.read(a), instruments)
    before = len(c.calls)
    broker.manage(a, {"btc": quote}, {"btc": opportunity}, instruments)
    assert len(c.calls) == before
    assert workspace.store.get("v2_execution_blocks")[a["id"]]


@pytest.mark.parametrize("selected", ["isolated", "cross"])
def test_installed_gate_adapter_encodes_mode_without_network(selected):
    c = ccxt.gate()
    c.set_markets(
        [
            {
                "id": "SOL_USDT",
                "symbol": "SOL/USDT:USDT",
                "base": "SOL",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "future": False,
                "spot": False,
                "linear": True,
                "inverse": False,
            }
        ]
    )
    captured = []
    c.privateFuturesPostSettlePositionsContractLeverage = lambda request: captured.append(request) or {}
    c.set_leverage(10, "SOL/USDT:USDT", {"marginMode": selected})
    assert captured[0]["leverage"] == ("0" if selected == "cross" else "10")
    assert captured[0].get("cross_leverage_limit") == ("10" if selected == "cross" else None)
