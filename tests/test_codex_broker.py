import json
import threading
from pathlib import Path

import httpx
import pytest

from quantpilot.codex_broker import DEVICE_URL, Broker, Handler, Server, classify
from quantpilot.v2.ai import list_models, review
from quantpilot.v2.ai_client import credential, response
from quantpilot.v2.ai_errors import AIError
from quantpilot.v2.codex_client import CodexConnection


class RPC:
    def __init__(self, notify):
        self.notify, self.account, self.calls = notify, None, []
        self.result = {
            "type": "chatgptDeviceCode",
            "loginId": "login-1",
            "verificationUrl": DEVICE_URL,
            "userCode": "ABCD-1234",
        }
        self.turn_status = "completed"
        self.answer = '{"revisions":[]}'
        self.items = []

    def call(self, method, params, timeout=20):
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method == "account/login/start":
            return self.result
        if method == "account/logout":
            self.account = None
        if method == "model/list":
            return {
                "data": [{"model": "test-model"}, {"model": "hidden-model", "hidden": True}],
                "nextCursor": None,
            }
        if method == "thread/start":
            return {
                "thread": {"id": "thread-1"},
                "activePermissionProfile": {"id": "quantpilot", "extends": None},
            }
        if method == "turn/start":
            # Notifications arriving before the response must be buffered.
            for item in self.items:
                self.notify("item/completed", {"threadId": "thread-1", "turnId": "turn-1", "item": item})
            self.notify(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": self.turn_status,
                        "items": [
                            {
                                "id": "final",
                                "type": "agentMessage",
                                "text": self.answer,
                                "phase": "final_answer",
                            }
                        ],
                        "error": {"message": "usage limit reached secret-value"},
                    },
                },
            )
            return {"turn": {"id": "turn-1"}}
        return {}


@pytest.fixture
def broker(tmp_path):
    return Broker(tmp_path, RPC)


def connect(broker):
    broker.connection().account = {
        "type": "chatgpt",
        "planType": "plus",
        "email": "not-returned@example.test",
    }


def payload(research=False):
    return {
        "model": "test-model",
        "instructions": "Only supplied evidence",
        "input": "{}",
        "text": {
            "format": {
                "name": "public_observations" if research else "opportunity_review",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        },
    }


def test_login_pending_reuse_and_success(broker):
    first = broker.dispatch("login", {})
    assert first == broker.dispatch("login", {})
    assert "loginId" not in first
    assert broker.status()["pending"] and not broker.status()["ready"]
    old = broker.status()["revision"]
    broker.notification("account/login/completed", {"loginId": "other", "success": True})
    assert broker.status()["pending"]
    connect(broker)
    broker.notification("account/login/completed", {"loginId": "login-1", "success": True})
    status = broker.status()
    assert status["ready"] and not status["pending"] and status["revision"] != old
    assert "email" not in status and "user_code" not in status
    assert len([m for m, _ in broker.rpc.calls if m == "account/login/start"]) == 1


@pytest.mark.parametrize(
    "field,value", [("verificationUrl", "https://evil.test"), ("userCode", "bad\ncode"), ("loginId", "")]
)
def test_login_rejects_untrusted_redirect_and_code(broker, field, value):
    broker.connection().result[field] = value
    with pytest.raises(AIError, match="校验"):
        broker.dispatch("login", {})
    assert broker.pending is None


def test_expiry_cancel_failure_and_logout(broker):
    broker.dispatch("login", {})
    broker.pending["expires_at"] = 0
    assert broker.status()["error"]["kind"] == "codex_expired"
    assert any(m == "account/login/cancel" for m, _ in broker.rpc.calls)
    broker.dispatch("login", {})
    broker.notification(
        "account/login/completed", {"loginId": "login-1", "success": False, "error": "SECRET"}
    )
    assert "SECRET" not in json.dumps(broker.status())
    broker.dispatch("login", {})
    assert not broker.dispatch("cancel", {})["pending"]
    connect(broker)
    assert not broker.dispatch("logout", {})["ready"]


def test_connected_start_and_busy_rejected(broker):
    connect(broker)
    with pytest.raises(AIError, match="已连接"):
        broker.dispatch("login", {})
    broker.operation.acquire()
    try:
        with pytest.raises(AIError, match="正在处理"):
            broker.dispatch("logout", {})
        assert broker.status()["ready"]
    finally:
        broker.operation.release()


def test_unconnected_or_dead_broker_fails_closed(broker):
    with pytest.raises(AIError, match="重新登录"):
        broker.dispatch("response", payload())
    connect(broker)
    old = broker.status()["revision"]
    broker.notification("broker/stopped", {})
    assert broker.revision != old
    with pytest.raises(AIError):
        broker.status()


def test_model_catalog_and_sandbox(broker):
    connect(broker)
    assert broker.dispatch("models", {})["models"] == ["test-model"]
    result = broker.dispatch("response", payload())
    assert result["status"] == "completed"
    start = next(p for m, p in broker.rpc.calls if m == "thread/start")
    turn = next(p for m, p in broker.rpc.calls if m == "turn/start")
    assert start["ephemeral"] is True and start["approvalPolicy"] == "never"
    assert start["config"]["web_search"] == "disabled"
    assert start["permissions"] == turn["permissions"] == "quantpilot"
    assert "sandbox" not in start and "sandboxPolicy" not in turn
    assert broker.thread_id is None


@pytest.mark.parametrize("status", ["failed", "interrupted", "inProgress"])
def test_partial_turn_never_adopted(broker, status):
    connect(broker)
    broker.rpc.turn_status = status
    with pytest.raises(AIError):
        broker.dispatch("response", payload())
    assert broker.thread_id is None


def test_research_only_completed_opened_sources(broker):
    connect(broker)
    broker.rpc.answer = '{"observations":[]}'
    broker.rpc.items = [
        {"id": "search", "type": "webSearch", "action": {"type": "search", "query": "fake url"}},
        {
            "id": "open",
            "type": "webSearch",
            "action": {"type": "openPage", "url": "https://federalreserve.gov/example"},
        },
    ]
    result = broker.dispatch("response", payload(True))
    assert result["output"][-1]["action"]["sources"] == [{"url": "https://federalreserve.gov/example"}]


def test_invalid_schema_and_json_rejected(broker):
    connect(broker)
    bad = payload()
    bad["text"]["format"]["name"] = "execute_orders"
    with pytest.raises(AIError):
        broker.dispatch("response", bad)
    broker.rpc.answer = "not json"
    with pytest.raises(ValueError):
        broker.dispatch("response", payload())


@pytest.mark.parametrize(
    "message,kind",
    [
        ("usage limit SECRET", "codex_limit"),
        ("401 SECRET", "oauth_login"),
        ("device denied SECRET", "codex_permission"),
        ("model not supported SECRET", "model"),
        ("unknown SECRET", "codex_failed"),
    ],
)
def test_errors_never_echo_provider_content(message, kind):
    error = classify({"message": message})
    assert error.kind == kind and "SECRET" not in json.dumps(error.public())


def test_http_bridge_and_no_credentials(tmp_path, broker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sock = Path("broker.sock")
    with Server(str(sock), Handler) as server:
        server.broker = broker
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        monkeypatch.setenv("QP_CODEX_SOCKET", str(sock))
        try:
            client = CodexConnection()
            assert client.status()["available"]
            assert client.call("login")["verification_url"] == DEVICE_URL
            client.call("cancel")
            connect(broker)
            assert client.call("models")["models"] == ["test-model"]
            with pytest.raises(AIError):
                client.call("arbitrary_command")
        finally:
            server.shutdown()
            worker.join(timeout=2)


def test_provider_does_not_use_api_key(workspace):
    workspace.vault.put("v2:ai:openai", {"apiKey": "test-key-should-not-be-used"})

    def handler(request):
        assert request.url.host == "localhost" and "authorization" not in request.headers
        return httpx.Response(409, json={"kind": "codex_limit"})

    with pytest.raises(AIError, match="用量"):
        response({"ai_provider": "codex"}, workspace.vault, payload(), httpx.MockTransport(handler))
    with pytest.raises(AIError):
        credential({"ai_provider": "codex"}, workspace.vault)
    assert list_models(
        workspace.vault,
        httpx.MockTransport(lambda r: httpx.Response(200, json={"models": ["test-model"]})),
        "codex",
    )["models"] == ["test-model"]


def test_caller_validates_codex_review(workspace, opportunity):
    prefs = {"ai_provider": "codex", "ai_model": "test-model"}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": '{"revisions":[]}'}]}
                ],
            },
        )

    assert review(prefs, workspace.vault, [opportunity], httpx.MockTransport(handler)) == []


def test_authentication_csrf_and_activation_pause(logged, workspace, account, monkeypatch):
    class FakeConnection:
        def status(self):
            return {"ready": True}

        def call(self, op):
            return {"verification_url": DEVICE_URL, "user_code": "ABCD-1234"}

    monkeypatch.setattr(workspace, "codex", FakeConnection())
    old_key = workspace.vault.get("v2:ai:openai")
    result = logged.post("/api/v2/codex/activate", json={})
    assert result.status_code == 200
    prefs = workspace.store.get("v2_preferences")
    assert prefs["ai_provider"] == "codex" and not prefs["ai_enabled"] and not prefs["ai_data_enabled"]
    assert not any(a["auto"] for a in workspace.store.get("v2_accounts").values())
    assert workspace.vault.get("v2:ai:openai") == old_key
    assert logged.post("/api/v2/codex/arbitrary", json={}).status_code == 422
    logged.headers.pop("x-csrf-token")
    assert logged.post("/api/v2/codex/logout", json={}).status_code == 403
    logged.cookies.clear()
    assert logged.post("/api/v2/codex/login", json={}).status_code in (401, 403)


def test_activation_requires_actual_login(workspace, monkeypatch):
    monkeypatch.setattr(workspace.codex, "status", lambda: {"ready": False})
    with pytest.raises(ValueError):
        workspace.codex_action("activate")
    assert workspace.store.get("v2_preferences")["ai_provider"] == "api_key"


def test_deployment_isolation_and_integrity():
    unit = Path("scripts/quantpilot-ai.service").read_text()
    assert "User=quantpilot-ai" in unit and "InaccessiblePaths=/var/lib/quantpilot" in unit
    assert "ProtectSystem=strict" in unit and "UMask=0077" in unit
    assert "AF_NETLINK" in unit and "CapabilityBoundingSet=\n" in unit
    script = Path("scripts/install-codex.sh").read_text()
    assert "sha256sum --check --status" in script and "0.165.0" in script
    assert "--version" in script


def test_protocol_denies_tool_requests_and_sanitizes_child_env(tmp_path, monkeypatch):
    import subprocess

    from quantpilot.codex_broker import Protocol

    executable = tmp_path / "fake-codex"
    executable.write_text("""#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='account/read':
  print(json.dumps({'id':'denied-tool','method':'item/commandExecution/requestApproval','params':{}}),flush=True)
  print(json.dumps({'id':m['id'],'result':{'account':None}}),flush=True)
 elif m.get('id')=='denied-tool':
  assert m['error']['code']==-32601
""")
    executable.chmod(0o700)
    original = subprocess.Popen
    captured = {}

    def spawn(args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return original(args, **kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("QP_CONFIG", "/sensitive/trading/config")
    monkeypatch.setattr(subprocess, "Popen", spawn)
    rpc = Protocol(executable, tmp_path, tmp_path, lambda *args: None)
    try:
        assert rpc.call("account/read", {}) == {"account": None}
        assert "OPENAI_API_KEY" not in captured["env"] and "QP_CONFIG" not in captured["env"]
        assert captured["stderr"] == subprocess.DEVNULL
        assert "features.shell_tool=false" in captured["args"]
        assert 'forced_login_method="chatgpt"' in captured["args"]
        assert 'default_permissions="quantpilot"' in captured["args"]
        assert "permissions.quantpilot.network.enabled=false" in captured["args"]
        fs_argument = next(a for a in captured["args"] if a.startswith("permissions.quantpilot.filesystem="))
        import tomllib

        assert tomllib.loads(fs_argument)["permissions"]["quantpilot"]["filesystem"] == {
            str(tmp_path): "read"
        }
        assert not any("sandbox_mode=" in a for a in captured["args"])
    finally:
        rpc.close()


@pytest.mark.parametrize("code", [-32600, -32601, -32602])
def test_protocol_errors_are_actionable_without_sensitive_details(code):
    error = classify({"code": code, "message": "readOnly.access is no longer supported SECRET"})
    assert error.kind == "codex_protocol"
    assert "SECRET" not in json.dumps(error.public())


def test_unexpected_active_permission_profile_never_starts_inference(broker, monkeypatch):
    connect(broker)
    original = broker.rpc.call

    def call(method, params, timeout=20):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "activePermissionProfile": {"id": ":read-only"}}
        return original(method, params, timeout)

    monkeypatch.setattr(broker.rpc, "call", call)
    with pytest.raises(AIError) as error:
        broker.dispatch("response", payload())
    assert error.value.kind == "codex_protocol"
    assert not any(method == "turn/start" for method, _ in broker.rpc.calls)


@pytest.mark.parametrize("message", ["bubblewrap is unavailable SECRET", "fs sandbox helper failed SECRET"])
def test_sandbox_failures_are_actionable_and_redacted(message):
    error = classify({"code": -32603, "message": message})
    assert error.kind == "codex_sandbox"
    assert "SECRET" not in json.dumps(error.public())


def test_service_acceptance_preserves_isolation_without_owning_live_socket():
    import importlib.util

    spec = importlib.util.spec_from_file_location("service_check", "scripts/check-codex-service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.command(Path("scripts"), "/example/python")
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in args
    assert "CapabilityBoundingSet=" in args
    assert "NoNewPrivileges=true" in args
    assert "InaccessiblePaths=/var/lib/quantpilot -/etc/quantpilot" in args
    assert "ReadWritePaths=/var/lib/quantpilot-ai -/run/quantpilot-ai" in args
    assert not any(arg.startswith("RuntimeDirectory=") for arg in args)
