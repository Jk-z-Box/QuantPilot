"""CPU hot-path changes must preserve fresh reads, transaction isolation and protection."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from quantpilot.store import Store


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SQLite file locks")
def test_multiple_stores_preserve_cross_process_database_locks(tmp_path):
    first = Store(tmp_path)
    second = Store(tmp_path)
    # A raw open/close of an existing DB releases all POSIX locks owned by this
    # process. A separate process must still be unable to take SQLite's shared
    # lock range exclusively after another Store is initialized.
    result = subprocess.run([  # noqa: S603 -- fixed Python probe and pytest-owned DB path
        sys.executable, "-c", """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 510, 0x40000002)
except BlockingIOError:
    sys.exit(0)
else:
    sys.exit(1)
finally:
    os.close(fd)
""", str(first.path),
    ], timeout=5, check=False)
    assert result.returncode == 0
    with second.transaction() as db:
        second.write(db, "probe", 7)
    assert first.get("probe") == 7


def test_pool_reuses_connection_without_caching_values(tmp_path):
    store = Store(tmp_path)
    with store.connect() as first:
        store.write(first, "probe", {"value": 1})
    with store.connect() as second:
        assert first is second
    # External commits and deletes are visible immediately, including logout/password changes.
    with sqlite3.connect(store.path) as external:
        external.execute("UPDATE kv SET value=? WHERE key=?", (json.dumps({"value": 2}), "probe"))
    assert store.get("probe") == {"value": 2}
    value = store.get("probe")
    value["value"] = 7
    assert store.get("probe")["value"] == 2


def test_pool_rollback_and_nested_connection_isolation(tmp_path):
    store = Store(tmp_path)
    with store.transaction() as db:
        store.write(db, "probe", 1)
    with pytest.raises(ValueError):
        with store.transaction() as outer:
            store.write(outer, "probe", 2)
            with store.connect() as inner:
                assert inner is not outer
                assert store.read(inner, "probe") == 1
            raise ValueError("rollback")
    assert store.get("probe") == 1
    with store.transaction() as db:
        store.write(db, "probe", 3)
    assert store.get("probe") == 3


def test_pool_exclusive_concurrent_checkout_and_bounded_idle(tmp_path):
    store = Store(tmp_path)
    barrier = threading.Barrier(12)
    identities = set()
    lock = threading.Lock()

    def worker(_):
        with store.connect() as db:
            with lock:
                assert id(db) not in identities
                identities.add(id(db))
            barrier.wait(timeout=5)
            assert store.read(db, "kill") is False

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(12)))
    assert store._connections.qsize() == 8
    assert len(identities) == 12


def test_get_many_is_fresh_and_parameterized(tmp_path):
    store = Store(tmp_path)
    weird = "x'); DROP TABLE kv;--"
    with store.transaction() as db:
        store.write(db, weird, 4)
    assert store.get_many([weird, "kill"]) == {weird: 4, "kill": False}
    assert store.get_many([]) == {}
    with pytest.raises(KeyError):
        store.get_many(["kill", "missing"])


def test_fast_tick_reuses_fresh_stream_quotes_and_still_runs_protection(workspace, quote, monkeypatch):
    item = workspace.store.get("v2_watchlist")[0]
    workspace.save_watchlist([item])
    workspace.streams.update_quote(item, {**quote, "timestamp": quote["timestamp"] * 1000}, {})
    calls = []
    monkeypatch.setattr(workspace.market, "quote", lambda _: pytest.fail("fresh stream must not refetch"))
    monkeypatch.setattr(
        workspace.logic, "current", lambda: pytest.fail("fast quote must not load full strategy")
    )
    monkeypatch.setattr(workspace, "execute", lambda: calls.append("protect"))
    monkeypatch.setattr(workspace.position_reviews, "scheduled", lambda: None)
    workspace.fast_tick()
    assert calls == ["protect"]
    assert workspace.streams.changed.is_set()


def test_quote_only_scan_does_not_load_strategy(workspace, monkeypatch):
    item = workspace.store.get("v2_watchlist")[0]
    monkeypatch.setattr(workspace.logic, "current", lambda: pytest.fail("unused strategy read"))
    workspace.scan(item, False, workspace.store.get("v2_preferences"))
    assert workspace.quotes[item["id"]]["mid"] > 0


def test_no_browser_skips_display_valuation_but_subscription_gets_snapshot(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(workspace.funds, "packets", lambda: calls.append(1) or [{"id": "sample"}])
    workspace.funds.publish()
    assert not calls

    async def check():
        queue = workspace.funds.subscribe()
        result = await asyncio.wait_for(queue.get(), 1)
        assert result["accounts"] == [{"id": "sample"}]
        workspace.funds.unsubscribe(queue)

    asyncio.run(check())
    assert len(calls) == 1
