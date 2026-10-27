#!/usr/bin/env python3
"""Tests for in-session reconnect (browser-socket drop → resume, no re-login).

All offline. Covers the backend half of the feature:

1. Session persistence — an SSH browser-socket drop RETAINS the server-side
   session (token + device registry + held creds) and arms the grace countdown,
   releasing only the transient runtime + console lock (so a reconnect resumes
   with no re-login).
2. Resume under the SAME token — reopening the WebSocket with the retained token
   cancels the grace countdown and re-issuing ``connect_device`` emits
   ``device_status: connected`` (no re-login, no fresh session).
3. Heartbeat — a ``ping`` is answered with a ``pong`` echoing the client
   timestamp (pure liveness, no device I/O).
4. Grace reaper — a socket-less session left idle past the grace TTL is reaped:
   the session, its device registry (held SSH password), stale runtimes, and any
   console lock it owned are all released.

The drop bookkeeping runs in the WS handler's ``finally`` which the Starlette
TestClient defers to portal shutdown, so it is not observable inside the test
that owns the socket. It is therefore exercised through the same helper the
``finally`` calls (:func:`server._release_socket_registrations`) over state
seeded directly — the deterministic equivalent of a socket drop.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from cliq.connection.live_device import ConnectionLostError
from cliq.web import server


# ---------------------------------------------------------------------------
# Fake SSH console device (mirrors the WS backend the loop drives).
# ---------------------------------------------------------------------------
class FakeSSHDevice:
    def __init__(self, *, host="", username="", password="", port_user="",
                 device_type="cisco_ios"):
        self.node_type = "isr4331"
        self.capability = frozenset({"routing"})
        self.pid = ""
        self._prompt = "C8k>"
        self._connected = False
        self.dead = False
        self.disconnect_calls = 0
        self.help_calls = 0

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
        self._connected = True

    def reattach(self) -> str:
        if self.dead:
            raise ConnectionLostError("SSH console channel closed")
        return "exec"

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def _check_connection(self) -> None:
        if self.dead:
            raise ConnectionLostError("SSH console channel closed")

    def execute(self, command: str) -> str:
        return ""

    def execute_paged(self, command: str, on_chunk=None):
        return ("", False)

    def execute_interactive(self, line: str, on_chunk=None):
        return ("", "done")

    def context_help_start(self, partial: str):
        self.help_calls += 1
        return ("", False)

    def context_help_more(self, key: str = " "):
        return ("", False)


def _reset_server_state() -> None:
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._DISCOVERY_WALK_CACHE.clear()
    server._SESSION_LAST_SEEN.clear()


def _seed_ssh_session(*, connected: bool = True):
    """Seed a live SSH session directly (session + device registry + console
    lock + runtime) — the deterministic equivalent of a connected WS session.
    Returns ``(token, dev_id, console_path, device)``.
    """
    _reset_server_state()
    token = "tok-reconnect-1"
    dev_id = "ssh-abc12345"
    console_path = "ssh://host/19/user"
    server.SESSIONS[token] = server.AuthSession(
        host="host", username="user", password="s3cr3t", auth=None,
        connection_type="ssh",
    )
    server.DEVICES_BY_TOKEN[token] = {
        dev_id: {
            "id": dev_id, "connection_type": "ssh", "ssh_host": "host",
            "ssh_console_user": "user", "ssh_console_port": "19",
            "console_path": console_path, "mode": "exec",
        }
    }
    device = FakeSSHDevice()
    device.connect()
    if connected:
        server._CONSOLE_LOCKS[console_path] = token
        server._DEVICE_RUNTIMES[(token, dev_id)] = server.DeviceRuntime(
            device=device, node_type="isr4331", mode="exec",
            capability=frozenset({"routing"}),
        )
    return token, dev_id, console_path, device


def _drain_until(ws, msg_type, predicate=None, limit=80):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == msg_type and (predicate is None or predicate(msg)):
            return msg
    raise AssertionError(f"did not receive a {msg_type!r} message")


class TestSessionPersistence:
    def test_socket_drop_retains_ssh_session_and_arms_grace(self):
        token, dev_id, console_path, _ = _seed_ssh_session()

        # Deterministic equivalent of the WS handler's drop bookkeeping.
        server._release_socket_registrations(token)

        # Session + device registry (held creds) survive so a reconnect resumes
        # with NO re-login, and the grace countdown is armed.
        assert token in server.SESSIONS
        assert dev_id in server.DEVICES_BY_TOKEN[token]
        assert token in server._SESSION_LAST_SEEN
        # The dead socket's transient runtime + console lock were torn down so
        # the console is free for a fresh attach on reconnect.
        assert (token, dev_id) not in server._DEVICE_RUNTIMES
        assert console_path not in server._CONSOLE_LOCKS

    def test_socket_drop_releases_console_lock_for_reattach(self):
        token, dev_id, console_path, _ = _seed_ssh_session()
        assert server._CONSOLE_LOCKS[console_path] == token
        server._release_socket_registrations(token)
        # No lingering lock owned by the dropped token.
        assert all(owner != token for owner in server._CONSOLE_LOCKS.values())

    def test_ping_is_answered_with_pong_echoing_ts(self, monkeypatch):
        _reset_server_state()
        instances: list[FakeSSHDevice] = []

        class _Rec(FakeSSHDevice):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                instances.append(self)

        monkeypatch.setattr(server, "SSHConsoleDevice", _Rec)
        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={"host": "h", "console_user": "u", "console_port": "19",
                  "password": "p"},
        )
        token = resp.json()["token"]
        host = server.SESSIONS[token].host
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "ping", "ts": 123456})
            pong = _drain_until(ws, "pong")
            assert pong["ts"] == 123456


class TestReconnectResume:
    def _connect(self, monkeypatch):
        _reset_server_state()
        instances: list[FakeSSHDevice] = []

        class _Rec(FakeSSHDevice):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                instances.append(self)

        monkeypatch.setattr(server, "SSHConsoleDevice", _Rec)
        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={"host": "h", "console_user": "u", "console_port": "19",
                  "password": "p"},
        )
        token = resp.json()["token"]
        dev_id = resp.json()["device"]["id"]
        host = server.SESSIONS[token].host
        return client, token, dev_id, host

    def test_reconnect_under_same_token_resumes_without_relogin(self, monkeypatch):
        client, token, dev_id, host = self._connect(monkeypatch)

        # First live session.
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(ws, "device_status", lambda m: m.get("status") == "connected")

        # Simulate the browser-socket drop having armed the grace countdown.
        server._release_socket_registrations(token)
        assert token in server._SESSION_LAST_SEEN

        # Reconnect under the SAME token/host — NO new /api/ssh/connect, NO
        # re-login. Accepting the socket cancels the grace countdown and
        # re-issuing connect_device resumes the device under the same token.
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws2:
            assert token not in server._SESSION_LAST_SEEN  # resumed → grace cleared
            ws2.send_json({"type": "connect_device", "device_id": dev_id})
            status = _drain_until(
                ws2, "device_status", lambda m: m.get("status") == "connected",
            )
            assert status["device_id"] == dev_id
            assert (token, dev_id) in server._DEVICE_RUNTIMES

        # The same single session carried across the drop/resume.
        assert token in server.SESSIONS

    def test_stale_token_is_rejected_not_resumed(self):
        _reset_server_state()
        client = TestClient(server.app)
        # No such session — the WS must refuse (no silent fresh session).
        try:
            with client.websocket_connect("/ws?token=bogus&host=h"):
                pass
        except Exception:
            pass
        assert "bogus" not in server.SESSIONS


class TestGraceReaper:
    def test_reaper_drops_idle_session_and_wipes_creds(self):
        token, dev_id, console_path, _ = _seed_ssh_session(connected=False)
        # Socket-less and idle past the grace window.
        server._SESSION_LAST_SEEN[token] = (
            time.monotonic() - server._SESSION_GRACE_TTL_S - 1.0
        )
        server._CONSOLE_LOCKS[console_path] = token

        server._reap_stale_sessions(time.monotonic())

        # Session, device registry (held SSH password), last-seen, and lock gone.
        assert token not in server.SESSIONS
        assert token not in server.DEVICES_BY_TOKEN
        assert token not in server._SESSION_LAST_SEEN
        assert all(k[0] != token for k in server._DEVICE_RUNTIMES)
        assert console_path not in server._CONSOLE_LOCKS

    def test_reaper_keeps_session_within_grace_window(self):
        token, dev_id, console_path, _ = _seed_ssh_session(connected=False)
        # Only just dropped — well inside the grace window.
        server._SESSION_LAST_SEEN[token] = time.monotonic()

        server._reap_stale_sessions(time.monotonic())

        assert token in server.SESSIONS
        assert token in server._SESSION_LAST_SEEN
