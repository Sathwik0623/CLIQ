"""Regression tests for CLIQ Phase L: exit mode transition and reconnect loop prevention."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cliq.connection.live_device import ConnectionLostError
from cliq.connection.session_health import HealthState, probe_session_health
from cliq.connection.ssh_console_device import SSHConsoleDevice
from cliq.web import server
from cliq.web.server import app

client = TestClient(app)


class MockNetmikoChannel:
    """Mock Netmiko connection that simulates channel reads/writes and mode transitions."""

    def __init__(
        self,
        initial_prompt="cat9200#",
        valid_secret="enable_sec_123",
        secret="",
    ):
        self._prompt = initial_prompt
        self.secret = secret
        self.valid_secret = valid_secret
        self.closed = False
        self.in_buffer = ""
        self.write_history: list[str] = []
        self.enable_call_count = 0

    def is_alive(self) -> bool:
        return not self.closed

    def check_enable_mode(self) -> bool:
        return self._prompt.endswith("#")

    def find_prompt(self) -> str:
        return self._prompt

    def write_channel(self, data: str) -> None:
        self.write_history.append(data)
        # Simulate device responses
        cmd = data.strip().lower()
        if cmd == "exit":
            if self._prompt.endswith("#"):
                # Transition privileged EXEC to user EXEC
                self._prompt = "cat9200>"
                self.in_buffer += "cat9200#exit\r\ncat9200>"
            else:
                # User EXEC exit closes the session
                self.closed = True
                self.in_buffer += "cat9200>exit\r\n[Connection closed]\r\n"
        elif cmd == "show privilege":
            lvl = "15" if self._prompt.endswith("#") else "1"
            self.in_buffer += f"show privilege\r\nCurrent privilege level is {lvl}\r\n{self._prompt}"
        elif cmd == "enable":
            if self._prompt.endswith("#"):
                self.in_buffer += f"Already in privileged EXEC mode.\r\n{self._prompt}"
            elif self.secret == self.valid_secret:
                self._prompt = "cat9200#"
                self.in_buffer += f"\r\n{self._prompt}"
            else:
                self.in_buffer += f"% Error in authentication.\r\n{self._prompt}"
        elif cmd:
            self.in_buffer += f"{data.strip()}\r\n{self._prompt}"

    def read_channel(self) -> str:
        chunk = self.in_buffer
        self.in_buffer = ""
        return chunk

    def enable(self, *args, **kwargs) -> str:
        self.enable_call_count += 1
        if self.secret == self.valid_secret:
            self._prompt = "cat9200#"
            return ""
        raise ValueError("Invalid secret")

    def send_command_timing(self, cmd: str) -> str:
        return ""

    def set_base_prompt(self) -> str:
        return self._prompt

    def disconnect(self) -> None:
        self.closed = True


def drain_initial(ws):
    """Drain connection messages until initial device_output."""
    msgs = []
    while True:
        m = ws.receive_json()
        msgs.append(m)
        if m.get("type") == "device_output":
            return msgs


def drain_command_output(ws):
    """Drain device_stream chunks until final device_output."""
    chunks = []
    while True:
        m = ws.receive_json()
        if m.get("type") == "device_stream":
            chunks.append(m.get("chunk", ""))
        elif m.get("type") == "device_output":
            return "".join(chunks), m


class TestSSHExitModeTransition:
    # -------------------------------------------------------------------------
    # 1. exit at cat9200# changes prompt to cat9200> without reconnecting
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_exit_at_hash_changes_prompt_to_gt_without_reconnecting(self, mock_ch):
        """exit at cat9200# transitions session to cat9200> and mode to exec without reconnecting."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#", valid_secret="secret123", secret="secret123")
        mock_ch.return_value = fake_conn

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
                drain_initial(ws)

                # Send exit at privileged prompt
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})

                # Collect responses
                output_msg = ws.receive_json()
                status_msg = ws.receive_json()

                assert output_msg["type"] == "device_output"
                assert output_msg["prompt"] == "cat9200>"

                assert status_msg["type"] == "device_status"
                assert status_msg["mode"] == "exec"
                assert status_msg["status"] == "connected"
                assert status_msg["prompt"] == "cat9200>"

                # Verify session was NOT marked dead or reconnected
                runtime = server._DEVICE_RUNTIMES.get((token, device_id))
                assert runtime is not None
                assert runtime.status == "connected"
                assert runtime.prompt == "cat9200>"
                assert runtime.mode == "exec"
                assert not runtime.dead

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 2. A command such as show privilege works after exit
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_command_works_after_exit(self, mock_ch):
        """After exiting to cat9200>, subsequent commands like 'show privilege' execute successfully."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#", valid_secret="secret123", secret="secret123")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "login_pass",
                "secret": "secret123",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_initial(ws)

                # Send exit
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})
                ws.receive_json()  # device_output
                ws.receive_json()  # device_status

                # Send show privilege in user EXEC mode
                ws.send_json({"type": "command", "device_id": device_id, "command": "show privilege"})

                streamed, out_msg = drain_command_output(ws)
                status_msg = ws.receive_json()

                full_output = streamed or out_msg.get("output", "")
                assert "Current privilege level is 1" in full_output
                assert out_msg["prompt"] == "cat9200>"
                assert status_msg["type"] == "device_status"
                assert status_msg["mode"] == "exec"

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 3. enable after exit returns to privileged mode using login-time secret
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_enable_after_exit_elevates_with_login_secret(self, mock_ch):
        """Typing 'enable' after 'exit' returns to privileged mode ('#') using the login secret."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#", valid_secret="my_enable_secret", secret="my_enable_secret")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "login_pass",
                "secret": "my_enable_secret",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_initial(ws)

                # Exit from privileged mode
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})
                ws.receive_json()
                ws.receive_json()

                # Re-elevate using enable
                ws.send_json({"type": "command", "device_id": device_id, "command": "enable"})
                status_msg = ws.receive_json()
                out_msg = ws.receive_json()

                assert status_msg["type"] == "device_status"
                assert status_msg["mode"] == "enable"
                assert status_msg["prompt"] == "cat9200#"
                assert out_msg["type"] == "device_output"
                assert out_msg["prompt"] == "cat9200#"
                assert fake_conn.enable_call_count >= 1

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 4. Prompt transition from # to > does not mark the session dead
    # -------------------------------------------------------------------------
    def test_prompt_transition_hash_to_gt_keeps_session_alive(self):
        """A session whose prompt transitions from # to > is classified ALIVE by probe_session_health."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200>")
        dev = SSHConsoleDevice("10.10.20.181", "admin", "pass")
        dev._conn = fake_conn
        dev._connected = True
        dev._current_prompt = "cat9200>"

        result = asyncio.run(probe_session_health(dev))
        assert result.state is HealthState.ALIVE
        assert not result.is_dead
        assert result.prompt_last_seen == "cat9200>"

    # -------------------------------------------------------------------------
    # 5. A genuine SSH transport failure is still detected and handled
    # -------------------------------------------------------------------------
    def test_genuine_transport_failure_detected(self):
        """If the underlying channel dies unexpectedly, it is classified DEAD with socket_close."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#")
        fake_conn.closed = True  # transport dropped

        dev = SSHConsoleDevice("10.10.20.181", "admin", "pass")
        dev._conn = fake_conn
        dev._connected = True
        dev._current_prompt = "cat9200#"

        # Disallow reattach to test raw failure detection
        result = asyncio.run(probe_session_health(dev, allow_reattach=False))
        assert result.state is HealthState.DEAD
        assert result.is_dead
        assert result.reason in ("socket_close", "keepalive_timeout")

    # -------------------------------------------------------------------------
    # 6. Health probes cannot interfere with an in-flight command
    # -------------------------------------------------------------------------
    def test_health_probes_cannot_interfere_with_inflight_command(self):
        """device._lock protects execute() so concurrent probes cannot interleave channel I/O."""
        dev = SSHConsoleDevice("10.10.20.181", "admin", "pass")
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#")
        dev._conn = fake_conn
        dev._connected = True

        lock_acquired_during_exec = False

        def slow_read_channel():
            nonlocal lock_acquired_during_exec
            def try_probe():
                nonlocal lock_acquired_during_exec
                acquired = dev._lock.acquire(blocking=False)
                if acquired:
                    lock_acquired_during_exec = True
                    dev._lock.release()

            t = threading.Thread(target=try_probe)
            t.start()
            t.join()
            return "cat9200#show version\r\nCisco IOS\r\ncat9200#"

        fake_conn.read_channel = slow_read_channel

        dev.execute("show version")
        assert not lock_acquired_during_exec

    # -------------------------------------------------------------------------
    # 7. WebSocket reattachment does not create duplicate SSH connections
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_ws_reattachment_does_not_duplicate_ssh_connections(self, mock_ch):
        """Calling connect_device on an existing healthy session reuses it without opening a new connection."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200#")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "login_pass",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_initial(ws)
                assert mock_ch.call_count == 1

                # Re-issue connect_device for same device in same or reattached WS
                ws.send_json({"type": "connect_device", "device_id": device_id})
                status_msg = ws.receive_json()

                assert status_msg["type"] == "device_status"
                assert status_msg["status"] == "connected"
                # ConnectHandler was NOT called a second time
                assert mock_ch.call_count == 1

        client.post(f"/api/session/disconnect?token={token}")

    # -------------------------------------------------------------------------
    # 8. exit at user EXEC mode is handled correctly if it closes device session
    # -------------------------------------------------------------------------
    @patch("cliq.connection.ssh_console_device.ConnectHandler")
    def test_exit_at_user_exec_closes_session_without_reconnect_loop(self, mock_ch):
        """When exit at user EXEC (cat9200>) causes device to close SSH, report closure cleanly without looping."""
        fake_conn = MockNetmikoChannel(initial_prompt="cat9200>")
        mock_ch.return_value = fake_conn

        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "admin",
                "password": "login_pass",
            },
        )
        token = resp.json()["token"]
        device_id = resp.json()["device"]["id"]

        with patch("cliq.connection.ssh_console_device.SSHConsoleDevice._identify_device"):
            with client.websocket_connect(f"/ws?token={token}&host=10.10.20.181") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                drain_initial(ws)

                # Send exit at cat9200> (which triggers closed = True in MockNetmikoChannel)
                ws.send_json({"type": "command", "device_id": device_id, "command": "exit"})

                streamed, out_msg = drain_command_output(ws)
                status_msg = ws.receive_json()

                assert out_msg["type"] == "device_output"
                assert "closed" in (streamed + out_msg["output"]).lower()

                assert status_msg["type"] == "device_status"
                assert status_msg["status"] == "disconnected"

                # Verify runtime was removed and marked dead (not in reconnect loop)
                assert (token, device_id) not in server._DEVICE_RUNTIMES
                assert fake_conn.closed

        client.post(f"/api/session/disconnect?token={token}")
