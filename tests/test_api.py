import time

import pytest

from quantpilot.app import digest


@pytest.mark.parametrize("path", ["/api/state", "/api/market", "/api/orders", "/api/audit", "/api/session", "/api/balance/okx"])
def test_private_routes_require_login(client, path):
    assert client.get(path).status_code == 401


def test_login_cookie_and_session(logged):
    response = logged.get("/api/session")
    assert response.json()["username"] == "tester"
    assert logged.get("/api/state").json()["live_available"] is False
    assert logged.get("/api/state").headers["cache-control"] == "no-store"


def test_cookie_flags(client):
    response = client.post("/api/login", json={"username": "tester", "password": "test-only-password"})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_wrong_password_and_throttle(client):
    for _ in range(10):
        assert client.post("/api/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"username": "tester", "password": "test-only-password"}).status_code == 429


def test_no_cross_site_mutations(logged):
    assert logged.post("/api/kill", json={"enabled": True}, headers={"origin": "https://attacker.example"}).status_code == 403


def test_csrf_required(logged):
    logged.headers.pop("x-csrf-token")
    assert logged.post("/api/kill", json={"enabled": True}).status_code == 403


def test_kill_and_strategy_config(logged):
    assert logged.post("/api/kill", json={"enabled": True}).status_code == 200
    assert logged.post("/api/strategy", json={"enabled": True}).status_code == 409
    logged.post("/api/kill", json={"enabled": False})
    assert logged.post("/api/strategy", json={"enabled": True}).status_code == 200
    assert logged.post("/api/strategy", json={"budget": "100000"}).status_code == 422


def test_logout_revokes_cookie_server_side(logged, app):
    token = logged.cookies.get("qp_session")
    assert logged.post("/api/logout", json={}).status_code == 200
    with app.state.store.connect() as db:
        assert not db.execute("SELECT 1 FROM sessions WHERE token=?", (digest(token),)).fetchone()
    logged.cookies.set("qp_session", token)
    assert logged.get("/api/state").status_code == 401


def test_session_expiry(logged, app):
    with app.state.store.transaction() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert logged.get("/api/state").status_code == 401


def test_credentials_encrypted_never_returned(logged, app):
    secret = "unit-test-secret-value"
    response = logged.post("/api/credentials/okx", json={"api_key": "unit-test-key", "secret": secret, "passphrase": "unit-pass"})
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in logged.get("/api/state").text
    assert secret not in logged.get("/api/audit").text
    with app.state.store.connect() as db:
        encrypted = db.execute("SELECT encrypted FROM credentials").fetchone()[0]
    assert secret.encode() not in encrypted
    assert app.state.vault.get("okx")["secret"] == secret
    assert logged.request("DELETE", "/api/credentials/okx", json={}).status_code == 200
    assert app.state.vault.get("okx") is None


def test_validation_does_not_echo_secrets(client):
    sensitive = "do-not-echo-" * 60
    response = client.post("/api/login", json={"username": "tester", "password": sensitive})
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


def test_body_size_and_content_type(logged):
    assert logged.post("/api/kill", content="x" * 17000, headers={"content-type": "application/json"}).status_code == 413
    assert logged.post("/api/kill", content="hello", headers={"content-type": "text/plain"}).status_code == 415


def test_host_header_rejected(logged):
    assert logged.get("/api/state", headers={"host": "attacker.example"}).status_code == 400


def test_no_live_endpoint(logged, order):
    response = logged.post("/api/orders", json={**order.model_dump(mode="json"), "mode": "live"})
    assert response.json()["code"] == "live_trading_locked"
    assert logged.post("/api/live/arm", json={}).status_code == 404


def test_api_order_and_market(logged, order):
    assert logged.get("/api/market").status_code == 200
    assert logged.post("/api/orders", json=order.model_dump(mode="json")).json()["status"] == "filled"
    assert len(logged.get("/api/orders").json()) == 1
    assert logged.get("/api/state").json()["account"]["positions"]


def test_ai_disabled_no_request(logged):
    assert logged.post("/api/advice", json={}).json()["detail"] == "openai_disabled_or_model_missing"


def test_static_and_security_headers(client):
    response = client.get("/")
    assert "QuantPilot" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/docs").status_code == 404


def test_password_rotation_immediately_revokes_old_sessions(logged, app, settings):
    from quantpilot.config import atomic_json, hash_password

    updated = settings.model_copy(update={"password_hash": hash_password("new-test-password")})
    atomic_json(app.state.store.directory / "config.json", updated.model_dump())
    assert logged.get("/api/state").status_code == 401
    assert logged.post("/api/login", json={"username": "tester", "password": "test-only-password"}).status_code == 401
    assert logged.post("/api/login", json={"username": "tester", "password": "new-test-password"}).status_code == 200


def test_unicode_username_is_a_normal_auth_failure(client):
    assert client.post("/api/login", json={"username": "中文", "password": "test-only-password"}).status_code == 401


def test_web_ai_configuration_preserves_credentials(logged, app, settings):
    assert logged.post("/api/ai/settings", json={"enabled": True, "model": ""}).status_code == 422
    assert logged.post("/api/ai/settings", json={"enabled": True, "model": "test-model"}).status_code == 200
    assert logged.get("/api/state").json()["openai_model"] == "test-model"
    assert settings.password_hash in (app.state.store.directory / "config.json").read_text()


def test_single_server_process_guard(app, settings, tmp_path):
    from fastapi.testclient import TestClient

    from quantpilot.app import create_app

    other = create_app(settings, tmp_path, background=False)
    with TestClient(app, base_url=settings.public_origin):
        with pytest.raises(RuntimeError, match="one QuantPilot"):
            with TestClient(other, base_url=settings.public_origin):
                pass
