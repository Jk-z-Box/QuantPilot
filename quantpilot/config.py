from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

EXCHANGES = ("binance", "okx", "bybit", "kraken", "coinbase")
SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password_hash: str = Field(repr=False)
    host: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"  # noqa: S104
    port: int = Field(default=8080, ge=1024, le=65535)
    public_origin: str = "http://127.0.0.1:8080"
    tls_cert: str | None = None
    tls_key: str | None = None
    initial_cash: str = "10000"
    max_order: str = "500"
    max_exposure: str = "3000"
    max_position: str = "1500"
    daily_loss_limit: str = "300"
    max_trade_loss: str = "50"
    fee_bps: int = Field(default=10, ge=0, le=100)
    slippage_bps: int = Field(default=5, ge=0, le=100)
    max_spread_bps: int = Field(default=30, ge=1, le=500)
    max_quote_age: int = Field(default=30, ge=1, le=60)
    symbols: list[str] = Field(default_factory=lambda: list(SYMBOLS), min_length=1, max_length=20)
    exchange: Literal["binance", "okx", "bybit", "kraken", "coinbase"] = "okx"
    market_mode: Literal["public", "demo"] = "public"
    openai_model: str = ""
    openai_enabled: bool = False
    live_enabled: bool = False

    @model_validator(mode="after")
    def validate_settings(self):
        from decimal import Decimal, InvalidOperation

        for field in ("initial_cash", "max_order", "max_exposure", "max_position", "daily_loss_limit", "max_trade_loss"):
            try:
                value = Decimal(getattr(self, field))
                if not value.is_finite() or value <= 0 or value > Decimal("1000000000"):
                    raise ValueError("Risk limits must be finite and positive")
            except InvalidOperation as exc:
                raise ValueError("Invalid risk limit") from exc
        if self.live_enabled:
            raise ValueError("Live trading is not certified in this release; paper only")
        if not all(re.fullmatch(r"[A-Z0-9]{2,12}/USDT", s) for s in self.symbols):
            raise ValueError("Only allowlisted USDT spot symbols are supported")
        origin = urlsplit(self.public_origin)
        if origin.username or origin.password or origin.path or origin.query or origin.fragment or not origin.hostname:
            raise ValueError("Use a bare origin without path or credentials")
        if origin.scheme != "https" and not (
            origin.scheme == "http" and origin.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError("Remote access requires HTTPS")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("TLS certificate and key must be supplied together")
        return self

    @property
    def secure_cookie(self) -> bool:
        return self.public_origin.startswith("https://")


def config_path() -> Path:
    return Path(os.getenv("QP_CONFIG", "data/config.json")).resolve()


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_settings(path: Path, changes: dict, fallback: Settings | None = None):
    """Serialize read/modify/write with CLI password rotations and Web settings."""
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = load_settings(path) if path.exists() else fallback
        if saved is None:
            raise ValueError("Missing config")
        updated = Settings.model_validate({**saved.model_dump(), **changes})
        atomic_json(path, updated.model_dump())
        return updated


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12–256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt + digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 256:
        return False
    try:
        raw = base64.b64decode(encoded.removeprefix("scrypt$"), validate=True)
        expected = hashlib.scrypt(password.encode(), salt=raw[:16], n=2**14, r=8, p=1)
        return hmac.compare_digest(expected, raw[16:])
    except (ValueError, TypeError):
        return False


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        raise ValueError("Run qp init before starting the server")
    if path.stat().st_mode & 0o077:
        raise ValueError("Config permissions must be 0600")
    if os.getenv("QP_LIVE_ENABLED", "false").lower() != "false":
        raise ValueError("Live trading is locked in this release")
    return Settings.model_validate_json(path.read_text())
