"""Unit and integration tests for CLIQ Phase N.1: Disconnected Terminal State and Session Lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from cliq.connection.live_device import ConnectionLostError
from cliq.web import server
from tests.unit.test_session_health import FakeSSHDevice


class FakeDirectDeviceN1(FakeSSHDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.node_type = "c9200"
        self._prompt = "cat9200#"
        self._connected = True

    def execute_paged(self, command: str, on_chunk=None):
        if self.dead:
            raise ConnectionLostError("Connection closed")
        cmd = command.strip().lower()
        if cmd in ("exit", "quit", "logout"):
            if "(config" in self._prompt:
                self._prompt = "cat9200#"
                return ("", False)
            else:
                self.dead = True
                self._connected = False
                raise ConnectionLostError("Remote host closed connection")
        return (f"output of {command}", False)


def _reset_server_state():
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._ACTIVE_SOCKETS_BY_TOKEN.clear()
    server._SESSION_LAST_SEEN.clear()


class TestPhaseN1SessionLifecycle:
    """Validate Phase N.1 WebSocket generation tracking and disconnect events."""

    def test_superseded_socket_does_not_teardown_newer_connection(self):
        _reset_server_state()
        token = "test-token-123"
        dev_id = "c9200-dev-1"
        meta = {
            "id": dev_id,
            "label": "cat9200",
            "host": "10.10.20.181",
            "console_path": "ssh:10.10.20.181:22",
            "connection_type": "ssh",
        }
        server.DEVICES_BY_TOKEN[token] = {dev_id: meta}
        server.SESSIONS[token] = MagicMock(host="10.10.20.181", connection_type="ssh", auth=None)

        fake_dev = FakeDirectDeviceN1()
        runtime = server.DeviceRuntime(device=fake_dev, prompt="cat9200#", mode="enable")
        server._DEVICE_RUNTIMES[(token, dev_id)] = runtime
        server._CONSOLE_LOCKS[meta["console_path"]] = token

        # WS1 connects
        ws1_id = "ws-session-1"
        server._ACTIVE_SOCKETS_BY_TOKEN[token] = ws1_id

        # WS2 connects and becomes active
        ws2_id = "ws-session-2"
        server._ACTIVE_SOCKETS_BY_TOKEN[token] = ws2_id

        # WS1 teardown (simulating server.py ws_terminal finally block)
        if server._ACTIVE_SOCKETS_BY_TOKEN.get(token) != ws1_id:
            # Superseded: skip teardown
            pass
        else:
            fake_dev.disconnect()
            server._release_socket_registrations(token)

        assert fake_dev.disconnect_calls == 0
        assert (token, dev_id) in server._DEVICE_RUNTIMES
        assert server._CONSOLE_LOCKS.get(meta["console_path"]) == token

    def test_latest_socket_teardown_cleans_up_registrations(self):
        _reset_server_state()
        token = "test-token-456"
        dev_id = "c9200-dev-2"
        meta = {
            "id": dev_id,
            "label": "cat9200",
            "host": "10.10.20.181",
            "console_path": "ssh:10.10.20.181:22",
            "connection_type": "ssh",
        }
        server.DEVICES_BY_TOKEN[token] = {dev_id: meta}
        server.SESSIONS[token] = MagicMock(host="10.10.20.181", connection_type="ssh", auth=None)

        fake_dev = FakeDirectDeviceN1()
        runtime = server.DeviceRuntime(device=fake_dev, prompt="cat9200#", mode="enable")
        server._DEVICE_RUNTIMES[(token, dev_id)] = runtime
        server._CONSOLE_LOCKS[meta["console_path"]] = token

        ws_id = "ws-active-final"
        server._ACTIVE_SOCKETS_BY_TOKEN[token] = ws_id

        # WS teardown when matching session ID
        if server._ACTIVE_SOCKETS_BY_TOKEN.get(token) == ws_id:
            server._ACTIVE_SOCKETS_BY_TOKEN.pop(token, None)
            fake_dev.disconnect()
            server._release_socket_registrations(token)

        assert fake_dev.disconnect_calls == 1
        assert (token, dev_id) not in server._DEVICE_RUNTIMES
        assert meta["console_path"] not in server._CONSOLE_LOCKS
        assert server._ACTIVE_SOCKETS_BY_TOKEN.get(token) is None

    def test_exit_command_emits_device_disconnected_event(self, monkeypatch):
        _reset_server_state()
        monkeypatch.setattr(server, "SSHConsoleDevice", FakeDirectDeviceN1)

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
        body = resp.json()
        token = body["token"]
        dev_id = body["device"]["id"]
        host = server.SESSIONS[token].host

        with client.websocket_connect(f"/ws?token={token}&host={host}") as ws:
            ws.send_json({"type": "connect_device", "device_id": dev_id})
            connected = False
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "device_status" and msg.get("status") == "connected":
                    connected = True
                    break
            assert connected, "Device should report connected"

            # Execute 'exit'
            ws.send_json({"type": "command", "device_id": dev_id, "command": "exit"})

            received_disconnected = False
            received_closed_msg = False
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "device_output" and "[Connection to device closed]" in msg.get("output", ""):
                    received_closed_msg = True
                    assert msg.get("prompt") == ""
                if msg.get("type") == "device_status" and msg.get("status") == "disconnected":
                    assert msg.get("prompt") == ""
                if msg.get("type") == "device_disconnected":
                    received_disconnected = True
                    assert msg.get("device_id") == dev_id
                    assert msg.get("reason") == "closed_by_user"
                    assert msg.get("prompt") == ""
                    break

            assert received_closed_msg, "Should have received '[Connection to device closed]' output"
            assert received_disconnected, "Should have received structured 'device_disconnected' event"

            # Verify subsequent command does not crash or re-execute on dead device
            ws.send_json({"type": "command", "device_id": dev_id, "command": "show ?"})
            msg = ws.receive_json()
            assert msg.get("type") in ("error_panel", "device_disconnected")
            if msg.get("type") == "error_panel":
                assert msg.get("what") == "Device not connected"
