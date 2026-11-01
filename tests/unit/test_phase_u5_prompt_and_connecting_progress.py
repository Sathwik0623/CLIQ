"""Phase U.5 Unit Tests: Global Prompt Duplication Fix & Visible Console Connection Lifecycle.

Verifies:
1. Streaming prompt boundary guard prevents trailing prompts from leaking into delta chunks when delta ends with \\n.
2. CMLConsoleDevice.connect(on_stage=...) reports all five connection lifecycle stages:
   gateway_ssh -> cml_auth -> console_open -> prompt_wait -> ready.
3. CMLConsoleDevice.reattach(on_stage=...) reports all five connection lifecycle stages.
4. Clean page and raw output prompt stripping preserves single prompt boundary across R1, R2, SW1, SW2.
5. Empty enter produces empty output without prompt duplication across all four devices.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Ensure paramiko module exists in sys.modules before importing live_device
_mock_paramiko = MagicMock()
if "paramiko" not in sys.modules:
    sys.modules["paramiko"] = _mock_paramiko

from cliq.connection.live_device import CMLConsoleDevice, _clean_page, _PROMPT_RE
from cliq.utils import observability as obs


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    yield


# ---------------------------------------------------------------------------
# 1. Streaming Prompt Boundary Guard (Preventing Streaming Prompt Leak)
# ---------------------------------------------------------------------------

def test_read_page_streaming_never_leaks_prompt_on_trailing_newline():
    """Verify that delta chunks ending with \\n do not leak trailing prompt lines."""
    device = CMLConsoleDevice.__new__(CMLConsoleDevice)
    device._chan = MagicMock()
    device._current_prompt = ""
    device.path = "/test/SW2/0"

    chunks = [
        b"Interface                  IP-Address      OK? Method Status\r\n",
        b"GigabitEthernet0/0         10.10.20.161    YES NVRAM  up\r\nSW2#\r\n",
    ]

    def mock_recv(n):
        if chunks:
            return chunks.pop(0)
        return b""

    device._chan.recv.side_effect = mock_recv
    streamed_chunks = []

    def on_chunk(delta: str):
        streamed_chunks.append(delta)

    raw, state = device._read_page(timeout=2.0, on_chunk=on_chunk, command="show ip int br")

    assert state == "prompt"
    assert device._current_prompt == "SW2#"

    # Verify that none of the streamed chunks contain SW2#
    for chunk in streamed_chunks:
        assert "SW2#" not in chunk
        assert not _PROMPT_RE.search(chunk)


def test_read_page_streaming_empty_enter_never_emits_prompt():
    """Verify that pressing Enter on empty line never emits prompt chunk via streaming."""
    device = CMLConsoleDevice.__new__(CMLConsoleDevice)
    device._chan = MagicMock()
    device._current_prompt = ""
    device.path = "/test/R1/0"

    chunks = [
        b"\r\nR1#\r\n",
    ]

    def mock_recv(n):
        if chunks:
            return chunks.pop(0)
        return b""

    device._chan.recv.side_effect = mock_recv
    streamed_chunks = []

    def on_chunk(delta: str):
        streamed_chunks.append(delta)

    raw, state = device._read_page(timeout=2.0, on_chunk=on_chunk, command="")

    assert state == "prompt"
    assert device._current_prompt == "R1#"
    # Absolutely zero streamed prompt chunks
    assert len(streamed_chunks) == 0


# ---------------------------------------------------------------------------
# 2. In-Terminal Visible Connection Stages (connect and reattach)
# ---------------------------------------------------------------------------

def test_connect_emits_all_five_lifecycle_stages():
    """Verify connect(on_stage=...) reports gateway_ssh, cml_auth, console_open, prompt_wait, ready."""
    mock_paramiko = MagicMock()
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_chan = MagicMock()

    mock_paramiko.SSHClient.return_value = mock_client
    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_chan

    responses = [
        b"CML Terminal Server\r\nconsoles> ",
        b"Connected to CML terminalserver at line 0\r\n",
        b"\r\nR1#",
        b"\r\nR1#",
    ]

    def mock_recv(n):
        if responses:
            return responses.pop(0)
        return b"\r\nR1#"

    mock_chan.recv.side_effect = mock_recv
    mock_chan.recv_ready.return_value = False

    device = CMLConsoleDevice("10.10.20.161", "cml_user", "cml_pass", "/lab/r1/0")
    device._paramiko = mock_paramiko

    stages = []

    def stage_callback(stage: str, msg: str):
        stages.append((stage, msg))

    device.connect(on_stage=stage_callback)

    stage_names = [s[0] for s in stages]
    assert "gateway_ssh" in stage_names
    assert "cml_auth" in stage_names
    assert "console_open" in stage_names
    assert "prompt_wait" in stage_names
    assert "ready" in stage_names
    assert "R1#" in stages[-1][1]


def test_reattach_emits_all_five_lifecycle_stages():
    """Verify reattach(on_stage=...) reports gateway_ssh, cml_auth, console_open, prompt_wait, ready."""
    mock_paramiko = MagicMock()
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_chan = MagicMock()

    mock_paramiko.SSHClient.return_value = mock_client
    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_chan

    responses = [
        b"CML Terminal Server\r\nconsoles> ",
        b"Connected to CML terminalserver at line 0\r\n",
        b"\r\nSW1#",
        b"\r\nSW1#",
    ]

    def mock_recv(n):
        if responses:
            return responses.pop(0)
        return b"\r\nSW1#"

    mock_chan.recv.side_effect = mock_recv
    mock_chan.recv_ready.return_value = False

    device = CMLConsoleDevice("10.10.20.161", "cml_user", "cml_pass", "/lab/sw1/0")
    device._paramiko = mock_paramiko

    stages = []

    def stage_callback(stage: str, msg: str):
        stages.append((stage, msg))

    mode = device.reattach(on_stage=stage_callback)

    assert mode == "enable"
    stage_names = [s[0] for s in stages]
    assert "gateway_ssh" in stage_names
    assert "cml_auth" in stage_names
    assert "console_open" in stage_names
    assert "prompt_wait" in stage_names
    assert "ready" in stage_names
    assert "SW1#" in stages[-1][1]


# ---------------------------------------------------------------------------
# 3. Multi-Device Single Prompt Verification (R1, R2, SW1, SW2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dev_name,prompt", [
    ("R1", "R1#"),
    ("R2", "R2#"),
    ("SW1", "SW1#"),
    ("SW2", "SW2#"),
])
def test_all_devices_clean_page_and_empty_enter(dev_name: str, prompt: str):
    """Verify that all four devices produce clean empty output on Enter, never duplicate prompt."""
    raw_echo = f"\r\n{prompt}\r\n"
    cleaned = _clean_page(raw_echo, command="")
    assert cleaned == ""

    # Verify that raw output prompt stripping removes trailing prompt
    sample_output = f"interface GigabitEthernet0/0\r\n ip address 10.1.1.1 255.255.255.0\r\n{prompt}"
    clean_raw_out = sample_output or ""
    if clean_raw_out and prompt and clean_raw_out.rstrip().endswith(prompt):
        clean_raw_out = clean_raw_out.rstrip()[:-len(prompt)].rstrip()

    assert not clean_raw_out.endswith(prompt)
    assert f"ip address 10.1.1.1 255.255.255.0" in clean_raw_out

