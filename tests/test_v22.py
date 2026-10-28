import copy
import datetime as dt
import json
import time

import httpx
import pytest
from pydantic import ValidationError
from test_v2 import demo_account, universe

from quantpilot.v2 import research as module
from quantpilot.v2.ai import apply_revision
from quantpilot.v2.models import Preferences, signal_policy
from quantpilot.v2.research import ResearchEngine, allowed_url, fetch_source, research, verify_observation
from quantpilot.v2.service import Workspace
from quantpilot.v2.signals import evaluate


def configure(workspace, **changes):
    return workspace.preferences({**workspace.store.get("v2_preferences"), **changes})


def enable_research(workspace):
    workspace.vault.put("v2:ai:openai", {"apiKey": "test-only-research-key"})
    return configure(workspace, ai_data_enabled=True, ai_model="test-search-model")


def observation(category="etf", now=None):
    now = now or time.time()
    stamp = dt.datetime.fromtimestamp(now - 60, dt.UTC).isoformat()
    return {
        "instrument_id": "btc",
        "category": category,
        "url": "https://tftc.io/bitcoin-etf-flows/"
        if category == "etf"
        else "https://deribit.com/observations",
        "date_text": stamp,
        "quote": "Bitcoin ETF net inflows were 150 million dollars.",
    }


def source_transport(row):
    return httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            text=f"<html><p>{row['date_text']}</p><p>{row['quote']}</p></html>",
            headers={"content-type": "text/html"},
        )
    )


def verified(workspace):
    row = observation()
    return verify_observation(
        row, universe(workspace)["btc"], {row["url"]}, time.time(), source_transport(row)
    )


@pytest.mark.parametrize("score", [1, 70, 80, 95, 100])
def test_threshold_persists_and_invalidates_previous_opportunity(workspace, logged, opportunity, score):
    workspace.opportunities = {"btc": copy.deepcopy(opportunity)}
    response = logged.put("/api/v2/preferences", json={**Preferences().model_dump(), "min_score": score})
    assert response.status_code == 200
    assert logged.get("/api/v2/state").json()["preferences"]["min_score"] == score
    if score != 80:
        assert not workspace.opportunities["btc"]["valid"]


@pytest.mark.parametrize("score", [0, 101, -1, 80.5, True, "80"])
def test_invalid_threshold_rejected(score):
    with pytest.raises(ValidationError):
        Preferences(min_score=score)


def test_threshold_applies_to_signal_ai_and_hard_risk_blocks(monkeypatch, quote, opportunity, workspace):
    monkeypatch.setattr(
        "quantpilot.v2.signals.indicators",
        lambda rows: {
            "trend": "long",
            "rsi": 55,
            "macd_hist": 1,
            "volume_ratio": 1.5,
            "atr": 2,
            "close": 100,
            "timestamp": 1000,
        },
    )
    item, frames = universe(workspace)["btc"], {f: [[1]] for f in ("1w", "1d", "4h", "1h")}
    assert evaluate(item, frames, quote, [], "balanced", time.time(), 70)["valid"]
    assert not evaluate(item, frames, quote, [], "balanced", time.time(), 71)["valid"]
    soon = time.time() + 10
    evidence = [
        {
            "category": "etf",
            "status": "fresh",
            "published_at": time.time() - 10,
            "expires_at": soon,
            "bias": "neutral",
        }
    ]
    assert evaluate(item, frames, quote, evidence, "balanced", time.time(), 70)["expires_at"] <= soon
    assert not evaluate(item, frames, {**quote, "timestamp": 0}, [], "balanced", time.time(), 1)["valid"]
    rev = {"score_delta": 0, "ttl": 60, "action": "hold", "stop": None, "targets": None}
    assert apply_revision({**opportunity, "score": 75, "min_score": 75}, rev, time.time(), False)["valid"]
    assert not apply_revision({**opportunity, "score": 89, "min_score": 90}, rev, time.time(), True)["valid"]


def test_paper_rejects_old_threshold_and_disabled_ai_snapshot(workspace, account, opportunity, quote):
    old = copy.deepcopy(opportunity)
    configure(workspace, min_score=75)
    with pytest.raises(ValueError, match="有效分数"):
        workspace.paper.enter(account, old, {"btc": quote}, universe(workspace), "balanced")
    prefs = enable_research(workspace)
    old.update(min_score=75, policy=signal_policy(prefs))
    configure(workspace, ai_data_enabled=False)
    with pytest.raises(ValueError, match="配置已变更"):
        workspace.paper.enter(account, old, {"btc": quote}, universe(workspace), "balanced")
    assert not workspace.state()["orders"]


def test_external_order_rechecks_policy_after_network_read(workspace, monkeypatch, opportunity, quote):
    account, broker = demo_account(workspace, monkeypatch)
    prefs = enable_research(workspace)
    opportunity["policy"] = signal_policy(prefs)
    client = broker.client(account)
    original = client.load_markets

    def changed_during_lookup(*args, **kwargs):
        result = original(*args, **kwargs)
        configure(workspace, ai_data_enabled=False)
        return result

    monkeypatch.setattr(client, "load_markets", changed_during_lookup)
    with pytest.raises(ValueError, match="配置已变化"):
        broker.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert not client.calls


def test_scan_discards_results_if_threshold_changed_in_flight(workspace, monkeypatch, quote):
    prefs = workspace.store.get("v2_preferences")
    monkeypatch.setattr(workspace.market, "quote", lambda item: quote)
    monkeypatch.setattr(workspace.feeds, "evidence", lambda item: [])

    def change(item, frame, **kwargs):
        configure(workspace, min_score=95)
        return []

    monkeypatch.setattr(workspace.market, "candles", change)
    workspace.scan(universe(workspace)["btc"], True, prefs)
    assert "btc" not in workspace.opportunities


def test_upgrade_preferences_preserves_existing_values(workspace):
    previous = {
        k: v
        for k, v in Preferences(profile="aggressive").model_dump().items()
        if k not in ("min_score", "ai_data_enabled", "ai_data_interval")
    }
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_preferences", previous)
    Workspace(workspace.store, workspace.vault, workspace.settings)
    prefs = workspace.store.get("v2_preferences")
    assert prefs["profile"] == "aggressive" and prefs["min_score"] == 80
    assert not prefs["ai_data_enabled"]


@pytest.mark.parametrize(
    "url",
    [
        "http://tftc.io/a",
        "https://tftc.io.evil.test/a",
        "https://localhost/",
        "https://169.254.169.254/",
        "https://tftc.io:999/a",
        "https://user:secret@tftc.io/",
        "https://tftc.io\\@evil.test/",
    ],
)
def test_research_source_rejects_untrusted_urls(url):
    assert not allowed_url(url, "etf")


def test_source_redirect_revalidated_before_request():
    seen = []

    def transport(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    with pytest.raises(ValueError, match="白名单"):
        fetch_source("https://tftc.io/a", "etf", httpx.MockTransport(transport))
    assert seen == ["https://tftc.io/a"]


@pytest.mark.parametrize(
    "failure",
    ["uncited", "invented", "far_date", "stale", "future", "wrong_asset", "forecast", "no_value", "hidden"],
)
def test_unverifiable_observation_cannot_become_evidence(workspace, failure):
    row, now = observation(), time.time()
    page = dict(row)
    sources = {row["url"]}
    if failure == "uncited":
        sources.clear()
    elif failure == "invented":
        row["quote"] = "Bitcoin ETF net inflows were 999 billion dollars."
    elif failure == "far_date":
        page["date_text"] += "x" * 1200
    elif failure in ("stale", "future"):
        row["date_text"] = dt.datetime.fromtimestamp(
            now + (600 if failure == "future" else -5 * 86400), dt.UTC
        ).isoformat()
        page = dict(row)
    elif failure == "wrong_asset":
        row["quote"] = "Ethereum ETF net inflows were 150 million dollars."
        page = dict(row)
    elif failure in ("forecast", "no_value"):
        row["quote"] = (
            "Bitcoin ETF expected inflows are 150 million dollars."
            if failure == "forecast"
            else "Bitcoin is a popular digital asset worldwide."
        )
        page = dict(row)
    elif failure == "hidden":
        page["quote"] = "<script>" + row["quote"] + "</script>"
    with pytest.raises(ValueError):
        verify_observation(row, universe(workspace)["btc"], sources, now, source_transport(page))


def test_verified_evidence_neutral_short_lived_and_dates_required(workspace):
    evidence = verified(workspace)
    assert evidence["origin"] == "ai_web_search" and evidence["bias"] == "neutral"
    assert evidence["expires_at"] <= time.time() + 1800
    row = observation("options")
    row.update(
        date_text=dt.datetime.now(dt.UTC).date().isoformat(),
        quote="Bitcoin options open interest is 100 million dollars.",
    )
    with pytest.raises(ValueError, match="带时区"):
        verify_observation(row, universe(workspace)["btc"], {row["url"]}, time.time(), source_transport(row))


def test_research_requires_completed_web_search_and_minimizes_sent_data(workspace):
    prefs, row = enable_research(workspace), observation()
    request = {
        **universe(workspace)["btc"],
        "categories": ["etf"],
        "balance": 12345,
        "credential": "never-send",
    }
    body = {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": row["url"]}]}},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps({"observations": [row]})}],
            },
        ],
    }

    def transport(req):
        payload = json.loads(req.content)
        assert "never-send" not in str(payload) and "12345" not in str(payload)
        assert payload["max_tool_calls"] == 4 and payload["tools"][0]["type"] == "web_search"
        assert not payload["store"] and "apiKey" not in payload["input"]
        return httpx.Response(200, json=body)

    result = research(
        prefs, workspace.vault, [request], httpx.MockTransport(transport), source_transport(row)
    )
    assert len(result["observations"]) == 1 and result["rejected"] == 0
    body["output"].pop(0)
    with pytest.raises(ValueError, match="联网补全失败"):
        research(prefs, workspace.vault, [request], httpx.MockTransport(transport), source_transport(row))


def test_disabled_missing_key_and_provider_failure_do_not_leak(workspace):
    with pytest.raises(ValueError, match="AI 连接"):
        configure(workspace, ai_data_enabled=True, ai_model="test")
    assert workspace.research.run_once() is False
    prefs = enable_research(workspace)
    req = {**universe(workspace)["btc"], "categories": ["etf"]}
    with pytest.raises(ValueError) as exc:
        research(
            prefs,
            workspace.vault,
            [req],
            httpx.MockTransport(lambda r: httpx.Response(401, text="test-only-research-key")),
        )
    assert "test-only-research-key" not in str(exc.value)


def test_only_missing_sources_supplemented_and_stale_cache_never_fresh(workspace):
    enable_research(workspace)
    row = verified(workspace)
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_ai_evidence", [row])
    item = universe(workspace)["btc"]
    native = {**row, "origin": "provider", "source": "actual provider"}
    assert workspace.research.supplement(item, [native]) == [native]
    missing = {"category": "etf", "status": "missing"}
    assert workspace.research.supplement(item, [missing]) == [row]
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_ai_evidence", [{**row, "expires_at": 0}])
    assert workspace.research.supplement(item, [missing])[1]["status"] == "stale"
    assert workspace.research.supplement({**item, "exchange": "okx"}, [missing]) == [missing]
    configure(workspace, ai_data_enabled=False)
    assert not workspace.research.observations()
    assert not workspace.store.get("v2_ai_evidence")


def test_research_batches_throttles_and_invalidation_discards_inflight(workspace, monkeypatch):
    enable_research(workspace)
    calls, row = [], verified(workspace)

    def run(prefs, vault, requests):
        calls.append(requests)
        return {"observations": [row], "rejected": 2}

    monkeypatch.setattr(module, "research", run)
    assert workspace.research.run_once()
    assert len(calls[0]) == 3
    assert workspace.store.get("v2_ai_data_status")["accepted"] == 1
    assert not workspace.research.run_once(force=True)
    with pytest.raises(ValueError, match="5 分钟"):
        workspace.research.request_now()
    # Fresh worker represents a later request; toggling the switch while it is away discards results.
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_ai_data_status", {"last_attempt": 0})

    def disable(prefs, vault, requests):
        configure(workspace, ai_data_enabled=False)
        return {"observations": [row], "rejected": 0}

    monkeypatch.setattr(module, "research", disable)
    assert not workspace.research.run_once()
    assert not workspace.store.get("v2_ai_evidence")
    assert workspace.store.get("v2_ai_data_status")["state"] == "disabled"


def test_research_no_gaps_no_api_call_and_failure_status(workspace, monkeypatch):
    enable_research(workspace)

    def unavailable(*args):
        raise RuntimeError("private-provider-response")

    monkeypatch.setattr(module, "research", unavailable)
    assert not workspace.research.run_once()
    status = workspace.store.get("v2_ai_data_status")
    assert status["state"] == "error" and "private-provider-response" not in str(status)
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_ai_data_status", {"last_attempt": 0})
    workspace.opportunities = {
        i: {
            "evidence": [
                {"category": c, "status": "fresh", "expires_at": time.time() + 300} for c in module.DOMAINS
            ]
        }
        for i in universe(workspace)
    }
    assert workspace.research.run_once()
    assert workspace.store.get("v2_ai_data_status")["state"] == "empty"


def test_manual_research_endpoint_auth_csrf_and_key_deletion(client, logged, workspace):
    assert logged.post("/api/v2/ai/data", json={}).status_code == 409
    enable_research(workspace)
    assert logged.post("/api/v2/ai/data", json={}).json()["queued"]
    logged.headers.pop("x-csrf-token")
    assert logged.post("/api/v2/ai/data", json={}).status_code == 403
    workspace.delete_key("openai")
    assert not workspace.store.get("v2_preferences")["ai_data_enabled"]
    assert workspace.store.get("v2_ai_data_status")["state"] == "disabled"
    client.cookies.clear()
    assert client.post("/api/v2/ai/data", json={}).status_code == 401


def test_research_cache_not_trusted_across_restart(workspace):
    enable_research(workspace)
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_ai_evidence", [verified(workspace)])
    ResearchEngine(workspace)
    assert not workspace.store.get("v2_ai_evidence")


@pytest.mark.parametrize("threshold,add_score", [(70, 75), (95, 100), (100, 100)])
def test_add_score_tracks_threshold_with_100_cap(
    workspace, account, opportunity, quote, threshold, add_score
):
    from quantpilot.v2.accounts import size_order

    configure(workspace, min_score=threshold)
    opportunity.update(min_score=threshold, score=100)
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    book = workspace.store.get("v2_ledgers")[account["id"]]
    # A profitable continuation, still inside the refreshed entry range.
    q = {**quote, "mid": 100.5, "bid": 100.49, "ask": 100.51}
    opportunity["score"] = add_score
    assert (
        size_order(
            account,
            book,
            opportunity,
            {"btc": q},
            universe(workspace),
            "balanced",
            time.time(),
            True,
            threshold,
        )["qty"]
        > 0
    )
    opportunity["score"] = add_score - 1
    with pytest.raises(ValueError):
        size_order(
            account,
            book,
            opportunity,
            {"btc": q},
            universe(workspace),
            "balanced",
            time.time(),
            True,
            threshold,
        )


@pytest.mark.parametrize("change", ["key", "stop", "symbol"])
def test_inflight_research_cannot_restore_obsolete_context(workspace, monkeypatch, change):
    enable_research(workspace)
    row = verified(workspace)

    def run(*args):
        if change == "key":
            workspace.vault.put("v2:ai:openai", {"apiKey": "rotated-test-key"})
        elif change == "stop":
            workspace.stop_event.set()
        else:
            watch = workspace.store.get("v2_watchlist")
            watch[0]["exchange"] = "gate"
            workspace.save_watchlist(watch)
        return {"observations": [row], "rejected": 0}

    monkeypatch.setattr(module, "research", run)
    workspace.research.run_once()
    assert not workspace.store.get("v2_ai_evidence")
