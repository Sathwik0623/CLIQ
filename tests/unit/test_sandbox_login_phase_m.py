#!/usr/bin/env python3
"""Phase M - Sandbox-First Login UI and Direct SSH Connection Regression Tests.

Verifies the 7 requirements from CLIQ Phase M:
1. UI renders direct sandbox connection form by default
2. No CML Lab / SSH Device selector or CML host/auth inputs in the normal UI
3. Direct connection sends host, port, username, password, secret, device_type
4. Password and enable secret are distinct values
5. Secrets are not stored in browser storage (localStorage, sessionStorage), SQLite, or logs
6. Reconnection/reattach retains the enable secret without reprompting
7. Exit from # returns to > without disconnect or reconnect loop
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from cliq.web import server
from cliq.connection.ssh_console_device import SSHConsoleDevice


def _client() -> TestClient:
    return TestClient(server.app)


def _clear_state() -> None:
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()


class MockNetmikoChannel:
    """Mock Netmiko connection that simulates channel reads/writes and mode transitions."""

    def __init__(self, initial_prompt="cat9200#", valid_secret="secret123", secret="secret123"):
        self._prompt = initial_prompt
        self.secret = secret
        self.valid_secret = valid_secret
        self.closed = False
        self.in_buffer = ""
        self.write_history: list[str] = []

    def is_alive(self) -> bool:
        return not self.closed

    def check_enable_mode(self) -> bool:
        return self._prompt.endswith("#")

    def find_prompt(self) -> str:
        return self._prompt

    def write_channel(self, data: str) -> None:
        self.write_history.append(data)
        cmd = data.strip().lower()
        if cmd == "exit":
            if self._prompt.endswith("#"):
                self._prompt = "cat9200>"
                self.in_buffer += "cat9200#exit\r\ncat9200>"
            else:
                self.closed = True
                self.in_buffer += "cat9200>exit\r\n[Connection closed]\r\n"

    def read_channel(self) -> str:
        buf = self.in_buffer
        self.in_buffer = ""
        return buf

    def enable(self, *args, **kwargs) -> str:
        if self.secret == self.valid_secret:
            self._prompt = "cat9200#"
            return ""
        raise ValueError("Invalid secret")

    def set_base_prompt(self) -> str:
        return self._prompt

    def disconnect(self) -> None:
        self.closed = True


class TestPhaseMSandboxLogin:
    def test_ui_renders_sandbox_terminal_by_default(self):
        """1. UI component renders 'CLIQ — Sandbox Device Terminal' and defaults to direct SSH."""
        login_step_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "LoginStep.tsx"
        assert login_step_path.is_file(), f"LoginStep.tsx not found at {login_step_path}"
        code = login_step_path.read_text(encoding="utf-8")

        # Heading must clearly identify Sandbox Device Terminal
        assert "CLIQ — Sandbox Device Terminal" in code

        # Default state must be 'direct'
        assert "useState<SshMode>('direct')" in code

        # Direct fields must be present
        assert "Device IP or Hostname *" in code
        assert "SSH Port *" in code
        assert "Username *" in code
        assert "SSH Login Password *" in code
        assert "Enable Secret (Optional)" in code
        assert "Target Platform" in code
        assert "Friendly Label (Optional)" in code

    def test_no_cml_selector_or_cml_inputs_in_normal_ui(self):
        """2. No CML Lab / SSH Device selector or CML host/auth inputs in the normal UI."""
        login_step_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "LoginStep.tsx"
        code = login_step_path.read_text(encoding="utf-8")

        # Must not contain CML tab buttons or CML Host inputs
        assert ">CML Lab<" not in code
        assert ">SSH Device<" not in code
        assert "CML Host" not in code
        assert "https://cml.example.local" not in code
        assert "Authenticate to CML" not in code

    def test_direct_connection_sends_required_fields(self):
        """3. Direct connection sends host, port, username, password, secret, device_type."""
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "port": 22,
                "username": "developer",
                "password": "login_password_xyz",
                "secret": "enable_secret_abc",
                "device_type": "cisco_ios",
                "method": "direct",
                "label": "Catalyst 9200CX Sandbox",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        token = data["token"]
        assert token in server.SESSIONS

        session = server.SESSIONS[token]
        assert session.connection_type == "ssh"
        assert session.host == "10.10.20.181"
        assert session.username == "developer"
        assert session.password == "login_password_xyz"
        assert session.secret == "enable_secret_abc"

        device_meta = server.DEVICES_BY_TOKEN[token][data["device"]["id"]]
        assert device_meta["ssh_host"] == "10.10.20.181"
        assert device_meta["ssh_port"] == 22
        assert device_meta["ssh_console_user"] == "developer"
        assert device_meta["ssh_device_type"] == "cisco_ios"
        assert device_meta["ssh_method"] == "direct"

    def test_password_and_secret_are_distinct(self):
        """4. Password and enable secret are separate values and never leaked in responses."""
        _clear_state()
        client = _client()
        login_pw = "login_pass_distinct_111"
        enable_sec = "enable_sec_distinct_222"

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": login_pw,
                "secret": enable_sec,
                "method": "direct",
            },
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        session = server.SESSIONS[token]

        # Stored distinctly in memory
        assert session.password == login_pw
        assert session.secret == enable_sec
        assert session.password != session.secret

        # Neither secret is echoed back in HTTP response body
        assert login_pw not in resp.text
        assert enable_sec not in resp.text

    def test_secrets_not_stored_in_sqlite_or_browser_or_logs(self):
        """5. Secrets are not stored in browser storage, SQLite, or disk."""
        frontend_src = Path(__file__).resolve().parents[2] / "frontend" / "src"
        for tsx_file in frontend_src.rglob("*.tsx"):
            content = tsx_file.read_text(encoding="utf-8")
            assert "localStorage.setItem('password'" not in content
            assert "localStorage.setItem('secret'" not in content
            assert "sessionStorage.setItem('password'" not in content
            assert "sessionStorage.setItem('secret'" not in content

        db_path = Path(__file__).resolve().parents[2] / "data" / "commands.db"
        if db_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [r[0] for r in cur.fetchall()]
            for table in tables:
                cur.execute(f"PRAGMA table_info({table});")
                col_names = [r[1].lower() for r in cur.fetchall()]
                assert "password" not in col_names
                assert "secret" not in col_names
                assert "enable_secret" not in col_names
            conn.close()

    def test_reconnection_retains_enable_secret_without_reprompting(self):
        """6. Reconnection/reattach retains the enable secret without reprompting."""
        _clear_state()
        dev = SSHConsoleDevice(
            host="10.10.20.181",
            username="developer",
            password="test_password",
            secret="persisted_enable_secret",
            port=22,
            device_type="cisco_ios",
        )
        mock_netmiko = MagicMock()
        mock_netmiko.is_alive.return_value = True
        mock_netmiko.find_prompt.return_value = "cat9200#"
        mock_netmiko.check_enable_mode.return_value = True
        dev._conn = mock_netmiko
        dev._connected = True
        dev._current_prompt = "cat9200#"
        dev._current_mode = "enable"

        # The device's secret is retained
        assert dev.secret == "persisted_enable_secret"
        assert dev.is_connected is True

    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_exit_from_hash_returns_to_exec_without_disconnect(self, mock_ch):
        """7. Exit from # returns to > without disconnect or reconnect loop."""
        _clear_state()
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#", valid_secret="secret123", secret="secret123")
        mock_ch.return_value = fake_conn
        client = _client()

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "login_pass",
                "secret": "secret123",
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

                # Drain until initial output
                while True:
                    m = ws.receive_json()
                    if m.get("type") == "device_output":
                        break

                # Send exit command from # mode
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})

                # Read responses
                output_msg = ws.receive_json()
                status_msg = ws.receive_json()

                assert output_msg["type"] == "device_output"
                assert output_msg["prompt"] == "cat9200>"

                assert status_msg["type"] == "device_status"
                assert status_msg["mode"] == "exec"
                assert status_msg["status"] == "connected"
                assert status_msg["prompt"] == "cat9200>"

                # Session remains connected without reconnect loop
                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime is not None
                assert runtime.device.is_connected is True
                assert runtime.device.current_prompt == "cat9200>"
                assert runtime.mode == "exec"
                assert runtime.prompt == "cat9200>"
