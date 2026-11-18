import time

import pytest
from fastapi.testclient import TestClient

from quantpilot.app import create_app
from quantpilot.config import Settings, hash_password
from quantpilot.v2.models import Sizing


@pytest.fixture
def settings():
    return Settings(username="tester", password_hash=hash_password("test-only-password"))


@pytest.fixture
def app(tmp_path, settings):
    return create_app(settings, tmp_path, background=False)


@pytest.fixture
def client(app, settings):
    with TestClient(app, base_url=settings.public_origin) as client:
        client.headers["origin"] = settings.public_origin
        yield client


@pytest.fixture
def logged(client):
    response = client.post("/api/login", json={"username": "tester", "password": "test-only-password"})
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf"]
    return client


@pytest.fixture
def workspace(app):
    return app.state.workspace


@pytest.fixture
def quote():
    return {
        "mid": 100.0,
        "bid": 99.99,
        "ask": 100.01,
        "timestamp": time.time(),
        "received_at": time.time(),
        "funding_rate": 0.0001,
        "open_interest": 100,
        "mark": 100,
        "source": "test",
    }


@pytest.fixture
def opportunity():
    return {
        "id": "test-signal",
        "instrument_id": "btc",
        "exchange": "binance",
        "symbol": "BTC/USDT:USDT",
        "direction": "long",
        "score": 90,
        "valid": True,
        "coverage": 100,
        "evaluated_at": time.time(),
        "expires_at": time.time() + 120,
        "profile": "balanced",
        "blockers": [],
        "reasons": [],
        "plan": {
            "entry": 100,
            "entry_low": 99,
            "entry_high": 101,
            "stop": 96,
            "targets": [{"price": 106, "ratio": 0.5}, {"price": 112, "ratio": 0.5}],
            "rr": 2,
            "risk_per_unit": 4,
            "add_at": 103,
            "reduce_at": 98,
        },
    }


@pytest.fixture
def account(workspace):
    a = workspace.store.get("v2_accounts")["paper-default"]
    a["auto"] = True
    a["sizing"] = [Sizing(instrument_id="btc", quantity=1).model_dump()]
    return workspace.save_account(a)
