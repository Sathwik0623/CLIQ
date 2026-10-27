#!/usr/bin/env python3
"""Regression tests for the console re-attach reservation gate.

An expired CML reservation was still operable through a SECOND door: CLIQ's
console re-attach performs its own console SSH auth, which is NOT reservation-
gated. While the terminal-server socket is still physically alive (grace period
before node teardown) the re-attach succeeded regardless of reservation validity
— a console-layer variant of the cached-token bypass.

These tests assert, all offline:

1. ``CMLAuthManager.check_reservation_state`` — the SINGLE SOURCE OF TRUTH reused
   by both the API gate and the console re-attach gate (active / unauthorized /
   expired).
2. WS integration — the CML re-attach path consults that check FIRST: an expired
   / unauthorized reservation is REFUSED before any console auth, no runtime is
   registered, and the device's ``reattach`` is never called; a valid reservation
   re-attaches normally.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cliq.connection import cml_auth
from cliq.connection.cml_auth import CMLAuthManager
from cliq.utils import observability as obs
from cliq.web import server


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    # The reservation authority + policy are env-driven; keep every test
    # deterministic (default policy 'warn', no key).
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
    monkeypatch.delenv("CLIQ_ALLOW_UNVERIFIED_RESERVATION", raising=False)
    monkeypatch.delenv("CLIQ_RESERVATION_POLICY", raising=False)
    yield


# ── check_reservation_state → NetSim reservation authority ──────────────────
class _Res:
    def __init__(self, *, available=True, grace=False):
        self.is_available = available
        self.in_grace_period = grace


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.ok = status < 400


class _Sess:
    """Controller-liveness session stand-in (used only for degraded fallback)."""

    def __init__(self, status=None, raise_exc=None):
        self.verify = False
        self._status = status
        self._raise = raise_exc

    def get(self, *_a, **_kw):
        if self._raise is not None:
            raise self._raise
        return _Resp(self._status)


def _fake_netsim(result=None, raise_exc=None):
    class _FakeNetSim:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def find_by_cml_host(self, _host):
            if raise_exc is not None:
                raise raise_exc
            return result

    return _FakeNetSim


def _mgr(session=None, token="tok", expired=False):
    import time
    auth = CMLAuthManager()
    auth.host = "cml.example.com"
    auth.base = "https://cml.example.com/api/v0"
    auth.token = token
    auth.token_expiry = (time.time() - 3600) if expired else (time.time() + 3600)
    if session is not None:
        auth._session = session
    return auth


class TestCheckReservationState:
    def test_active_when_token_valid_and_controller_live(self):
        auth = _mgr(_Sess(status=200))
        assert auth.check_reservation_state() == "active"

    def test_expired_when_token_expired(self):
        auth = _mgr(_Sess(status=200), expired=True)
        assert auth.check_reservation_state() == "expired"

    def test_expired_when_controller_rejects_token(self):
        auth = _mgr(_Sess(status=401))
        assert auth.check_reservation_state() == "expired"

    def test_unverifiable_when_controller_unreachable(self):
        auth = _mgr(_Sess(raise_exc=Exception("network timeout")))
        assert auth.check_reservation_state() == "unverifiable"
        det = auth.check_reservation_detail()
        assert det.state == "unverifiable"
        assert det.cause == "controller_unreachable"

    def test_detail_active_and_expired_have_no_cause(self):
        assert _mgr(_Sess(status=200)).check_reservation_detail().cause == ""
        assert _mgr(_Sess(status=401)).check_reservation_detail().cause == ""

    def test_validate_cached_token_live_uses_controller(self):
        assert _mgr(_Sess(status=200))._validate_cached_token_live() is True
        assert _mgr(_Sess(status=401))._validate_cached_token_live() is False

    def test_is_lab_accessible_checks_controller(self):
        assert _mgr(_Sess(status=200)).is_lab_accessible("lab1") is True
        assert _mgr(_Sess(status=403)).is_lab_accessible("lab1") is False
        assert _mgr(_Sess(status=404)).is_lab_accessible("lab1") is False


# ── Reservation POLICY resolution (shared by both gates) ─────────────────────
class TestReservationPolicy:
    def test_default_policy_is_warn(self):
        assert cml_auth.reservation_policy() == "warn"

    def test_enforce_via_env(self, monkeypatch):
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        assert cml_auth.reservation_policy() == "enforce"

    def test_legacy_optin_alias_maps_to_warn(self, monkeypatch):
        monkeypatch.setenv("CLIQ_ALLOW_UNVERIFIED_RESERVATION", "1")
        assert cml_auth.reservation_policy() == "warn"

    def test_active_always_allowed(self):
        d = cml_auth.resolve_reservation_access("active")
        assert d.allowed and d.outcome == "allowed" and not d.warn

    def test_expired_always_refused_regardless_of_policy(self, monkeypatch):
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "warn")
        d = cml_auth.resolve_reservation_access("expired")
        assert not d.allowed and d.outcome == "refused"
        assert "expired" in d.message.lower()

    def test_unverifiable_warn_allows_with_banner(self):
        d = cml_auth.resolve_reservation_access("unverifiable")  # default warn
        assert d.allowed and d.warn and d.outcome == "allowed_warned"
        assert "unverified access" in d.message.lower()
        assert "may be operating an expired lab" in d.message.lower()

    def test_unverifiable_enforce_refuses(self, monkeypatch):
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        d = cml_auth.resolve_reservation_access("unverifiable")
        assert not d.allowed and d.outcome == "refused"
        assert "cannot verify" in d.message.lower()

    def test_key_invalid_warn_uses_rotation_message(self):
        d = cml_auth.resolve_reservation_access("unverifiable", "key_invalid")
        assert d.allowed and d.warn and d.outcome == "allowed_warned"
        assert "rotate every 30 days" in d.message.lower()
        assert "regenerate" in d.message.lower()

    def test_key_invalid_enforce_keeps_rotation_message(self, monkeypatch):
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        d = cml_auth.resolve_reservation_access("unverifiable", "key_invalid")
        assert not d.allowed and d.outcome == "refused"
        assert "rotate every 30 days" in d.message.lower()

    def test_no_key_message_says_configure_not_rotate(self):
        d = cml_auth.resolve_reservation_access("unverifiable", "no_key")
        assert "configure cliq_netsim_api_key" in d.message.lower()
        assert "rotate" not in d.message.lower()

    def test_netsim_unreachable_message_distinct(self):
        d = cml_auth.resolve_reservation_access("unverifiable", "netsim_unreachable")
        assert "could not reach" in d.message.lower()
        assert "rotate" not in d.message.lower()


# ── WS integration — CML re-attach path ─────────────────────────────────────
class FakeCMLDevice:
    """Minimal CML console stand-in (NOT an SSHConsoleDevice, so the transport
    label resolves to 'cml'). Records whether console auth (reattach) ran."""

    def __init__(self, *, gateway=None, username=None, password=None, path=None, **_kw):
        self.node_type = "iosv"
        self.capability = None
        self.pid = ""
        self._prompt = "C8k#"
        self._connected = False
        self.reattach_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def current_prompt(self) -> str:
        return self._prompt

    @property
    def awaiting_auth(self) -> bool:
        return False

    def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    def reattach(self) -> str:
        # This is the console SSH auth ("Authentication (password) successful!").
        # It must NEVER run for an unauthorized reservation.
        self.reattach_calls += 1
        self._connected = True
        return "exec"

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def _check_connection(self) -> None:
        return None

    def execute(self, command: str) -> str:
        return ""


class _FakeAuth:
    """Stands in for the session's CMLAuthManager reservation DETECTION. The
    server gate applies the shared policy to whatever state this returns."""

    def __init__(self, state: str):
        self._state = state
        self.calls = 0

    def check_reservation_state(self, *, proxy=None) -> str:
        self.calls += 1
        return self._state


def _reset() -> None:
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._DISCOVERY_WALK_CACHE.clear()


def _drain_until(ws, msg_type, predicate=None, limit=80):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == msg_type and (predicate is None or predicate(msg)):
            return msg
    raise AssertionError(f"did not receive a {msg_type!r} message")


class TestConsoleReattachGate:
    def _setup(self, monkeypatch, reservation_state):
        _reset()
        host = "cml.example.com"
        token = "tok-123"
        fake_auth = _FakeAuth(reservation_state)
        server.SESSIONS[token] = server.AuthSession(
            host=host, username="cli_test", password="pw", auth=fake_auth,
        )
        device_id = "n0"
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "lab_id": "cli_test1",
                "label": "cat8000v-0",
                "type": "iosv",
                "connection_type": "cml",
                "console_path": "/cli_test1/cat8000v-0/0",
            }
        }
        # Force the RE-ATTACH path: prior session memory exists.
        monkeypatch.setattr(
            server.SESSION_LOG, "load_state",
            lambda _k: {"mode": "exec", "hostname": "C8k#"},
        )
        monkeypatch.setattr(server.SESSION_LOG, "get_history", lambda _k: [])
        monkeypatch.setattr(server.SESSION_LOG, "save_state", lambda *a, **k: None)
        monkeypatch.setattr(server, "CMLConsoleDevice", FakeCMLDevice)
        client = TestClient(server.app)
        return client, token, device_id, host, fake_auth

    def test_expired_reservation_refuses_reattach(self, monkeypatch):
        client, token, dev_id, host, fake_auth = self._setup(monkeypatch, "expired")
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            panel = _drain_until(ws, "error_panel")
            assert "access denied" in panel["what"].lower()
            assert "reservation" in panel["what"].lower()
        # The reservation check ran, console auth did NOT, runtime not registered.
        assert fake_auth.calls == 1
        assert (token, dev_id) not in server._DEVICE_RUNTIMES
        # Console lock released so a later (valid) attempt can attach.
        assert all(v != token for v in server._CONSOLE_LOCKS.values())

    def test_expired_refused_even_under_warn_policy(self, monkeypatch):
        # A VERIFIED-expired reservation is refused regardless of policy.
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "warn")
        client, token, dev_id, host, _auth = self._setup(monkeypatch, "expired")
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            panel = _drain_until(ws, "error_panel")
            assert "access denied" in panel["what"].lower()
        assert (token, dev_id) not in server._DEVICE_RUNTIMES

    def test_unverifiable_refused_under_enforce(self, monkeypatch):
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        client, token, dev_id, host, fake_auth = self._setup(monkeypatch, "unverifiable")
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            panel = _drain_until(ws, "error_panel")
            assert "access denied" in panel["what"].lower()
            assert "cannot verify" in panel["what"].lower()
        assert (token, dev_id) not in server._DEVICE_RUNTIMES

    def test_unverifiable_allowed_under_warn_default(self, monkeypatch):
        # Default policy 'warn' → an unverifiable reservation re-attaches (the
        # regression fix: valid labs are usable without a NetSim key).
        client, token, dev_id, host, fake_auth = self._setup(monkeypatch, "unverifiable")
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(
                ws, "device_status", lambda m: m.get("status") == "connected",
            )
            assert (token, dev_id) in server._DEVICE_RUNTIMES
            assert server._DEVICE_RUNTIMES[(token, dev_id)].device.reattach_calls == 1

    def test_console_auth_alone_never_grants_access(self, monkeypatch):
        # Even though the fake console would happily authenticate (reattach), an
        # inactive reservation must block it BEFORE any console auth runs.
        client, token, dev_id, host, _auth = self._setup(monkeypatch, "expired")
        captured: dict = {}

        real_cls = FakeCMLDevice

        class _Recording(real_cls):
            def __init__(self, **kw):
                super().__init__(**kw)
                captured["dev"] = self

        monkeypatch.setattr(server, "CMLConsoleDevice", _Recording)
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(ws, "error_panel")
        # The device may be constructed, but its console auth must never run.
        if "dev" in captured:
            assert captured["dev"].reattach_calls == 0
            assert captured["dev"].connect_calls == 0

    def test_valid_reservation_reattaches_normally(self, monkeypatch):
        client, token, dev_id, host, fake_auth = self._setup(monkeypatch, "active")
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            status = _drain_until(
                ws, "device_status", lambda m: m.get("status") == "connected",
            )
            assert status["device_id"] == dev_id
            # Reservation confirmed active, console auth ran, runtime registered
            # (asserted while the socket is open, before the disconnect cleanup).
            assert fake_auth.calls == 1
            assert (token, dev_id) in server._DEVICE_RUNTIMES
            assert server._DEVICE_RUNTIMES[(token, dev_id)].device.reattach_calls == 1
