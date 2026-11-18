"""One-time removal of the retired spot console, never current v2 trading data."""

import json
import time


def retire_console(store):
    keys = ("account", "strategy", "last_advice")
    with store.connect() as db:
        present = db.execute(
            "SELECT 1 FROM kv WHERE key IN (SELECT value FROM json_each(?))", (json.dumps(keys),)
        ).fetchone()
        old_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        old_credentials = db.execute(
            "SELECT 1 FROM credentials WHERE name IN ('binance','okx','bybit','kraken','coinbase','openai')"
        ).fetchone()
    if not present and not old_table and not old_credentials:
        return
    # Backup the consistent database before deleting legacy records. Shared login,
    # audit, kill switch and v2:* credentials belong to the current console.
    from .cli import backup

    config = store.directory / "config.json"
    if config.exists() and (store.directory / "master.key").exists():
        backup(config, store.directory / "backups" / f"before-v014-cleanup-{time.time_ns()}.tar.gz")
    with store.transaction() as db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("DELETE FROM kv WHERE key IN (SELECT value FROM json_each(?))", (json.dumps(keys),))
        db.execute("DROP TABLE IF EXISTS orders")
        db.execute(
            "DELETE FROM credentials WHERE name IN ('binance','okx','bybit','kraken','coinbase','openai')"
        )
        store.audit(db, "retired_console_removed", {"current_workspace_preserved": True})
