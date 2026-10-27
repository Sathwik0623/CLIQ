#!/usr/bin/env python3
"""Tests for the Session Health Monitor (dead/expired device sessions).

Covers three layers, all offline:

1. :func:`cliq.connection.session_health.probe_session_health` — the
   transport-agnostic death detector run over the ``DeviceTransport`` surface.
   The four death modes (socket close / keepalive timeout / auth loss / persistent
   empty reads), the healthy path, and a silent idle-drop recovery are asserted
   with fake devices that stand in for BOTH the CML and SSH backends (they feed
   the identical primitives ``_check_connection`` / ``reattach``).

2. Advisory degradation — a dead runtime is treated as no-device so
   ``_walk_device_commands`` halts and ``_build_discovery_reference`` falls back to
   Learned + KB flagged "unverified on this platform".

3. WS integration — a mid-session death emits a single ``session_expired`` notice
   over the EXISTING channel, tears the device down, and releases the runtime from
   ``_DEVICE_RUNTIMES`` + the console lock, so device I/O is halted cleanly.
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from cliq.connection.live_device import ConnectionLostError, InteractiveAuthRequired
from cliq.connection.session_health import (
    DeathReason,
    HealthState,
    probe_session_health,
)
from cliq.web import server


# ---------------------------------------------------------------------------
# Fake transports for the classifier (structurally satisfy DeviceTransport).
# ---------------------------------------------------------------------------
class _FakeTransport:
    """Minimal console device driven by injectable ``_check_connection`` /
    ``reattach`` behaviors, standing in for either backend."""

    def __init__(self, *, check, reattach=None, prompt="R1#"):
        self._check = check
        self._reattach = reattach
        self._prompt = prompt
        self.check_calls = 0
        self.reattach_calls = 0

    @property
    def current_prompt(self) -> str:
        return self._prompt

    def _check_connection(self) -> None:
        self.check_calls += 1
        self._check(self)

    def reattach(self) -> str:
        self.reattach_calls += 1
        if self._reattach is None:
            raise AssertionError("reattach must not be called in this scenario")
        return self._reattach(self)


def _raise(exc):
    def _fn(_self):
        raise exc
    return _fn


def _ok(_self):
    return None


class TestProbeSessionHealth:
    def test_alive_when_check_connection_succeeds(self):
        dev = _FakeTransport(check=_ok)
        result = asyncio.run(probe_session_health(dev, timeout_s=1.0))
        assert result.state is HealthState.ALIVE
        assert result.reason == ""
        assert result.recovered is False
        assert dev.reattach_calls == 0  # healthy path never re-attaches

    def test_socket_close_when_check_and_reattach_both_drop(self):
        dev = _FakeTransport(
            check=_raise(ConnectionLostError("SSH channel closed")),
            reattach=_raise(ConnectionLostError("SSH console channel closed")),
        )
        result = asyncio.run(probe_session_health(dev, timeout_s=1.0))
        assert result.is_dead
        assert result.reason == DeathReason.SOCKET_CLOSE
        assert result.prompt_last_seen == "R1#"

    def test_keepalive_timeout_when_check_hangs(self):
        def _hang(_self):
            time.sleep(0.6)

        dev = _FakeTransport(check=_hang)
        result = asyncio.run(probe_session_health(dev, timeout_s=0.1))
        assert result.is_dead
        assert result.reason == DeathReason.KEEPALIVE_TIMEOUT
        assert dev.reattach_calls == 0  # a timeout is terminal, no re-attach

    def test_auth_loss_when_reattach_demands_login(self):
        dev = _FakeTransport(
            check=_raise(ConnectionLostError("channel reset")),
            reattach=_raise(InteractiveAuthRequired("password")),
        )
        result = asyncio.run(probe_session_health(dev, timeout_s=1.0))
        assert result.is_dead
        assert result.reason == DeathReason.AUTH_LOSS

    def test_empty_reads_when_strikes_exhausted(self):
        # The command path already saw persistent empty reads; the probe must
        # classify dead WITHOUT poking a socket that is not answering.
        dev = _FakeTransport(check=_raise(AssertionError("must not poke")))
        result = asyncio.run(
            probe_session_health(
                dev, timeout_s=1.0, empty_read_strikes=3, empty_read_limit=3,
            )
        )
        assert result.is_dead
        assert result.reason == DeathReason.EMPTY_READS
        assert dev.check_calls == 0  # short-circuited before the poke

    def test_recovered_after_silent_reattach(self):
        state = {"first": True}

        def _check(_self):
            if state["first"]:
                state["first"] = False
                raise ConnectionLostError("idle drop")
            # After a successful re-attach the verify poke succeeds.
            return None

        dev = _FakeTransport(check=_check, reattach=lambda _s: "exec")
        result = asyncio.run(probe_session_health(dev, timeout_s=1.0))
        assert result.state is HealthState.ALIVE
        assert result.recovered is True
        assert dev.reattach_calls == 1

    def test_reattach_timeout_is_keepalive_timeout(self):
        def _slow_reattach(_self):
            time.sleep(0.6)
            return "exec"

        dev = _FakeTransport(
            check=_raise(ConnectionLostError("drop")),
            reattach=_slow_reattach,
        )
        result = asyncio.run(probe_session_health(dev, timeout_s=0.1))
        assert result.is_dead
        assert result.reason == DeathReason.KEEPALIVE_TIMEOUT

    def test_no_reattach_when_disabled(self):
        dev = _FakeTransport(check=_raise(ConnectionLostError("drop")))
        result = asyncio.run(
            probe_session_health(dev, timeout_s=1.0, allow_reattach=False)
        )
        assert result.is_dead
        assert result.reason == DeathReason.SOCKET_CLOSE
        assert dev.reattach_calls == 0


# ---------------------------------------------------------------------------
# Fake SSH console device for advisory + WS integration.
# ---------------------------------------------------------------------------
class FakeSSHDevice:
    """Structurally conforms to the SSH console backend the WS loop drives.

    Flip ``dead`` to True to simulate the reservation being reclaimed: every
    device primitive then fails the way a released console does.
    """

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
        if self.dead:
            raise ConnectionError("Command failed")
        return ""

    def execute_paged(self, command: str, on_chunk=None):
        if self.dead:
            raise ConnectionError("Command failed: read timed out")
        return ("", False)

    def execute_interactive(self, line: str, on_chunk=None):
        if self.dead:
            raise ConnectionError("Command failed")
        return ("", "done")

    def context_help_start(self, partial: str):
        self.help_calls += 1
        if self.dead:
            raise ConnectionError("Command failed")
        return ("", False)

    def context_help_more(self, key: str = " "):
        return ("", False)


class TestAdvisoryDegradation:
    def test_walk_device_commands_halts_on_dead_session(self):
        rt = server.DeviceRuntime(device=FakeSSHDevice(), node_type="isr4331")
        rt.dead = True
        out = asyncio.run(server._walk_device_commands(rt, ["show ip ospf"]))
        assert out == []
        assert rt.device.help_calls == 0  # device never poked

    def test_discovery_reference_degrades_to_kb_when_dead(self, monkeypatch):
        class _StubGhost:
            def commands_with_prefix(self, prefix, max_tier=1):
                return ["show ip ospf neighbor"]

        monkeypatch.setattr(server, "_ghost_index", _StubGhost())
        rt = server.DeviceRuntime(
            device=FakeSSHDevice(), node_type="isr4331", mode="exec",
            capability=frozenset({"routing"}),
        )
        rt.dead = True
        payload = asyncio.run(
            server._build_discovery_reference(
                "list all ospf commands", rt, "exec", "dev1",
            )
        )
        assert payload is not None
        # A dead session is treated as no-device: reference is KB-derived and
        # explicitly flagged unverified on this platform.
        assert payload["device_available"] is False
        assert "UNVERIFIED" in payload["unverified_note"]
        assert rt.device.help_calls == 0  # no device ?-walk against a dead session


# ---------------------------------------------------------------------------
# WS integration — mid-session death → clean notice + registry release.
# ---------------------------------------------------------------------------
def _reset_server_state() -> None:
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


class TestWsSessionDeath:
    def _connect(self, monkeypatch):
        _reset_server_state()
        instances: list[FakeSSHDevice] = []

        class _RecordingFakeSSH(FakeSSHDevice):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                instances.append(self)

        # Patch with a real CLASS (not a factory fn) so the transport-label
        # isinstance() check still resolves the SSH backend.
        monkeypatch.setattr(server, "SSHConsoleDevice", _RecordingFakeSSH)
        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "ts01.example.org",
                "console_user": "operator",
                "console_port": "19",
                "password": "s3cr3t",
            },
        )
        body = resp.json()
        token = body["token"]
        dev_id = body["device"]["id"]
        host = server.SESSIONS[token].host
        return client, instances, token, dev_id, host

    def test_mid_session_death_emits_notice_and_releases_runtime(self, monkeypatch):
        client, instances, token, dev_id, host = self._connect(monkeypatch)
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(
                ws, "device_status", lambda m: m.get("status") == "connected",
            )
            assert (token, dev_id) in server._DEVICE_RUNTIMES
            dev = instances[-1]

            # Kill the session mid-use (reservation reclaimed / socket gone).
            dev.dead = True
            ws.send_json(
                {"type": "command", "command": "show ip route", "device_id": dev_id}
            )

            notice = _drain_until(ws, "session_expired")
            assert notice["device_id"] == dev_id
            assert notice["reason"] == DeathReason.SOCKET_CLOSE
            assert "reconnect" in notice["message"].lower()

            # A follow-up command now finds no runtime — device I/O is halted.
            ws.send_json(
                {"type": "command", "command": "show version", "device_id": dev_id}
            )
            follow = _drain_until(ws, "error_panel")
            assert follow["what"] == "Device not connected"

        # Registry + console lock released; the dead channel was torn down.
        assert (token, dev_id) not in server._DEVICE_RUNTIMES
        assert all(v != token for v in server._CONSOLE_LOCKS.values())
        assert dev.disconnect_calls >= 1

    def test_healthy_session_never_false_expires(self, monkeypatch):
        client, instances, token, dev_id, host = self._connect(monkeypatch)
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(
                ws, "device_status", lambda m: m.get("status") == "connected",
            )
            # A normal command on a healthy session — no expiry notice, device_output.
            ws.send_json(
                {"type": "command", "command": "show ip route", "device_id": dev_id}
            )
            for _ in range(80):
                msg = ws.receive_json()
                assert msg.get("type") != "session_expired"
                if msg.get("type") == "device_output":
                    break
            else:
                raise AssertionError("no device_output for a healthy command")
            assert server._DEVICE_RUNTIMES[(token, dev_id)].dead is False
            assert server._DEVICE_RUNTIMES[(token, dev_id)].empty_read_strikes == 0

    def test_no_connect_time_help_priming(self, monkeypatch):
        """Connect must NOT auto-issue any '?'-help (e.g. 'show vlan ?').

        The device-inappropriate connect-time warm-up was removed: a '?'-capture
        may fire ONLY in response to real user input. After a fresh connect the
        device's ``context_help_start`` must never have been called.
        """
        client, instances, token, dev_id, host = self._connect(monkeypatch)
        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            _drain_until(
                ws, "device_status", lambda m: m.get("status") == "connected",
            )
            # Drain the trailing connect device_output so any (removed) background
            # prewarm would have had its chance to run.
            _drain_until(ws, "device_output")
            dev = instances[-1]
            assert dev.help_calls == 0  # no connect-time 'show vlan ?' round-trip
