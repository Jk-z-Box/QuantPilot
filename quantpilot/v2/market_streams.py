"""Shared public exchange streams; reconnect/resnapshot without blocking execution or UI."""

from __future__ import annotations

import asyncio
import copy
import math
import threading
import time
from contextlib import suppress

import ccxt.pro as ccxtpro

from .contracts import client_name, client_options, settlement, validate_market
from .models import FRAMES


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError):
        return None


class MarketStreams:
    def __init__(self, workspace, factory=None):
        self.w = workspace
        self.factory = factory or (lambda name, options: getattr(ccxtpro, name)(options))
        self.lock = threading.RLock()
        self.quotes, self.bars, self.statuses = {}, {}, {}
        self.active = None
        self.clocks = {}
        self.loop, self.task, self.thread = None, None, None
        self.changed = threading.Event()

    @staticmethod
    def key(item):
        return (item["exchange"], item["symbol"])

    def quote(self, item):
        with self.lock:
            q = copy.deepcopy(self.quotes.get(self.key(item)))
        return q if q and -5 <= time.time() - q["timestamp"] <= 10 else None

    def status(self, item):
        with self.lock:
            result = copy.deepcopy(self.statuses.get(self.key(item), {"state": "connecting"}))
        if result.get("state") == "live" and time.time() - result.get("received_at", 0) > 15:
            result["state"] = "stale"
        return result

    def timing(self, exchange, source_ms, received_ms):
        """Approximate one-way age, with clock uncertainty explicitly retained."""
        clock = self.clocks.get(exchange)
        raw = received_ms - source_ms
        if not clock or time.time() - clock["checked_at"] > 600:
            return {"clock_delta_ms": raw, "exchange_to_server_ms": None}
        delay = raw + clock["offset_ms"]
        return {
            "clock_delta_ms": raw,
            "exchange_to_server_ms": round(max(0, delay), 2) if delay >= -clock["uncertainty_ms"] else None,
            "clock_uncertainty_ms": round(clock["uncertainty_ms"], 2),
        }

    def local_stamp(self, exchange, source):
        clock = self.clocks.get(exchange)
        return (
            source - clock["offset_ms"] / 1000
            if clock and time.time() - clock["checked_at"] <= 600
            else source
        )

    async def calibrate(self, client, exchange):
        if not client.has.get("fetchTime"):
            return
        samples = []
        for _ in range(3):
            before, started = time.time() * 1000, time.monotonic()
            source = await client.fetch_time()
            elapsed = (time.monotonic() - started) * 1000
            if finite(source) and elapsed < 3000:
                samples.append(
                    {
                        "offset_ms": source - before - elapsed / 2,
                        "uncertainty_ms": elapsed / 2 + 1,
                        "checked_at": time.time(),
                    }
                )
        if samples:
            self.clocks[exchange] = min(samples, key=lambda s: s["uncertainty_ms"])

    def merge_candles(self, item, frame, rows):
        with self.lock:
            updates = copy.deepcopy(self.bars.get((*self.key(item), frame), {}))
        # A live bar never papers over a historical hole; caller validates continuity.
        by_time = {r[0]: r for r in rows}
        by_time.update({t: r for t, r in updates.items() if not rows or t >= rows[0][0]})
        return [by_time[t] for t in sorted(by_time)][-420:]

    def update_quote(self, item, book, metadata):
        now = time.time()
        bid = finite(book.get("bid"))
        ask = finite(book.get("ask"))
        source = (book.get("timestamp") or 0) / 1000
        stamp = self.local_stamp(item["exchange"], source)
        if not bid or not ask or ask < bid or not -5 <= now - stamp <= 15:
            return False
        key = self.key(item)
        with self.lock:
            previous = self.quotes.get(key, {})
            if source < previous.get("source_timestamp", 0):
                return False
            q = {
                **previous,
                **metadata,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
                "timestamp": stamp,
                "source_timestamp": source,
                "received_at": now,
                "source": "exchange_ws",
            }
            if book.get("mark"):
                q.update(mark=book["mark"], mark_timestamp=stamp)
            self.quotes[key] = q
            self.statuses[key] = {
                "state": "live",
                "received_at": now,
                "source_at": source,
                **self.timing(item["exchange"], source * 1000, now * 1000),
            }
        with self.w.lock:
            # Deleted/replaced instruments cannot be resurrected by a late socket event.
            if self.active is None or self.active.get(item["id"]) == key:
                self.w.quotes[item["id"]] = copy.deepcopy(q)
        self.changed.set()
        self.w.quote_wake.set()
        return True

    def update_candles(self, item, frame, rows):
        now, span = time.time(), FRAMES[frame]
        for row in rows[-3:]:
            if len(row) < 6:
                continue
            try:
                t, op, high, low, close, vol = map(float, row[:6])
            except (ValueError, TypeError):
                continue
            if (
                not all(math.isfinite(x) for x in (t, op, high, low, close, vol))
                or min(op, high, low, close) <= 0
                or vol < 0
                or not low <= min(op, close) <= max(op, close) <= high
                or t / 1000 > now + 5
                or now - (t / 1000 + span) > span + 30
            ):
                continue
            key = (*self.key(item), frame)
            with self.lock:
                bars = self.bars.setdefault(key, {})
                bars[t] = [t, op, high, low, close, vol]
                for old in sorted(bars)[:-4]:
                    del bars[old]
            self.changed.set()

    async def session(self, items):
        item = items[0]
        settle = settlement(item["symbol"])
        client = self.factory(
            client_name(item["exchange"], settle),
            {
                "enableRateLimit": True,
                "timeout": 10000,
                "newUpdates": True,
                "verbose": False,
                "options": client_options(settle),
            },
        )
        tasks = []
        began = time.monotonic()

        async def book_loop(item, metadata):
            while True:
                book = await client.watch_order_book(item["symbol"], 5 if item["exchange"] == "okx" else 20)
                if book.get("bids") and book.get("asks"):
                    self.update_quote(
                        item,
                        {
                            "bid": book["bids"][0][0],
                            "ask": book["asks"][0][0],
                            "timestamp": book.get("timestamp"),
                        },
                        metadata,
                    )

        async def mark_loop(item):
            while True:
                if client.has.get("watchMarkPrice"):
                    ticker = await client.watch_mark_price(item["symbol"])
                else:
                    ticker = await client.watch_ticker(item["symbol"])
                raw = ticker.get("info") or {}
                mark = finite(ticker.get("markPrice") or raw.get("mark_price") or raw.get("p"))
                stamp = self.local_stamp(item["exchange"], (ticker.get("timestamp") or 0) / 1000)
                if mark and -5 <= time.time() - stamp <= 15:
                    with self.lock:
                        q = self.quotes.get(self.key(item))
                        if q and stamp >= q.get("mark_timestamp", 0):
                            q.update(mark=mark, mark_timestamp=stamp)
                            rate = ticker.get("fundingRate", raw.get("funding_rate"))
                            if rate is not None:
                                with suppress(ValueError, TypeError):
                                    value = float(rate)
                                    if math.isfinite(value):
                                        q["funding_rate"] = value
                    # Copy into the execution/valuation cache without renewing the bid/ask timestamp.
                    q = self.quote(item)
                    if q:
                        with self.w.lock:
                            if self.active is None or self.active.get(item["id"]) == self.key(item):
                                self.w.quotes[item["id"]] = q
                        self.changed.set()
                        self.w.quote_wake.set()

        async def candles_loop(item, frame):
            while True:
                rows = await client.watch_ohlcv(item["symbol"], frame)
                self.update_candles(item, frame, rows)

        async def retry_channel(fn, *args):
            delay = 1
            while True:
                try:
                    await fn(*args)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Exchange error text can contain signed URLs. Status only, never raw exceptions.
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)

        async def clock_loop():
            while True:
                with suppress(Exception):
                    await self.calibrate(client, items[0]["exchange"])
                await asyncio.sleep(300)

        try:
            await client.load_markets()
            tasks.append(asyncio.create_task(clock_loop()))
            for item in items:
                try:
                    m = validate_market(client.market(item["symbol"]))
                except ValueError:
                    with self.lock:
                        self.statuses[self.key(item)] = {"state": "unavailable"}
                    continue
                precision = m.get("precision", {}).get("amount")
                metadata = {
                    "inverse": bool(m.get("inverse")),
                    "settle": m["settle"],
                    "contract_size": m["contractSize"],
                    "contract_step": (10 ** -int(precision) if client.precisionMode == 2 else precision),
                    "min_contracts": m.get("limits", {}).get("amount", {}).get("min"),
                }
                tasks.append(asyncio.create_task(retry_channel(book_loop, item, metadata)))
                tasks.append(asyncio.create_task(retry_channel(mark_loop, item)))
                for frame in FRAMES:
                    if client.has.get("watchOHLCV") and frame in client.timeframes:
                        tasks.append(asyncio.create_task(retry_channel(candles_loop, item, frame)))
            if not tasks:
                await asyncio.sleep(30)
                return
            while True:
                await asyncio.sleep(5)
                connected = list(client.clients.values())
                # Force a new socket and CCXT order-book snapshot after a dead/stalled connection.
                if connected and any(
                    c.isConnected and c.lastPong and time.time() * 1000 - c.lastPong > 60000
                    for c in connected
                ):
                    raise ConnectionError("stalled_public_stream")
                if time.monotonic() - began > 45 and not any(self.quote(i) for i in items):
                    raise ConnectionError("public_quotes_stalled")
                for item in items:
                    q = self.quote(item)
                    if q is None:
                        with self.lock:
                            self.statuses[self.key(item)] = {"state": "reconnecting"}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                await client.close()

    async def run_group(self, items):
        delay = 1
        while True:
            try:
                await self.session(items)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.changed.set()
            with self.lock:
                for item in items:
                    self.statuses[self.key(item)] = {"state": "reconnecting"}
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def run(self):
        tasks = {}
        try:
            while True:
                held = {k for b in self.w.store.get("v2_ledgers").values() for k in b["positions"]}
                held |= {k for b in self.w.store.get("v2_managed").values() for k in b}
                wanted = {}
                self.active = {i["id"]: self.key(i) for i in self.w.store.get("v2_watchlist")}
                for item in self.w.store.get("v2_watchlist"):
                    if item["enabled"] or item["id"] in held:
                        group = (item["exchange"], settlement(item["symbol"]) == "USDT")
                        wanted.setdefault(group, []).append(item)
                for key, (identity, task) in list(tasks.items()):
                    if identity != wanted.get(key):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del tasks[key]
                for key, items in wanted.items():
                    if key not in tasks:
                        tasks[key] = (copy.deepcopy(items), asyncio.create_task(self.run_group(items)))
                await asyncio.sleep(1)
        finally:
            for _, task in tasks.values():
                task.cancel()
            await asyncio.gather(*(t for _, t in tasks.values()), return_exceptions=True)

    def start(self):
        if self.w.market.demo:
            return

        def main():
            async def run():
                self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
                await self.run()

            with suppress(asyncio.CancelledError):
                asyncio.run(run())

        self.thread = threading.Thread(target=main, name="public-market-streams", daemon=True)
        self.thread.start()

    def stop(self):
        if self.loop and not self.loop.is_closed() and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:
            self.thread.join(timeout=10)
