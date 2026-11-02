"""One provider per request; never silently bill an API key after plan failure."""

from __future__ import annotations

import copy
import hashlib
import json
import time

import httpx

from .ai_errors import AIError, http_error
from .chatgpt import ChatGPTConnection
from .codex_client import CodexConnection


def fingerprint(preferences, vault):
    if preferences.get("ai_provider") == "codex":
        return CodexConnection().fingerprint()
    if preferences.get("ai_provider", "api_key") == "chatgpt":
        return ("chatgpt", *ChatGPTConnection(vault).fingerprint())
    key = (vault.get("v2:ai:openai") or {}).get("apiKey", "")
    return ("api_key", hashlib.sha256(key.encode()).hexdigest())


def credential(preferences, vault, transport=None):
    if preferences.get("ai_provider") == "codex":
        raise AIError("parameters")
    if preferences.get("ai_provider", "api_key") == "chatgpt":
        auth = ChatGPTConnection(vault, transport)
        return auth.access(), auth
    key = (vault.get("v2:ai:openai") or {}).get("apiKey")
    if not key:
        raise ValueError("请先保存 OpenAI API Key")
    return key, None


def ready(preferences, vault):
    if preferences.get("ai_provider") == "codex":
        return CodexConnection().status()["ready"]
    if preferences.get("ai_provider", "api_key") == "chatgpt":
        return ChatGPTConnection(vault).status()["ready"]
    return bool((vault.get("v2:ai:openai") or {}).get("apiKey"))


def stream_response(response, deadline):
    data, total = [], 0
    for line in response.iter_lines():
        total += len(line.encode())
        if total > 2_000_000 or time.monotonic() > deadline:
            raise AIError("incomplete")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            raw, data = "\n".join(data), []
            if raw == "[DONE]":
                break
            event = json.loads(raw)
            kind = event.get("type")
            if kind in ("error", "response.failed"):
                error = event.get("error") or event.get("response", {}).get("error") or event
                raise http_error(httpx.Response(400, json={"error": error}), oauth=True)
            if kind == "response.incomplete":
                raise AIError("incomplete")
            if kind == "response.completed":
                body = event.get("response", {})
                if body.get("status") != "completed":
                    raise AIError("incomplete")
                return body
    # Deltas and even valid JSON text are not an authoritative completed response.
    raise AIError("incomplete")


def response(preferences, vault, payload, transport=None, timeout=45):
    if preferences.get("ai_provider") == "codex":
        return CodexConnection(transport).response(payload)
    token, auth = credential(preferences, vault, transport)
    context = auth.fingerprint() if auth else None
    body = copy.deepcopy(payload)
    if auth:
        body.pop("max_output_tokens", None)
        body.pop("max_tool_calls", None)
        body["stream"], body["store"] = True, False
        if isinstance(body.get("input"), str):
            body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": body["input"]}]}]
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False) as client:
            if not auth:
                result = client.post(
                    "https://api.openai.com/v1/responses",
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                )
                if not result.is_success:
                    raise http_error(result)
                return result.json()
            with client.stream(
                "POST",
                "https://api.openai.com/v1/responses",
                json=body,
                headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream"},
            ) as result:
                if not result.is_success:
                    result.read()
                    raise http_error(result, oauth=True)
                return stream_response(result, time.monotonic() + 120)
    except AIError as exc:
        if auth:
            auth.note_error(exc, context)
        raise
    except httpx.TimeoutException:
        raise AIError("timeout") from None
    except httpx.RequestError:
        raise AIError("network") from None
    except Exception:
        raise AIError("invalid_response") from None
