from __future__ import annotations

import fcntl
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .admin_api import install as install_admin
from .config import Settings, config_path, load_settings, verify_password
from .market import MarketError
from .store import Store, Vault
from .v2.api import install as install_v2
from .v2.service import Workspace


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictModel):
    username: str = Field(max_length=64)
    password: SecretStr = Field(max_length=256)


class Switch(StrictModel):
    enabled: bool


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(settings: Settings | None = None, directory: Path | None = None, market=None, background=True):
    settings = settings or load_settings()
    store = Store(directory or config_path().parent, settings.initial_cash)
    vault = Vault(store)
    from .retired import retire_console

    retire_console(store)
    workspace = Workspace(store, vault, settings)

    @asynccontextmanager
    async def lifespan(app):
        lock = open(store.directory / "server.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Only one QuantPilot server process is allowed") from None
        try:
            if background:
                workspace.start()
            yield
        finally:
            workspace.stop()
            lock.close()

    app = FastAPI(title="QuantPilot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.vault = store, vault
    app.state.workspace = workspace
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(settings.public_origin).hostname, "127.0.0.1", "localhost"],
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get("origin") != settings.public_origin:
                return JSONResponse({"detail": "origin_rejected"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "-1"))
            except ValueError:
                length = -1
            max_body = 131072 if request.url.path == "/api/v2/chatgpt/pair/complete" else 16384
            if request.url.path == "/api/v2/logic/import":
                max_body = 1048576
            if not 0 <= length <= max_body or request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "invalid_body_size"}, status_code=413)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "json_required"}, status_code=415)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store, no-transform"
                if request.url.path in ("/api/v2/account-events", "/api/v2/realtime-events")
                else "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' "
                + settings.public_origin.replace("https://", "wss://").replace("http://", "ws://")
                + "; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        # FastAPI's default errors echo rejected input, which may be a password or API key.
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    @app.exception_handler(MarketError)
    async def market_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    def auth_settings():
        path = store.directory / "config.json"
        return load_settings(path) if path.exists() else settings

    def session(request: Request):
        token = request.cookies.get("qp_session", "")
        token_hash = digest(token + auth_settings().password_hash)
        with store.connect() as db:
            row = db.execute("SELECT csrf,expires FROM sessions WHERE token=?", (token_hash,)).fetchone()
        if not row or row["expires"] <= time.time():
            raise HTTPException(401, "authentication_required")
        return dict(row)

    def mutation(request: Request, current=Depends(session)):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), current["csrf"]):
            raise HTTPException(403, "csrf_rejected")
        return current

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": "0.14.0", "mode": "paper-first"}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        now = time.time()
        peer = request.client.host if request.client else "unknown"
        with store.transaction() as db:
            db.execute("DELETE FROM attempts WHERE created < ?", (now - 900,))
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE peer=?", (peer,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            if count >= 10 or total >= 100:
                raise HTTPException(429, "login_rate_limited")
            # Reserve an attempt before hashing so concurrent requests cannot evade the limit.
            db.execute("INSERT INTO attempts VALUES (?,?)", (now, peer))
        auth = auth_settings()
        password_ok = verify_password(body.password.get_secret_value(), auth.password_hash)
        if not (secrets.compare_digest(body.username.encode(), auth.username.encode()) and password_ok):
            store.record("login_failed", {})
            raise HTTPException(401, "invalid_credentials")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.transaction() as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)", (digest(token + auth.password_hash), csrf, now + 28800)
            )
            db.execute(
                "DELETE FROM sessions WHERE token NOT IN (SELECT token FROM sessions ORDER BY expires DESC LIMIT 20)"
            )
            # Keep successful login churn bounded; sessions are not counted as failed attempts.
            db.execute("DELETE FROM attempts WHERE peer=?", (peer,))
            store.audit(db, "login", {})
        response.set_cookie(
            "qp_session",
            token,
            max_age=28800,
            secure=settings.secure_cookie,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"csrf": csrf, "username": auth.username}

    @app.get("/api/session")
    def get_session(current=Depends(session)):
        return {"csrf": current["csrf"], "username": auth_settings().username}

    @app.post("/api/logout", dependencies=[Depends(mutation)])
    def logout(request: Request, response: Response):
        with store.transaction() as db:
            db.execute(
                "DELETE FROM sessions WHERE token=?",
                (digest(request.cookies.get("qp_session", "") + auth_settings().password_hash),),
            )
        response.delete_cookie(
            "qp_session", path="/", secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.get("/api/audit", dependencies=[Depends(session)])
    def audit():
        return [{**row, "detail": json.loads(row["detail"])} for row in store.recent("audit")]

    @app.post("/api/kill", dependencies=[Depends(mutation)])
    def kill(body: Switch):
        store.set_kill(body.enabled)
        return {"enabled": body.enabled}

    install_v2(app, workspace, session, mutation)
    install_admin(app, store, settings, auth_settings, session, mutation)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "workspace.html")

    return app
