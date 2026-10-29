"""Unit tests for Phase K: Login-time enable secret handling, lifecycle, and elevation."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cliq.connection.live_device import ConnectionError
from cliq.connection.ssh_console_device import SSHConsoleDevice
from cliq.web import server
from cliq.web.server import app

client = TestClient(app)


class FakeNetmikoConn:
    def __init__(
        self,
        *args,
        initial_prompt="cat9200>",
        privileged_prompt="cat9200#",
        valid_secret="cisco123",
        secret="",
        **kwargs,
    ):
        self._prompt = initial_prompt
        self._priv_prompt = privileged_prompt
        self._valid_secret = valid_secret
        self.secret = secret
        self.closed = False
        self.enable_call_count = 0

    def is_alive(self) -> bool:
        return not self.closed

    def check_enable_mode(self) -> bool:
        return self._prompt.endswith("#")

    def find_prompt(self) -> str:
        return self._prompt

    def enable(self, *args, **kwargs) -> str:
        self.enable_call_count += 1
        if self.secret == self._valid_secret:
            self._prompt = self._priv_prompt
            return ""
        raise ValueError(f"Failed to enter enable mode with secret '{self.secret}'")

    def send_command_timing(self, cmd: str) -> str:
        return ""

    def set_base_prompt(self) -> str:
        return self._prompt

    def write_channel(self, data: str) -> None:
        pass

    def disconnect(self) -> None:
        self.closed = True


def drain_until_connected(ws):
    """Helper to drain initial connect messages (connecting, status, initial output)."""
    while True:
        m = ws.receive_json()
        if m.get("type") == "device_output":
            return m


class TestSSHEnableAuth:
    # -------------------------------------------------------------------------
    # 1. Login passes a distinct enable secret through to the connection
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_login_passes_distinct_enable_secret_to_connection(self, mock_ch):
        """Login passes distinct password and enable secret all the way to Netmiko."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>", valid_secret="enable_sec_999")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "ssh_login_password_xyz",
                "secret": "enable_sec_999",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        token = data["token"]
        device_id = data["device"]["id"]

        session = server.SESSIONS.get(token)
        assert session is not None
        assert session.password == "ssh_login_password_xyz"
        assert session.secret == "enable_sec_999"

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                call_kwargs = mock_ch.call_args.kwargs
                assert call_kwargs["password"] == "ssh_login_password_xyz"
                assert call_kwargs["secret"] == "enable_sec_999"
                assert fake_conn.secret == "enable_sec_999"

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 2. SSH password and enable secret remain separate
    # -------------------------------------------------------------------------
    def test_ssh_password_and_enable_secret_remain_separate(self):
        """SSH login password and enable secret are separate and password is never substituted for secret."""
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "login_only_pw",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        token = data["token"]

        session = server.SESSIONS.get(token)
        assert session is not None
        assert session.password == "login_only_pw"
        assert session.secret is None

        dev = SSHConsoleDevice(
            host="10.10.20.181",
            username="developer",
            password="login_only_pw",
            secret="",
            method="direct",
        )
        assert dev.password == "login_only_pw"
        assert dev.secret == ""

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 3. enable at '>' uses login-time secret without requesting more input
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_enable_at_user_exec_uses_login_secret_without_prompting(self, mock_ch):
        """Typing 'enable' at '>' uses previously supplied login secret to elevate to '#' without modal."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>", valid_secret="secret_abc")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "dev_password",
                "secret": "secret_abc",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})

                status_msg = ws.receive_json()
                assert status_msg["type"] == "device_status"
                assert status_msg["mode"] == "enable"
                assert status_msg["prompt"] == "cat9200#"

                out_msg = ws.receive_json()
                assert out_msg["type"] == "device_output"
                assert out_msg["prompt"] == "cat9200#"

                assert fake_conn.enable_call_count == 1
                assert fake_conn.check_enable_mode()

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 4. enable at '#' is a no-op
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_enable_at_hash_is_noop(self, mock_ch):
        """If prompt is already '#', typing 'enable' is a no-op and does not call Netmiko enable()."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200#", valid_secret="secret_abc")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "dev_password",
                "secret": "secret_abc",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})

                out_msg = ws.receive_json()
                assert out_msg["type"] == "device_output"
                assert "Already in privileged EXEC mode" in out_msg["output"]
                assert out_msg["prompt"] == "cat9200#"

                assert fake_conn.enable_call_count == 0

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 5. Missing or invalid secrets fail safely without a reconnect loop
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_missing_secret_fails_safely_without_reconnect(self, mock_ch):
        """Typing 'enable' when no secret was provided at login shows terminal error and preserves session."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "dev_password",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})

                out_msg = ws.receive_json()
                assert out_msg["type"] == "device_output"
                assert "% No enable secret configured at login" in out_msg["output"]
                assert out_msg["prompt"] == "cat9200>"

                err_panel = ws.receive_json()
                assert err_panel["type"] == "error_panel"
                assert "Enable Secret Missing" in err_panel["what"]

                assert not fake_conn.closed

        client.post(f"/api/session/disconnect?token={token}")

    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_invalid_secret_fails_safely_without_reconnect(self, mock_ch):
        """Typing 'enable' with wrong secret shows authentication error, preserves session, and does not reconnect."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>", valid_secret="the_real_secret")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "dev_password",
                "secret": "wrong_secret_entered",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})

                out_msg = ws.receive_json()
                assert out_msg["type"] == "device_output"
                assert "% Error in authentication" in out_msg["output"]
                assert out_msg["prompt"] == "cat9200>"

                err_panel = ws.receive_json()
                assert err_panel["type"] == "error_panel"
                assert "Enable Authentication Failed" in err_panel["what"]

                assert not fake_conn.closed

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 6. The UI never opens a second secret modal during normal enable flow
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_ui_never_opens_second_secret_modal_during_enable_flow(self, mock_ch):
        """WebSocket never emits 'device_auth_required' during enable flow."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>", valid_secret="secret_abc")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "dev_password",
                "secret": "secret_abc",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_until_connected(ws)

                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                m1 = ws.receive_json()
                m2 = ws.receive_json()

                assert m1.get("type") != "device_auth_required"
                assert m2.get("type") != "device_auth_required"

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 7. Secrets are not persisted or exposed in logs
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_secrets_not_persisted_or_exposed_in_logs(self, mock_ch, caplog):
        """Secrets are not persisted in responses, and exceptions redact secrets in logs."""
        fake_conn = FakeNetmikoConn(initial_prompt="cat9200>", valid_secret="correct_secret_val")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "login_pass_123",
                "secret": "super_secret_enable_token",
                "port": 22,
                "method": "direct",
                "device_type": "cisco_ios",
            },
        )
        data = resp.json()
        assert "super_secret_enable_token" not in str(data)
        assert "login_pass_123" not in str(data)

        dev = SSHConsoleDevice(
            host="10.10.20.181",
            username="developer",
            password="login_pass_123",
            secret="super_secret_enable_token",
            method="direct",
        )
        dev._identify_device = MagicMock()
        dev.connect()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ConnectionError):
                dev.enable()

        assert "super_secret_enable_token" not in caplog.text
        assert "<redacted>" in caplog.text

    # -------------------------------------------------------------------------
    # Additional Context Help and Streaming Tests
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_context_help_bare_question_mark_returns_commands(self, mock_ch):
        """Typing '?' by itself captures full command list and displays without entering ask mode."""
        class MockHelpConn:
            def __init__(self):
                self._prompt = "cat9200#"
                self._written = []
            def is_alive(self): return True
            def check_enable_mode(self): return True
            def find_prompt(self): return self._prompt
            def send_command_timing(self, cmd): return ""
            def set_base_prompt(self): return self._prompt
            def write_channel(self, data): self._written.append(data)
            def read_channel(self):
                if "?" in "".join(self._written):
                    return "\r\nExec commands:\r\n  access-enable   Create temporary entry\r\n  show            Show running system info\r\ncat9200#"
                return ""
            def disconnect(self): pass

        fake_conn = MockHelpConn()
        mock_ch.return_value = fake_conn

        dev = SSHConsoleDevice(host="10.10.20.181", username="developer", password="dev_password")
        dev._identify_device = MagicMock()
        dev.connect()

        raw_help, is_paged = dev.context_help_start("")
        assert "access-enable" in raw_help
        assert "show" in raw_help
        assert not is_paged

    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_progressive_streaming_delivers_chunks_incrementally(self, mock_ch):
        """Real-time streaming delivers output chunks progressively without buffering until end."""
        class StreamingConn:
            def __init__(self):
                self._prompt = "cat9200#"
            def is_alive(self): return True
            def check_enable_mode(self): return True
            def find_prompt(self): return self._prompt
            def send_command_timing(self, cmd): return ""
            def set_base_prompt(self): return self._prompt
            def write_channel(self, data): pass
            def read_channel(self): return ""
            def disconnect(self): pass

        conn = StreamingConn()
        mock_ch.return_value = conn

        dev = SSHConsoleDevice(host="10.10.20.181", username="developer", password="dev_password")
        dev._identify_device = MagicMock()
        dev.connect()

        chunks_received = []
        def on_chunk(chunk):
            chunks_received.append(chunk)

        simulated_chunks = [
            "show ip int br\r\n",
            "Interface              IP-Address      OK? Method Status                Protocol\r\n",
            "GigabitEthernet1/0/1   192.168.1.1     YES manual up                    up\r\n",
            "cat9200#",
        ]
        chunk_iter = iter(simulated_chunks)
        conn.read_channel = lambda: next(chunk_iter, "")
        out, is_paged = dev.execute_paged("show ip int br", on_chunk=on_chunk)

        assert len(chunks_received) >= 2
        assert "GigabitEthernet1/0/1" in "".join(chunks_received)
        assert "GigabitEthernet1/0/1" in out
        assert dev.current_prompt == "cat9200#" 
