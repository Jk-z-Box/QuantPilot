from __future__ import annotations

import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import Field, SecretStr

from .ai import list_models
from .models import Account, AccountSecrets, Evidence, Instrument, KeyInput, Model, Preferences, Reset


class Watchlist(Model):
    items: list[Instrument] = Field(min_length=1, max_length=20)


class Close(Model):
    instrument_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    ratio: float = Field(default=1, gt=0, le=1)


class ChatGPTProfile(Model):
    profile_id: str = Field(default="", pattern=r"^[a-f0-9]{0,24}$")


class OAuthTokens(Model):
    access_token: SecretStr = Field(min_length=1, max_length=32768)
    refresh_token: SecretStr = Field(min_length=1, max_length=32768)
    id_token: SecretStr = Field(min_length=1, max_length=32768)
    token_type: Literal["Bearer", "bearer"]
    expires_in: float = Field(gt=0, le=86400)
    scope: str = Field(max_length=4096)
    earliest_refresh_at: float = Field(default=0, ge=0)


class OAuthImport(Model):
    client_id: str = Field(pattern=r"^oaiapp_[A-Za-z0-9_-]{1,240}$")
    tokens: OAuthTokens


def install(app, workspace, session, mutation):
    def safe(fn, *args):
        try:
            return fn(*args)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v2/state", dependencies=[Depends(session)])
    def state():
        return workspace.state()

    @app.get("/api/v2/models", dependencies=[Depends(session)])
    def models(provider: Literal["api_key", "chatgpt", "codex"] = "api_key"):
        return safe(lambda: list_models(workspace.vault, provider=provider))

    @app.post("/api/v2/codex/{action}", dependencies=[Depends(mutation)])
    def codex_action(action: Literal["login", "cancel", "logout", "activate"]):
        return safe(workspace.codex_action, action)

    @app.post("/api/v2/chatgpt/pair", dependencies=[Depends(mutation)])
    def start_pair(body: ChatGPTProfile):
        return safe(workspace.chatgpt.start_pair, body.profile_id)

    def pair_code(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "连接码缺失")
        return header[7:]

    @app.post("/api/v2/chatgpt/pair/context")
    def pair_context(request: Request):
        return safe(workspace.chatgpt.pair_context, pair_code(request))

    @app.post("/api/v2/chatgpt/pair/complete")
    def complete_pair(body: OAuthImport, request: Request):
        tokens = body.tokens.model_dump()
        for k in ("access_token", "refresh_token", "id_token"):
            tokens[k] = tokens[k].get_secret_value()
        result = safe(workspace.chatgpt.complete_pair, pair_code(request), body.client_id, tokens)
        workspace.auth_changed()
        return result

    @app.post("/api/v2/chatgpt/select", dependencies=[Depends(mutation)])
    def select_profile(body: ChatGPTProfile):
        result = safe(workspace.chatgpt.select, body.profile_id)
        workspace.auth_changed()
        return result

    @app.post("/api/v2/chatgpt/resume", dependencies=[Depends(mutation)])
    def resume_chatgpt():
        result = safe(workspace.chatgpt.resume)
        workspace.auth_changed()
        return result

    @app.delete("/api/v2/chatgpt/profiles/{profile_id}", dependencies=[Depends(mutation)])
    def disconnect_profile(profile_id: str):
        result = safe(workspace.chatgpt.disconnect, profile_id)
        workspace.auth_changed()
        return result

    @app.get("/api/v2/limits/{instrument_id}", dependencies=[Depends(session)])
    def limits(instrument_id: str, exchange: str = "binance", account_id: str = ""):
        return safe(workspace.limits, instrument_id, exchange, account_id)

    @app.put("/api/v2/watchlist", dependencies=[Depends(mutation)])
    def watch(body: Watchlist):
        return safe(workspace.save_watchlist, [i.model_dump() for i in body.items])

    @app.get("/api/v2/markets", dependencies=[Depends(session)])
    def markets(exchange: str = "binance", q: str = ""):
        if exchange not in ("binance", "okx", "gate") or len(q) > 30:
            raise HTTPException(422, "参数无效")
        try:
            return workspace.market.markets(exchange, q)
        except Exception:
            raise HTTPException(503, "无法读取交易所合约目录，请检查网络与地区可用性") from None

    @app.get("/api/v2/chart/{instrument_id}", dependencies=[Depends(session)])
    def chart(instrument_id: str, frame: str = "1h"):
        item = next((i for i in workspace.store.get("v2_watchlist") if i["id"] == instrument_id), None)
        if not item:
            raise HTTPException(404, "交易对不存在")
        try:
            return {
                "candles": workspace.market.candles(item, frame),
                "frame": frame,
                "fetched_at": time.time(),
                "demo": workspace.market.demo,
            }
        except Exception:
            raise HTTPException(503, "K 线暂不可用；请检查合约、周期和行情连接") from None

    @app.put("/api/v2/preferences", dependencies=[Depends(mutation)])
    def preferences(body: Preferences):
        return safe(workspace.preferences, body.model_dump())

    @app.post("/api/v2/accounts", dependencies=[Depends(mutation)])
    def account(body: Account):
        return safe(workspace.save_account, body.model_dump())

    @app.delete("/api/v2/accounts/{aid}", dependencies=[Depends(mutation)])
    def delete_account(aid: str):
        return safe(workspace.delete_account, aid)

    @app.post("/api/v2/accounts/{aid}/credentials", dependencies=[Depends(mutation)])
    def credentials(aid: str, body: AccountSecrets):
        return safe(
            workspace.save_credentials,
            aid,
            {
                "apiKey": body.api_key.get_secret_value(),
                "secret": body.secret.get_secret_value(),
                "password": body.passphrase.get_secret_value(),
            },
        )

    @app.post("/api/v2/accounts/{aid}/check", dependencies=[Depends(mutation)])
    def check(aid: str):
        a = workspace.store.get("v2_accounts").get(aid)
        if not a or a["kind"] == "paper":
            raise HTTPException(404, "请选择交易所账户")
        try:
            result = workspace.exchange.reconcile(a)
            with workspace.store.transaction() as db:
                external = workspace.store.read(db, "v2_external")
                external[aid] = result
                workspace.store.write(db, "v2_external", external)
            return result
        except Exception:
            raise HTTPException(503, "账户读取失败；请核对密钥、权限、交易所及测试/实盘环境") from None

    @app.post("/api/v2/accounts/{aid}/reset", dependencies=[Depends(mutation)])
    def reset(aid: str, body: Reset):
        safe(workspace.reset, aid, body.capital)
        return {"reset": True}

    @app.post("/api/v2/accounts/{aid}/close", dependencies=[Depends(mutation)])
    def close(aid: str, body: Close):
        a = workspace.store.get("v2_accounts").get(aid)
        if not a or a["kind"] != "paper":
            raise HTTPException(409, "此入口仅操作模拟仓位；交易所仓位请使用原生保护单并核对")
        return safe(
            workspace.paper.close,
            aid,
            body.instrument_id,
            workspace.quotes.get(body.instrument_id),
            body.ratio,
        )

    @app.post("/api/v2/keys/{provider}", dependencies=[Depends(mutation)])
    def key(provider: str, body: KeyInput):
        if provider not in ("openai", "coinglass", "finnhub"):
            raise HTTPException(404, "服务不存在")
        workspace.vault.put(
            "v2:" + ("ai:" if provider == "openai" else "feed:") + provider,
            {"apiKey": body.api_key.get_secret_value()},
        )
        workspace.feeds.cache.clear()
        if provider == "openai":
            if workspace.store.get("v2_preferences")["ai_provider"] == "api_key":
                workspace.auth_changed()
        return {"saved": True}

    @app.delete("/api/v2/keys/{provider}", dependencies=[Depends(mutation)])
    def delete_key(provider: str):
        if provider not in ("openai", "coinglass", "finnhub"):
            raise HTTPException(404, "服务不存在")
        workspace.delete_key(provider)
        return {"deleted": True}

    @app.post("/api/v2/evidence", dependencies=[Depends(mutation)])
    def evidence(body: Evidence):
        if body.published_at > time.time() + 5 or body.expires_at - time.time() > 7 * 86400:
            raise HTTPException(422, "来源时间无效，有效期最多 7 天")
        if body.instrument_id not in {i["id"] for i in workspace.store.get("v2_watchlist")}:
            raise HTTPException(404, "交易对不存在")
        with workspace.store.transaction() as db:
            rows = workspace.store.read(db, "v2_evidence")
            rows = [
                r
                for r in rows
                if r["expires_at"] > time.time()
                and (r["instrument_id"], r["category"], r["source"])
                != (body.instrument_id, body.category, body.source)
            ]
            workspace.store.write(db, "v2_evidence", (rows + [body.model_dump()])[-100:])
            workspace.store.audit(
                db, "v2_evidence_import", {"category": body.category, "instrument_id": body.instrument_id}
            )
        workspace.last_evaluation = 0
        return {"saved": True, "verification": "user_supplied"}

    @app.post("/api/v2/ai/review", dependencies=[Depends(mutation)])
    def ai_review():
        return safe(workspace.review_ai)

    @app.post("/api/v2/ai/data", dependencies=[Depends(mutation)])
    def ai_data():
        return safe(workspace.research.request_now)
