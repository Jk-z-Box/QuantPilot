from __future__ import annotations

import math
import threading
import time

import ccxt

from .models import EXCHANGES, FRAMES


class FuturesMarket:
    def __init__(self, demo=False, factory=None):
        self.demo = demo
        self.factory = factory or (lambda name, config: getattr(ccxt, name)(config))
        self.clients = {}
        self.locks = {name: threading.RLock() for name in EXCHANGES}
        self.cache = {}

    def client(self, exchange):
        if exchange not in EXCHANGES:
            raise ValueError("不支持的交易所")
        if exchange not in self.clients:
            self.clients[exchange] = self.factory(
                "binanceusdm" if exchange == "binance" else exchange,
                {
                    "enableRateLimit": True,
                    "timeout": 8000,
                    "options": {"defaultType": "swap", "defaultSubType": "linear"},
                },
            )
        return self.clients[exchange]

    def instrument(self, client, symbol):
        client.load_markets()
        market = client.market(symbol)
        if (
            not market.get("swap")
            or not market.get("linear")
            or market.get("settle") != "USDT"
            or market.get("active") is False
        ):
            raise ValueError("仅支持已上市的 USDT 线性永续合约")
        return market

    def markets(self, exchange, query=""):
        with self.locks[exchange]:
            client = self.client(exchange)
            markets = client.load_markets()
            return [
                {"symbol": m["symbol"], "base": m["base"], "contract_size": m.get("contractSize")}
                for m in markets.values()
                if m.get("swap")
                and m.get("linear")
                and m.get("settle") == "USDT"
                and m.get("active") is not False
                and query.upper() in m["symbol"]
            ][:150]

    def quote(self, item):
        now = time.time()
        if self.demo:
            p = {"BTC": 68000, "ETH": 2600, "SOL": 160, "AVGO": 180, "SOXL": 40}.get(item["label"], 100)
            p *= 1 + math.sin(now / 80) * 0.002
            return {
                "mid": p,
                "bid": p * 0.9999,
                "ask": p * 1.0001,
                "timestamp": now,
                "received_at": now,
                "funding_rate": 0.0001,
                "open_interest": 100000,
                "mark": p,
                "source": "synthetic_demo",
            }
        name, symbol = item["exchange"], item["symbol"]
        with self.locks[name]:
            client = self.client(name)
            m = self.instrument(client, symbol)
            if name == "binance":
                book = client.fapiPublicGetTickerBookTicker({"symbol": m["id"]})
                bid, ask, stamp = float(book["bidPrice"]), float(book["askPrice"]), float(book["time"]) / 1000
            else:
                book = client.fetch_order_book(symbol, 5 if name == "okx" else 20)
                bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
                stamp = (book.get("timestamp") or 0) / 1000
            key = (name, symbol, "derivatives")
            cached = self.cache.get(key)
            if not cached or now - cached[0] > 60:
                funding, oi, mark = None, None, None
                try:
                    f = client.fetch_funding_rate(symbol)
                    funding, mark = f.get("fundingRate"), f.get("markPrice")
                except Exception:
                    funding, mark = None, None
                try:
                    o = (
                        client.fetch_open_interest_history(symbol, "5m", limit=2)[-1]
                        if name == "gate"
                        else client.fetch_open_interest(symbol)
                    )
                    if name == "gate" and not 0 <= time.time() - (o.get("timestamp") or 0) / 1000 <= 600:
                        raise ValueError("持仓量数据过期")
                    oi = o.get("openInterestAmount")
                except Exception:
                    oi = None
                cached = (now, {"funding_rate": funding, "open_interest": oi, "mark": mark})
                self.cache[key] = cached
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
            raise ValueError("交易所报价无效")
        return {
            "mid": (bid + ask) / 2,
            "bid": bid,
            "ask": ask,
            "timestamp": stamp,
            "received_at": time.time(),
            "source": "exchange",
            **cached[1],
        }

    def candles(self, item, frame="1h", closed=False):
        if frame not in FRAMES:
            raise ValueError("周期不支持")
        now, span = time.time(), FRAMES[frame]
        key = (item["exchange"], item["symbol"], frame, closed)
        cached = self.cache.get(key)
        if cached and now - cached[0] < (30 if closed else 4):
            return cached[1]
        if self.demo:
            base = self.quote(item)["mid"]
            end = int(now) // span * span
            rows = []
            for i in range(180):
                c = base * (0.92 + i * 0.00045 + math.sin(i / 4) * 0.001)
                rows.append(
                    [
                        (end - (179 - i) * span) * 1000,
                        c * 0.999,
                        c * 1.003,
                        c * 0.997,
                        c,
                        100 + i + 20 * math.cos(i),
                    ]
                )
        else:
            with self.locks[item["exchange"]]:
                client = self.client(item["exchange"])
                self.instrument(client, item["symbol"])
                rows = client.fetch_ohlcv(item["symbol"], frame, limit=180)
        valid = []
        for row in rows:
            values = [float(v) for v in row[:6]]
            if len(values) != 6 or not all(math.isfinite(x) for x in values):
                raise ValueError("K 线数值无效")
            t, op, high, low, close, vol = values
            if (
                min(op, high, low, close) <= 0
                or vol < 0
                or not low <= min(op, close) <= max(op, close) <= high
            ):
                raise ValueError("K 线价格无效")
            if valid and t - valid[-1][0] != span * 1000:
                raise ValueError("K 线缺失或重复")
            if t / 1000 > now + 5:
                raise ValueError("K 线时间来自未来")
            if not closed or t / 1000 + span <= now - 2:
                valid.append(values)
        if not valid or now - (valid[-1][0] / 1000 + span) > span + 30:
            raise ValueError("K 线已过期")
        self.cache[key] = (now, valid)
        return valid
