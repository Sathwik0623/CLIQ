#!/usr/bin/env python3
"""Regression tests for advisory-endpoint authorization + runtime isolation.

A security audit found a multi-tenant data-exposure path: ``/api/intel/ask`` and
``/api/intel/explain`` accepted a ``device_id`` with NO session-token check and
read a globally-keyed ``_DEVICE_RUNTIMES`` registry, so any client could probe
another user's live-device runtime state (``node_type`` / ``rejected`` /
``capability``) by supplying/guessing a ``device_id``. SSH device handles were
also derived as ``ssh-{token[:8]}``, disclosing 8 hex chars of the owner's
bearer token.

These tests assert, all offline:

1. Unauthenticated advisory calls fail closed with 401 (``require_session``).
2. Runtime lookups are ownership-scoped by ``(token, device_id)`` — a session
   only ever reads its OWN device; another session's ``device_id`` resolves to
   not-found and NO runtime state (node_type/rejected) is disclosed.
3. Authenticated KB-only advisory (no device attached) still works.
4. The SSH device handle contains no bytes of the bearer token.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cliq.utils import observability as obs
from cliq.web import server


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Keep observability writes off the real home dir and state deterministic.
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    yield
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()


@pytest.fixture()
def client():
    return TestClient(server.app)


def _register_session(token: str, host: str = "cml.example.com") -> None:
    server.SESSIONS[token] = server.AuthSession(
        host=host, username="cli_test", password="pw", auth=None,
    )


def _register_runtime(token: str, device_id: str, *, node_type: str, rejected: set) -> None:
    """Mirror a live runtime into the module registry under (token, device_id)."""
    server._DEVICE_RUNTIMES[(token, device_id)] = server.DeviceRuntime(
        device=object(), node_type=node_type, rejected=set(rejected),
    )


# ---------------------------------------------------------------------------
# 1. Fail-closed: unauthenticated advisory → 401
# ---------------------------------------------------------------------------

class TestUnauthenticated:
    def test_ask_without_token_is_401(self, client):
        r = client.post("/api/intel/ask", json={"question": "show ip route"})
        assert r.status_code == 401

    def test_explain_without_token_is_401(self, client):
        r = client.post("/api/intel/explain", json={"command": "show ip route"})
        assert r.status_code == 401

    def test_bogus_token_is_401(self, client):
        r = client.post(
            "/api/intel/explain",
            json={"command": "show ip route", "device_id": "n0"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert r.status_code == 401

    def test_validate_recall_resolve_require_token(self, client):
        assert client.post("/api/intel/validate", json={"command": "x"}).status_code == 401
        assert client.post("/api/intel/recall", json={"query": "x"}).status_code == 401
        assert client.post(
            "/api/intel/resolve",
            json={"issue_text": "x", "commands": ["show ip route"]},
        ).status_code == 401

    def test_confirming_curl_no_token_guessed_device(self, client):
        """The exact audit repro: no token + a guessed device_id → 401, not data."""
        _register_session("owner-tok")
        _register_runtime(
            "owner-tok", "n0", node_type="iosxr", rejected={"show secret"},
        )
        r = client.post(
            "/api/intel/explain",
            json={"command": "show secret", "device_id": "n0"},
        )
        assert r.status_code == 401
        assert "iosxr" not in r.text  # no runtime state leaked in the body


# ---------------------------------------------------------------------------
# 2. Ownership-scoped runtime lookup (no cross-session leak)
# ---------------------------------------------------------------------------

class TestOwnershipScoping:
    def test_explain_own_device_reconciles(self, client, monkeypatch):
        captured: dict = {}

        def _fake_explain(command, device_platform, mode, device_rejected=False):
            captured["device_platform"] = device_platform
            captured["device_rejected"] = device_rejected
            return {"ok": True}

        monkeypatch.setattr(server, "_explain_command", _fake_explain)
        _register_session("tokA")
        _register_runtime(
            "tokA", "nodeA", node_type="iosxr", rejected={"show ip ospf neighbor"},
        )
        r = client.post(
            "/api/intel/explain",
            json={
                "command": "show ip ospf neighbor",
                "device_id": "nodeA",
                "platform": "iosv",
            },
            headers={"Authorization": "Bearer tokA"},
        )
        assert r.status_code == 200
        # Own device: its node_type is authoritative and its rejection is seen.
        assert captured["device_platform"] == "iosxr"
        assert captured["device_rejected"] is True

    def test_explain_cross_session_is_not_found_no_leak(self, client, monkeypatch):
        captured: dict = {}

        def _fake_explain(command, device_platform, mode, device_rejected=False):
            captured["device_platform"] = device_platform
            captured["device_rejected"] = device_rejected
            return {"ok": True}

        monkeypatch.setattr(server, "_explain_command", _fake_explain)
        # Session A owns nodeA (iosxr, rejects the command).
        _register_session("tokA")
        _register_runtime(
            "tokA", "nodeA", node_type="iosxr", rejected={"show ip ospf neighbor"},
        )
        # Session B is valid but probes A's device_id.
        _register_session("tokB")
        r = client.post(
            "/api/intel/explain",
            json={
                "command": "show ip ospf neighbor",
                "device_id": "nodeA",
                "platform": "iosv",
            },
            headers={"Authorization": "Bearer tokB"},
        )
        assert r.status_code == 200
        # Cross-session lookup resolved to None → A's node_type/rejected NEVER
        # surface. The endpoint fell back to B's own payload platform, no reject.
        assert captured["device_platform"] == "iosv"
        assert captured["device_rejected"] is False

    def test_explain_kb_only_authenticated_works(self, client, monkeypatch):
        captured: dict = {}

        def _fake_explain(command, device_platform, mode, device_rejected=False):
            captured["device_platform"] = device_platform
            captured["device_rejected"] = device_rejected
            return {"ok": True}

        monkeypatch.setattr(server, "_explain_command", _fake_explain)
        _register_session("tokA")  # authenticated, but NO device attached
        r = client.post(
            "/api/intel/explain",
            json={"command": "show ip route", "platform": "iosv"},
            headers={"Authorization": "Bearer tokA"},
        )
        assert r.status_code == 200
        assert captured["device_platform"] == "iosv"
        assert captured["device_rejected"] is False

    def test_ask_kb_only_authenticated_works(self, client, monkeypatch):
        # Force the KB-absent fallback so this stays offline + fast; the point is
        # that an authenticated call is ACCEPTED (200), not rejected.
        monkeypatch.setattr(server, "_init_advisory_stack", lambda: None)
        monkeypatch.setattr(server, "_assistant", None, raising=False)
        _register_session("tokA")
        r = client.post(
            "/api/intel/ask",
            json={"question": "how do I check ospf neighbors"},
            headers={"Authorization": "Bearer tokA"},
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "fallback"


# ---------------------------------------------------------------------------
# 3. SSH device handle is not derived from the bearer token
# ---------------------------------------------------------------------------

class TestSshDeviceHandle:
    def test_handle_contains_no_bytes_of_token(self, client):
        r = client.post(
            "/api/ssh/connect",
            json={
                "host": "ts01.example.org",
                "console_user": "operator",
                "console_port": "19",
                "password": "s3cr3t",
            },
        )
        assert r.status_code == 200
        body = r.json()
        token = body["token"]
        device_id = body["device"]["id"]
        assert device_id.startswith("ssh-")
        handle = device_id[len("ssh-"):]
        # The 8-hex handle must not be the first 8 chars of the token, nor of the
        # token with dashes stripped — i.e. it discloses no part of the secret.
        assert handle != token[:8]
        assert handle != token.replace("-", "")[:8]
        assert handle not in token
        assert handle not in token.replace("-", "")

    def test_two_ssh_sessions_get_distinct_unlinked_handles(self, client):
        def _connect():
            return client.post(
                "/api/ssh/connect",
                json={
                    "host": "ts.example.com",
                    "console_user": "u",
                    "console_port": "1",
                    "password": "p",
                },
            ).json()

        a, b = _connect(), _connect()
        assert a["device"]["id"] != b["device"]["id"]
        # Neither handle leaks its own or the other session's token bytes.
        for body in (a, b):
            handle = body["device"]["id"][len("ssh-"):]
            assert handle not in body["token"].replace("-", "")
