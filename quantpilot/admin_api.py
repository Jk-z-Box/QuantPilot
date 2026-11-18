from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field, SecretStr

from . import maintenance
from .config import hash_password, update_settings, verify_password
from .v2.models import Model


class PasswordCheck(Model):
    current_password: SecretStr = Field(min_length=1, max_length=256)


class Identity(PasswordCheck):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    new_password: SecretStr = Field(default=SecretStr(""), max_length=256)


class Update(PasswordCheck):
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class Display(Model):
    refresh_seconds: int = Field(default=5, ge=3, le=60, strict=True)


def install(app, store, settings, auth_settings, session, mutation):
    def reauthenticate(password, request):
        now = time.time()
        peer = "reauth:" + (request.client.host if request.client else "unknown")
        with store.transaction() as db:
            db.execute("DELETE FROM attempts WHERE created < ?", (now - 900,))
            if db.execute("SELECT COUNT(*) FROM attempts WHERE peer=?", (peer,)).fetchone()[0] >= 5:
                raise HTTPException(429, "验证次数过多，请 15 分钟后再试")
            db.execute("INSERT INTO attempts VALUES (?,?)", (now, peer))
        auth = auth_settings()
        if not verify_password(password.get_secret_value(), auth.password_hash):
            raise HTTPException(403, "当前密码不正确")
        with store.transaction() as db:
            db.execute("DELETE FROM attempts WHERE peer=?", (peer,))
        return auth

    @app.get("/api/v2/admin", dependencies=[Depends(session)])
    def admin():
        with store.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key='display_settings'").fetchone()
            display = store.read(db, "display_settings") if row else Display().model_dump()
        return {
            "username": auth_settings().username,
            "version": maintenance.VERSION,
            "origin": settings.public_origin,
            "port": settings.port,
            "host": settings.host,
            "display": display,
            "update": maintenance.status(),
            "channel": maintenance.BRANCH,
        }

    @app.put("/api/v2/admin/identity", dependencies=[Depends(mutation)])
    def identity(body: Identity, request: Request, response: Response):
        auth = reauthenticate(body.current_password, request)
        changes = {"username": body.username}
        if body.new_password.get_secret_value():
            try:
                changes["password_hash"] = hash_password(body.new_password.get_secret_value())
            except ValueError:
                raise HTTPException(422, "新密码须为 12–256 个字符") from None
        try:
            update_settings(
                store.directory / "config.json", changes, auth, expected_password_hash=auth.password_hash
            )
        except ValueError:
            raise HTTPException(409, "账号已变更，请重新登录后操作") from None
        with store.transaction() as db:
            db.execute("DELETE FROM sessions")
            store.audit(db, "admin_identity_changed", {"password_changed": "password_hash" in changes})
        response.delete_cookie(
            "qp_session", path="/", secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return {"saved": True, "login_required": True}

    @app.put("/api/v2/admin/display", dependencies=[Depends(mutation)])
    def display(body: Display):
        with store.transaction() as db:
            store.write(db, "display_settings", body.model_dump())
        return body.model_dump()

    @app.post("/api/v2/admin/update/check", dependencies=[Depends(mutation)])
    def check_update():
        try:
            return maintenance.latest()
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/v2/admin/update", dependencies=[Depends(mutation)])
    def update(body: Update, request: Request):
        reauthenticate(body.current_password, request)
        try:
            return maintenance.queue_update(store, body.commit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
