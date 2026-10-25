import json
import os
import tarfile

import httpx
import pytest
from pydantic import ValidationError

from quantpilot.advisor import advise
from quantpilot.cli import backup, cloudflare_rule, main
from quantpilot.config import Settings, atomic_json, hash_password, load_settings, verify_password
from quantpilot.store import Store, Vault


def test_password_salted_and_verified():
    encoded = hash_password("test-only-password")
    assert encoded != hash_password("test-only-password")
    assert verify_password("test-only-password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("test-only-password", "broken")
    with pytest.raises(ValueError):
        hash_password("short")


@pytest.mark.parametrize("overrides", [
    {"live_enabled": True}, {"max_order": "NaN"}, {"initial_cash": "-1"},
    {"port": 80}, {"public_origin": "http://remote.example"}, {"public_origin": "https://user:password@domain.example"},
    {"public_origin": "https://example.com/path"}, {"tls_cert": "x"}, {"symbols": ["BTC/USDT:USDT"]},
])
def test_unsafe_config_rejected(settings, overrides):
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), **overrides})


def test_environment_cannot_enable_live(tmp_path, settings, monkeypatch):
    path = tmp_path / "config.json"
    atomic_json(path, settings.model_dump())
    monkeypatch.setenv("QP_LIVE_ENABLED", "true")
    with pytest.raises(ValueError, match="locked"):
        load_settings(path)


def test_config_permissions(tmp_path, settings):
    path = tmp_path / "config.json"
    atomic_json(path, settings.model_dump())
    assert load_settings(path).username == "tester"
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="permissions"):
        load_settings(path)


def test_missing_vault_key_never_silently_rotates(tmp_path):
    vault = Vault(Store(tmp_path))
    vault.put("okx", {"apiKey": "test"})
    (tmp_path / "master.key").unlink()
    with pytest.raises(ValueError, match="original master key"):
        Vault(Store(tmp_path))


def test_vault_and_backup_can_restore(tmp_path, settings):
    path = tmp_path / "config.json"
    atomic_json(path, settings.model_dump())
    vault = Vault(Store(tmp_path))
    vault.put("okx", {"apiKey": "unit-key", "secret": "unit-secret"})
    destination = tmp_path / "backups" / "test.tar.gz"
    backup(path, destination)
    assert destination.stat().st_mode & 0o077 == 0
    with tarfile.open(destination) as archive:
        assert set(archive.getnames()) == {"config.json", "master.key", "quantpilot.db"}
        restore = tmp_path / "restored"
        restore.mkdir(mode=0o700)
        for member in archive.getmembers():
            (restore / member.name).write_bytes(archive.extractfile(member).read())
            os.chmod(restore / member.name, 0o600)
    assert Vault(Store(restore)).get("okx")["secret"] == "unit-secret"
    with pytest.raises(ValueError, match="exists"):
        backup(path, destination)


def test_advice_strict_read_only_request(settings, tmp_path):
    settings.openai_enabled, settings.openai_model = True, "test-model"
    vault = Vault(Store(tmp_path))
    vault.put("openai", {"apiKey": "unit-key"})

    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert body["store"] is False and "tools" not in body
        assert body["text"]["format"]["strict"] is True
        assert "private-position" not in body["input"]
        advice = {"assessment": "wait", "summary": "等待", "risks": ["波动"]}
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(advice)}]}]})

    result = advise(settings, vault, {"equity": "100", "exposure": "0", "positions": "private-position"}, {}, httpx.MockTransport(handler))
    assert result["assessment"] == "wait"


@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"assessment":"buy","execute":true}'}]}]},
])
def test_advice_malformed_or_refusal_fails_closed(settings, tmp_path, response):
    settings.openai_enabled, settings.openai_model = True, "test-model"
    vault = Vault(Store(tmp_path))
    vault.put("openai", {"apiKey": "unit-key"})
    with pytest.raises(ValueError, match="invalid_response"):
        advise(settings, vault, {}, {}, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))


def test_cloudflare_rule_only_changes_destination_port():
    rule = cloudflare_rule("trade.example.com", 8443)
    assert rule["action_parameters"] == {"origin": {"port": 8443}}
    assert rule["expression"] == '(http.host eq "trade.example.com")'
    for domain in ['a.com" or true', "http://a.com", "a.com/path"]:
        with pytest.raises(ValueError):
            cloudflare_rule(domain, 8443)


def test_cli_status_doctor_kill(tmp_path, settings, monkeypatch, capsys):
    path = tmp_path / "config.json"
    atomic_json(path, settings.model_dump())
    monkeypatch.setenv("QP_CONFIG", str(path))
    assert main(["doctor"]) == 0
    assert main(["kill", "on"]) == 0
    assert Store(tmp_path).get("kill") is True
    assert main(["cloudflare", "--domain", "trade.example.com", "--port", "8443"]) == 0
    assert settings.password_hash not in capsys.readouterr().out


def test_cli_init_and_password_rotation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setenv("QP_CONFIG", str(path))
    monkeypatch.setattr("getpass.getpass", lambda _: "first-test-password")
    assert main(["init", "--username", "owner", "--port", "8766", "--demo"]) == 0
    settings = load_settings(path)
    assert settings.username == "owner" and settings.port == 8766
    assert settings.market_mode == "demo"
    assert verify_password("first-test-password", settings.password_hash)
    assert main(["init"]) == 1
    monkeypatch.setattr("getpass.getpass", lambda _: "second-test-password")
    assert main(["password"]) == 0
    assert verify_password("second-test-password", load_settings(path).password_hash)
    output = capsys.readouterr()
    assert "first-test-password" not in output.out + output.err
    assert "second-test-password" not in output.out + output.err


def test_config_update_never_reverts_password(tmp_path, settings):
    from quantpilot.config import update_settings

    path = tmp_path / "config.json"
    atomic_json(path, settings.model_dump())
    new_hash = hash_password("newer-test-password")
    update_settings(path, {"password_hash": new_hash})
    update_settings(path, {"openai_enabled": True, "openai_model": "test-model"}, settings)
    assert load_settings(path).password_hash == new_hash
