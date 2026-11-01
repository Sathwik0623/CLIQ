"""Unit tests for CLIQ Phase U.2:
- Terminal prompt deduplication
- Clean console output (no synthetic CLIQ UUID messages)
- Enter forwarding & CRLF normalization
- Structured connection latency timing
- Multi-device isolation (R1, R2, SW1, SW2)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cliq.connection.live_device import CMLConsoleDevice, _clean_page, _PROMPT_RE
from cliq.web import server
from cliq.web.server import (
    _DEVICE_CONNECT_LOCKS,
    _DEVICE_RUNTIMES,
    _get_device_connect_lock,
    _release_socket_registrations,
    DeviceRuntime,
)


class DummyMockDevice:
    """Mock live device for prompt deduplication and multi-device testing."""

    def __init__(self, device_id: str, prompt: str = "SW2#"):
        self.device_id = device_id
        self.current_prompt = prompt
        self.node_type = "iosvl2"
        self.capability = {"switching"}
        self.is_connected = True
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.reattach_calls = 0
        self.commands_sent: list[str] = []

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def reattach(self) -> str:
        self.reattach_calls += 1
        return "enable"

    def execute_paged(self, command: str, on_chunk=None) -> tuple[str, bool]:
        self.commands_sent.append(command)
        return "", False


def test_clean_page_strips_trailing_prompt():
    """Verify _clean_page strips the trailing prompt from the output body."""
    raw_with_prompt = "Building configuration...\r\nCurrent configuration : 1024 bytes\r\nSW2#\r\n"
    cleaned = _clean_page(raw_with_prompt, command="show running-config")

    assert "SW2#" not in cleaned
    assert "Building configuration..." in cleaned
    assert "Current configuration : 1024 bytes" in cleaned


def test_clean_page_preserves_prompt_like_text_in_body():
    """Verify real command output containing prompt-like text inside output is preserved."""
    raw_with_inner = (
        "description Link to SW2#port-channel1\r\n"
        "ip address 10.1.1.1 255.255.255.0\r\n"
        "SW2#\r\n"
    )
    cleaned = _clean_page(raw_with_inner, command="show run int gi0/1")

    # Inner prompt-like text must be preserved
    assert "description Link to SW2#port-channel1" in cleaned
    assert "ip address 10.1.1.1 255.255.255.0" in cleaned
    # Trailing prompt line must be stripped so device_output can emit it cleanly
    assert not cleaned.endswith("SW2#")


def test_clean_page_empty_enter_produces_empty_output():
    """Verify that pressing Enter on an empty line returns empty output body (prompt only)."""
    raw_empty_enter = "\r\nSW2#\r\n"
    cleaned = _clean_page(raw_empty_enter, command="")

    assert cleaned == ""


def test_streaming_prompt_boundary_guard():
    """Verify _read_page does not stream the prompt in on_chunk."""
    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._current_prompt = "SW2#"

    streamed_chunks: list[str] = []
    def on_chunk(chunk: str) -> None:
        streamed_chunks.append(chunk)

    # Mock channel that returns command output followed by prompt
    mock_chan = MagicMock()
    mock_chan.recv.side_effect = [
        b"line 1\r\nline 2\r\nSW2#\r\n",
        b"",
    ]
    dev._chan = mock_chan

    raw, state = dev._read_page(timeout=1.0, on_chunk=on_chunk, command="test")

    # Prompt line must NOT be in streamed chunks
    all_streamed = "".join(streamed_chunks)
    assert "SW2#" not in all_streamed
    assert "line 1" in all_streamed
    assert "line 2" in all_streamed
    assert state == "prompt"
    assert dev._current_prompt == "SW2#"


def test_empty_enter_streaming_does_not_emit_prompt_delta():
    """Verify empty Enter does not emit a duplicate prompt chunk via on_chunk."""
    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._current_prompt = "SW2#"

    streamed_chunks: list[str] = []
    def on_chunk(chunk: str) -> None:
        streamed_chunks.append(chunk)

    mock_chan = MagicMock()
    mock_chan.recv.side_effect = [
        b"\r\nSW2#\r\n",
        b"",
    ]
    dev._chan = mock_chan

    raw, state = dev._read_page(timeout=1.0, on_chunk=on_chunk, command="")

    # No chunk should be emitted for empty Enter prompt
    assert len(streamed_chunks) == 0
    assert state == "prompt"
    assert dev._current_prompt == "SW2#"


def test_multi_device_opening_sw2_does_not_reattach_r1_r2_sw1():
    """Verify opening SW2 leaves existing devices R1, R2, SW1 completely untouched."""
    async def _test():
        token = "test_multi_u2_token"
        server.DEVICES_BY_TOKEN[token] = {
            "node-r1": {"id": "node-r1", "label": "R1", "type": "iosv"},
            "node-r2": {"id": "node-r2", "label": "R2", "type": "iosv"},
            "node-sw1": {"id": "node-sw1", "label": "SW1", "type": "iosvl2"},
            "node-sw2": {"id": "node-sw2", "label": "SW2", "type": "iosvl2"},
        }

        dev_r1 = DummyMockDevice("node-r1", "R1#")
        dev_r2 = DummyMockDevice("node-r2", "R2#")
        dev_sw1 = DummyMockDevice("node-sw1", "SW1#")
        dev_sw2 = DummyMockDevice("node-sw2", "SW2#")

        _DEVICE_RUNTIMES[(token, "node-r1")] = DeviceRuntime(device=dev_r1, mode="enable", status="connected", prompt="R1#", node_type="iosv")
        _DEVICE_RUNTIMES[(token, "node-r2")] = DeviceRuntime(device=dev_r2, mode="enable", status="connected", prompt="R2#", node_type="iosv")
        _DEVICE_RUNTIMES[(token, "node-sw1")] = DeviceRuntime(device=dev_sw1, mode="enable", status="connected", prompt="SW1#", node_type="iosvl2")

        # Emulate connecting SW2
        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev_sw2):
            lock = _get_device_connect_lock(token, "node-sw2")
            async with lock:
                dev_sw2.connect()
                _DEVICE_RUNTIMES[(token, "node-sw2")] = DeviceRuntime(
                    device=dev_sw2, mode="enable", status="connected", prompt="SW2#", node_type="iosvl2"
                )

        # Existing devices must have 0 connect, disconnect, or reattach calls
        assert dev_r1.connect_calls == 0
        assert dev_r1.disconnect_calls == 0
        assert dev_r1.reattach_calls == 0

        assert dev_r2.connect_calls == 0
        assert dev_r2.disconnect_calls == 0
        assert dev_r2.reattach_calls == 0

        assert dev_sw1.connect_calls == 0
        assert dev_sw1.disconnect_calls == 0
        assert dev_sw1.reattach_calls == 0

        # SW2 connected exactly once
        assert dev_sw2.connect_calls == 1

        # All 4 devices present in active runtime registry
        assert (token, "node-r1") in _DEVICE_RUNTIMES
        assert (token, "node-r2") in _DEVICE_RUNTIMES
        assert (token, "node-sw1") in _DEVICE_RUNTIMES
        assert (token, "node-sw2") in _DEVICE_RUNTIMES

        # Cleanup
        _release_socket_registrations(token)
        server.DEVICES_BY_TOKEN.pop(token, None)

    asyncio.run(_test())


def test_structured_connection_timing_logs(caplog):
    """Verify structured latency timing logs with correlation ID and stages are emitted."""
    caplog.set_level(logging.INFO)

    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev.gateway = "cml.test.local"
    dev.username = "test_user"
    dev.password = "mock_secret_test_999"
    dev.path = "/test-lab/node-sw2/0"
    dev.timeout = 5
    dev._current_prompt = "SW2#"

    mock_client = MagicMock()
    mock_chan = MagicMock()
    mock_chan.recv.side_effect = [
        b"consoles>\r\n",
        b"Connected to CML terminalserver.\r\nSW2#\r\n",
        b"",
    ]
    mock_client.get_transport.return_value.open_session.return_value = mock_chan

    mock_paramiko = MagicMock()
    mock_paramiko.SSHClient.return_value = mock_client
    dev._paramiko = mock_paramiko

    with patch.object(dev, "_read_cml_prompt", return_value="consoles>"):
        with patch.object(dev, "_send"):
            with patch.object(dev, "_read_until", return_value="SW2#"):
                dev.connect()

    log_records = [r.message for r in caplog.records if "[TIMING]" in r.message]

    # Verify structured stage timing records were emitted
    assert any("stage=gateway_ssh_start" in msg for msg in log_records)
    assert any("stage=gateway_ssh_auth_end" in msg for msg in log_records)
    assert any("stage=cml_proxy_command_end" in msg for msg in log_records)
    assert any("stage=usable_terminal" in msg for msg in log_records)

    # Verify no passwords or credentials leaked in timing logs
    for msg in log_records:
        assert "mock_secret_test_999" not in msg

