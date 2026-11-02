"""Local loopback helper for official OAuth; sends credentials only over HTTPS."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import atomic_json
from .v2.chatgpt import AUTHORIZE, CLIENT_ID, RESOURCE, SCOPES, TOKEN, validate_identity


def server_origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("服务器地址必须为完整 HTTPS 域名，不能包含路径、密码或参数")
    return value.rstrip("/")


class AuthorizationDenied(ValueError):
    pass


def callback_result(path, state, client_id=None):
    parts = urlsplit(path)
    if parts.path != "/auth/callback":
        raise ValueError("callback")
    query = parse_qs(parts.query, keep_blank_values=True)
    if any(len(v) != 1 for v in query.values()) or not secrets.compare_digest(
        query.get("state", [""])[0], state
    ):
        raise ValueError("state")
    if "error" in query:
        raise AuthorizationDenied("authorization denied")
    issued = query.get("client_id", [client_id])[0]
    if not isinstance(issued, str) or not CLIENT_ID.fullmatch(issued) or (client_id and issued != client_id):
        raise ValueError("registration")
    code = query.get("code", [""])[0]
    if not code or len(code) > 8192:
        raise ValueError("code")
    return code, issued


def authorize_url(context, host_id, redirect, state, verifier):
    params = {
        "client_id": context.get("client_id") or "dynamic_agent_client",
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": context["nonce"],
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode(),
    }
    if context.get("client_id"):
        for key in ("login_hint", "id_token_hint"):
            if context.get(key):
                params[key] = context[key]
    else:
        params["agent_name_hint"] = "QuantPilot"
    return AUTHORIZE + "?" + urlencode(params)


def _login(args):
    origin = server_origin(args.server)
    # This directory holds only a stable host ID, never transferred OAuth tokens.
    directory = (
        Path(args.local_data).expanduser()
        if args.local_data
        else Path.home() / ".config" / "quantpilot-login"
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    host_file = directory / "host.json"
    if not host_file.exists():
        atomic_json(host_file, {"host_id": "urn:uuid:" + str(uuid.uuid4())})
    host_id = json.loads(host_file.read_text())["host_id"]
    print("服务器：" + origin)
    print("请确认这就是你正在操作的 QuantPilot。连接码仅在网页生成，10 分钟内有效。")
    pair = getpass.getpass("粘贴网页中的一次性连接码（输入隐藏）: ").strip()
    headers = {"Authorization": "Bearer " + pair, "Origin": origin}
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        r = client.post(origin + "/api/v2/chatgpt/pair/context", headers=headers, json={})
        if r.status_code != 200:
            raise ValueError("pairing")
        context = r.json()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Callback URLs contain authorization codes: never log them.

            def do_GET(self):  # noqa: N802
                if (
                    self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or len(self.path) > 16384
                ):
                    self.send_error(400)
                    return
                if urlsplit(self.path).path != "/auth/callback":
                    self.send_error(404)
                    return
                try:
                    if result:
                        raise ValueError("duplicate")
                    code, issued = callback_result(self.path, state, context.get("client_id"))
                    result.update(code=code, client_id=issued)
                    message = "登录回调已收到。请返回终端完成安全传输，再回到 QuantPilot。"
                except AuthorizationDenied:
                    result["denied"] = True
                    message = "授权已取消，可以关闭此页面。"
                except ValueError:
                    # Invalid state does not terminate an unrelated pending attempt.
                    message = "登录校验未通过，请返回 QuantPilot 重新开始。"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(
                    (
                        "<!doctype html><meta charset=utf-8><title>QuantPilot 登录</title><p>"
                        + message
                        + "</p>"
                    ).encode()
                )

        with HTTPServer(("127.0.0.1", 0), Handler) as listener:
            listener.timeout = 0.5
            redirect = f"http://127.0.0.1:{listener.server_port}/auth/callback"
            url = authorize_url(context, host_id, redirect, state, verifier)
            # Keep the URL (including a possible ID-token hint) out of terminal logs.
            if args.no_browser:
                atomic_json(directory / "pending-browser.json", {"url": url})
                print("授权地址已保存到本机受保护的 pending-browser.json，等待在浏览器中打开。")
            elif not webbrowser.open(url):
                raise ValueError("browser")
            print("请在 OpenAI 官方页面完成登录并确认授权；本程序不会读取 ChatGPT 密码。")
            deadline = time.monotonic() + 540
            try:
                while not result and time.monotonic() < deadline:
                    listener.handle_request()
                if not result:
                    raise ValueError("login timeout")
                if result.get("denied"):
                    print("官方授权已取消，未连接账户。")
                    return 1
            finally:
                (directory / "pending-browser.json").unlink(missing_ok=True)
        r = client.post(
            TOKEN,
            data={
                "grant_type": "authorization_code",
                "client_id": result["client_id"],
                "code": result["code"],
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "resource": RESOURCE,
            },
        )
        if r.status_code != 200:
            raise ValueError("code exchange")
        tokens = r.json()
        identity = validate_identity(tokens.get("id_token"), result["client_id"], context["nonce"])
        old = context.get("identity")
        if old and any(old[k] != identity[k] for k in ("issuer", "subject")):
            raise ValueError("identity changed")
        # The browser never receives these tokens. The server revalidates the ID token.
        r = client.post(
            origin + "/api/v2/chatgpt/pair/complete",
            headers=headers,
            json={
                "client_id": result["client_id"],
                "tokens": {
                    k: tokens[k]
                    for k in (
                        "access_token",
                        "refresh_token",
                        "id_token",
                        "token_type",
                        "expires_in",
                        "scope",
                        "earliest_refresh_at",
                    )
                    if k in tokens
                },
            },
        )
        if r.status_code != 200:
            raise ValueError("credential transfer")
        ready = r.json()["ready"]
        print(
            "官方登录已连接。请回到网页获取模型，再按需启用 AI。自动交易保持暂停。"
            if ready
            else "身份登录成功，但未获准使用 ChatGPT 计划；请在官方用量设置中检查授权。"
        )
        # No renewable token is retained locally: only the VM owns further refreshes.
    return 0


def login(args):
    try:
        return _login(args)
    except Exception:
        raise ValueError(
            "官方登录未完成。请核对服务器、连接码与授权状态后重新开始；敏感信息未输出。"
        ) from None
