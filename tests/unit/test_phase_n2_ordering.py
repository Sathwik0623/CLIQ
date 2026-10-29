"""Unit and integration tests for CLIQ Phase N.2: Disconnect Event Ordering and Terminal UX."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
import pytest
from starlette.testclient import TestClient

from cliq.connection.live_device import ConnectionLostError
from cliq.web import server
from tests.unit.test_session_health import FakeSSHDevice


class FakeDirectDeviceN2(FakeSSHDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.node_type = "c9200"
        self._prompt = "cat9200#"
        self._connected = True
        self.commands_executed = []

    def execute_paged(self, command: str, on_chunk=None):
        cmd = command.strip().lower()
        self.commands_executed.append(command)
        if self.dead:
            raise ConnectionLostError("Connection closed")
        if cmd in ("exit", "quit", "logout"):
            if "(config" in self._prompt:
                self._prompt = "cat9200#"
                return ("", False)
            else:
                self.dead = True
                self._connected = False
                raise ConnectionLostError("Remote host closed connection")
        elif cmd == "disable":
            self._prompt = "cat9200>"
            return ("", False)
        return (f"output of {command}", False)


def _reset_server_state():
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._ACTIVE_SOCKETS_BY_TOKEN.clear()
    server._SESSION_LAST_SEEN.clear()


class TestPhaseN2OrderingAndUX:
    """Validate Phase N.2 disconnect event ordering, session_gen, seq, and queued command guard."""

    def test_session_gen_and_seq_in_lifecycle_events(self, monkeypatch):
        _reset_server_state()
        monkeypatch.setattr(server, "SSHConsoleDevice", FakeDirectDeviceN2)

        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "password",
            },
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        dev_id = resp.json()["device"]["id"]
        host = server.SESSIONS[token].host

        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})

            # Check connect_device events carry session_gen and seq
            status_seen = False
            session_gen = None
            seq_numbers = []
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("type") == "device_status" and msg.get("status") == "connected":
                    status_seen = True
                    session_gen = msg.get("session_gen")
                    assert session_gen is not None and len(session_gen) > 0
                    assert "seq" in msg
                    seq_numbers.append(msg["seq"])
                elif msg.get("type") == "device_output" and msg.get("prompt") == "cat9200#":
                    assert msg.get("session_gen") == session_gen
                    assert "seq" in msg
                    seq_numbers.append(msg["seq"])
                    break

            assert status_seen
            assert len(seq_numbers) >= 2
            # Sequence numbers should be strictly monotonically increasing
            assert seq_numbers[1] > seq_numbers[0]

            # Run a normal command
            ws.send_json({"type": "command", "device_id": dev_id, "command": "show version"})
            cmd_out_seen = False
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("type") == "device_output" and "output of show version" in msg.get("output", ""):
                    cmd_out_seen = True
                    assert msg.get("session_gen") == session_gen
                    assert msg.get("seq") > seq_numbers[-1]
                    seq_numbers.append(msg["seq"])
                    break
            assert cmd_out_seen

            # Execute 'exit'
            ws.send_json({"type": "command", "device_id": dev_id, "command": "exit"})

            exit_out_seen = False
            exit_status_seen = False
            exit_disc_seen = False

            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "device_output" and "[Connection to device closed]" in msg.get("output", ""):
                    exit_out_seen = True
                    assert msg.get("session_gen") == session_gen
                    assert msg.get("seq") > seq_numbers[-1]
                    seq_numbers.append(msg["seq"])
                elif msg.get("type") == "device_status" and msg.get("status") == "disconnected":
                    exit_status_seen = True
                    assert msg.get("session_gen") == session_gen
                    assert msg.get("seq") > seq_numbers[-1]
                    seq_numbers.append(msg["seq"])
                elif msg.get("type") == "device_disconnected":
                    exit_disc_seen = True
                    assert msg.get("session_gen") == session_gen
                    assert msg.get("seq") > seq_numbers[-1]
                    seq_numbers.append(msg["seq"])
                    assert "⚡ CLIQ: Device disconnected" in msg.get("message", "")
                    break

            assert exit_out_seen
            assert exit_status_seen
            assert exit_disc_seen

    def test_queued_commands_after_exit_are_discarded(self, monkeypatch):
        _reset_server_state()
        device_instances = []

        def make_fake_dev(*args, **kwargs):
            dev = FakeDirectDeviceN2(*args, **kwargs)
            device_instances.append(dev)
            return dev

        monkeypatch.setattr(server, "SSHConsoleDevice", make_fake_dev)

        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "password",
            },
        )
        token = resp.json()["token"]
        dev_id = resp.json()["device"]["id"]
        host = server.SESSIONS[token].host

        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("type") == "device_status" and msg.get("status") == "connected":
                    break

            # Send exit and immediately pipe show ip interface brief
            ws.send_json({"type": "command", "device_id": dev_id, "command": "exit"})
            ws.send_json({"type": "command", "device_id": dev_id, "command": "show ip interface brief"})

            received_disconnected = False
            queued_output_seen = False

            for _ in range(10):
                msg = ws.receive_json()
                if msg.get("type") == "device_output" and "output of show ip interface brief" in msg.get("output", ""):
                    queued_output_seen = True
                if msg.get("type") == "device_disconnected":
                    received_disconnected = True
                    break

            assert received_disconnected, "Should have received device_disconnected"
            assert not queued_output_seen, "Queued command after exit should be discarded without executing"

            # Check device history
            dev = device_instances[0]
            assert "exit" in dev.commands_executed
            assert "show ip interface brief" not in dev.commands_executed

    def test_submode_transition_does_not_disconnect(self, monkeypatch):
        _reset_server_state()

        class FakeSubmodeDevice(FakeDirectDeviceN2):
            def execute_paged(self, command: str, on_chunk=None):
                cmd = command.strip().lower()
                if cmd == "disable":
                    self._prompt = "cat9200>"
                    return ("", False)
                return super().execute_paged(command, on_chunk)

        monkeypatch.setattr(server, "SSHConsoleDevice", FakeSubmodeDevice)

        client = TestClient(server.app)
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "password",
            },
        )
        token = resp.json()["token"]
        dev_id = resp.json()["device"]["id"]
        host = server.SESSIONS[token].host

        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("type") == "device_status" and msg.get("status") == "connected":
                    break

            # Run 'disable' to transition # -> >
            ws.send_json({"type": "command", "device_id": dev_id, "command": "disable"})

            got_exec_prompt = False
            for _ in range(15):
                msg = ws.receive_json()
                if msg.get("type") == "device_output" and msg.get("prompt") == "cat9200>":
                    got_exec_prompt = True
                    break

            assert got_exec_prompt, "Prompt should transition cleanly to cat9200>"
            assert (token, dev_id) in server._DEVICE_RUNTIMES
            assert server._DEVICE_RUNTIMES[(token, dev_id)].status == "connected"
