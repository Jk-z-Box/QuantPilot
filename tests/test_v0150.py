import copy
import json
import time

import pytest
from pydantic import ValidationError

from quantpilot.v2 import ai_client
from quantpilot.v2.accounts import ledger, size_order
from quantpilot.v2.models import Account, account_revision
from quantpilot.v2.risk_budget import BudgetExceeded
from quantpilot.v2.risk_policy import RiskAI, RiskPolicy, bounded_changes, migrate, policy, value
from quantpilot.v2.risk_review import funds_token
from quantpilot.v2.rules import Parameters
from quantpilot.v2.service import Workspace


def sized(w, a, op, q, book=None, profile="balanced"):
    return size_order(
        a,
        book or ledger(100),
        op,
        {"btc": q},
        {i["id"]: i for i in w.store.get("v2_watchlist")},
        profile,
        time.time(),
    )


def unlimited(a):
    a = copy.deepcopy(a)
    for v in a["risk_policy"]["limits"].values():
        v["enabled"] = False
    a["sizing"][0].update(mode="fixed", quantity=1, leverage=100, margin_mode="cross", add_count=0)
    return a


def setup_review(w, a, monkeypatch, enabled=True):
    w.vault.put("v2:ai:openai", {"apiKey": "test-key"})
    w.preferences({**w.store.get("v2_preferences"), "ai_model": "test-model"})
    a = copy.deepcopy(a)
    a["risk_ai"].update(
        enabled=enabled,
        authority={
            "exposure_multiple": {"allowed": True, "minimum": 0.3, "maximum": 2, "allow_disable": False}
        },
    )
    a = w.save_account(a)
    queue = []
    monkeypatch.setattr(w.risk_reviews.pool, "submit", lambda fn, *args: queue.append((fn, args)))
    return a, queue


def response(changes=None, adequate=True, reason="核验账户可用资金、止损预算与强平缓冲后调整"):
    data = {"data_adequate": adequate, "changes": changes or [], "reason": reason}
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(data)}]}],
    }


def drain(queue):
    fn, args = queue.pop(0)
    fn(*args)


def test_disabled_limits_have_no_fallback_and_signal_profile_is_independent(
    workspace, account, opportunity, quote
):
    a = unlimited(account)
    results = [
        sized(workspace, a, opportunity, quote, profile=p) for p in ("conservative", "balanced", "aggressive")
    ]
    assert all(r["qty"] == 1 for r in results)
    assert {r["key"] for r in results[0]["risk"]["budget_breakdown"]} == {"available", "quantity"}
    assert results[0]["risk"]["initial_margin"] < "2"


@pytest.mark.parametrize(
    "key,v",
    [
        ("trade_loss_pct", 1),
        ("exposure_multiple", 0.5),
        ("group_multiple", 0.3),
        ("portfolio_loss_pct", 1),
        ("margin_pct", 0.1),
    ],
)
def test_independent_limit_rejections_have_amount_breakdown(workspace, account, opportunity, quote, key, v):
    a = unlimited(account)
    a["risk_policy"]["limits"][key] = {"enabled": True, "value": v}
    with pytest.raises(BudgetExceeded) as error:
        sized(workspace, a, opportunity, quote)
    assert error.value.details["currency"] == "USDT"
    assert key in {r["key"] for r in error.value.details["limits"]}
    assert "本次需要" in str(error.value) and "允许新增" in str(error.value)


def test_disabled_daily_budget_does_not_block(workspace, account, opportunity, quote):
    a = unlimited(account)
    book = ledger(100)
    book.update(day=time.strftime("%Y-%m-%d", time.gmtime()), day_equity="200")
    assert sized(workspace, a, opportunity, quote, book)["qty"] == 1
    a["risk_policy"]["limits"]["daily_loss_pct"] = {"enabled": True, "value": 3}
    with pytest.raises(ValueError, match="每日"):
        sized(workspace, a, opportunity, quote, book)


def test_optional_buffers_never_allow_liquidation_before_stop(workspace, account, opportunity, quote):
    a = unlimited(account)
    a["sizing"][0]["margin_mode"] = "isolated"
    with pytest.raises(ValueError, match="强平"):
        sized(workspace, a, opportunity, quote)


def test_allocation_is_explicit_and_legacy_level_has_no_hidden_discount(
    workspace, account, opportunity, quote
):
    a = unlimited(account)
    a["sizing"][0].update(mode="system", quantity=None)
    amounts = []
    for level in ("conservative", "balanced", "aggressive"):
        a["sizing"][0]["system_level"] = level
        amounts.append(sized(workspace, a, opportunity, quote)["qty"])
    assert amounts[0] == amounts[1] == amounts[2]
    a["sizing"][0]["allocation"]["trade_loss_pct"] = 1
    assert sized(workspace, a, opportunity, quote)["qty"] < amounts[0]
    assert value(a, "trade_loss_pct", a["sizing"][0]) is not None


def test_legacy_migration_preserves_effective_caps_and_is_idempotent(account):
    old = copy.deepcopy(account)
    old.pop("risk_policy")
    old["sizing"][0].update(mode="system", system_level="conservative")
    params = {
        "group_exposure_fraction": 0.5,
        "portfolio_risk_multiple": 1.5,
        "overrides": {"portfolio_risk_multiple": False},
    }
    a = Account.model_validate(migrate(old, "aggressive", params)).model_dump()
    assert a["risk_policy"]["limits"]["portfolio_loss_pct"]["value"] == 3
    assert a["risk_policy"]["limits"]["margin_pct"]["enabled"] is False
    assert a["risk_policy"]["limits"]["mark_liq_pct"]["value"] == 3
    assert a["sizing"][0]["allocation"] == {
        "available_margin_pct": 20,
        "trade_loss_pct": 0.5,
        "exposure_multiple": 0.45,
        "group_multiple": 0.225,
    }
    assert migrate(a, "conservative", {}) == a


def test_workspace_migrates_once_with_backup(workspace, account):
    old = copy.deepcopy(account)
    old.pop("risk_policy")
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_accounts", {old["id"]: old})
    w2 = Workspace(workspace.store, workspace.vault, workspace.settings)
    try:
        backup = w2.store.get("v2_risk_pre_v015")
        assert backup["accounts"][old["id"]] == old
        saved = w2.store.get("v2_accounts")[old["id"]]
        assert saved["risk_policy"]["limits"]["group_multiple"]["value"] == pytest.approx(0.45)
        assert saved["risk_ai"]["enabled"] is False
    finally:
        w2.stop()


def test_strategy_has_no_account_budget_controls():
    assert "portfolio_risk_multiple" not in Parameters.model_json_schema()["properties"]
    p = Parameters.model_validate(
        {
            "portfolio_risk_multiple": 2,
            "group_exposure_fraction": 0.3,
            "overrides": {"group_exposure_fraction": False},
        }
    )
    assert "group_exposure_fraction" not in p.model_dump()


def test_policy_validation_and_ai_cannot_expand_own_authority(account):
    with pytest.raises(ValidationError):
        RiskPolicy(limits={})
    with pytest.raises(ValidationError):
        RiskAI(enabled=True)
    proposed = policy(account)
    proposed["limits"]["trade_loss_pct"]["value"] = 50
    with pytest.raises(ValueError, match="未获授权"):
        bounded_changes(account, proposed)
    a = copy.deepcopy(account)
    a["risk_ai"]["authority"] = {
        "trade_loss_pct": {"allowed": True, "minimum": 0.1, "maximum": 1, "allow_disable": False}
    }
    with pytest.raises(ValueError, match="超出"):
        bounded_changes(a, proposed)
    proposed["limits"]["trade_loss_pct"] = {"enabled": False, "value": 0.5}
    with pytest.raises(ValueError, match="切换"):
        bounded_changes(a, proposed)


def test_manual_review_works_when_automatic_disabled_and_logs_only_changes(
    workspace, account, monkeypatch, logged
):
    a, queue = setup_review(workspace, account, monkeypatch, enabled=False)
    monkeypatch.setattr(
        ai_client,
        "response",
        lambda *args, **kw: response([{"key": "exposure_multiple", "enabled": True, "value": 1.2}]),
    )
    assert logged.post(f"/api/v2/accounts/{a['id']}/risk-review", json={}).status_code == 200
    drain(queue)
    current = workspace.store.get("v2_accounts")[a["id"]]
    assert current["risk_policy"]["limits"]["exposure_multiple"]["value"] == 1.2
    assert not current["risk_ai"]["enabled"]
    records = workspace.risk_reviews.history(a["id"])
    assert len(records) == 1 and records[0]["before_policy"] == policy(a)
    assert records[0]["after_policy"] == policy(current)
    with workspace.store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM notification_events WHERE identity LIKE 'risk-change:%'"
            ).fetchone()[0]
            == 1
        )
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response())
    workspace.risk_reviews.run(current, workspace.store.get("v2_preferences"), time.time())
    assert len(workspace.risk_reviews.history(a["id"])) == 1
    assert workspace.store.get("v2_risk_reviews")[a["id"]]["status"] == "unchanged"


@pytest.mark.parametrize("change", ["configuration", "fill", "expired", "provider"])
def test_ai_discards_stale_results(workspace, account, monkeypatch, change):
    a, queue = setup_review(workspace, account, monkeypatch)

    def respond(*args, **kw):
        with workspace.store.transaction() as db:
            if change == "configuration":
                accounts = workspace.store.read(db, "v2_accounts")
                accounts[a["id"]]["name"] = "Changed"
                workspace.store.write(db, "v2_accounts", accounts)
            elif change == "fill":
                books = workspace.store.read(db, "v2_ledgers")
                books[a["id"]]["cash"] = "9999"
                workspace.store.write(db, "v2_ledgers", books)
            elif change == "expired":
                workspace.stop_event.set()
            else:
                workspace.vault.put("v2:ai:openai", {"apiKey": "changed-test-key"})
        return response([{"key": "exposure_multiple", "enabled": True, "value": 1.5}])

    monkeypatch.setattr(ai_client, "response", respond)
    workspace.risk_reviews.request(a["id"])
    drain(queue)
    assert workspace.risk_reviews.history(a["id"]) == []
    assert workspace.store.get("v2_accounts")[a["id"]]["risk_policy"] == a["risk_policy"]
    assert workspace.store.get("v2_risk_reviews")[a["id"]]["status"] == "failed"
    workspace.stop_event.clear()


def test_confirmed_fill_requires_reassessment_and_coalesces(
    workspace, account, opportunity, quote, monkeypatch
):
    a, queue = setup_review(workspace, account, monkeypatch)
    workspace.quotes = {"btc": quote}
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response())
    assert workspace.risk_reviews.gate(a)
    workspace.risk_reviews.scheduled()
    workspace.risk_reviews.scheduled()
    assert len(queue) == 1
    drain(queue)
    assert workspace.risk_reviews.gate(a) is None
    workspace.paper.enter(
        a, opportunity, {"btc": quote}, {i["id"]: i for i in workspace.store.get("v2_watchlist")}, "balanced"
    )
    assert workspace.risk_reviews.gate(a)
    workspace.risk_reviews.scheduled()
    assert not queue  # Cooldown merges consecutive events.


def test_ai_failure_never_delays_protective_stop(workspace, account, opportunity, quote, monkeypatch):
    workspace.paper.enter(
        account,
        opportunity,
        {"btc": quote},
        {i["id"]: i for i in workspace.store.get("v2_watchlist")},
        "balanced",
    )
    a, queue = setup_review(workspace, account, monkeypatch)
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: {"status": "incomplete"})
    workspace.risk_reviews.request(a["id"])
    drain(queue)
    assert workspace.risk_reviews.gate(a)
    quote.update(mid=95, bid=94.99, ask=95.01, timestamp=time.time())
    workspace.paper.protect(a, {"btc": quote}, {})
    assert not workspace.store.get("v2_ledgers")[a["id"]]["positions"]


def test_manual_policy_save_and_cas_preserve_account_ai(workspace, account):
    original = copy.deepcopy(account)
    changed = copy.deepcopy(account)
    changed["risk_policy"]["limits"]["trade_loss_pct"]["value"] = 0.8
    saved = workspace.save_account(changed, account_revision(original))
    assert workspace.risk_reviews.history(saved["id"])[0]["source"] == "manual"
    with pytest.raises(ValueError, match="已被其他操作修改"):
        workspace.save_account(original, account_revision(original))
    assert workspace.state()["accounts"][0]["risk_budget"]["currency"] == "USDT"


def test_model_change_invalidates_cached_risk_review(workspace, account, monkeypatch):
    a, queue = setup_review(workspace, account, monkeypatch)
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response())
    workspace.risk_reviews.request(a["id"])
    drain(queue)
    assert workspace.risk_reviews.gate(a) is None
    workspace.preferences({**workspace.store.get("v2_preferences"), "ai_model": "other-model"})
    assert workspace.risk_reviews.gate(a)


def test_equity_tick_token_ignores_nonstructural_stream_fields(account):
    raw = {
        "wallet": "100",
        "positions": [{"symbol": "BTC/USDT:USDT", "contracts": 1, "entryPrice": 100, "markPrice": 101}],
    }
    a = {**account, "kind": "live"}
    token = funds_token(a, raw)
    raw["positions"][0]["markPrice"] = 102
    raw["checked_at"] = time.time()
    assert funds_token(a, raw) == token
    raw["positions"][0]["contracts"] = 2
    assert funds_token(a, raw) != token


def test_risk_api_auth_and_asset(client, logged):
    assert logged.get("/static/risk-settings.js").status_code == 200
    logged.cookies.clear()
    assert client.post("/api/v2/accounts/paper-default/risk-review", json={}).status_code in (401, 403)


def test_old_browser_cannot_reset_new_capital_settings(logged, account):
    body = {**account, "expected_revision": account_revision(account)}
    body.pop("risk_policy")
    assert logged.post("/api/v2/accounts", json=body).status_code == 409


def test_limit_change_does_not_force_existing_liquidation_exit(workspace, account, opportunity, quote):
    items = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, opportunity, {"btc": quote}, items, "balanced")
    before = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    changed = copy.deepcopy(account)
    changed["risk_policy"]["limits"]["mark_liq_pct"]["value"] = 100
    changed = workspace.save_account(changed)
    workspace.paper.protect(changed, {"btc": quote}, {})
    after = workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]
    assert before["qty"] == after["qty"]
    assert before["stop"] == after["stop"]
    assert after["liquidation_buffer_pct"] == 5


def test_daily_equity_baseline_is_captured_without_opening(workspace, account):
    from quantpilot.v2 import journal

    journal.sample(workspace)
    book = workspace.store.get("v2_ledgers")[account["id"]]
    assert book["day"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert float(book["day_equity"]) == 10000


def test_exchange_unknown_pending_orders_block_more_risk(workspace, account):
    external = {
        "equity": "100",
        "available": "90",
        "positions": [],
        "open_orders": [{"id": "pending", "reduceOnly": False}],
    }
    a = {**account, "kind": "live"}
    with pytest.raises(ValueError, match="挂单"):
        workspace.exchange.risk_book(a, external, {})


def test_ai_malformed_and_duplicate_changes_leave_policy_untouched(workspace, account, monkeypatch):
    a, queue = setup_review(workspace, account, monkeypatch)
    mutation = {"key": "exposure_multiple", "enabled": True, "value": 1}
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response([mutation, mutation]))
    workspace.risk_reviews.request(a["id"])
    drain(queue)
    assert workspace.risk_reviews.history(a["id"]) == []
    assert "重复" in workspace.store.get("v2_risk_reviews")[a["id"]]["message"]
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response([{**mutation, "key": "unknown"}]))
    workspace.risk_reviews.run(a, workspace.store.get("v2_preferences"), time.time())
    assert workspace.risk_reviews.history(a["id"]) == []
    assert workspace.store.get("v2_risk_reviews")[a["id"]]["message"] == "AI 额度方案格式无效，未应用"


def test_exchange_review_drift_uses_reported_not_conservative_equity(workspace, monkeypatch):
    a, queue = setup_review(workspace, Account(name="read-only", kind="live").model_dump(), monkeypatch)
    raw = {"wallet": "10000", "equity": "10000", "available": "9000", "positions": [], "open_orders": [], "native_equity": True}
    monkeypatch.setattr(workspace.exchange, "read", lambda *args, **kw: {**raw, "checked_at": time.time(), "orders_checked_at": time.time()})
    monkeypatch.setattr(ai_client, "response", lambda *args, **kw: response())
    workspace.risk_reviews.request(a["id"])
    drain(queue)
    result = workspace.store.get("v2_risk_reviews")[a["id"]]
    assert result["status"] == "unchanged"
    assert result["equity"] == 10000
    assert workspace.risk_reviews.capture(a)["budget"]["equity"] == "9000"
    assert workspace.risk_reviews.gate(a) is None


def test_scheduler_prioritizes_least_recently_attempted_account(workspace, monkeypatch):
    a, _ = setup_review(workspace, Account(name="old", id="a").model_dump(), monkeypatch)
    b, _ = setup_review(workspace, Account(name="new", id="b").model_dump(), monkeypatch)
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_risk_reviews", {a["id"]: {"attempted_at": time.time()}})
    order = []
    monkeypatch.setattr(workspace.risk_reviews, "gate", lambda a: "required")
    monkeypatch.setattr(workspace.risk_reviews, "request", lambda aid: order.append(aid))
    workspace.risk_reviews.scheduled()
    assert order == [b["id"], a["id"]]
