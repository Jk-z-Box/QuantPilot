import copy
import json

import pytest
from pydantic import ValidationError
from test_logic import add_history, mock_review
from test_v080 import enter, prepare_position, review_now
from test_v2 import demo_account, universe

from quantpilot.v2.models import Account, HistoryReset
from quantpilot.v2.position_review import PositionPlan, PositionReviews, profit_context


def changes(w):
    with w.store.connect() as db:
        return [
            {**dict(r), "changes": json.loads(r["changes"])}
            for r in db.execute("SELECT * FROM v2_position_changes ORDER BY id")
        ]


def reset(w):
    return w.logic.reset_history(
        HistoryReset(confirmation="RESET HISTORY", expected_generation=w.logic.history_scope()["generation"])
    )


def apply_review(w, account):
    r = review_now(w, account)
    w.paper.protect(account, w.quotes, r.opportunities(account, w.opportunities, w.quotes))
    return r


def test_features_require_account_position_review():
    a = Account(name="test", profit_trailing_enabled=True, ai_take_profit_enabled=True)
    assert not a.profit_trailing_enabled and not a.ai_take_profit_enabled
    a = Account(
        name="test", position_review_enabled=True, profit_trailing_enabled=True, ai_take_profit_enabled=True
    )
    assert a.profit_trailing_enabled and a.ai_take_profit_enabled


@pytest.mark.parametrize("ratio", [0, 0.01, 1, float("nan")])
def test_invalid_profit_allocation_rejected(ratio):
    with pytest.raises(ValidationError):
        PositionPlan(
            instrument_id="btc",
            stop=None,
            target_1=None,
            target_2=None,
            add_at=None,
            reduce_at=None,
            reduce_ratio=0.5,
            reason="test",
            take_profit_mode="split",
            first_target_ratio=ratio,
        )


@pytest.mark.parametrize("short", [False, True])
def test_profitable_stop_must_lock_net_profit(quote, short):
    p = {
        "side": "short" if short else "long",
        "entry": 100,
        "stop": 104 if short else 96,
        "qty": 1,
        "original_qty": 1,
        "targets": [
            {"price": 94 if short else 106, "ratio": 0.5},
            {"price": 88 if short else 112, "ratio": 0.5},
        ],
        "tp_done": [],
    }
    q = {
        **quote,
        "mid": 97 if short else 103,
        "bid": 96.99 if short else 102.99,
        "ask": 97.01 if short else 103.01,
    }
    a = Account(name="test", position_review_enabled=True, profit_trailing_enabled=True).model_dump()
    plan = {
        "stop": 98 if short else 102,
        "target_1": None,
        "target_2": None,
        "add_at": None,
        "reduce_at": None,
        "reduce_ratio": 0.5,
        "take_profit_mode": "keep",
    }
    assert profit_context(p, q)["net_profitable"]
    assert PositionReviews.validate_plan(plan, p, q, a, "btc")["stop"] == plan["stop"]
    with pytest.raises(ValueError, match="覆盖估算费用"):
        PositionReviews.validate_plan({**plan, "stop": 100}, p, q, a, "btc")
    with pytest.raises(ValueError, match="不得放宽"):
        PositionReviews.validate_plan({**plan, "stop": 105 if short else 95}, p, q, a, "btc")


def test_profit_context_fee_slippage_buffer(quote):
    p = {"side": "long", "entry": 100}
    c = profit_context(p, {**quote, "bid": 100.05})
    assert not c["net_profitable"] and c["breakeven"] > 100.1


def test_trailing_stop_applies_and_no_change_does_not_notify(
    workspace, account, opportunity, quote, monkeypatch
):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    account = workspace.save_account({**account, "profit_trailing_enabled": True})
    workspace.quotes = {"btc": {**quote, "mid": 103, "bid": 102.99, "ask": 103.01}}
    proposal.update(stop=101.5, target_1=None, target_2=None, add_at=None, reduce_at=None)
    r = apply_review(workspace, account)
    assert float(r.held(account)["btc"]["stop"]) == 101.5
    rows = changes(workspace)
    assert len(rows) == 1 and rows[0]["changes"]["stop"] == {"before": 96, "after": 101.5}
    assert rows[0]["reason"] == proposal["reason"]
    apply_review(workspace, account)
    assert len(changes(workspace)) == 1
    proposal["stop"] = 102
    apply_review(workspace, account)
    assert changes(workspace)[-1]["changes"]["stop"] == {"before": 101.5, "after": 102}


def test_unchanged_assessment_emits_no_change(workspace, account, opportunity, quote, monkeypatch):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    proposal.update(stop=None, target_1=None, target_2=None, add_at=None, reduce_at=None)
    r = apply_review(workspace, account)
    assert r.state(account)["status"] == "ready" and not changes(workspace)
    with workspace.store.connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='position_plan_changed'").fetchone()[0] == 0


@pytest.mark.parametrize("mode,ratio", [("single", None), ("split", 0.3)])
def test_ai_can_choose_single_or_split(workspace, account, opportunity, quote, monkeypatch, mode, ratio):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    account = workspace.save_account({**account, "ai_take_profit_enabled": True})
    proposal.update(
        stop=None,
        target_1=None,
        target_2=None,
        add_at=None,
        reduce_at=None,
        take_profit_mode=mode,
        first_target_ratio=ratio,
    )
    r = apply_review(workspace, account)
    p = r.held(account)["btc"]
    assert p["targets"][0]["ratio"] == (1 if mode == "single" else ratio)
    assert p["targets"][1]["enabled"] == (mode == "split")
    qty = float(p["qty"])
    q = {**quote, "mid": 107, "bid": 106.99, "ask": 107.01}
    workspace.paper.protect(account, {"btc": q}, {})
    held = r.held(account)
    if mode == "single":
        assert not held
    else:
        assert float(held["btc"]["qty"]) == pytest.approx(qty * 0.7)


def test_split_after_risk_reduce_uses_remaining_quantity(workspace, account, opportunity, quote, monkeypatch):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    workspace.paper.close(account["id"], "btc", quote, 0.5, "risk_reduce")
    account = workspace.save_account({**account, "ai_take_profit_enabled": True})
    proposal.update(
        stop=None,
        target_1=None,
        target_2=None,
        add_at=None,
        reduce_at=None,
        take_profit_mode="split",
        first_target_ratio=0.5,
    )
    r = apply_review(workspace, account)
    qty = float(r.held(account)["btc"]["qty"])
    assert float(r.held(account)["btc"]["target_basis_qty"]) == qty
    workspace.paper.protect(account, {"btc": {**quote, "mid": 107, "bid": 106.99, "ask": 107.01}}, {})
    assert float(r.held(account)["btc"]["qty"]) == pytest.approx(qty * 0.5)
    assert any("target_basis_qty" in row["changes"] for row in changes(workspace))


@pytest.mark.parametrize("block", ["disabled", "second_off", "tp_done", "external_partial"])
def test_batch_changes_cannot_override_safety(workspace, account, opportunity, quote, monkeypatch, block):
    account, proposal = prepare_position(workspace, account, opportunity, quote, monkeypatch)
    account = {**account, "ai_take_profit_enabled": block != "disabled"}
    p = workspace.position_reviews.held(account)["btc"]
    if block == "second_off":
        p["parameters"] = {"target_2_enabled": False}
    if block == "tp_done":
        p["tp_done"] = [0]
    if block == "external_partial":
        p["external_qty"] = float(p["original_qty"]) / 2
    proposal.update(take_profit_mode="split", first_target_ratio=0.3)
    with pytest.raises(ValueError, match="重新分配"):
        PositionReviews.validate_plan(proposal, p, quote, account, "btc")


def test_native_single_target_does_not_submit_disabled_leg(workspace, monkeypatch, opportunity, quote):
    a, b = demo_account(workspace, monkeypatch)
    c = b.client(a)
    b.enter(a, opportunity, {"btc": quote}, universe(workspace), "balanced")
    ai = {
        "intervention_enabled": True,
        "action": "tighten",
        "stop": None,
        "targets": [
            {"price": 105, "ratio": 1, "enabled": True},
            {"price": 112, "ratio": 0, "enabled": False},
        ],
    }
    before = len(c.calls)
    b.manage(a, {"btc": quote}, {"btc": {**opportunity, "ai": ai}}, universe(workspace))
    new = c.calls[before:]
    assert len(new) == 1 and new[0][1] == "limit" and new[0][5]["reduceOnly"]
    assert len(workspace.store.get("v2_managed")[a["id"]]["btc"]["legs"][0]["target_ids"]) == 1


def test_history_reset_hides_orders_preserves_positions_and_dedup(workspace, account, opportunity, quote):
    enter(workspace, account, opportunity, quote)
    before = copy.deepcopy(workspace.store.get("v2_ledgers"))
    scope = reset(workspace)
    assert scope["generation"] == 1 and workspace.state()["orders"] == []
    assert workspace.store.get("v2_ledgers") == before
    assert enter(workspace, account, opportunity, quote)["status"] == "duplicate"
    with workspace.store.connect() as db:
        assert db.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 1


def test_reset_excludes_pre_reset_position_after_later_close(workspace, account, opportunity, quote):
    current = workspace.logic.current()
    op = {**opportunity, "logic_version": current["id"], "demo": False}
    enter(workspace, account, op, quote)
    reset(workspace)
    workspace.paper.close(account["id"], "btc", quote)
    assert workspace.logic.state()["metrics"]["trades"] == 0
    enter(workspace, account, {**op, "id": "new-period-signal"}, quote)
    workspace.paper.close(account["id"], "btc", quote)
    assert workspace.logic.state()["metrics"]["trades"] == 1


def test_reset_old_score_and_stats_do_not_enter_new_review(workspace, monkeypatch):
    add_history(workspace)
    calls = mock_review(monkeypatch, workspace, score=90)
    workspace.logic.review()
    assert workspace.logic.state()["reviews"][-1]["score"] == 90
    current = workspace.logic.current()
    reset(workspace)
    state = workspace.logic.state()
    assert state["metrics"]["trades"] == 0 and state["reviews"] == [] and state["current"] == current
    workspace.logic.review()
    assert len(calls) == 1 and workspace.logic.state()["reviews"][-1]["score"] is None


def test_reset_invalidates_inflight_optimizer(workspace, monkeypatch):
    add_history(workspace)
    workspace.logic.settings({**workspace.logic.state()["settings"], "auto_optimize": True})
    current = workspace.logic.current()
    mock_review(
        monkeypatch,
        workspace,
        score=40,
        proposal={**current["parameters"], "ema_fast": 25},
        mutate=lambda: reset(workspace),
    )
    with pytest.raises(ValueError, match="配置已变更"):
        workspace.logic.review()
    assert workspace.logic.current() == current and workspace.logic.state()["reviews"] == []


def test_history_reset_confirmation_and_epoch_api(logged, workspace):
    assert (
        logged.post(
            "/api/v2/history/reset", json={"confirmation": "wrong", "expected_generation": 0}
        ).status_code
        == 422
    )
    r = logged.post("/api/v2/history/reset", json={"confirmation": "RESET HISTORY", "expected_generation": 0})
    assert r.status_code == 200 and r.json()["generation"] == 1
    assert (
        logged.post(
            "/api/v2/history/reset", json={"confirmation": "RESET HISTORY", "expected_generation": 0}
        ).status_code
        == 409
    )
    logged.headers.pop("x-csrf-token")
    assert (
        logged.post(
            "/api/v2/history/reset", json={"confirmation": "RESET HISTORY", "expected_generation": 1}
        ).status_code
        == 403
    )


def test_account_feature_controls_roundtrip(logged, account):
    body = {
        **account,
        "position_review_enabled": True,
        "profit_trailing_enabled": True,
        "ai_take_profit_enabled": True,
    }
    assert logged.post("/api/v2/accounts", json=body).status_code == 200
    a = next(a for a in logged.get("/api/v2/state").json()["accounts"] if a["id"] == account["id"])
    assert a["profit_trailing_enabled"] and a["ai_take_profit_enabled"]
    assert logged.post("/api/v2/accounts", json={**body, "position_review_enabled": False}).status_code == 200
    a = next(a for a in logged.get("/api/v2/state").json()["accounts"] if a["id"] == account["id"])
    assert not a["profit_trailing_enabled"] and not a["ai_take_profit_enabled"]
