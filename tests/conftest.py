import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from quantpilot.app import create_app
from quantpilot.config import Settings, hash_password
from quantpilot.engine import Engine, OrderRequest
from quantpilot.market import Market, MarketError, Quote
from quantpilot.store import Store


class FakeMarket(Market):
    def __init__(self, settings):
        super().__init__(settings)
        self.price = Decimal("100")
        self.age = 0
        self.spread = Decimal("0.02")
        self.failure = False
        self.timestamp_missing = False

    def quote(self, exchange, symbol):
        self.check(exchange, symbol)
        if self.failure:
            raise MarketError("exchange_unavailable")
        now = time.time()
        return Quote(exchange, symbol, self.price - self.spread / 2, self.price + self.spread / 2,
                     None if self.timestamp_missing else now - self.age, now)

    def validate_amount(self, *args):
        return None

    def budget_amount(self, exchange, symbol, budget, price):
        return (budget / price).quantize(Decimal("0.000001"))

    def candles(self, exchange, symbol, timeframe="1h"):
        from quantpilot.market import TIMEFRAMES

        self.check(exchange, symbol)
        if timeframe not in TIMEFRAMES:
            raise MarketError("unsupported_timeframe")
        span = TIMEFRAMES[timeframe]
        end = (int(time.time()) // span - 1) * span
        return [[(end - (119 - i) * span) * 1000, 100, 102, 98, 100, 100] for i in range(120)]


@pytest.fixture
def settings():
    return Settings(username="tester", password_hash=hash_password("test-only-password"))


@pytest.fixture
def engine(tmp_path, settings):
    return Engine(settings, Store(tmp_path), FakeMarket(settings))


@pytest.fixture
def order():
    return OrderRequest(id="test-order-0001", exchange="okx", symbol="BTC/USDT", side="buy", quantity="1",
                        limit_price="101", stop_price="90", take_price="110", expires_at=time.time() + 30)


@pytest.fixture
def app(tmp_path, settings):
    return create_app(settings, tmp_path, FakeMarket(settings), background=False)


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
