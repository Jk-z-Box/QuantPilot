"""Narrow local RPC. The trading process never reads Codex OAuth credentials."""

from __future__ import annotations

import os

import httpx

from .ai_errors import AIError

SOCKET = "/run/quantpilot-ai/broker.sock"


class CodexConnection:
    def __init__(self, transport=None):
        self.transport = transport

    def call(self, operation, payload=None, timeout=25):
        transport = self.transport or httpx.HTTPTransport(uds=os.environ.get("QP_CODEX_SOCKET", SOCKET))
        try:
            with httpx.Client(transport=transport, timeout=timeout, trust_env=False) as client:
                response = client.post("http://localhost/" + operation, json=payload or {})
                body = response.json()
            if not response.is_success:
                raise AIError(body.get("kind", "codex_unavailable"))
            return body
        except AIError:
            raise
        except (httpx.RequestError, ValueError):
            raise AIError("codex_unavailable") from None

    def status(self):
        try:
            return self.call("status", timeout=2)
        except AIError:
            return {"available": False, "ready": False, "revision": "unavailable", "pending": False}

    def fingerprint(self):
        status = self.status()
        return ("codex", status["revision"], status["ready"])

    def response(self, payload):
        return self.call("response", payload, timeout=190)
