"""Regression coverage for account units, lifecycle, provider deletion and public sources."""

import copy
import datetime as dt
import time
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from test_v2 import universe

from quantpilot.v2.accounts import ledger, size_order
from quantpilot.v2.ai import list_models
from quantpilot.v2.limits import contract_limits
from quantpilot.v2.models import Account, Preferences, Sizing
from quantpilot.v2.public_feeds import btc_etf, company_news, news_items
from quantpilot.v2.service import Workspace


def test_real_account_without_simulated_capital(logged):
    response = logged.post("/api/v2/accounts", json={"name": "live no virtual cash", "kind": "live"})
    assert response.status_code == 200
    assert response.json()["capital"] is None
    assert "leverage" not in response.json()
    assert Account(name="paper").capital == 10000


def test_delete_account_archives_history_and_removes_secret(workspace, logged):
    a = workspace.save_account(Account(name="removable", kind="live").model_dump())
    aid = a["id"]
    workspace.vault.put("v2:account:" + aid, {"apiKey": "fake-secret", "secret": "fake"})
    with workspace.store.transaction() as db:
        db.execute(
            "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
            ("old-order", aid, "open", "unfilled", "{}", time.time()),
        )
    workspace.exchange.clients[(aid, "binance", "live", "fingerprint")] = object()
    assert logged.request("DELETE", "/api/v2/accounts/" + aid, json={}).status_code == 200
    s = workspace.state()
    assert not any(x["id"] == aid for x in s["accounts"])
    assert s["archived_accounts"][aid] == "removable"
    assert any(o["id"] == "old-order" for o in s["orders"])
    assert not workspace.vault.get("v2:account:" + aid)
    assert not workspace.exchange.clients
    with pytest.raises(ValueError, match="已删除"):
        workspace.save_account(a)


def test_delete_requires_login(client):
    assert client.request("DELETE", "/api/v2/accounts/paper-default", json={}).status_code in (401, 403)


def test_delete_requires_csrf(logged, workspace):
    aid = workspace.store.get("v2_accounts")["paper-default"]["id"]
    logged.headers.pop("x-csrf-token")
    assert logged.request("DELETE", "/api/v2/accounts/" + aid, json={}).status_code == 403


def test_delete_blocks_running_held_or_uncertain(workspace, account, opportunity, quote):
    aid = account["id"]
    with pytest.raises(ValueError, match="暂停"):
        workspace.delete_account(aid)
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    account["auto"] = False
    workspace.save_account(account)
    with pytest.raises(ValueError, match="持仓"):
        workspace.delete_account(aid)
    workspace.reset(aid, 10000)
    with workspace.store.transaction() as db:
        db.execute(
            "INSERT INTO v2_orders VALUES (?,?,?,?,?,?)",
            ("unknown-delete", aid, "open", "unknown", "{}", time.time()),
        )
    with pytest.raises(ValueError, match="待核对"):
        workspace.delete_account(aid)


def test_migration_never_interprets_dollars_as_coins(workspace):
    old = {
        "id": "old",
        "name": "old",
        "kind": "live",
        "capital": 10000,
        "exchange": "binance",
        "leverage": 3,
        "auto": True,
        "allowed": ["btc"],
        "sizing": [
            {"instrument_id": "btc", "mode": "fixed", "notional": 1000, "add_count": 0, "add_ratios": []}
        ],
    }
    with workspace.store.transaction() as db:
        workspace.store.write(db, "v2_accounts", {"old": old})
    Workspace(
        workspace.store, workspace.vault, workspace.settings, market=workspace.market, feeds=workspace.feeds
    )
    a = workspace.store.get("v2_accounts")["old"]
    assert a["capital"] is None and not a["auto"] and "leverage" not in a
    assert a["sizing"][0]["quantity"] is None and "notional" not in a["sizing"][0]
    assert a["sizing"][0]["leverage"] == 3
    assert workspace.store.get("v2_sizing_migration")["old"] == old
    with pytest.raises(ValidationError):
        Account(**{**a, "auto": True})


def test_fixed_quantity_and_add_ratio_use_initial_coins(workspace, account, opportunity, quote):
    account["sizing"] = [
        Sizing(instrument_id="btc", mode="fixed", quantity=2, add_count=1, add_ratios=[0.25]).model_dump()
    ]
    account = workspace.save_account(account)
    result = workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    assert Decimal(result["qty"]) == 2
    q = {**quote, "bid": 103.99, "ask": 104.01, "mid": 104}
    result = workspace.paper.enter(account, opportunity, {"btc": q}, universe(workspace), "balanced", True)
    assert Decimal(result["qty"]) == Decimal(".5")
    assert Decimal(workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"]) == Decimal(
        "2.5"
    )


def test_maximum_is_total_coins_despite_price_rise(workspace, account, opportunity, quote):
    account["sizing"] = [Sizing(instrument_id="btc", quantity=3, add_count=1, add_ratios=[0.5]).model_dump()]
    account = workspace.save_account(account)
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    workspace.paper.enter(
        account,
        opportunity,
        {"btc": {**quote, "bid": 104, "ask": 104.01, "mid": 104}},
        universe(workspace),
        "balanced",
        True,
    )
    assert Decimal(workspace.store.get("v2_ledgers")[account["id"]]["positions"]["btc"]["qty"]) == 3


def test_independent_leverage_is_not_global_profile_cap(workspace, account, opportunity, quote, monkeypatch):
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": True, "max_leverage": 20, "note": "test"}
    )
    account["sizing"] = [
        Sizing(instrument_id="btc", quantity=1, leverage=5).model_dump(),
        Sizing(instrument_id="eth", quantity=2, leverage=3).model_dump(),
    ]
    account = workspace.save_account(account)
    result = size_order(
        account, ledger(10000), opportunity, {"btc": quote}, universe(workspace), "conservative", time.time()
    )
    assert result["leverage"] == 5
    assert account["sizing"][1]["leverage"] == 3
    account["sizing"][0]["leverage"] = 21
    with pytest.raises(ValueError, match="杠杆"):
        workspace.save_account(account)


def test_leverage_change_rejected_while_held(workspace, account, opportunity, quote, monkeypatch):
    workspace.paper.enter(account, opportunity, {"btc": quote}, universe(workspace), "balanced")
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": True, "max_leverage": 20, "note": "test"}
    )
    account["sizing"][0]["leverage"] = 2
    with pytest.raises(ValueError, match="平仓"):
        workspace.save_account(account)


class LimitsClient:
    precisionMode = 4
    has = {"fetchMarketLeverageTiers": True}

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {
            "swap": True,
            "linear": True,
            "settle": "USDT",
            "contractSize": 0.01,
            "precision": {"amount": 1},
            "limits": {"amount": {"min": 1}, "leverage": {"max": 50}},
        }

    def fetch_market_leverage_tiers(self, symbol):
        return [
            {"minNotional": 0, "maxNotional": 100, "maxLeverage": 50},
            {"minNotional": 100, "maxNotional": 500, "maxLeverage": 10},
        ]


def test_leverage_tiers_use_okx_contracts_and_gate_usdt():
    c = LimitsClient()
    assert contract_limits(c, "okx", "BTC/USDT:USDT", 6000, 2)["max_leverage"] == 10
    assert contract_limits(c, "gate", "BTC/USDT:USDT", 50, 2)["max_leverage"] == 50
    assert contract_limits(c, "okx", "BTC/USDT:USDT")["quantity_step"] == 0.01
    with pytest.raises(ValueError, match="风险档位"):
        contract_limits(c, "okx", "BTC/USDT:USDT", 6000, 6)


@pytest.mark.parametrize("provider", ["openai", "coinglass", "finnhub"])
def test_delete_key_invalidates_signals_and_ai(logged, workspace, provider):
    assert logged.post("/api/v2/keys/" + provider, json={"api_key": "not-a-real-key"}).status_code == 200
    if provider == "openai":
        workspace.preferences(
            Preferences(ai_enabled=True, ai_intervene=True, ai_model="example").model_dump()
        )
    workspace.opportunities = {"btc": {"valid": True, "blockers": []}}
    workspace.ai_cache = {"btc": {"old": True}}
    assert logged.request("DELETE", "/api/v2/keys/" + provider, json={}).status_code == 200
    assert not workspace.vault.get("v2:" + ("ai:" if provider == "openai" else "feed:") + provider)
    assert not workspace.opportunities["btc"]["valid"] and not workspace.ai_cache
    if provider == "openai":
        prefs = workspace.store.get("v2_preferences")
        assert not prefs["ai_enabled"] and not prefs["ai_intervene"]


def test_model_discovery_and_safe_failure(workspace):
    workspace.vault.put("v2:ai:openai", {"apiKey": "model-list-test-secret"})

    def respond(req):
        assert req.url.path == "/v1/models" and req.method == "GET"
        assert req.headers["authorization"] == "Bearer model-list-test-secret"
        return httpx.Response(
            200, json={"data": [{"id": "model-z"}, {"id": "model-a"}, {"id": "model-a"}, {"id": "<unsafe>"}]}
        )

    assert list_models(workspace.vault, httpx.MockTransport(respond))["models"] == ["model-a", "model-z"]
    with pytest.raises(ValueError, match="自定义") as e:
        list_models(
            workspace.vault,
            httpx.MockTransport(lambda req: httpx.Response(403, text="model-list-test-secret")),
        )
    assert "model-list-test-secret" not in str(e.value)


def test_public_etf_rejects_partial_nan_and_marks_stale():
    date = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    body = {
        "units": "USD",
        "latestSession": {"date": str(date), "netFlowUsd": 100, "perEtfUsd": {str(i): 10 for i in range(10)}},
    }
    assert btc_etf(body, time.time())["status"] == "fresh"
    assert btc_etf(body, time.time() + 7 * 86400)["status"] == "stale"
    partial = copy.deepcopy(body)
    partial["latestSession"]["perEtfUsd"]["0"] = None
    with pytest.raises((ValueError, TypeError)):
        btc_etf(partial, time.time())
    partial["latestSession"]["perEtfUsd"]["0"] = float("nan")
    with pytest.raises(ValueError):
        btc_etf(partial, time.time())
    partial = copy.deepcopy(body)
    partial["latestSession"]["netFlowUsd"] = 100000000
    with pytest.raises(ValueError):
        btc_etf(partial, time.time())


def test_public_news_fallback_preserves_source_time_and_scope():
    now = time.time()
    date = dt.datetime.fromtimestamp(now - 3600, dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    xml = f"<rss><channel><item><title>Company results published</title><link>https://example.org/news</link><pubDate>{date}</pubDate></item></channel></rss>"
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if "broadcom.com" in url:
            raise ValueError("403")
        return xml

    out = company_news(get, "AVGO", now)
    assert len(calls) == 2 and out["source"].startswith("Google News")
    assert out["published_at"] < now - 3500 and not out["details"]["events"]
    assert news_items(xml, now + 10 * 86400) == []


def test_archived_account_stale_snapshot_cannot_trade(workspace, opportunity, quote):
    a = workspace.save_account(
        Account(name="gone", sizing=[Sizing(instrument_id="btc", quantity=1)]).model_dump()
    )
    workspace.delete_account(a["id"])
    with pytest.raises(ValueError, match="停用"):
        workspace.paper.enter(
            {**a, "auto": True}, opportunity, {"btc": quote}, universe(workspace), "balanced"
        )


def test_okx_requires_passphrase_and_removed_account_rejects_credentials(workspace, logged):
    a = workspace.save_account(Account(name="OKX connection", kind="live", exchange="okx").model_dump())
    body = {"api_key": "fake-key", "secret": "fake-secret"}
    assert logged.post(f"/api/v2/accounts/{a['id']}/credentials", json=body).status_code == 409
    workspace.delete_account(a["id"])
    body["passphrase"] = "fake-passphrase"
    assert logged.post(f"/api/v2/accounts/{a['id']}/credentials", json=body).status_code == 409
    assert not workspace.vault.get("v2:account:" + a["id"])


@pytest.mark.parametrize("path", ["/api/v2/models", "/api/v2/limits/btc?exchange=okx"])
def test_discovery_requires_login(client, path):
    assert client.get(path).status_code == 401


def test_unknown_leverage_is_one_and_cannot_be_saved_above_it(workspace, monkeypatch):
    monkeypatch.setattr(
        workspace, "limits", lambda *args: {"verified": False, "max_leverage": 1, "note": "不可核验"}
    )
    a = Account(name="unverified", sizing=[Sizing(instrument_id="btc", quantity=1, leverage=2)]).model_dump()
    with pytest.raises(ValueError, match="不可核验"):
        workspace.save_account(a)
