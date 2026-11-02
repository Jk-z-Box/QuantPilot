import copy
import json
import time

import pytest

from quantpilot.market import decimal as D
from quantpilot.v2 import ai_client
from quantpilot.v2.accounts import ledger, size_order
from quantpilot.v2.action_review import Decision
from quantpilot.v2.entry_status import report
from quantpilot.v2.models import Account, pair_enabled
from quantpilot.v2.risk_context import account_context
from quantpilot.v2.service import Workspace


def setup(w, a, monkeypatch):
    w.vault.put("v2:ai:openai", {"apiKey": "test-only"})
    w.preferences({**w.store.get("v2_preferences"), "ai_model": "test-model"})
    a.update(w.save_account({**a, "ai_action_enabled": True}))
    queue, calls = [], []
    monkeypatch.setattr(w.action_review.pool, "submit", lambda fn, *args: queue.append((fn, args)))

    def response(prefs, vault, payload, **kwargs):
        data = json.loads(payload["input"])
        calls.append(data)
        value = Decision(
            decision="reject" if data.get("account_name") == "B" else "approve",
            reason="B 风险不通过" if data.get("account_name") == "B" else "账户风险允许",
            entry_low=None,
            entry_high=None,
            stop=None,
            target_1=None,
            target_2=None,
            exit_ratio=None,
        ).model_dump()
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}],
        }

    monkeypatch.setattr(ai_client, "response", response)
    return queue, calls


def drain(queue):
    while queue:
        fn, args = queue.pop(0)
        fn(*args)


def inspect(w, a, op, q, action="open"):
    return w.action_review.inspect(a["id"], op["instrument_id"], action, q, op)


def enter(w, a, op, q):
    return w.paper.enter(
        a, op, {op["instrument_id"]: q}, {i["id"]: i for i in w.store.get("v2_watchlist")}, "balanced"
    )


def test_shared_market_independent_accounts_and_messages(workspace, account, opportunity, quote, monkeypatch):
    w, a = workspace, account
    queue, calls = setup(w, a, monkeypatch)
    b = w.save_account({**a, "id": "", "name": "B"})
    for acct in (a, b):
        assert inspect(w, acct, opportunity, quote)["state"] == "pending"
    assert len(queue) == 1
    drain(queue)
    for acct in (a, b):
        assert inspect(w, acct, opportunity, quote)["state"] == "pending"
    assert len(queue) == 2
    drain(queue)
    assert [d["stage"] for d in calls] == ["shared_market", "account_risk", "account_risk"]
    assert "account_balance" not in calls[0]
    assert inspect(w, a, opportunity, quote)["state"] == "approved"
    assert inspect(w, b, opportunity, quote)["state"] == "rejected"
    for data in calls[1:]:
        assert D(data["account_balance"]["equity"]) == 10000
        assert D(data["account_balance"]["available"]) == 10000
        assert data["portfolio_complete"] and data["portfolio_empty"]
        assert D(data["proposed_order"]["qty"]) > 0
        assert data["proposed_order"]["risk"]["maintenance_rate_estimate"] == "0.005"
        assert data["proposed_order"]["risk"]["liquidation_status"] == "no_positive_price"
    w.quotes, w.opportunities = {"btc": quote}, {"btc": opportunity}
    w.execute()
    assert w.store.get("v2_ledgers")[a["id"]]["positions"]
    assert not w.store.get("v2_ledgers")[b["id"]]["positions"]
    messages = [e["message"] for e in w.notifications.state(0)["events"] if e["category"] == "ai"]
    assert any(a["name"] in m and "已成交" in m and "B" in m and "未开仓" in m for m in messages)
    assert len(calls) == 3


def test_disabled_account_ignores_ai_but_not_risk(workspace, account, opportunity, quote, monkeypatch):
    queue, calls = setup(workspace, account, monkeypatch)
    disabled = workspace.save_account({**account, "id": "", "ai_action_enabled": False})
    assert enter(workspace, disabled, opportunity, quote)["status"] == "filled"
    assert not calls and not queue
    disabled["sizing"][0]["quantity"] = 1000000000
    disabled["sizing"][0]["mode"] = "fixed"
    disabled = workspace.save_account(disabled)
    op = {**opportunity, "id": "next"}
    with pytest.raises(ValueError):
        enter(workspace, disabled, op, quote)


def test_legacy_global_migrates_to_accounts_only(workspace):
    with workspace.store.transaction() as db:
        accounts = workspace.store.read(db, "v2_accounts")
        accounts["paper-default"].pop("ai_action_enabled")
        workspace.store.write(db, "v2_accounts", accounts)
        prefs = workspace.store.read(db, "v2_preferences")
        prefs["ai_action_enabled"] = True
        workspace.store.write(db, "v2_preferences", prefs)
    w = Workspace(
        workspace.store, workspace.vault, workspace.settings, market=workspace.market, feeds=workspace.feeds
    )
    assert w.store.get("v2_accounts")["paper-default"]["ai_action_enabled"]
    assert not w.store.get("v2_preferences")["ai_action_enabled"]
    assert not Account(name="new").ai_action_enabled
    with pytest.raises(ValueError, match="迁移"):
        w.preferences({**prefs, "ai_action_enabled": True})


@pytest.mark.parametrize("change", ["exchange", "direction", "score", "evidence", "plan", "expired", "price"])
def test_shared_market_invalidations(workspace, account, opportunity, quote, monkeypatch, change):
    queue, _ = setup(workspace, account, monkeypatch)
    g = workspace.action_review
    inspect(workspace, account, opportunity, quote)
    drain(queue)
    new = copy.deepcopy(opportunity)
    q = dict(quote)
    if change == "exchange":
        new["exchange"] = "gate"
    elif change == "direction":
        new["direction"] = "short"
    elif change == "score":
        new["score"] -= 1
    elif change == "evidence":
        new["evidence"] = [{"note": "new risk"}]
    elif change == "plan":
        new["plan"]["stop"] -= 3
    elif change == "expired":
        next(iter(g.market.jobs.values()))["completed_at"] -= 31
    else:
        q["mid"] *= 1.01
    # Test market isolation directly; account risk data is not shared.
    assert g.market.inspect(new, q, workspace.store.get("v2_preferences"))["state"] == "pending"
    assert not g.jobs


def test_small_plan_refresh_uses_reviewed_plan_until_expiry(
    workspace, account, opportunity, quote, monkeypatch
):
    queue, _ = setup(workspace, account, monkeypatch)
    inspect(workspace, account, opportunity, quote)
    new = copy.deepcopy(opportunity)
    new["plan"]["entry"] += 0.01
    inspect(workspace, account, new, quote)
    assert len(queue) == 1
    drain(queue)
    inspect(workspace, account, new, quote)
    drain(queue)
    verdict = inspect(workspace, account, new, quote)
    assert verdict["state"] == "approved"
    assert verdict["opportunity"]["plan"]["entry"] == opportunity["plan"]["entry"]


def test_account_approval_cannot_transfer_or_survive_balance_mutation(
    workspace, account, opportunity, quote, monkeypatch
):
    queue, _ = setup(workspace, account, monkeypatch)
    inspect(workspace, account, opportunity, quote)
    drain(queue)
    inspect(workspace, account, opportunity, quote)
    drain(queue)
    approved = inspect(workspace, account, opportunity, quote)["opportunity"]
    b = workspace.save_account({**account, "id": "", "name": "B"})
    with workspace.store.transaction() as db:
        with pytest.raises(ValueError, match="已变化"):
            workspace.action_review.commit_check(db, b["id"], approved)
        books = workspace.store.read(db, "v2_ledgers")
        books[account["id"]]["cash"] = "9000"
        workspace.store.write(db, "v2_ledgers", books)
    with workspace.store.transaction() as db:
        with pytest.raises(ValueError, match="已变化"):
            workspace.action_review.commit_check(db, account["id"], approved)


def test_market_timeout_blocks_entries_but_not_stop(workspace, account, opportunity, quote, monkeypatch):
    enter(workspace, account, opportunity, quote)
    queue, _ = setup(workspace, account, monkeypatch)
    inspect(workspace, account, opportunity, quote)
    for j in workspace.action_review.market.jobs.values():
        j["deadline"] = time.time() - 1
    with pytest.raises(ValueError, match="不可用"):
        inspect(workspace, account, opportunity, quote)
    monkeypatch.setattr(ai_client, "ready", lambda *a: False)
    q = {**quote, "bid": 94.99, "ask": 95.01, "mid": 95}
    workspace.paper.protect(account, {"btc": q}, {})
    assert not workspace.store.get("v2_ledgers")[account["id"]]["positions"]
    drain(queue)
    assert all(j["state"] == "error" for j in workspace.action_review.market.jobs.values())


def sized(w, a, op, q):
    return size_order(
        a,
        copy.deepcopy(w.store.get("v2_ledgers")[a["id"]]),
        op,
        {"btc": q},
        {i["id"]: i for i in w.store.get("v2_watchlist")},
        "balanced",
        time.time(),
    )


def test_system_levels_and_budget_limits(workspace, account, opportunity, quote):
    amounts = []
    for level, fraction in [("conservative", 0.2), ("balanced", 0.4), ("aggressive", 0.7)]:
        a = copy.deepcopy(account)
        a["sizing"][0].update(
            mode="system", quantity=None, system_level=level, add_count=0, margin_mode="cross"
        )
        a = Account.model_validate(a).model_dump()
        assert pair_enabled(a, "btc")
        order = sized(workspace, a, opportunity, quote)
        amounts.append(order["qty"])
        risk = order["risk"]
        assert D(risk["initial_margin"]) + D(risk["estimated_entry_fee"]) <= D(risk["available_before"]) * D(
            str(fraction)
        )
        assert D(risk["available_after"]) > 0
        assert risk["stop_to_liquidation_pct"] is None or risk["stop_to_liquidation_pct"] >= 3
    assert 0 < amounts[0] < amounts[1] < amounts[2]


def test_system_reserves_for_adds_and_rejects_infeasible_isolated(workspace, account, opportunity, quote):
    a = copy.deepcopy(account)
    a["sizing"][0].update(mode="system", quantity=None, add_count=0, leverage=10, margin_mode="cross")
    initial = sized(workspace, a, opportunity, quote)
    a["sizing"][0].update(add_count=2, add_ratios=[0.25, 0.25])
    reserved = sized(workspace, a, opportunity, quote)
    assert reserved["qty"] < initial["qty"]
    a["sizing"][0].update(leverage=100, margin_mode="isolated")
    with pytest.raises(ValueError, match="强平"):
        sized(workspace, a, opportunity, quote)


def test_paper_context_cross_100x_has_equity_and_projected_liquidation(
    workspace, account, opportunity, quote
):
    a = copy.deepcopy(account)
    a["sizing"][0].update(mode="fixed", quantity=1000, leverage=100, margin_mode="cross")
    op = copy.deepcopy(opportunity)
    op["direction"] = "short"
    op["plan"].update(stop=101.11, targets=[{"price": 98, "ratio": 0.5}, {"price": 96, "ratio": 0.5}])
    data = account_context(workspace, a, ledger(10000000), "btc", quote, op, "open")
    assert D(data["account_balance"]["equity"]) == 10000000
    assert D(data["account_balance"]["available"]) == 10000000
    assert D(data["proposed_order"]["qty"]) == 1000
    assert D(data["proposed_order"]["liquidation"]) > 1000
    assert data["proposed_order"]["risk"]["stop_to_liquidation_pct"] > 3


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"equity": "1000", "available": "100", "positions": [], "checked_at": 1},
        {"equity": "1000", "positions": [], "checked_at": time.time()},
    ],
)
def test_external_missing_risk_data_blocks(workspace, account, opportunity, quote, raw):
    a = {**account, "kind": "live", "allowed": ["btc"]}
    with pytest.raises(ValueError):
        account_context(workspace, a, raw, "btc", quote, opportunity, "open")


def test_external_snapshot_uses_available_and_labels_estimates(workspace, account, opportunity, quote):
    a = {**account, "kind": "live", "allowed": ["btc"]}
    data = account_context(
        workspace,
        a,
        {"equity": "10000", "available": "5000", "positions": [], "checked_at": time.time()},
        "btc",
        quote,
        opportunity,
        "open",
    )
    assert data["exchange_reported_balance"]["equity"] == "10000"
    assert data["account_balance"]["available"] == "5000"
    assert data["portfolio_empty"]
    assert "not exchange" in data["proposed_order"]["risk"]["model"]


def test_notifications_deduplicate_and_distinguish_accounts(workspace, account, opportunity, monkeypatch):
    setup(workspace, account, monkeypatch)
    report(workspace, account["id"], opportunity, "blocked", "强平距离不足")
    count = len(workspace.notifications.state(0)["events"])
    report(workspace, account["id"], opportunity, "blocked", "强平距离不足")
    assert len(workspace.notifications.state(0)["events"]) == count


def test_auth_change_disables_account_reviews(workspace, account, monkeypatch):
    setup(workspace, account, monkeypatch)
    workspace.auth_changed()
    assert not workspace.store.get("v2_accounts")[account["id"]]["ai_action_enabled"]


def test_funding_failure_reported_without_spending_account_ai(
    workspace, account, opportunity, quote, monkeypatch
):
    queue, calls = setup(workspace, account, monkeypatch)
    a = workspace.save_account({**account, "name": "A"})
    b = copy.deepcopy(account)
    b.update(id="", name="资金不足账户", capital=100)
    b["sizing"][0].update(mode="fixed", quantity=1)
    b = workspace.save_account(b)
    workspace.quotes, workspace.opportunities = {"btc": quote}, {"btc": opportunity}
    for _ in range(3):
        workspace.execute()
        drain(queue)
    assert workspace.store.get("v2_ledgers")[a["id"]]["positions"]
    assert not workspace.store.get("v2_ledgers")[b["id"]]["positions"]
    assert [c["stage"] for c in calls] == ["shared_market", "account_risk"]
    assert "固定仓位超过" in workspace.errors[b["id"]]
    messages = [e["message"] for e in workspace.notifications.state(0)["events"]]
    assert any("A（模拟）：已成交" in m and "资金不足账户（模拟）：未开仓" in m for m in messages)


def test_system_size_after_ai_cannot_exceed_reviewed_quantity(
    workspace, account, opportunity, quote, monkeypatch
):
    queue, _ = setup(workspace, account, monkeypatch)
    a = copy.deepcopy(account)
    a["sizing"][0].update(mode="system", quantity=None, add_count=0)
    a = workspace.save_account(a)
    inspect(workspace, a, opportunity, quote)
    drain(queue)
    inspect(workspace, a, opportunity, quote)
    drain(queue)
    approved = inspect(workspace, a, opportunity, quote)["opportunity"]
    order = sized(workspace, a, approved, quote)
    prior = copy.deepcopy(order)
    # Fresh recalculation at a slightly different price must not inflate approved units.
    order["qty"] *= D("1.01")
    order["notional"] *= D("1.01")
    workspace.action_review.check_size(approved, order)
    assert order["qty"] <= prior["qty"]
    assert order["notional"] <= prior["notional"]


def test_market_adjustment_shared_once_before_account_risk(
    workspace, account, opportunity, quote, monkeypatch
):
    queue, calls = setup(workspace, account, monkeypatch)
    b = workspace.save_account({**account, "id": "", "name": "C"})
    inspect(workspace, account, opportunity, quote)
    drain(queue)
    market_job = next(iter(workspace.action_review.market.jobs.values()))
    market_job["result"].update(decision="adjust", stop=97)
    for a in (account, b):
        inspect(workspace, a, opportunity, quote)
    drain(queue)
    assert len(calls) == 3
    for data in calls[1:]:
        assert data["opportunity"]["plan"]["stop"] == 97
        assert D(data["proposed_order"]["stop"]) == 97


def test_system_entry_and_add_preserve_limits(workspace, account, opportunity, quote):
    a = copy.deepcopy(account)
    a["sizing"][0].update(mode="system", quantity=None, add_count=1, add_ratios=[0.25])
    a = workspace.save_account(a)
    filled = enter(workspace, a, opportunity, quote)
    assert filled["status"] == "filled"
    before = workspace.store.get("v2_ledgers")[a["id"]]["positions"]["btc"]
    q = {**quote, "mid": 103, "bid": 102.99, "ask": 103.01}
    op = {**opportunity, "id": "add-signal"}
    result = workspace.paper.enter(
        a, op, {"btc": q}, {i["id"]: i for i in workspace.store.get("v2_watchlist")}, "balanced", adding=True
    )
    assert result["status"] == "filled"
    assert D(result["qty"]) <= D(before["initial_qty"]) * D(".25")
    with pytest.raises(ValueError, match="加仓条件"):
        workspace.paper.enter(
            a,
            {**op, "id": "add-again"},
            {"btc": q},
            {i["id"]: i for i in workspace.store.get("v2_watchlist")},
            "balanced",
            adding=True,
        )
