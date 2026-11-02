import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from quantpilot.chatgpt_login import authorize_url, callback_result, server_origin
from quantpilot.v2.ai import list_models, review
from quantpilot.v2.ai_client import fingerprint, response
from quantpilot.v2.ai_errors import AIError, http_error
from quantpilot.v2.chatgpt import ISSUER, NAME, SCOPES, ChatGPTConnection, validate_identity
from quantpilot.v2.models import Preferences, signal_policy

CLIENT = "oaiapp_test_registration"


@pytest.fixture
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
    }

    def sign(nonce="test-nonce", **changes):
        claims = {
            "iss": ISSUER,
            "aud": CLIENT,
            "sub": "subject-a",
            "email": "user@example.test",
            "exp": time.time() + 600,
            "iat": time.time(),
            "nonce": nonce,
            **changes,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return sign, httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))


def connect(workspace, signer, scope=SCOPES):
    sign, transport = signer
    auth = ChatGPTConnection(workspace.vault, transport)
    start = auth.start_pair()
    context = auth.pair_context(start["pair_code"])
    tokens = {
        "access_token": "test-access-secret",
        "refresh_token": "test-refresh-secret",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
        "id_token": sign(context["nonce"]),
    }
    state = auth.complete_pair(start["pair_code"], CLIENT, tokens)
    return auth, state["active"], tokens


def test_real_signature_and_nonce_checks(signer):
    sign, transport = signer
    assert validate_identity(sign(), CLIENT, "test-nonce", transport)["subject"] == "subject-a"
    for claims in (
        {"iss": "https://evil.test"},
        {"aud": "oaiapp_other"},
        {"exp": time.time() - 60},
        {"iat": time.time() + 60},
        {"nonce": "wrong"},
        {"sub": ""},
    ):
        with pytest.raises(AIError) as error:
            validate_identity(sign(**claims), CLIENT, "test-nonce", transport)
        assert error.value.kind == "oauth_validation"
    forged = jwt.encode(
        {"sub": "subject-a"},
        "test-attacker-secret-of-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(AIError):
        validate_identity(forged, CLIENT, "test-nonce", transport)


def test_callback_state_denial_registration_and_pkce():
    query = {"state": "test-state", "code": "code", "client_id": CLIENT}
    assert callback_result("/auth/callback?" + urlencode(query), "test-state") == ("code", CLIENT)
    bad = [
        {**query, "state": "wrong"},
        {**query, "error": "access_denied"},
        {**query, "client_id": "dynamic_agent_client"},
        {"state": "test-state", "code": "code"},
    ]
    for item in bad:
        with pytest.raises(ValueError):
            callback_result("/auth/callback?" + urlencode(item), "test-state")
    with pytest.raises(ValueError):
        callback_result("/auth/callback?" + urlencode(query) + "&state=test-state", "test-state")
    with pytest.raises(ValueError):
        callback_result("/auth/callback?" + urlencode(query), "test-state", "oaiapp_other")
    url = authorize_url(
        {"nonce": "nonce"}, "urn:uuid:host", "http://127.0.0.1:1234/auth/callback", "state", "verifier"
    )
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(ISSUER + "/api/accounts/authorize?")
    assert q["agent_name_hint"] == ["QuantPilot"]
    assert q["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(b"verifier").digest()).rstrip(b"=").decode()
    ]
    returning = parse_qs(
        urlsplit(
            authorize_url(
                {"nonce": "nonce", "client_id": CLIENT},
                "urn:uuid:host",
                "http://127.0.0.1:5678/auth/callback",
                "state",
                "v",
            )
        ).query
    )
    assert "agent_name_hint" not in returning and returning["client_id"] == [CLIENT]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://u:p@example.test",
        "https://example.test/path",
        "https://example.test/?key=x",
        "https://example.test/#x",
    ],
)
def test_helper_rejects_insecure_destination(url):
    with pytest.raises(ValueError):
        server_origin(url)


def test_pairing_one_use_encryption_identity_and_host_preserved(workspace, signer):
    auth = workspace.chatgpt
    host = auth.load()["host_id"]
    auth, pid, tokens = connect(workspace, signer)
    assert auth.status()["ready"]
    assert auth.load()["host_id"] == host
    assert not auth.load()["pair"]
    public = json.dumps(workspace.state())
    assert all(
        value not in public for value in (tokens["access_token"], tokens["refresh_token"], tokens["id_token"])
    )
    with workspace.store.connect() as db:
        data = db.execute("SELECT encrypted FROM credentials WHERE name=?", (NAME,)).fetchone()[0]
    assert b"test-access-secret" not in data
    assert workspace.store.get("v2_preferences")["ai_provider"] == "chatgpt"
    assert all(not a["auto"] for a in workspace.store.get("v2_accounts").values())
    old_revision = auth.status()["revision"]
    attempt = auth.start_pair(pid)
    ctx = auth.pair_context(attempt["pair_code"])
    assert ctx["client_id"] == CLIENT
    wrong = {**tokens, "id_token": signer[0](ctx["nonce"], sub="other-subject")}
    with pytest.raises(AIError):
        auth.complete_pair(attempt["pair_code"], CLIENT, wrong)
    assert auth.status()["revision"] == old_revision
    valid = {**tokens, "id_token": signer[0](ctx["nonce"])}
    auth.complete_pair(attempt["pair_code"], CLIENT, valid)
    with pytest.raises(ValueError):
        auth.complete_pair(attempt["pair_code"], CLIENT, valid)


def test_pairing_expiry_and_new_attempt_invalidates_previous(workspace):
    auth = workspace.chatgpt
    old = auth.start_pair()["pair_code"]
    current = auth.start_pair()["pair_code"]
    with pytest.raises(ValueError):
        auth.pair_context(old)
    state = auth.load()
    state["pair"]["expires_at"] = time.time() - 1
    auth.save(state)
    with pytest.raises(ValueError):
        auth.pair_context(current)


def test_identity_without_plan_permission_cannot_infer_or_fallback(workspace, signer):
    auth, _, _ = connect(workspace, signer, "openid profile email offline_access")
    workspace.vault.put("v2:ai:openai", {"apiKey": "must-not-be-used"})
    assert not auth.status()["ready"]
    with pytest.raises(AIError) as error:
        response({"ai_provider": "chatgpt"}, workspace.vault, {})
    assert error.value.kind == "plan_permission"
    with pytest.raises(ValueError):
        workspace.preferences(
            {**Preferences(ai_provider="chatgpt", ai_enabled=True, ai_model="m").model_dump()}
        )


def expire(auth, pid):
    state = auth.load()
    state["profiles"][pid]["expires_at"] = time.time() - 1
    auth.save(state)


def test_refresh_rotation_serialized_and_scopes_preserved(workspace, signer):
    auth, pid, _ = connect(workspace, signer)
    before = auth.fingerprint()
    expire(auth, pid)
    seen = []

    def refresh(req):
        body = parse_qs(req.content.decode())
        seen.append(body)
        assert body["refresh_token"] == ["test-refresh-secret"] and "scope" not in body
        assert body["client_id"] == [CLIENT]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    auth.transport = httpx.MockTransport(refresh)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: auth.access(), range(4))) == ["new-access"] * 4
    assert len(seen) == 1
    assert auth.load()["profiles"][pid]["refresh_token"] == "new-refresh"
    assert auth.fingerprint() == before


@pytest.mark.parametrize("code", ["invalid_grant", "refresh_token_reused", "refresh_token_expired"])
def test_terminal_refresh_removes_tokens_retains_registration(workspace, signer, code):
    auth, pid, _ = connect(workspace, signer)
    expire(auth, pid)
    auth.transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": code}))
    with pytest.raises(AIError) as error:
        auth.access()
    assert error.value.kind == "oauth_login"
    p = auth.load()["profiles"][pid]
    assert p["client_id"] == CLIENT and "refresh_token" not in p and "id_token" not in p


def test_temporary_refresh_backoff_retains_tokens(workspace, signer):
    auth, pid, _ = connect(workspace, signer)
    expire(auth, pid)
    calls = []
    auth.transport = httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(503))
    for _ in range(2):
        with pytest.raises(AIError):
            auth.access()
    assert len(calls) == 1 and auth.load()["profiles"][pid]["refresh_token"] == "test-refresh-secret"


def test_earliest_refresh_is_respected(workspace, signer):
    auth, pid, _ = connect(workspace, signer)
    state = auth.load()
    state["profiles"][pid].update(expires_at=time.time() - 1, earliest_refresh_at=time.time() + 120)
    auth.save(state)
    auth.transport = httpx.MockTransport(lambda r: pytest.fail("must not refresh yet"))
    with pytest.raises(AIError):
        auth.access()


@pytest.mark.parametrize("status", [200, 503])
def test_disconnect_revokes_then_clears_and_keeps_registration(workspace, signer, status):
    auth, pid, _ = connect(workspace, signer)
    requests = []
    auth.transport = httpx.MockTransport(
        lambda r: requests.append(parse_qs(r.content.decode())) or httpx.Response(status)
    )
    result = auth.disconnect(pid)
    assert result["revoked"] == (status == 200)
    assert requests[0]["token"] == ["test-refresh-secret"]
    p = auth.load()["profiles"][pid]
    assert p["client_id"] == CLIENT and "access_token" not in p
    assert not auth.status()["ready"]
    with pytest.raises(AIError):
        auth.select(pid)


def sse(*events):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(e) + "\n\n" for e in events),
    )


def test_oauth_stream_contract_and_completed_review(workspace, signer, opportunity):
    connect(workspace, signer)

    def handler(req):
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer test-access-secret"
        assert body["stream"] and body["store"] is False
        assert isinstance(body["input"], list) and body["input"][0]["role"] == "user"
        assert "max_output_tokens" not in body and "max_tool_calls" not in body
        return sse(
            {"type": "response.output_text.delta", "delta": "ignored"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": '{"revisions":[]}'}]}
                    ],
                },
            },
        )

    prefs = Preferences(ai_provider="chatgpt", ai_model="test").model_dump()
    assert review(prefs, workspace.vault, [opportunity], httpx.MockTransport(handler)) == []


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "delta": '{"revisions":[]}'},
        {"type": "response.incomplete"},
        {"type": "response.completed", "response": {"status": "failed"}},
    ],
)
def test_partial_stream_never_used(workspace, signer, event):
    connect(workspace, signer)
    with pytest.raises(AIError) as error:
        response({"ai_provider": "chatgpt"}, workspace.vault, {}, httpx.MockTransport(lambda r: sse(event)))
    assert error.value.kind == "incomplete"


def test_streamed_plan_limit_pauses_without_paid_fallback(workspace, signer):
    auth, _, _ = connect(workspace, signer)
    workspace.vault.put("v2:ai:openai", {"apiKey": "must-not-send"})
    requests = []

    def limited(req):
        requests.append(req)
        return sse(
            {
                "type": "error",
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": "provider secret",
            }
        )

    for _ in range(2):
        with pytest.raises(AIError) as error:
            response({"ai_provider": "chatgpt"}, workspace.vault, {}, httpx.MockTransport(limited))
        assert error.value.kind == "plan_limit"
        assert "provider secret" not in str(error.value)
    assert len(requests) == 1 and not auth.status()["ready"]
    auth.resume()
    assert auth.status()["ready"]


def test_oauth_model_catalog_order_and_visibility(workspace, signer):
    connect(workspace, signer)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "z", "visibility": "list"},
                    {"slug": "a", "visibility": "list"},
                    {"slug": "hidden", "visibility": "hide"},
                ]
            },
        )
    )
    assert list_models(workspace.vault, transport, "chatgpt")["models"] == ["z", "a"]


def test_api_error_specific_and_redacted():
    error = http_error(
        httpx.Response(
            429,
            json={
                "error": {
                    "code": "credit_balance_exhausted",
                    "type": "insufficient_quota",
                    "message": "never echo this key",
                }
            },
        )
    )
    assert error.kind == "balance" and error.public()["provider_code"] == "credit_balance_exhausted"
    assert "never echo" not in str(error)
    assert error.public()["action_url"].startswith("https://platform.openai.com/")


def test_pair_routes_auth_csrf_and_credentials_never_echo(logged, workspace, signer):
    start = logged.post("/api/v2/chatgpt/pair", json={}).json()
    headers = {"Authorization": "Bearer " + start["pair_code"]}
    context = logged.post("/api/v2/chatgpt/pair/context", json={}, headers=headers).json()
    workspace.chatgpt.transport = signer[1]
    body = {
        "client_id": CLIENT,
        "tokens": {
            "access_token": "secret-access",
            "refresh_token": "secret-refresh",
            "id_token": signer[0](context["nonce"]),
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": SCOPES,
        },
    }
    logged.cookies.clear()
    assert logged.post("/api/v2/chatgpt/pair", json={}).status_code == 401
    assert logged.post("/api/v2/chatgpt/pair/context", json={}).status_code == 401
    result = logged.post("/api/v2/chatgpt/pair/complete", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["ready"]
    assert "secret-access" not in result.text and "secret-refresh" not in result.text
    assert logged.post("/api/v2/chatgpt/pair/complete", json=body, headers=headers).status_code == 409


def test_csrf_required_for_connection_mutations(logged):
    logged.headers.pop("x-csrf-token")
    for endpoint in ("pair", "select", "resume"):
        assert logged.post("/api/v2/chatgpt/" + endpoint, json={}).status_code == 403
    assert (
        logged.delete(
            "/api/v2/chatgpt/profiles/abc",
            headers={"content-type": "application/json", "content-length": "0"},
        ).status_code
        == 403
    )


def test_provider_migration_and_api_deletion_does_not_disable_oauth(workspace, signer):
    assert workspace.store.get("v2_preferences")["ai_provider"] == "api_key"
    auth, _, _ = connect(workspace, signer)
    prefs = workspace.preferences(
        {**workspace.store.get("v2_preferences"), "ai_model": "test", "ai_enabled": True}
    )
    before = fingerprint(prefs, workspace.vault)
    workspace.vault.put("v2:ai:openai", {"apiKey": "test-other-key"})
    workspace.delete_key("openai")
    assert workspace.store.get("v2_preferences")["ai_enabled"]
    assert auth.status()["ready"] and fingerprint(prefs, workspace.vault) == before
    assert signal_policy(prefs)["ai_provider"] == "chatgpt"


def test_refresh_scope_removed_blocks_access_and_saves_rotation(workspace, signer):
    auth, pid, _ = connect(workspace, signer)
    expire(auth, pid)
    auth.transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "openid email",
            },
        )
    )
    with pytest.raises(AIError) as error:
        auth.access()
    assert error.value.kind == "plan_permission"
    assert auth.load()["profiles"][pid]["refresh_token"] == "rotated"
    assert not auth.status()["ready"]


def test_same_email_registrations_remain_distinct_and_select_pauses(workspace, signer):
    auth, first, _ = connect(workspace, signer)
    start = auth.start_pair()
    context = auth.pair_context(start["pair_code"])
    other_client = "oaiapp_second"
    tokens = {
        "access_token": "other-access",
        "refresh_token": "other-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": SCOPES,
        "id_token": signer[0](context["nonce"], aud=other_client),
    }
    second = auth.complete_pair(start["pair_code"], other_client, tokens)["active"]
    assert len(auth.status()["profiles"]) == 2 and second != first
    auth.select(first)
    assert auth.access() == "test-access-secret"
    assert auth.load()["profiles"][second]["access_token"] == "other-access"
    assert not workspace.store.get("v2_preferences")["ai_enabled"]


def test_stale_error_cannot_pause_switched_session(workspace, signer):
    auth, pid, _ = connect(workspace, signer)
    before = auth.fingerprint()
    state = auth.load()
    state["profiles"][pid]["generation"] = "new-authorization"
    auth.save(state)
    auth.note_error(AIError("plan_limit"), before)
    assert auth.status()["ready"]


def test_provider_switch_invalidates_signals_and_pauses_accounts(workspace, signer, account, opportunity):
    connect(workspace, signer)
    workspace.opportunities = {"btc": opportunity}
    workspace.vault.put("v2:ai:openai", {"apiKey": "api-key-test"})
    account["auto"] = True
    workspace.save_account(account)
    workspace.preferences({**workspace.store.get("v2_preferences"), "ai_provider": "api_key"})
    assert not workspace.opportunities["btc"]["valid"]
    assert not workspace.store.get("v2_accounts")[account["id"]]["auto"]


def test_ai_failure_status_retains_only_safe_code(workspace, monkeypatch, opportunity):
    from quantpilot.v2 import service

    workspace.vault.put("v2:ai:openai", {"apiKey": "test"})
    prefs = workspace.preferences(
        {**workspace.store.get("v2_preferences"), "ai_enabled": True, "ai_model": "test"}
    )
    workspace.opportunities = {"btc": {**opportunity, "ai": {"action": "add"}}}

    def failure(*args, **kwargs):
        raise AIError("balance", 429, "credit_balance_exhausted")

    monkeypatch.setattr(service, "review", failure)
    with pytest.raises(AIError):
        workspace.review_ai(prefs)
    assert workspace.state()["ai_error"]["kind"] == "balance"
    assert "ai" not in workspace.opportunities["btc"]


def test_loopback_helper_full_protocol_no_credentials_on_disk(tmp_path, monkeypatch, signer, capsys):
    import http.client
    import threading
    from types import SimpleNamespace

    from quantpilot import chatgpt_login as helper

    calls, auth_query = [], {}
    tokens = {
        "access_token": "helper-access",
        "refresh_token": "helper-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": SCOPES,
    }

    def transport(req):
        calls.append(req)
        if req.url.path.endswith("/pair/context"):
            assert req.headers["origin"] == "https://quantpilot.test"
            assert req.headers["authorization"] == "Bearer test-pair-code"
            return httpx.Response(200, json={"nonce": "test-nonce", "client_id": None, "identity": None})
        if req.url.path.endswith("/oauth/token"):
            params = parse_qs(req.content.decode())
            assert params["redirect_uri"] == auth_query["redirect_uri"]
            assert params["client_id"] == [CLIENT]
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(params["code_verifier"][0].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert digest == auth_query["code_challenge"][0]
            return httpx.Response(200, json={**tokens, "id_token": signer[0]()})
        if req.url.path.endswith("jwks.json"):
            return signer[1].handle_request(req)
        if req.url.path.endswith("/pair/complete"):
            data = json.loads(req.content)
            assert data["tokens"]["access_token"] == "helper-access"
            assert req.headers["origin"] == "https://quantpilot.test"
            return httpx.Response(200, json={"ready": True})
        pytest.fail("unexpected request")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(transport)}),
    )
    monkeypatch.setattr(helper.getpass, "getpass", lambda prompt: "test-pair-code")

    def browser(url):
        auth_query.update(parse_qs(urlsplit(url).query))
        redirect = urlsplit(auth_query["redirect_uri"][0])

        def send():
            conn = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
            path = (
                redirect.path
                + "?"
                + urlencode({"state": auth_query["state"][0], "code": "test-code", "client_id": CLIENT})
            )
            conn.request("GET", path)
            assert conn.getresponse().status == 200
            conn.close()

        thread = threading.Thread(target=send)
        thread.start()
        return True

    monkeypatch.setattr(helper.webbrowser, "open", browser)
    assert (
        helper.login(SimpleNamespace(server="https://quantpilot.test", local_data=tmp_path, no_browser=False))
        == 0
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["host.json"]
    output = capsys.readouterr().out
    assert "helper-access" not in output and "helper-refresh" not in output and "test-code" not in output
    assert len(calls) == 4
