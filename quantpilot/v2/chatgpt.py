"""Official Sign in with ChatGPT for a user's self-hosted QuantPilot instance.

OAuth takes place on the browser's computer. A single-use pairing grant imports
validated credentials over HTTPS. Only this server then rotates the session.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import uuid

import httpx
import jwt

from .ai_errors import AIError

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
REVOKE = ISSUER + "/api/accounts/oauth/revoke"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
NAME = "v2:ai:chatgpt"
CLIENT_ID = re.compile(r"oaiapp_[A-Za-z0-9_-]{1,240}")
TERMINAL = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}
_GUARD = threading.RLock()


def validate_identity(token, client_id, nonce=None, transport=None):
    """Verify only RS256 against OpenAI's fixed JWKS, never a token-supplied URL."""
    try:
        if not CLIENT_ID.fullmatch(client_id) or not isinstance(token, str) or len(token) > 32768:
            raise ValueError("registration")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("algorithm")
        with httpx.Client(timeout=12, transport=transport, follow_redirects=False) as client:
            response = client.get(JWKS)
            response.raise_for_status()
            keys = [
                k
                for k in response.json()["keys"]
                if k.get("kid") == header["kid"]
                and k.get("kty") == "RSA"
                and k.get("use", "sig") == "sig"
                and k.get("alg", "RS256") == "RS256"
            ]
        if len(keys) != 1:
            raise ValueError("key")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0]).key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=client_id,
            leeway=5,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("subject")
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("nonce")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
            raise ValueError("authorized party")
        email = claims.get("email", "")
        return {
            "issuer": ISSUER,
            "subject": claims["sub"],
            "email": email[:254] if isinstance(email, str) else "",
        }
    except httpx.RequestError:
        raise AIError("network") from None
    except Exception:
        raise AIError("oauth_validation") from None


def token_record(body, client_id, identity, previous=None):
    previous = previous or {}
    try:
        if body.get("token_type", "").lower() != "bearer":
            raise ValueError("token type")
        for key in ("access_token", "refresh_token"):
            if not isinstance(body.get(key), str) or not 1 <= len(body[key]) <= 32768:
                raise ValueError("missing token")
        expires = float(body["expires_in"])
        earliest = float(body.get("earliest_refresh_at", 0))
        if (
            not math.isfinite(expires)
            or not 0 < expires <= 86400
            or not math.isfinite(earliest)
            or earliest < 0
        ):
            raise ValueError("expiry")
        scope = body.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise ValueError("scope")
        scopes = scope.split() if scope is not None else previous.get("scopes", [])
        return {
            **identity,
            "client_id": client_id,
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "id_token": body.get("id_token", previous.get("id_token", "")),
            "scopes": scopes,
            "expires_at": time.time() + expires,
            "earliest_refresh_at": earliest,
            "saved_at": time.time(),
            "generation": previous.get("generation", secrets.token_hex(12)),
            "paused": None,
        }
    except (ValueError, KeyError, TypeError):
        raise AIError("oauth_validation") from None


class ChatGPTConnection:
    def __init__(self, vault, transport=None):
        self.vault, self.store, self.transport = vault, vault.store, transport
        with self.lock():
            if not vault.get(NAME):
                self.save(
                    {"host_id": "urn:uuid:" + str(uuid.uuid4()), "profiles": {}, "active": None, "pair": None}
                )

    @contextlib.contextmanager
    def lock(self):
        # Both monitor threads and any CLI process share the same refresh lock.
        with _GUARD:
            fd = os.open(self.store.directory / "chatgpt.lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self):
        return self.vault.get(NAME)

    def save(self, state, reset=False):
        if reset:
            state["revision"] = secrets.token_hex(12)
        with self.store.transaction() as db:
            encrypted = self.vault.fernet.encrypt(json.dumps(state).encode())
            db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)", (NAME, encrypted))
            if reset:
                prefs = self.store.read(db, "v2_preferences")
                prefs.update(
                    ai_provider="chatgpt",
                    ai_enabled=False,
                    ai_intervene=False,
                    ai_data_enabled=False,
                    ai_model="",
                )
                self.store.write(db, "v2_preferences", prefs)
                accounts = self.store.read(db, "v2_accounts")
                for account in accounts.values():
                    account["auto"] = False
                self.store.write(db, "v2_accounts", accounts)
                self.store.audit(db, "v2_chatgpt_connection_changed", {"automation_paused": True})

    def status(self):
        state = self.load()
        profiles = []
        for pid, p in state["profiles"].items():
            profiles.append(
                {
                    "id": pid,
                    "label": (p.get("email") or "ChatGPT 账户") + " · " + pid[:6],
                    "connected": bool(p.get("refresh_token")),
                    "plan_access": PLAN_SCOPES.issubset(p.get("scopes", [])) and bool(p.get("refresh_token")),
                    "paused": p.get("paused"),
                }
            )
        active = next((p for p in profiles if p["id"] == state["active"]), None)
        return {
            "profiles": profiles,
            "active": state["active"],
            "ready": bool(active and active["plan_access"] and not active["paused"]),
            "revision": state.get("revision", "initial"),
            "usage_url": "https://chatgpt.com/settings/usage",
        }

    def fingerprint(self):
        state = self.load()
        p = state["profiles"].get(state["active"], {})
        return (state["active"], p.get("generation"), p.get("paused"))

    def start_pair(self, profile_id=""):
        with self.lock():
            state = self.load()
            if profile_id and profile_id not in state["profiles"]:
                raise ValueError("请选择已保存的 ChatGPT 账户")
            if not profile_id and len(state["profiles"]) >= 20:
                raise ValueError("最多保存 20 个 ChatGPT 账户注册")
            code = secrets.token_urlsafe(32)
            state["pair"] = {
                "digest": hashlib.sha256(code.encode()).hexdigest(),
                "nonce": secrets.token_urlsafe(32),
                "expires_at": time.time() + 600,
                "profile_id": profile_id,
            }
            self.save(state)
            return {"pair_code": code, "expires_at": state["pair"]["expires_at"]}

    def pair(self, state, code):
        p = state.get("pair")
        if (
            not isinstance(code, str)
            or not 40 <= len(code) <= 64
            or not p
            or p["expires_at"] <= time.time()
            or not secrets.compare_digest(p["digest"], hashlib.sha256(code.encode()).hexdigest())
        ):
            raise ValueError("连接码无效或已过期，请在网页重新开始授权")
        return p

    def pair_context(self, code):
        with self.lock():
            state = self.load()
            pair = self.pair(state, code)
            p = state["profiles"].get(pair["profile_id"], {})
            return {
                "nonce": pair["nonce"],
                "client_id": p.get("client_id"),
                "identity": {k: p.get(k) for k in ("issuer", "subject")} if p else None,
                "login_hint": p.get("email"),
                "id_token_hint": p.get("id_token"),
            }

    def complete_pair(self, code, client_id, tokens):
        with self.lock():
            state = self.load()
            pair = self.pair(state, code)
            old = state["profiles"].get(pair["profile_id"])
            if old and client_id != old["client_id"]:
                raise AIError("oauth_validation")
            identity = validate_identity(tokens.get("id_token"), client_id, pair["nonce"], self.transport)
            if old and any(identity[k] != old[k] for k in ("subject", "issuer")):
                raise AIError("oauth_validation")
            p = token_record(tokens, client_id, identity)
            pid = hashlib.sha256((client_id + "\0" + identity["subject"]).encode()).hexdigest()[:24]
            state["profiles"][pid] = p
            state["active"], state["pair"] = pid, None
            self.save(state, reset=True)
        return self.status()

    def select(self, pid):
        with self.lock():
            state = self.load()
            p = state["profiles"].get(pid)
            if not p or not p.get("refresh_token"):
                raise AIError("oauth_login")
            state["active"] = pid
            state["pair"] = None
            self.save(state, reset=True)
        return self.status()

    def resume(self):
        with self.lock():
            state = self.load()
            p = state["profiles"].get(state["active"], {})
            if not p.get("refresh_token"):
                raise AIError("oauth_login")
            p["paused"] = None
            p.pop("retry_after", None)
            p["generation"] = secrets.token_hex(12)
            self.save(state)
        return self.status()

    def note_error(self, error, fingerprint):
        if error.kind not in ("plan_limit", "plan_eligibility", "plan_permission", "oauth_login"):
            return
        with self.lock():
            if fingerprint != self.fingerprint():
                return
            state = self.load()
            state["profiles"][state["active"]]["paused"] = error.kind
            self.save(state)

    def access(self):
        with self.lock():
            state = self.load()
            pid = state["active"]
            p = state["profiles"].get(pid, {})
            if not p.get("refresh_token"):
                raise AIError("oauth_login")
            if p.get("paused"):
                raise AIError(p["paused"])
            if not PLAN_SCOPES.issubset(p.get("scopes", [])):
                raise AIError("plan_permission")
            if p["expires_at"] > time.time() + 60:
                return p["access_token"]
            if time.time() < max(p.get("earliest_refresh_at", 0), p.get("retry_after", 0)):
                if p["expires_at"] > time.time() + 5:
                    return p["access_token"]
                raise AIError("oauth_temporary")
            try:
                with httpx.Client(timeout=15, transport=self.transport, follow_redirects=False) as client:
                    response = client.post(
                        TOKEN,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": p["client_id"],
                            "refresh_token": p["refresh_token"],
                            "resource": RESOURCE,
                        },
                    )
                if not response.is_success:
                    body = response.json()
                    error = body.get("error")
                    code = error.get("code") if isinstance(error, dict) else error
                    if code in TERMINAL:
                        self.clear_tokens(p)
                        p["paused"] = "oauth_login"
                        self.save(state)
                        raise AIError("oauth_login")
                    raise AIError("oauth_temporary")
                body = response.json()
                identity = {k: p[k] for k in ("issuer", "subject", "email")}
                if body.get("id_token"):
                    identity = validate_identity(body["id_token"], p["client_id"], transport=self.transport)
                    if any(identity[k] != p[k] for k in ("issuer", "subject")):
                        raise AIError("oauth_validation")
                updated = token_record(body, p["client_id"], identity, p)
                state["profiles"][pid] = updated
                self.save(state)
                if not PLAN_SCOPES.issubset(updated["scopes"]):
                    raise AIError("plan_permission")
                return updated["access_token"]
            except Exception as exc:
                # Back off after a temporary failure; a terminal rotation error requires fresh sign-in.
                if isinstance(exc, AIError) and exc.kind in ("oauth_login", "plan_permission"):
                    raise
                p["retry_after"] = time.time() + 60
                self.save(state)
                raise AIError("oauth_temporary") from None

    @staticmethod
    def clear_tokens(p):
        for key in ("access_token", "refresh_token", "id_token"):
            p.pop(key, None)
        p["scopes"] = []
        p["generation"] = secrets.token_hex(12)

    def disconnect(self, pid):
        confirmed = False
        with self.lock():
            state = self.load()
            p = state["profiles"].get(pid)
            if not p:
                raise ValueError("账户不存在")
            token = p.get("refresh_token")
            if token:
                for attempt in range(2):
                    try:
                        with httpx.Client(
                            timeout=8, transport=self.transport, follow_redirects=False
                        ) as client:
                            response = client.post(
                                REVOKE,
                                data={
                                    "token": token,
                                    "token_type_hint": "refresh_token",
                                    "client_id": p["client_id"],
                                },
                            )
                        confirmed = response.status_code == 200
                        if confirmed or response.status_code < 500:
                            break
                    except httpx.RequestError:
                        pass
                    if attempt == 0:
                        time.sleep(0.25)
            else:
                confirmed = True
            self.clear_tokens(p)
            p["paused"] = "oauth_login"
            state["pair"] = None
            self.save(
                state,
                reset=state["active"] == pid
                and self.store.get("v2_preferences").get("ai_provider") == "chatgpt",
            )
        return {
            "disconnected": True,
            "revoked": confirmed,
            "message": "已断开授权"
            if confirmed
            else "本机凭据已删除，但尚未确认远程撤销。请到 ChatGPT 用量设置中断开 QuantPilot。",
        }


def connection(vault):
    return ChatGPTConnection(vault)
