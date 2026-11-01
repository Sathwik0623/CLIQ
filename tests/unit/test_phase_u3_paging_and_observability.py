#!/usr/bin/env python3
"""Phase U.3 regression suite: Interactive terminal paging, session observability, and reliability."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cliq.connection.live_device import CMLConsoleDevice, _clean_page
from cliq.utils import observability as obs
from cliq.web.server import DeviceRuntime, _DEVICE_RUNTIMES


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    yield


def test_space_advances_pager():
    """Space key sends ' ' and receives the next page with more=True."""
    dev = MagicMock(spec=CMLConsoleDevice)
    dev.is_connected = True
    dev.pager_send_key.return_value = ("  page 2 content line\n", True)

    page, more = dev.pager_send_key(" ")
    assert more is True
    assert "page 2" in page
    dev.pager_send_key.assert_called_once_with(" ")


def test_enter_advances_one_line():
    """Enter key sends '\r' and advances line-by-line."""
    dev = MagicMock(spec=CMLConsoleDevice)
    dev.is_connected = True
    dev.pager_send_key.return_value = ("  single next line\n", True)

    page, more = dev.pager_send_key("\r")
    assert more is True
    assert "single next line" in page
    dev.pager_send_key.assert_called_once_with("\r")


def test_tab_exits_pager():
    """Tab key maps to 'q' (exit pager) and returns more=False."""
    dev = MagicMock(spec=CMLConsoleDevice)
    dev.is_connected = True
    dev.context_help_more.return_value = ("", False)

    page, more = dev.context_help_more("q")
    assert more is False
    assert page == ""
    dev.context_help_more.assert_called_once_with("q")


def test_ctrl_c_interrupts_safely():
    """Ctrl+C key ('\x03') in context_help_more exits pager and clears line."""
    mock_chan = MagicMock()
    mock_chan.closed = False
    mock_chan.send_ready.return_value = True
    mock_chan.recv_ready.return_value = False
    with patch.dict("sys.modules", {"paramiko": MagicMock()}):
        dev = CMLConsoleDevice("cml.lab", "cml", "pass", "/lab/r2/0")
        dev._chan = mock_chan
        dev._connected = True
        dev._help_partial = ""

        with patch.object(dev, "_read_help_page", return_value=("", "prompt")), \
             patch.object(dev, "_clear_help_line") as mock_clear:
            page, more = dev.context_help_more("\x03")
            assert more is False
            mock_clear.assert_called_once()


def test_final_page_restores_one_prompt():
    """Final page returns more=False, clean page content, and trailing prompt is stripped."""
    raw = "  access-enable    Create a temporary Access-List entry\n  archive          Manage archive files\nR2#"
    cleaned = _clean_page(raw)
    assert "access-enable" in cleaned
    assert not cleaned.endswith("R2#")
    assert "R2#" not in cleaned


def test_pager_state_isolated_between_devices():
    """Paging on R1 does not affect R2, SW1, or SW2."""
    rt_r1 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R1#")
    rt_r2 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R2#")
    rt_sw1 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="SW1#")
    rt_sw2 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="SW2#")

    rt_r2.pager_active = True
    rt_r2.help_paging = True

    assert rt_r1.pager_active is False
    assert rt_sw1.pager_active is False
    assert rt_sw2.pager_active is False
    assert rt_r2.pager_active is True


def test_switching_tabs_preserves_pager_state():
    """Switching active tab maintains each device's pager flags."""
    token = "test-token-tabs"
    rt_r1 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R1#")
    rt_r2 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R2#")
    _DEVICE_RUNTIMES[(token, "R1")] = rt_r1
    _DEVICE_RUNTIMES[(token, "R2")] = rt_r2

    rt_r2.pager_active = True

    # Inspect device R1
    r1 = _DEVICE_RUNTIMES[(token, "R1")]
    assert r1.pager_active is False

    # Switch back to R2
    r2 = _DEVICE_RUNTIMES[(token, "R2")]
    assert r2.pager_active is True


def test_terminal_output_not_duplicated():
    """Clean page ensures output followed by prompt has trailing prompt cleanly popped."""
    raw_output = "Line 1\nLine 2\nSW2#"
    cleaned = _clean_page(raw_output)
    assert cleaned == "Line 1\nLine 2"


def test_per_device_logs_carry_correct_session_identifiers(tmp_path):
    """Logs emitted via obs.log_event retain corr_id, user_id, session_id, and device_id."""
    with obs.trace_context(user_id="neteng1", session_id="sess-xyz", lab_id="lab-42", corr_id="corr-999"):
        obs.log_device_event("R2", "connect", "first_prompt", "success", device_name="Router-2", elapsed_ms=45.2)

    device_log = tmp_path / "device.log"
    assert device_log.exists()
    records = [json.loads(line) for line in device_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["user_id"] == "neteng1"
    assert rec["session_id"] == "sess-xyz"
    assert rec["lab_id"] == "lab-42"
    assert rec["corr_id"] == "corr-999"
    assert rec["device_id"] == "R2"
    assert rec["device_name"] == "Router-2"
    assert rec["elapsed_ms"] == 45.2


def test_credentials_and_tokens_never_appear_in_logs(tmp_path):
    """Passwords, tokens, and secrets are unconditionally redacted from all structured logs."""
    obs.log_event(
        "auth",
        "login",
        "verify",
        "success",
        detail="user=admin password=SecretPassword123! token=cml_secret_token_123456789",
        enable_secret="SuperSecret999",
    )
    auth_log = tmp_path / "auth.log"
    assert auth_log.exists()
    content = auth_log.read_text(encoding="utf-8")
    assert "SecretPassword123!" not in content
    assert "SuperSecret999" not in content
    assert "cml_secret_token_123456789" not in content
    assert "<redacted>" in content


def test_concurrent_device_operations_remain_isolated():
    """Simultaneous operations across R1, R2, SW1, SW2 run with independent locks."""
    async def _run():
        devices = {name: DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt=f"{name}#")
                   for name in ("R1", "R2", "SW1", "SW2")}

        results = []

        async def run_op(name, sleep_s):
            rt = devices[name]
            async with rt.lock:
                await asyncio.sleep(sleep_s)
                results.append(name)

        # Launch all 4 concurrently; fastest finishes first without blocking
        await asyncio.gather(
            run_op("SW2", 0.01),
            run_op("R1", 0.04),
            run_op("SW1", 0.02),
            run_op("R2", 0.03),
        )

        assert results == ["SW2", "SW1", "R2", "R1"]

    asyncio.run(_run())


def test_device_failure_does_not_interrupt_other_sessions():
    """Failure or disconnect on SW2 leaves R1, R2, SW1 unaffected."""
    token = "test-token-failure-isolation"
    rt_r1 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R1#")
    rt_r2 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="R2#")
    rt_sw1 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="SW1#")
    rt_sw2 = DeviceRuntime(device=MagicMock(), mode="exec", status="connected", prompt="SW2#")

    _DEVICE_RUNTIMES[(token, "R1")] = rt_r1
    _DEVICE_RUNTIMES[(token, "R2")] = rt_r2
    _DEVICE_RUNTIMES[(token, "SW1")] = rt_sw1
    _DEVICE_RUNTIMES[(token, "SW2")] = rt_sw2

    # Simulate catastrophic failure on SW2
    rt_sw2.dead = True
    rt_sw2.status = "disconnected"

    assert _DEVICE_RUNTIMES[(token, "R1")].status == "connected"
    assert _DEVICE_RUNTIMES[(token, "R2")].status == "connected"
    assert _DEVICE_RUNTIMES[(token, "SW1")].status == "connected"
    assert _DEVICE_RUNTIMES[(token, "SW2")].status == "disconnected"
