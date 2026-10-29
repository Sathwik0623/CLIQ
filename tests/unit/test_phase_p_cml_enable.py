"""Unit tests for Phase P: CML Terminal Mode Detection, Interactive Enable Authentication,
and Safe User-EXEC Preservation.
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
    """Mock Paramiko channel that mimics an interactive Cisco IOS console behind CML proxy."""

    def __init__(self, initial_prompt: str = "R1>", enable_secret: str = "mock_test_enable_secret_001"):
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

            if "\x03" in text:  # Ctrl-C
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

            # Bare newline (probe prompt or enter)
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

    def gettimeout(self) -> float:
        return 1.0

    def settimeout(self, timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class MockCMLConsoleDevice(CMLConsoleDevice):
    """Subclass of CMLConsoleDevice with simulated channel I/O for unit testing."""

    def __init__(self, initial_prompt: str = "R1>", enable_secret: str = "mock_test_enable_secret_001"):
        self.gateway = "cml.example.com"
        self.username = "cml_controller_user"
        self.password = "TEST_ONLY_CML_PASSWORD"
        self.path = "/default-lab/R1/0"
        self.timeout = 5
        self._current_prompt = initial_prompt
        self._connected = True
        self._chan = FakeParamikoChannel(initial_prompt=initial_prompt, enable_secret=enable_secret)
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
        self._chan.send(command + "\r\n")
        raw = self._chan.recv(4096).decode("utf-8", errors="replace")
        clean = raw.replace(self._current_prompt, "").strip()
        return clean, False


class TestPhasePCmlEnableAuth:
    """Test suite covering Phase P requirements."""

    def test_mode_from_prompt_preserves_exec_and_enable_modes(self):
        """1. Mode detection accurately differentiates user EXEC and privileged EXEC."""
        assert mode_from_prompt("R1>") == "exec"
        assert mode_from_prompt("R1#") == "enable"
        assert mode_from_prompt("cat9200>") == "exec"
        assert mode_from_prompt("cat9200#") == "enable"
        assert mode_from_prompt("R1(config)#") == "config"
        assert mode_from_prompt("R1(config-if)#") == "config-if"

    def test_cml_console_device_preserves_user_exec_prompt_on_connect(self):
        """2. CMLConsoleDevice connects without auto-escalating from user EXEC ('>') to enable."""
        dev = MockCMLConsoleDevice(initial_prompt="R1>")
        dev.connect()
        assert dev.current_prompt == "R1>"
        assert not dev.check_enable_mode()

    def test_cml_console_device_enable_method_success(self):
        """3. Calling enable(secret) sends enable, password, and transitions prompt to '#'."""
        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="mock_test_enable_secret_001")
        result = dev.enable("mock_test_enable_secret_001")
        assert result is True
        assert dev.current_prompt.endswith("#")
        assert dev.check_enable_mode()

    def test_cml_console_device_enable_method_failure_restores_user_exec(self):
        """4. Invalid enable secret raises ConnectionError and restores prompt without crashing."""
        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="mock_test_enable_secret_001")
        with pytest.raises(ConnectionError, match="Enable authentication rejected"):
            dev.enable("mock_test_wrong_password")

        # Must not be left in enable mode
        assert not dev.check_enable_mode()
        assert dev.current_prompt == "R1>"

    def test_websocket_cml_enable_command_triggers_interactive_secret_prompt(self):
        """5. In the WebSocket loop, typing 'enable' on CML prompts for enable password instead of failing."""
        token = "cml_test_token_phase_p"
        device_id = "r1-device-id"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="mock_test_enable_secret_001")

        session = server.AuthSession(
            host="cml.example.com",
            username="cml_controller_user",
            password="TEST_ONLY_CML_PASSWORD",
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

                # Drain connection messages until initial device_output
                recv_msg_of_type(ws, "device_output")

                # 1. Type normal command 'show ip int br' at R1>
                ws.send_json({"type": "command", "device_id": device_id, "command": "show ip int br"})
                out = recv_msg_of_type(ws, "device_output")
                assert "192.168.1.1" in out["output"]
                assert out["prompt"] == "R1>"
                status = recv_msg_of_type(ws, "device_status")
                assert status["mode"] == "exec"

                # 2. Type 'enable'
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                auth_req = recv_msg_of_type(ws, "device_auth_required")
                assert auth_req["type"] == "device_auth_required"
                assert auth_req["stage"] == "password"
                assert auth_req["stage_type"] == "enable"
                assert "Enable Secret Required" in auth_req["message"]

                # Verify device prompt is still R1> while awaiting secret
                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime is not None
                assert runtime.mode == "exec"

                # 3. Submit the enable secret via auth_credentials
                ws.send_json({
                    "type": "auth_credentials",
                    "device_id": device_id,
                    "username": "",
                    "password": "mock_test_enable_secret_001",
                })

                # Expect successful elevation to privileged EXEC mode
                dev_status = recv_msg_of_type(ws, "device_status")
                assert dev_status["mode"] == "enable"
                assert dev_status["prompt"] == "R1#"

                dev_out = recv_msg_of_type(ws, "device_output")
                assert dev_out["prompt"] == "R1#"

                # 4. Subsequent enable when already at R1# reports already in privileged mode
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                priv_out = recv_msg_of_type(ws, "device_output")
                assert "Already in privileged EXEC mode" in priv_out["output"]
                assert priv_out["prompt"] == "R1#"

        client.post(f"/api/session/disconnect?token={token}")

    def test_websocket_cml_enable_wrong_secret_shows_error_and_keeps_session_alive(self):
        """6. Submitting an invalid enable secret shows authentication error without dropping session."""
        token = "cml_test_token_invalid_secret"
        device_id = "r1-device-id-2"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="mock_test_enable_secret_001")

        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="TEST_ONLY_CML_PASSWORD",
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
                auth_req = recv_msg_of_type(ws, "device_auth_required")
                assert auth_req["type"] == "device_auth_required"

                # Submit WRONG password
                ws.send_json({
                    "type": "auth_credentials",
                    "device_id": device_id,
                    "username": "",
                    "password": "mock_test_wrong_password",
                })

                err_out = recv_msg_of_type(ws, "device_output")
                assert "% Error in authentication" in err_out["output"]
                assert err_out["prompt"] == "R1>"

                err_panel = recv_msg_of_type(ws, "error_panel")
                assert "Enable Authentication Failed" in err_panel["what"]

                # Session is still alive and at R1>
                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime is not None
                assert runtime.mode == "exec"
                assert runtime.prompt == "R1>"

        client.post(f"/api/session/disconnect?token={token}")

    def test_websocket_cml_cancel_auth_restores_prompt(self):
        """7. Canceling the enable secret modal sends cancel_auth and keeps session intact."""
        token = "cml_test_token_cancel_auth"
        device_id = "r1-device-id-3"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret="mock_test_enable_secret_001")

        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password="TEST_ONLY_CML_PASSWORD",
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
                auth_req = recv_msg_of_type(ws, "device_auth_required")
                assert auth_req["type"] == "device_auth_required"

                # Send cancel_auth
                ws.send_json({"type": "cancel_auth", "device_id": device_id})

                # Normal commands continue to work at R1>
                ws.send_json({"type": "command", "device_id": device_id, "command": "show ip int br"})
                out = recv_msg_of_type(ws, "device_output")
                assert "192.168.1.1" in out["output"]
                assert out["prompt"] == "R1>"

        client.post(f"/api/session/disconnect?token={token}")

    def test_cml_controller_password_is_never_auto_substituted(self, caplog):
        """8. Verification that CML controller password is never automatically sent as device secret."""
        caplog.set_level(logging.DEBUG)
        token = "cml_token_no_auto_sub"
        device_id = "r1-device-id-4"

        cml_controller_pwd = "mock_canary_cml_controller_pwd"
        device_enable_pwd = "mock_test_enable_secret_001"

        dev = MockCMLConsoleDevice(initial_prompt="R1>", enable_secret=device_enable_pwd)
        dev.password = cml_controller_pwd

        session = server.AuthSession(
            host="cml.example.com",
            username="cml_user",
            password=cml_controller_pwd,
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
                auth_req = recv_msg_of_type(ws, "device_auth_required")
                assert auth_req["type"] == "device_auth_required"

                # Verify that cml_controller_pwd was NEVER written to the channel
                for write_bytes, _ in dev._chan.writes:
                    assert cml_controller_pwd.encode("utf-8") not in write_bytes

                # Verify that cml_controller_pwd is not exposed in logs
                for record in caplog.records:
                    assert cml_controller_pwd not in record.getMessage()

        client.post(f"/api/session/disconnect?token={token}")
