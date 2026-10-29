"""Unit tests for Phase Q: CML Console Lifecycle, Initial Mode, Enter Forwarding,
and Reconnect Stability.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cliq.connection.live_device import (
    CMLConsoleDevice,
    mode_from_prompt,
)
from cliq.web import server
from cliq.web.server import app

client = TestClient(app)


def recv_msg_of_type(ws, msg_type: str, max_msgs: int = 30) -> dict[str, Any]:
    """Receive messages from WebSocket until a message of msg_type arrives, ignoring advisory events."""
    for _ in range(max_msgs):
        m = ws.receive_json()
        if m.get("type") == msg_type:
            return m
    raise AssertionError(f"Did not receive message of type '{msg_type}' within {max_msgs} messages")


def recv_command_response(ws, max_msgs: int = 30) -> tuple[str, str, dict[str, Any]]:
    """Receive streamed chunks, device_output, and device_status for an executed command.

    Returns (full_output, prompt, status_dict).
    """
    chunks = []
    dev_out = None
    dev_status = None
    for _ in range(max_msgs):
        m = ws.receive_json()
        m_type = m.get("type")
        if m_type == "device_stream":
            chunks.append(m.get("chunk", ""))
        elif m_type == "device_output":
            dev_out = m
            if dev_status is not None:
                break
        elif m_type == "device_status":
            dev_status = m
            if dev_out is not None:
                break
    full_output = "".join(chunks) + (dev_out.get("output", "") if dev_out else "")
    prompt = dev_out.get("prompt", "") if dev_out else ""
    return full_output, prompt, (dev_status or {})


class FakeCmlConsoleChannel:
    """Mock Paramiko channel simulating CML console behavior with R1."""

    def __init__(self, initial_prompt: str = "R1#"):
        self.prompt = initial_prompt
        self.closed = False
        self.out_buffer = bytearray()
        self.commands_received: list[str] = []
        self._parked_at_return_banner = False
        self._lock = threading.Lock()

    def send(self, data: bytes | str) -> int:
        if isinstance(data, str):
            bdata = data.encode("utf-8")
        else:
            bdata = bytes(data)
        with self._lock:
            text = bdata.decode("utf-8", errors="replace")
            self.commands_received.append(text)

            # If parked at 'Press RETURN to get started.'
            if self._parked_at_return_banner:
                # Any Enter (\r\n) wakes the router back up to exec mode (R1>)
                self._parked_at_return_banner = False
                self.prompt = "R1>"
                self.out_buffer.extend(b"\r\n\r\n*Oct 29 10:00:00.000: %SYS-5-CONFIG_I: Configured from console\r\nR1>\r\n")
                return len(bdata)

            cmd = text.strip().lower()
            if cmd in ("exit", "logout", "quit"):
                # EXEC logout displays CML console banner and parks
                self._parked_at_return_banner = True
                self.out_buffer.extend(
                    b"\r\n\r\nR1 con0 is now available\r\n\r\n\r\nPress RETURN to get started.\r\n"
                )
                return len(bdata)

            if cmd == "enable":
                if self.prompt.endswith("#"):
                    self.out_buffer.extend(f"\r\n{self.prompt}\r\n".encode("utf-8"))
                else:
                    self.out_buffer.extend(b"\r\nPassword: ")
                return len(bdata)

            if cmd == "":
                # Bare return
                self.out_buffer.extend(f"\r\n{self.prompt}\r\n".encode("utf-8"))
                return len(bdata)

            # Generic command output
            self.out_buffer.extend(f"\r\nCommand output for {cmd}\r\n{self.prompt}\r\n".encode("utf-8"))
            return len(bdata)

    def recv(self, nbytes: int) -> bytes:
        with self._lock:
            chunk = bytes(self.out_buffer[:nbytes])
            del self.out_buffer[:nbytes]
            return chunk

    def recv_ready(self) -> bool:
        with self._lock:
            return len(self.out_buffer) > 0

    def send_ready(self) -> bool:
        return not self.closed

    def gettimeout(self) -> float:
        return 1.0

    def settimeout(self, timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class MockCMLDeviceForLifecycle(CMLConsoleDevice):
    """CML device test double for Phase Q lifecycle testing."""

    def __init__(self, initial_prompt: str = "R1#"):
        self.gateway = "cml.example.com"
        self.username = "developer"
        self.password = "secret"
        self.path = "/default-lab/R1/0"
        self.timeout = 5
        self._current_prompt = initial_prompt
        self._connected = True
        self._chan = FakeCmlConsoleChannel(initial_prompt=initial_prompt)
        self._io_lock = threading.RLock()
        self._auth_stage_timer = None
        self._awaiting_auth = False
        self._client = MagicMock()

    def connect(self) -> None:
        self._connected = True

    def reattach(self) -> str:
        self._connected = True
        return mode_from_prompt(self._current_prompt)

    def disconnect(self) -> None:
        self._connected = False
        if self._chan:
            self._chan.close()

    def execute_paged(self, command: str, on_chunk=None) -> tuple[str, bool]:
        if not self.is_connected:
            raise ConnectionError("Not connected")
        with self._io_lock:
            self._send(command)
            raw, state = self._read_page(timeout=5.0, on_chunk=on_chunk, command=command)
            if state == "logout":
                self._current_prompt = ""
            return raw, (state == "more")


def _reset_server_state():
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._ACTIVE_SOCKETS_BY_TOKEN.clear()
    server._SESSION_LAST_SEEN.clear()


class TestPhaseQCmlLifecycle:
    """Test suite covering Phase Q requirements."""

    def setup_method(self):
        _reset_server_state()

    def teardown_method(self):
        _reset_server_state()

    def test_fresh_cml_connect_preserves_privileged_mode_without_forced_disable(self):
        """1. When R1 is already in privileged EXEC (#), connect does NOT send 'disable'."""
        _reset_server_state()
        token = "test-token-q-priv"
        device_id = "r1"

        mock_dev = MockCMLDeviceForLifecycle(initial_prompt="R1#")

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "type": "cisco_ios",
                "console_path": "/default-lab/R1/0",
                "connection_type": "cml",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})

                status = recv_msg_of_type(ws, "device_status")
                assert status["device_id"] == device_id
                assert status["status"] == "connected"
                # Mode must be 'enable', NOT degraded to 'exec'
                assert status["mode"] == "enable"
                # Prompt must be authentic 'R1#'
                assert status["prompt"] == "R1#"

                recv_msg_of_type(ws, "device_output")

                # Verify 'disable' was never sent to the channel
                assert not any("disable" in c.lower() for c in mock_dev._chan.commands_received)

    def test_fresh_cml_connect_preserves_user_exec_mode(self):
        """1b. When R1 is in user EXEC (>), connect faithfully detects mode='exec' and prompt='R1>'."""
        _reset_server_state()
        token = "test-token-q-exec"
        device_id = "r1"

        mock_dev = MockCMLDeviceForLifecycle(initial_prompt="R1>")

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "type": "cisco_ios",
                "console_path": "/default-lab/R1/0",
                "connection_type": "cml",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})

                status = recv_msg_of_type(ws, "device_status")
                assert status["mode"] == "exec"
                assert status["prompt"] == "R1>"
                recv_msg_of_type(ws, "device_output")

    def test_cml_exit_displays_press_return_and_clears_prompt_while_staying_connected(self):
        """3. When 'exit' is sent on CML, captures 'Press RETURN to get started', clears prompt, stays connected."""
        _reset_server_state()
        token = "test-token-q-exit"
        device_id = "r1"

        mock_dev = MockCMLDeviceForLifecycle(initial_prompt="R1#")

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "type": "cisco_ios",
                "console_path": "/default-lab/R1/0",
                "connection_type": "cml",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_status")
                recv_msg_of_type(ws, "device_output")

                # Send exit command
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})

                full_output, prompt, status = recv_command_response(ws)
                assert "R1 con0 is now available" in full_output
                assert "Press RETURN to get started" in full_output
                # Active prompt must be CLEARED so no stale 'R1#' remains
                assert prompt == ""

                # Must stay connected!
                assert status["status"] == "connected"
                assert status["prompt"] == ""

    def test_empty_enter_wakes_cml_console_to_exec_prompt(self):
        """2. Sending empty Enter wakes console parked at Press RETURN and restores genuine R1> prompt."""
        _reset_server_state()
        token = "test-token-q-enter"
        device_id = "r1"

        mock_dev = MockCMLDeviceForLifecycle(initial_prompt="R1#")

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "type": "cisco_ios",
                "console_path": "/default-lab/R1/0",
                "connection_type": "cml",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_status")
                recv_msg_of_type(ws, "device_output")

                # Step 1: Send exit to park at Press RETURN
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})
                full_out1, prompt1, _ = recv_command_response(ws)
                assert "Press RETURN to get started" in full_out1
                assert prompt1 == ""

                # Step 2: Send bare Enter (command="")
                ws.send_json({"type": "command", "device_id": device_id, "command": ""})

                # Console wakes up and renders R1>
                _, prompt2, status2 = recv_command_response(ws)
                assert prompt2 == "R1>"
                assert status2["status"] == "connected"
                assert status2["mode"] == "exec"
                assert status2["prompt"] == "R1>"

                # Step 3: Now typing 'enable' from R1> triggers Phase P interactive password modal!
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                auth_req = recv_msg_of_type(ws, "device_auth_required")
                assert auth_req["stage_type"] == "enable"

    def test_enable_at_privileged_prompt_reports_already_in_exec_mode(self):
        """When router is already at R1#, typing 'enable' returns 'Already in privileged EXEC mode'."""
        _reset_server_state()
        token = "test-token-q-already-priv"
        device_id = "r1"

        mock_dev = MockCMLDeviceForLifecycle(initial_prompt="R1#")

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "type": "cisco_ios",
                "console_path": "/default-lab/R1/0",
                "connection_type": "cml",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_status")
                recv_msg_of_type(ws, "device_output")

                # Send enable at R1#
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                out = recv_msg_of_type(ws, "device_output")
                assert "Already in privileged EXEC mode." in out["output"]
                assert out["prompt"] == "R1#"

    def test_heartbeat_pong_answered_immediately(self):
        """Heartbeat ping is answered with pong immediately without blocking."""
        _reset_server_state()
        token = "test-token-q-ping"

        server.SESSIONS[token] = server.AuthSession(
            host="cml.example.com",
            username="developer",
            password="cmlpassword",
            connection_type="cml",
        )

        with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
            ts = int(time.time() * 1000)
            ws.send_json({"type": "ping", "ts": ts})
            pong = recv_msg_of_type(ws, "pong")
            assert pong["ts"] == ts
