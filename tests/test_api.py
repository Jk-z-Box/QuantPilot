import time

import pytest

from quantpilot.app import digest


@pytest.mark.parametrize("path", ["/api/v2/state", "/api/v2/logic", "/api/v2/notifications", "/api/audit", "/api/session"])
def test_private_routes_require_login(client, path):
    assert client.get(path).status_code == 401


def test_login_cookie_and_session(logged):
    response = logged.get("/api/session")
    assert response.json()["username"] == "tester"
    assert logged.get("/api/v2/state").json()["accounts"][0]["kind"] == "paper"
    assert logged.get("/api/v2/state").headers["cache-control"] == "no-store"


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


def test_kill_current_workspace(logged):
    assert logged.post("/api/kill", json={"enabled": True}).status_code == 200
    assert logged.get("/api/v2/state").json()["preferences"]["halted"] is True
    logged.post("/api/kill", json={"enabled": False})
    assert logged.get("/api/v2/state").json()["preferences"]["halted"] is False


@pytest.mark.parametrize("path",["/legacy","/static/app.js","/static/index.html","/api/state","/api/orders","/api/market"])
def test_retired_console_is_removed(logged,path):
    assert logged.get(path).status_code == 404


def test_logout_revokes_cookie_server_side(logged, app):
    token = logged.cookies.get("qp_session")
    assert logged.post("/api/logout", json={}).status_code == 200
    with app.state.store.connect() as db:
        assert not db.execute("SELECT 1 FROM sessions WHERE token=?", (digest(token),)).fetchone()
    logged.cookies.set("qp_session", token)
    assert logged.get("/api/v2/state").status_code == 401


def test_session_expiry(logged, app):
    with app.state.store.transaction() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert logged.get("/api/v2/state").status_code == 401




def test_validation_does_not_echo_secrets(client):
    sensitive = "do-not-echo-" * 60
    response = client.post("/api/login", json={"username": "tester", "password": sensitive})
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


def test_body_size_and_content_type(logged):
    assert logged.post("/api/kill", content="x" * 17000, headers={"content-type": "application/json"}).status_code == 413
    assert logged.post("/api/kill", content="hello", headers={"content-type": "text/plain"}).status_code == 415


def test_host_header_rejected(logged):
    assert logged.get("/api/v2/state", headers={"host": "attacker.example"}).status_code == 400








def test_static_and_security_headers(client):
    response = client.get("/")
    assert "QuantPilot" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/workspace.js").status_code == 200
    assert client.get("/docs").status_code == 404


def test_password_rotation_immediately_revokes_old_sessions(logged, app, settings):
    from quantpilot.config import atomic_json, hash_password

    updated = settings.model_copy(update={"password_hash": hash_password("new-test-password")})
    atomic_json(app.state.store.directory / "config.json", updated.model_dump())
    assert logged.get("/api/v2/state").status_code == 401
    assert logged.post("/api/login", json={"username": "tester", "password": "test-only-password"}).status_code == 401
    assert logged.post("/api/login", json={"username": "tester", "password": "new-test-password"}).status_code == 200


def test_unicode_username_is_a_normal_auth_failure(client):
    assert client.post("/api/login", json={"username": "中文", "password": "test-only-password"}).status_code == 401




def test_single_server_process_guard(app, settings, tmp_path):
    from fastapi.testclient import TestClient

    from quantpilot.app import create_app

    other = create_app(settings, tmp_path, background=False)
    with TestClient(app, base_url=settings.public_origin):
        with pytest.raises(RuntimeError, match="one QuantPilot"):
            with TestClient(other, base_url=settings.public_origin):
                pass
