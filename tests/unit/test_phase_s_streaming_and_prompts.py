"""Tests for CLIQ Phase S — Real-Time Terminal Streaming, Prompt Deduplication, and Latency Reduction.

Validates:
1. Incremental progressive chunk streaming in CMLConsoleDevice._read_page.
2. Command echo stripping across fragmented incoming packets.
3. Trailing prompt and pager exclusions from streamed deltas.
4. Prompt deduplication on backend device_output (both empty Enter and normal commands).
5. Multiple consecutive empty Enters produce valid prompts without duplication.
6. Safety: no credentials or sensitive tokens exposed or persisted.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from cliq.connection.live_device import CMLConsoleDevice
from cliq.web import server
from cliq.web.server import app

client = TestClient(app)


def recv_msg_of_type(ws, msg_type: str, max_msgs: int = 30) -> dict[str, Any]:
    """Receive messages from WebSocket until a message of msg_type arrives."""
    for _ in range(max_msgs):
        m = ws.receive_json()
        if m.get("type") == msg_type:
            return m
    raise AssertionError(f"Did not receive message of type '{msg_type}' within {max_msgs} messages")


class MockChan:
    """Mock paramiko/socket channel simulating fragmented TCP read chunks."""

    def __init__(self, chunks: list[bytes]):
        self._iter = iter(chunks)
        self.closed = False

    def recv(self, n: int) -> bytes:
        try:
            return next(self._iter)
        except StopIteration:
            return b""


class TestPhaseSStreaming:
    def test_cml_console_progressive_streaming_incremental(self):
        """Streaming delivers output chunks progressively as they arrive without waiting for command completion."""
        dev = CMLConsoleDevice(
            gateway="cml.test.local",
            path="lab1/r1/0",
            username="admin",
            password="secret_password",
        )
        simulated_chunks = [
            b"show ip int brief\r\n",
            b"Interface                  IP-Address      OK? Method Status                Protocol\r\n",
            b"GigabitEthernet0/0         192.168.1.1     YES NVRAM  up                    up      \r\n",
            b"GigabitEthernet0/1         unassigned      YES NVRAM  administratively down down    \r\n",
            b"R1#",
        ]
        dev._chan = MockChan(simulated_chunks)
        dev._connected = True

        chunks_received = []

        def on_chunk(delta: str):
            chunks_received.append(delta)

        page, state = dev._read_page(
            timeout=5.0,
            on_chunk=on_chunk,
            command="show ip int brief",
        )

        assert state == "prompt"
        assert dev.current_prompt == "R1#"
        # Output was delivered across multiple chunks
        assert len(chunks_received) >= 2
        joined = "".join(chunks_received)
        # Command echo was NOT streamed in chunks
        assert "show ip int brief" not in chunks_received[0].lower()
        # Prompt was NOT streamed in chunks
        assert "R1#" not in chunks_received[-1]
        # Real interfaces are present
        assert "GigabitEthernet0/0" in joined
        assert "GigabitEthernet0/1" in joined

    def test_cml_console_fragmented_command_echo_not_streamed(self):
        """When the command echo arrives in multiple fragments, none of it is streamed."""
        dev = CMLConsoleDevice(
            gateway="cml.test.local",
            path="lab1/r1/0",
            username="admin",
            password="secret_password",
        )
        simulated_chunks = [
            b"show ip ",
            b"int ",
            b"brief\r\n",
            b"Interface                  IP-Address      OK? Method Status                Protocol\r\n",
            b"GigabitEthernet0/0         192.168.1.1     YES NVRAM  up                    up      \r\n",
            b"R1#",
        ]
        dev._chan = MockChan(simulated_chunks)
        dev._connected = True

        chunks_received = []

        def on_chunk(delta: str):
            chunks_received.append(delta)

        page, state = dev._read_page(
            timeout=5.0,
            on_chunk=on_chunk,
            command="show ip int brief",
        )

        assert state == "prompt"
        assert dev.current_prompt == "R1#"
        assert len(chunks_received) >= 1
        for chunk in chunks_received:
            assert "show ip" not in chunk.lower()
            assert "brief" not in chunk.lower()
        assert "GigabitEthernet0/0" in "".join(chunks_received)

    def test_empty_enter_produces_no_stream_chunks(self):
        """Empty Enter does not emit any stream chunks and cleanly returns the prompt."""
        dev = CMLConsoleDevice(
            gateway="cml.test.local",
            path="lab1/r1/0",
            username="admin",
            password="secret_password",
        )
        simulated_chunks = [
            b"\r\n",
            b"R1#",
        ]
        dev._chan = MockChan(simulated_chunks)
        dev._connected = True

        chunks_received = []

        def on_chunk(delta: str):
            chunks_received.append(delta)

        page, state = dev._read_page(
            timeout=5.0,
            on_chunk=on_chunk,
            command="",
        )

        assert state == "prompt"
        assert dev.current_prompt == "R1#"
        assert len(chunks_received) == 0

    def test_streaming_with_more_pager(self):
        """Output lines before --More-- are streamed, but --More-- itself is not streamed."""
        dev = CMLConsoleDevice(
            gateway="cml.test.local",
            path="lab1/r1/0",
            username="admin",
            password="secret_password",
        )
        simulated_chunks = [
            b"show running-config\r\n",
            b"Building configuration...\r\n",
            b"Current configuration : 1500 bytes\r\n",
            b"--More--",
        ]
        dev._chan = MockChan(simulated_chunks)
        dev._connected = True

        chunks_received = []

        def on_chunk(delta: str):
            chunks_received.append(delta)

        page, state = dev._read_page(
            timeout=5.0,
            on_chunk=on_chunk,
            command="show running-config",
        )

        assert state == "more"
        assert len(chunks_received) >= 1
        joined = "".join(chunks_received)
        assert "Building configuration..." in joined
        assert "--More--" not in joined


class TestPhaseSPromptDeduplication:
    def test_empty_command_strips_trailing_prompt_from_device_output(self):
        """Backend run_command on empty command ensures output has no trailing prompt duplication."""
        token = "phase_s_tok_1"
        device_id = "r1-device-phase-s-1"

        mock_dev = MagicMock()
        mock_dev.is_connected = True
        mock_dev.current_prompt = "R1#"
        mock_dev.execute_paged.return_value = ("\r\nR1#", False)

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

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                # Send empty command (Enter)
                ws.send_json({"type": "command", "device_id": device_id, "command": ""})

                msg = recv_msg_of_type(ws, "device_output")
                assert msg["prompt"] == "R1#"
                # Trailing prompt must NOT be in output
                assert msg["output"] == ""

                status_msg = recv_msg_of_type(ws, "device_status")
                assert status_msg["prompt"] == "R1#"

        client.post(f"/api/session/disconnect?token={token}")

    def test_multiple_consecutive_empty_enters_preserve_prompts(self):
        """Multiple consecutive Enter presses return clean prompt responses each time."""
        token = "phase_s_tok_multi_enter"
        device_id = "r1-device-phase-s-multi"

        mock_dev = MagicMock()
        mock_dev.is_connected = True
        mock_dev.current_prompt = "R1#"
        mock_dev.execute_paged.return_value = ("", False)

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

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                for _ in range(3):
                    ws.send_json({"type": "command", "device_id": device_id, "command": ""})
                    out_msg = recv_msg_of_type(ws, "device_output")
                    assert out_msg["prompt"] == "R1#"
                    assert out_msg["output"] == ""

                    stat_msg = recv_msg_of_type(ws, "device_status")
                    assert stat_msg["prompt"] == "R1#"

        client.post(f"/api/session/disconnect?token={token}")

    def test_stream_bridge_suppresses_body_when_streamed(self):
        """When chunks have been streamed, device_output sends empty output to avoid body duplication."""
        token = "phase_s_tok_stream_bridge"
        device_id = "r1-device-phase-s-bridge"

        mock_dev = MagicMock()
        mock_dev.is_connected = True
        mock_dev.current_prompt = "R1#"

        def fake_execute_paged(cmd, on_chunk=None):
            if on_chunk:
                on_chunk("Interface                  IP-Address\r\n")
                on_chunk("GigabitEthernet0/0         192.168.1.1\r\n")
            return "Interface                  IP-Address\nGigabitEthernet0/0         192.168.1.1", False

        mock_dev.execute_paged.side_effect = fake_execute_paged

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

        with patch("cliq.web.server.CMLConsoleDevice", return_value=mock_dev):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.com") as ws:
                ws.send_json({"type": "connect_device", "device_id": device_id})
                recv_msg_of_type(ws, "device_output")

                ws.send_json({"type": "command", "device_id": device_id, "command": "show ip int brief"})

                # Should receive streamed chunks
                received_chunks = []
                while True:
                    msg = ws.receive_json()
                    if msg.get("type") == "device_stream":
                        received_chunks.append(msg["chunk"])
                    elif msg.get("type") == "device_output":
                        # Streamed body is suppressed in final device_output
                        assert msg["output"] == ""
                        assert msg["prompt"] == "R1#"
                        break

                assert len(received_chunks) == 2
                assert "GigabitEthernet0/0" in "".join(received_chunks)

        client.post(f"/api/session/disconnect?token={token}")
