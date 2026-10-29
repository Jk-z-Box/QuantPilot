import importlib.util
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from quantpilot import maintenance
from quantpilot.config import load_settings
from quantpilot.v2 import ai_client
from quantpilot.v2.logic import (
    Bundle,
    Edit,
    Import,
    Parameters,
    Restore,
    ReviewSettings,
    assessment_schema,
    calculate_metrics,
)
from quantpilot.v2.models import Instrument
from quantpilot.v2.signals import evaluate, indicators


def edit(engine, **params):
    current = engine.current()
    return engine.edit(Edit(expected_version=current["id"], parameters={**current["parameters"], **params}))


def add_history(workspace, count=20, demo=False, version_id=None):
    current = workspace.logic.current()
    prefs = workspace.store.get("v2_preferences")
    policy = json.dumps({"profile": prefs["profile"], "min_score": prefs["min_score"]}, sort_keys=True)
    with workspace.store.transaction() as db:
        for n in range(count):
            db.execute(
                "INSERT INTO logic_trades VALUES (?,?,?,?,?,?)",
                (str(n), version_id or current["id"], time.time(), 10 if n % 2 else -5, demo, policy),
            )


def mock_review(monkeypatch, workspace, score=80, proposal=None, mutate=None):
    workspace.vault.put("v2:ai:openai", {"apiKey": "test-key"})
    with workspace.store.transaction() as db:
        prefs = workspace.store.read(db, "v2_preferences")
        prefs["ai_model"] = "test-model"
        workspace.store.write(db, "v2_preferences", prefs)
    calls = []

    def answer(prefs, vault, payload):
        calls.append(payload)
        if mutate:
            mutate()
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "score": score,
                                    "summary": "基于已实现净收益评估",
                                    "weaknesses": ["样本仍有限"],
                                    "proposal": proposal,
                                }
                            ),
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(ai_client, "response", answer)
    return calls


@pytest.mark.parametrize("path", ["logic", "logic/export", "admin"])
def test_management_auth(client, path):
    assert client.get("/api/v2/" + path).status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"ema_fast": 9},
        {"ema_slow": 61},
        {"volume_multiplier": 0.8},
        {"atr_stop": 4},
        {"target_1_ratio": float("nan")},
        {"target_1": 2.5, "target_2": 2.5},
        {"shell": "rm"},
        {"ema_fast": True},
    ],
)
def test_parameters_strict_bounds(params):
    with pytest.raises(ValidationError):
        Parameters(**params)


def test_real_indicators_and_plan_follow_parameters(quote):
    rows = [[n * 3600000, 100 + n, 103 + n, 99 + n, 101 + n, 100] for n in range(100)]
    a, b = indicators(rows), indicators(rows, Parameters(ema_fast=10, rsi_period=7).model_dump())
    assert a["ema20"] != b["ema20"]
    item = Instrument(id="btc", label="BTC", symbol="BTC/USDT:USDT").model_dump()
    args = (item, {f: rows for f in ("1h", "4h", "1d", "1w")}, quote, [], "balanced", time.time())
    first = evaluate(*args)
    second = evaluate(*args, parameters=Parameters(atr_stop=3, target_1_ratio=0.75).model_dump())
    assert first["plan"]["stop"] != second["plan"]["stop"]
    assert second["plan"]["targets"][0]["ratio"] == 0.75
    assert "成交量不支持当前方向" in evaluate(*args, parameters={"volume_multiplier": 1.5})["blockers"]


def test_edit_backup_cas_and_pruning(workspace):
    engine = workspace.logic
    original = engine.current()
    engine.settings({**ReviewSettings().model_dump(), "max_backups": 2})
    for value in (21, 22, 23):
        edit(engine, ema_fast=value)
    state = engine.state()
    assert len(state["archives"]) == 2
    assert [a["version"]["parameters"]["ema_fast"] for a in state["archives"]] == [21, 22]
    assert not workspace.opportunities
    with pytest.raises(ValueError, match="已被更新"):
        engine.edit(Edit(expected_version=original["id"], parameters={"ema_fast": 24}))
    assert engine.current() == state["current"]


def test_edit_rollback_keeps_current_and_archive(workspace, monkeypatch):
    engine = workspace.logic
    before = workspace.store.get("v2_logic")
    original = workspace.store.write

    def fail(db, key, value):
        if key == "v2_logic":
            raise RuntimeError("disk")
        return original(db, key, value)

    monkeypatch.setattr(workspace.store, "write", fail)
    with pytest.raises(RuntimeError):
        edit(engine, ema_fast=22)
    assert workspace.store.get("v2_logic") == before


def test_import_export_restore_does_not_import_evidence_or_secrets(workspace):
    engine = workspace.logic
    edit(engine, ema_fast=22)
    bundle = engine.export()
    assert set(bundle) == {"format", "current", "archives"}
    assert "credentials" not in json.dumps(bundle)
    old = engine.current()
    engine.import_bundle(Import(expected_version=old["id"], bundle=bundle))
    assert engine.current()["id"] != old["id"]
    archives = engine.state()["archives"]
    assert archives[-1]["version"] == old
    engine.restore(Restore(expected_version=engine.current()["id"], archive_id=archives[0]["version"]["id"]))
    assert engine.current()["source"] == "restore"
    assert engine.state()["metrics"]["trades"] == 0
    with pytest.raises(ValidationError):
        Bundle.model_validate({**bundle, "credentials": {"secret": "test"}})


def test_paper_roundtrip_includes_fees_partial_and_funding(workspace, account, opportunity, quote):
    op = {**opportunity, "logic_version": workspace.logic.current()["id"], "demo": False}
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    initial = float(workspace.store.get("v2_ledgers")[account["id"]]["cash"])
    workspace.paper.enter(account, op, {"btc": quote}, instruments, "balanced")
    # Force one funding boundary while preserving the signal, then partial close.
    with workspace.store.transaction() as db:
        books = workspace.store.read(db, "v2_ledgers")
        books[account["id"]]["positions"]["btc"]["funding_at"] = 0
        workspace.store.write(db, "v2_ledgers", books)
    workspace.paper.protect(account, {"btc": quote}, {"btc": op})
    q = {**quote, "bid": 104, "ask": 104.01, "mid": 104}
    workspace.paper.close(account["id"], "btc", q, 0.5)
    assert workspace.logic.state()["metrics"]["trades"] == 0
    workspace.paper.close(account["id"], "btc", q)
    metrics = workspace.logic.state()["metrics"]
    assert metrics["trades"] == 1
    assert metrics["net_pnl"] == pytest.approx(
        float(workspace.store.get("v2_ledgers")[account["id"]]["cash"]) - initial
    )
    assert metrics["profit_factor"] is None and metrics["payoff_ratio"] is None


def test_new_logic_rejects_old_signal_and_additions(workspace, account, opportunity, quote):
    old = workspace.logic.current()["id"]
    op = {**opportunity, "logic_version": old, "demo": False}
    instruments = {i["id"]: i for i in workspace.store.get("v2_watchlist")}
    workspace.paper.enter(account, op, {"btc": quote}, instruments, "balanced")
    edit(workspace.logic, ema_fast=22)
    with pytest.raises(ValueError, match="量化逻辑已更新"):
        workspace.paper.enter(account, op, {"btc": quote}, instruments, "balanced", True)
    with pytest.raises(ValueError, match="旧版本持仓"):
        workspace.paper.enter(
            account,
            {**op, "logic_version": workspace.logic.current()["id"]},
            {"btc": quote},
            instruments,
            "balanced",
            True,
        )
    workspace.paper.close(account["id"], "btc", quote)
    assert workspace.logic.state()["metrics"]["trades"] == 0
    with workspace.store.connect() as db:
        assert db.execute("SELECT version FROM logic_trades").fetchone()[0] == old


def test_metrics_definitions():
    m = calculate_metrics([{"net_pnl": p} for p in (20, -10, 30, -5, 0)], 0, 100)
    assert m["profit_factor"] == pytest.approx(50 / 15)
    assert m["payoff_ratio"] == pytest.approx(25 / 7.5)
    assert m["win_rate"] == 0.4 and m["max_drawdown"] == 10


@pytest.mark.parametrize("reason", ["no_trades", "demo", "different_version", "different_profile"])
def test_review_insufficient_never_calls_ai(monkeypatch, workspace, reason):
    calls = mock_review(monkeypatch, workspace)
    if reason != "no_trades":
        add_history(
            workspace, demo=reason == "demo", version_id="old" if reason == "different_version" else None
        )
    if reason == "different_profile":
        workspace.preferences({**workspace.store.get("v2_preferences"), "profile": "aggressive"})
    result = workspace.logic.review()
    assert result["score"] is None and result["action"] == "insufficient"
    assert not calls


@pytest.mark.parametrize("score,expected", [(75, "passing"), (74, "optimized")])
def test_threshold_pass_keeps_fail_optimizes(monkeypatch, workspace, score, expected):
    add_history(workspace)
    old = workspace.logic.current()
    workspace.logic.settings({**ReviewSettings().model_dump(), "enabled": True, "auto_optimize": True})
    calls = mock_review(monkeypatch, workspace, score, {**old["parameters"], "ema_fast": 22})
    result = workspace.logic.review()
    assert result["action"] == expected and len(calls) == 1
    assert "test-key" not in json.dumps(calls)
    if expected == "passing":
        assert workspace.logic.current() == old
    else:
        state = workspace.logic.state()
        assert state["current"]["parameters"]["ema_fast"] == 22
        assert state["archives"][-1]["score"] == 74
        assert state["archives"][-1]["metrics"]["profit_factor"] == 2
        assert state["archives"][-1]["version"] == old


def test_passing_does_not_disable_future_reviews(monkeypatch, workspace):
    add_history(workspace)
    config = {**ReviewSettings().model_dump(), "enabled": True, "auto_optimize": True}
    workspace.logic.settings(config)
    current = workspace.logic.current()
    mock_review(monkeypatch, workspace, 90)
    assert workspace.logic.review()["action"] == "passing"
    assert workspace.logic.state()["settings"]["enabled"]
    mock_review(monkeypatch, workspace, 50, {**current["parameters"], "ema_fast": 22})
    assert workspace.logic.review()["action"] == "optimized"


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("too_large", "no_safe_proposal"),
        ("none", "no_safe_proposal"),
        ("cooldown", "cooldown"),
        ("external", "external_active"),
        ("off", "below"),
    ],
)
def test_optimization_guards(monkeypatch, workspace, scenario, expected):
    add_history(workspace)
    workspace.logic.settings({**ReviewSettings().model_dump(), "auto_optimize": scenario != "off"})
    old = workspace.logic.current()
    proposal = {**old["parameters"], "ema_fast": 30 if scenario == "too_large" else 22}
    mock_review(monkeypatch, workspace, 50, None if scenario == "none" else proposal)
    with workspace.store.transaction() as db:
        if scenario == "cooldown":
            data = workspace.store.read(db, "v2_logic")
            data["last_optimized"] = time.time()
            workspace.store.write(db, "v2_logic", data)
        if scenario == "external":
            workspace.store.write(db, "v2_managed", {"a": {"btc": {"qty": 1}}})
    assert workspace.logic.review()["action"] == expected
    assert workspace.logic.current() == old


def test_ai_stale_result_does_not_overwrite_edit(monkeypatch, workspace):
    add_history(workspace)
    old = workspace.logic.current()
    mock_review(
        monkeypatch,
        workspace,
        10,
        {**old["parameters"], "ema_fast": 22},
        lambda: edit(workspace.logic, ema_fast=24),
    )
    with pytest.raises(ValueError, match="配置已变更"):
        workspace.logic.review()
    assert workspace.logic.current()["parameters"]["ema_fast"] == 24


def test_logic_csrf_and_invalid_import(logged, workspace):
    before = workspace.logic.current()
    assert (
        logged.post(
            "/api/v2/logic/import", json={"expected_version": before["id"], "bundle": {"format": "bad"}}
        ).status_code
        == 422
    )
    logged.headers.pop("x-csrf-token")
    assert (
        logged.put(
            "/api/v2/logic",
            json=Edit(expected_version=before["id"], parameters={"ema_fast": 22}).model_dump(),
        ).status_code
        == 403
    )
    assert workspace.logic.current() == before


def test_json_schema_all_fields_required():
    schema = assessment_schema()
    for node in [schema, *schema["$defs"].values()]:
        assert set(node["required"]) == set(node["properties"])
        assert node["additionalProperties"] is False


def test_admin_identity_reauth_and_session_revocation(logged, workspace):
    payload = {"username": "new-admin", "current_password": "wrong", "new_password": "new-test-password"}
    assert logged.put("/api/v2/admin/identity", json=payload).status_code == 403
    payload["current_password"] = "test-only-password"
    result = logged.put("/api/v2/admin/identity", json=payload)
    assert result.status_code == 200 and "password" not in result.text
    assert logged.get("/api/v2/state").status_code == 401
    assert load_settings(workspace.store.directory / "config.json").username == "new-admin"
    assert (
        logged.post("/api/login", json={"username": "new-admin", "password": "new-test-password"}).status_code
        == 200
    )
    assert "new-test-password" not in json.dumps(workspace.store.recent("audit"))


def test_admin_wrong_password_rate_limit(logged):
    for _ in range(5):
        assert (
            logged.put(
                "/api/v2/admin/identity", json={"username": "changed", "current_password": "wrong"}
            ).status_code
            == 403
        )
    assert (
        logged.put(
            "/api/v2/admin/identity", json={"username": "changed", "current_password": "test-only-password"}
        ).status_code
        == 429
    )


def test_admin_weak_password_and_display(logged):
    assert (
        logged.put(
            "/api/v2/admin/identity",
            json={"username": "new-admin", "current_password": "test-only-password", "new_password": "short"},
        ).status_code
        == 422
    )
    assert logged.put("/api/v2/admin/display", json={"refresh_seconds": 1}).status_code == 422
    assert logged.put("/api/v2/admin/display", json={"refresh_seconds": 10}).status_code == 200
    assert logged.get("/api/v2/admin").json()["display"]["refresh_seconds"] == 10


def test_update_source_validation():
    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "a" * 40})
        assert str(request.url).startswith(
            "https://raw.githubusercontent.com/Jk-z-Box/QuantPilot/" + "a" * 40
        )
        return httpx.Response(200, text='[project]\nversion="0.7.0"\n')

    result = maintenance.latest(httpx.MockTransport(handler))
    assert result["newer"] and result["commit"] == "a" * 40
    with pytest.raises(ValueError):
        maintenance.latest(
            httpx.MockTransport(lambda _: httpx.Response(302, headers={"Location": "http://bad"}))
        )


def test_update_queue_requires_idle_and_current_commit(monkeypatch, workspace, account):
    monkeypatch.setattr(maintenance, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(maintenance, "latest", lambda: {"newer": True, "commit": "a" * 40})
    with pytest.raises(ValueError, match="自动交易"):
        maintenance.queue_update(workspace.store, "a" * 40)
    account["auto"] = False
    workspace.save_account(account)
    with pytest.raises(ValueError, match="版本已变化"):
        maintenance.queue_update(workspace.store, "b" * 40)
    assert maintenance.queue_update(workspace.store, "a" * 40)["state"] == "queued"
    assert (workspace.store.directory / "update-request.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="已有更新"):
        maintenance.queue_update(workspace.store, "a" * 40)


def test_update_worker_rejects_symlinks_bad_owner_stale(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "worker", Path(__file__).parents[1] / "scripts/web-update.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    path = tmp_path / "request.json"
    request = {"id": "a" * 24, "commit": "a" * 40, "created_at": time.time()}
    path.write_text(json.dumps(request))
    assert worker.read_request(path, os.getuid()) == request
    with pytest.raises(ValueError):
        worker.read_request(path, os.getuid() + 1)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        worker.read_request(alias, os.getuid())
    path.write_text(json.dumps({**request, "created_at": 0}))
    with pytest.raises(ValueError):
        worker.read_request(path, os.getuid())


def test_review_job_is_async_and_debounced(monkeypatch, workspace):
    import threading

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(ai_client, "ready", lambda *_: True)
    with workspace.store.transaction() as db:
        prefs = workspace.store.read(db, "v2_preferences")
        prefs["ai_model"] = "test"
        workspace.store.write(db, "v2_preferences", prefs)

    def job():
        entered.set()
        release.wait(3)
        workspace.logic.running = False

    monkeypatch.setattr(workspace.logic, "run", job)
    try:
        assert workspace.logic.request()["started"]
        assert entered.wait(1)
        with pytest.raises(ValueError, match="正在进行"):
            workspace.logic.request()
    finally:
        release.set()
        workspace.logic.thread.join(2)
    with pytest.raises(ValueError, match="60 秒"):
        workspace.logic.request()


def test_failed_ai_never_changes_version(monkeypatch, workspace):
    from quantpilot.v2.ai_errors import AIError

    add_history(workspace)
    mock_review(monkeypatch, workspace)
    old = workspace.logic.current()

    def failure(*_):
        raise AIError("quota")

    monkeypatch.setattr(ai_client, "response", failure)
    workspace.logic.run()
    assert workspace.logic.current() == old
    assert not workspace.logic.state()["archives"]
    assert not workspace.logic.running


def test_scheduler_keeps_enabled_and_reports_missing_connection(monkeypatch, workspace):
    workspace.logic.settings({**ReviewSettings().model_dump(), "enabled": True})
    monkeypatch.setattr(ai_client, "ready", lambda *_: False)
    workspace.logic.scheduled()
    state = workspace.logic.state()
    assert state["settings"]["enabled"]
    assert state["last_attempt"] > 0
    assert "等待可用" in state["status"]


def test_account_identity_change_disables_strategy_automation(workspace):
    workspace.logic.settings({**ReviewSettings().model_dump(), "enabled": True, "auto_optimize": True})
    workspace.auth_changed()
    assert not workspace.logic.state()["settings"]["enabled"]
    assert not workspace.logic.state()["settings"]["auto_optimize"]


def test_review_api_does_not_wait_for_ai(monkeypatch, logged, workspace):
    monkeypatch.setattr(workspace.logic, "request", lambda: {"started": True})
    assert logged.post("/api/v2/logic/review", json={}).json() == {"started": True}


def test_transaction_record_is_anonymous_in_ai_payload(monkeypatch, workspace):
    add_history(workspace)
    with workspace.store.transaction() as db:
        db.execute(
            "INSERT INTO logic_trade_details VALUES (?,?)",
            ("0", json.dumps({"symbol": "BTC/USDT:USDT", "net_pnl": -5, "exit_reason": "stop_loss"})),
        )
    calls = mock_review(monkeypatch, workspace)
    workspace.logic.review()
    data = json.loads(calls[0]["input"])
    assert data["recent_closed_trades"][0]["exit_reason"] == "stop_loss"
    assert "paper-default" not in calls[0]["input"]


def test_update_job_success_and_failure(monkeypatch, workspace, tmp_path):
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "worker", Path(__file__).parents[1] / "scripts/web-update.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    directory = tmp_path / "root-worker"
    monkeypatch.setattr(worker, "ROOT", directory)
    monkeypatch.setattr(worker, "DATA", workspace.store.directory)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker.os, "seteuid", lambda _: None)
    monkeypatch.setattr(worker.os, "setegid", lambda _: None)
    monkeypatch.setattr(
        worker.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    )
    monkeypatch.setattr(worker, "latest", lambda: {"newer": True, "commit": "a" * 40})
    original_client = httpx.Client

    def fake_client(**kwargs):
        return original_client(
            **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, text="#!/bin/bash\nexit 0"))
        )

    monkeypatch.setattr(worker.httpx, "Client", fake_client)
    executed = []

    def run(command, **kwargs):
        executed.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    request_path = workspace.store.directory / "update-request.json"
    request_path.write_text(json.dumps({"id": "b" * 24, "commit": "a" * 40, "created_at": time.time()}))
    worker.main()
    assert json.loads((directory / "status.json").read_text())["state"] == "completed"
    assert workspace.store.get("v2_preferences")["halted"]
    assert workspace.store.get("kill")
    assert executed[0][0][-3:] == ["--ref", "a" * 40, "--upgrade"]
    assert executed[0][1]["stdin"] == worker.subprocess.DEVNULL
    assert (directory / "install.log").stat().st_mode & 0o777 == 0o600
    assert not request_path.exists()
    monkeypatch.setattr(worker.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=1))
    request_path.write_text(json.dumps({"id": "b" * 24, "commit": "a" * 40, "created_at": time.time()}))
    worker.main()
    assert json.loads((directory / "status.json").read_text())["state"] == "failed"
    assert workspace.store.get("kill")
