"""Unit tests for Phase R: CML Enable Password Forwarding, Interactive State Machine,
WebSocket Ownership Guard, and Duplicate Prompt Prevention.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cliq.connection.live_device import (
    CMLConsoleDevice,
    ConnectionError,
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


class FakeParamikoChannel:
    """Mock Paramiko channel that mimics an interactive Cisco IOS router console behind CML."""

    def __init__(self, initial_prompt: str = "R1>", enable_secret: str = "router_secret_456"):
        self.prompt = initial_prompt
        self.enable_secret = enable_secret
        self.closed = False
        self.out_buffer = bytearray()
        self.writes: list[tuple[bytes, bool]] = []
        self._waiting_for_password = False
        self._lock = threading.Lock()

    def send(self, data: bytes | str) -> int:
        if isinstance(data, str):
            bdata = data.encode("utf-8")
        else:
            bdata = bytes(data)
        with self._lock:
            self.writes.append((bdata, False))
            text = bdata.decode("utf-8", errors="replace")

            if "\x03" in text:  # Ctrl-C (cancel/abort)
                self._waiting_for_password = False
                self.out_buffer.extend(f"^C\r\n{self.prompt}\r\n".encode("utf-8"))
                return len(bdata)

            if self._waiting_for_password:
                entered = text.strip()
                if entered == self.enable_secret:
                    self.prompt = self.prompt.replace(">", "#")
                    self._waiting_for_password = False
                    self.out_buffer.extend(f"\r\n{self.prompt}\r\n".encode("utf-8"))
                else:
                    self._waiting_for_password = False
                    self.out_buffer.extend(b"\r\n% Bad secrets\r\nPassword: ")
                return len(bdata)

            if "enable" in text.lower():
                if self.prompt.endswith("#"):
                    self.out_buffer.extend(f"\r\n{self.prompt}\r\n".encode("utf-8"))
                else:
                    self._waiting_for_password = True
                    self.out_buffer.extend(b"\r\nPassword: ")
                return len(bdata)

            if "show ip int br" in text or "show ip interface brief" in text:
                self.out_buffer.extend(
                    f"\r\nInterface              IP-Address      OK? Method Status                Protocol\r\nGigabitEthernet0/0     192.168.1.1     YES NVRAM  up                    up\r\n{self.prompt}\r\n".encode("utf-8")
                )
                return len(bdata)

            # Bare Enter
            self.out_buffer.extend(f"\r\n{self.prompt}\r\n".encode("utf-8"))
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

    def settimeout(self, timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class MockCMLConsoleDevice(CMLConsoleDevice):
    """Subclass of CMLConsoleDevice with simulated channel I/O."""

    def __init__(self, initial_prompt: str = "R1>", enable_secret: str = "router_secret_456"):
        self.gateway = "cml.example.com"
        self.username = "cml_controller_user"
        self.password = "cml_controller_pass"
        self.path = "/default-lab/R1/0"
        self.timeout = 5
        self._current_prompt = initial_prompt
        self._connected = True
        self._chan = FakeParamikoChannel(initial_prompt=initial_prompt, enable_secret=enable_secret)
        self._io_lock = threading.RLock()
        self._auth_stage_timer = None
        self._awaiting_auth = False
        self._client = MagicMock()
        self.complete_login_called = False

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
        self._chan.send(command + "\r\n")
        raw = self._chan.recv(4096).decode("utf-8", errors="replace")
        clean = raw.replace(self._current_prompt, "").strip()
        return clean, False

    def complete_login(self, username: str, password: str) -> None:
        self.complete_login_called = True


class TestPhaseREnableAndOwnership:
    """Test suite covering Phase R requirements."""

    def test_enable_command_interactive_state_and_dialog_trigger(self):
        """1. Typing 'enable' transitions DeviceRuntime.interactive_state to 'awaiting_enable_secret',
        emits typed device_auth_required with stage_type='enable', and does not deadlock runtime lock.
        """
        token = "cml_phase_r_token_1"
        device_id = "r1-device-phase-r-1"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="router_secret_456")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime is not None
                assert runtime.interactive_state == "idle"

                # Send enable command
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                auth_req = recv_msg_of_type(ws, "device_auth_required")

                assert auth_req["type"] == "device_auth_required"
                assert auth_req["stage"] == "password"
                assert auth_req["stage_type"] == "enable"
                assert "Enable Secret Required" in auth_req["message"]

                # Runtime interactive_state must be 'awaiting_enable_secret'
                assert runtime.interactive_state == "awaiting_enable_secret"

                # Verify runtime.lock is NOT held while waiting for credentials
                assert not runtime.lock.locked()

        client.post(f"/api/session/disconnect?token={token}")

    def test_enable_password_direct_dispatch_elevates_prompt_and_mode(self):
        """2. Submitting enable secret via auth_credentials forwards secret directly to router console,
        transitions prompt to R1#, mode to enable, and resets interactive_state to idle.
        """
        token = "cml_phase_r_token_2"
        device_id = "r1-device-phase-r-2"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="router_secret_456")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                recv_msg_of_type(ws, "device_auth_required")

                # Submit the enable secret with stage_type='enable'
                ws.send_json({
                    "type": "auth_credentials",
                    "device_id": device_id,
                    "username": "",
                    "password": "router_secret_456",
                    "stage_type": "enable",
                })

                dev_status = recv_msg_of_type(ws, "device_status")
                assert dev_status["mode"] == "enable"
                assert dev_status["prompt"] == "R1#"

                dev_out = recv_msg_of_type(ws, "device_output")
                assert dev_out["prompt"] == "R1#"

                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime.mode == "enable"
                assert runtime.prompt == "R1#"
                assert runtime.interactive_state == "idle"

                # CML controller complete_login must NEVER be called
                assert not dev.complete_login_called

        client.post(f"/api/session/disconnect?token={token}")

    def test_enable_password_never_triggers_cml_controller_login(self):
        """3. Enable auth flow never falls through to CML controller login or reattachment."""
        token = "cml_phase_r_token_3"
        device_id = "r1-device-phase-r-3"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="router_secret_456")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                recv_msg_of_type(ws, "device_auth_required")

                # Deliberately submit without username
                ws.send_json({
                    "type": "auth_credentials",
                    "device_id": device_id,
                    "username": "",
                    "password": "router_secret_456",
                })

                dev_status = recv_msg_of_type(ws, "device_status")
                assert dev_status["mode"] == "enable"
                assert not dev.complete_login_called

        client.post(f"/api/session/disconnect?token={token}")

    def test_enable_wrong_password_clears_prompt_restores_user_exec(self):
        """4. Entering an invalid enable secret sends Ctrl-C (cancel_enable), restores prompt to R1>,
        sets interactive_state back to idle, and does not freeze terminal.
        """
        token = "cml_phase_r_token_4"
        device_id = "r1-device-phase-r-4"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="correct_secret_xyz")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                recv_msg_of_type(ws, "device_auth_required")

                # Submit WRONG password
                ws.send_json({
                    "type": "auth_credentials",
                    "device_id": device_id,
                    "username": "",
                    "password": "wrong_password_123",
                    "stage_type": "enable",
                })

                err_out = recv_msg_of_type(ws, "device_output")
                assert "% Error in authentication" in err_out["output"]
                assert err_out["prompt"] == "R1>"

                err_panel = recv_msg_of_type(ws, "error_panel")
                assert "Enable Authentication Failed" in err_panel["what"]

                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime.interactive_state == "idle"
                assert runtime.prompt == "R1>"
                assert not runtime.lock.locked()

        client.post(f"/api/session/disconnect?token={token}")

    def test_cancel_auth_clears_prompt_restores_user_exec(self):
        """5. Cancelling the enable authentication dialog sends Ctrl-C to router, restores prompt,
        and sets interactive_state back to idle.
        """
        token = "cml_phase_r_token_5"
        device_id = "r1-device-phase-r-5"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="correct_secret_xyz")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                recv_msg_of_type(ws, "device_auth_required")

                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime.interactive_state == "awaiting_enable_secret"

                # Send cancel_auth
                ws.send_json({
                    "type": "cancel_auth",
                    "device_id": device_id,
                    "stage_type": "enable",
                })

                # Follow with a normal command to verify the device channel is clean and unblocked
                ws.send_json({"type": "command", "device_id": device_id, "command": "show ip int br"})
                out = recv_msg_of_type(ws, "device_output")
                assert "192.168.1.1" in out["output"]
                assert out["prompt"] == "R1>"
                recv_msg_of_type(ws, "device_status")

                # Verify interactive_state is restored to idle and lock is unheld
                assert runtime.interactive_state == "idle"
                assert not runtime.lock.locked()

        client.post(f"/api/session/disconnect?token={token}")

    def test_subsequent_enable_when_privileged_is_noop(self):
        """6. When already in privileged mode (R1#), typing 'enable' emits 'Already in privileged EXEC mode.'
        and does not open authentication dialog.
        """
        token = "cml_phase_r_token_6"
        device_id = "r1-device-phase-r-6"

        dev = MockCMLConsoleDevice(initial_prompt="R1#")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                out = recv_msg_of_type(ws, "device_output")
                assert "Already in privileged EXEC mode" in out["output"]
                assert out["prompt"] == "R1#"

        client.post(f"/api/session/disconnect?token={token}")

    def test_superseded_websocket_ownership_and_runtime_inheritance(self):
        """7. A new WebSocket for the same token supersedes the prior socket and inherits
        active connected runtimes.
        """
        token = "cml_phase_r_token_7"
        device_id = "r1-device-phase-r-7"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="router_secret_456")
        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="cml_pass",
            connection_type="cml",
        )
        server.SESSIONS[token] = session
        server.DEVICES_BY_TOKEN[token] = {
            device_id: {
                "id": device_id,
                "label": "R1",
                "console_path": "/default-lab/R1/0",
            }
        }

        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev):
            # First WebSocket connects and connects device
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws1:
                ws1.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws1, "device_output")

                assert (token, device_id) in server._DEVICE_RUNTIMES

                # Second WebSocket connects with the same token
                with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws2:
                    # WebSocket 2 must have inherited runtime for device_id
                    # Execute a command on ws2
                    ws2.send_json({"type": "command", "device_id": device_id, "command": "show ip int br"})
                    out = recv_msg_of_type(ws2, "device_output")
                    assert "192.168.1.1" in out["output"]

        client.post(f"/api/session/disconnect?token={token}")
