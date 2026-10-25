from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: Path, initial_cash: str = "10000"):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "quantpilot.db"
        # Create privately before SQLite can create journals or WAL sidecars.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (created REAL NOT NULL, peer TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created);
                CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, encrypted BLOB NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO kv VALUES (?,?)", ("account", json.dumps({
                "cash": initial_cash, "positions": {}, "realized_pnl": "0", "fees": "0",
                "day": "", "day_equity": initial_cash,
            })))
            db.execute("INSERT OR IGNORE INTO kv VALUES ('kill', 'false')")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('strategy', ?)", (json.dumps({
                "enabled": False, "exchange": "okx", "symbol": "BTC/USDT", "timeframe": "1h",
                "budget": "100", "stop_pct": "0.02", "take_pct": "0.04",
            }),))
            db.execute("INSERT OR IGNORE INTO kv VALUES ('schema_version', '1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def read(db, key):
        return json.loads(db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])

    @staticmethod
    def write(db, key, value):
        db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key):
        with self.connect() as db:
            return self.read(db, key)

    @staticmethod
    def audit(db, event: str, detail: dict):
        # Call sites pass explicit allowlisted fields, never request bodies or exception messages.
        db.execute("INSERT INTO audit(created,event,detail) VALUES (?,?,?)",
                   (time.time(), event, json.dumps(detail)))

    def record(self, event, detail):
        with self.transaction() as db:
            self.audit(db, event, detail)

    def recent(self, table: str, limit=100):
        if table not in ("audit", "orders"):
            raise ValueError("Unknown table")
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY created DESC LIMIT ?", (limit,)).fetchall()  # noqa: S608
            return [dict(row) for row in rows]

    def set_kill(self, enabled: bool):
        with self.transaction() as db:
            self.write(db, "kill", enabled)
            if enabled:
                config = self.read(db, "strategy")
                config["enabled"] = False
                self.write(db, "strategy", config)
            self.audit(db, "kill_switch", {"enabled": enabled})


class Vault:
    def __init__(self, store: Store):
        self.store = store
        key_path = store.directory / "master.key"
        if not key_path.exists():
            with store.connect() as db:
                if db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]:
                    raise ValueError("Existing credentials require the original master key; restore a backup")
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
                f.flush()
                os.fsync(f.fileno())
        if key_path.stat().st_mode & 0o077:
            raise ValueError("Master key permissions must be 0600")
        self.fernet = Fernet(key_path.read_bytes())

    def put(self, name: str, credentials: dict):
        encrypted = self.fernet.encrypt(json.dumps(credentials).encode())
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)", (name, encrypted))
            self.store.audit(db, "credential_saved", {"name": name})

    def get(self, name: str):
        with self.store.connect() as db:
            row = db.execute("SELECT encrypted FROM credentials WHERE name=?", (name,)).fetchone()
        return json.loads(self.fernet.decrypt(row[0])) if row else None

    def delete(self, name: str):
        with self.store.transaction() as db:
            db.execute("DELETE FROM credentials WHERE name=?", (name,))
            self.store.audit(db, "credential_deleted", {"name": name})

    def names(self):
        with self.store.connect() as db:
            return [row[0] for row in db.execute("SELECT name FROM credentials ORDER BY name")]
