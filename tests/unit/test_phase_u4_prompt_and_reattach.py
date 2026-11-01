"""Phase U.4 Unit Tests: Duplicate Prompt Elimination & CML Proxy Latency Optimization.

Verifies:
1. Fast non-blocking reattach eliminates the legacy 10-second blocking timeout.
2. Structured per-stage connection timing logs with is_reattach=true and correlation id.
3. Plaintext JWT tokens, Cisco enable secrets, and passwords redacted unconditionally.
4. Diagnostic export strictly excludes token_cache.json and redacts logs in zip bundle.
5. Multi-device isolation across R1, R2, SW1, SW2 during reattachment.
6. Empty Enter clean output parsing producing exactly one prompt boundary.
"""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure paramiko module exists in sys.modules before importing live_device
_mock_paramiko = MagicMock()
if "paramiko" not in sys.modules:
    sys.modules["paramiko"] = _mock_paramiko

from cliq.connection.live_device import CMLConsoleDevice, _clean_page
from cliq.utils import observability as obs


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    yield


# ---------------------------------------------------------------------------
# 1. Fast Reattach Optimization & Timing
# ---------------------------------------------------------------------------

def test_reattach_uses_fast_wake_without_10s_blocking():
    """Verify reattach completes without blocking for 10s on idle console."""
    mock_paramiko = MagicMock()
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_chan = MagicMock()

    mock_paramiko.SSHClient.return_value = mock_client
    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_chan

    # Simulate CML console responses:
    # 1. CML proxy banner
    # 2. open command response: "Connected to CML terminalserver"
    # 3. IOS prompt: "R2#"
    # 4. prompt after terminal width 0: "R2#"
    responses = [
        b"CML Terminal Server\r\nconsoles> ",
        b"Connected to CML terminalserver at line 0\r\n",
        b"\r\nR2#",
        b"\r\nR2#",
    ]

    def mock_recv(n):
        if responses:
            return responses.pop(0)
        return b"\r\nR2#"

    mock_chan.recv.side_effect = mock_recv
    mock_chan.recv_ready.return_value = False

    with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
        device = CMLConsoleDevice(
            gateway="10.10.20.161",
            username="admin",
            password="test_password",
            path="/lab1/r2/0",
        )
        device._paramiko = mock_paramiko

        t0 = time.perf_counter()
        with patch("time.sleep", return_value=None):
            mode = device.reattach(reason="tab_switch")
        elapsed = time.perf_counter() - t0

    assert mode == "enable"
    assert device.current_prompt == "R2#"
    assert elapsed < 2.0, f"Reattach took {elapsed:.2f}s, expected < 2.0s without 10s block"


def test_reattach_timing_and_correlation_logging():
    """Verify structured log events record is_reattach=true and corr_id."""
    mock_paramiko = MagicMock()
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_chan = MagicMock()

    mock_paramiko.SSHClient.return_value = mock_client
    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_chan

    responses = [
        b"consoles> ",
        b"Connected to CML terminalserver\r\n",
        b"\r\nSW1#",
    ]
    mock_chan.recv.side_effect = lambda n: responses.pop(0) if responses else b""

    with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
        device = CMLConsoleDevice(
            gateway="10.10.20.161",
            username="admin",
            password="test_password",
            path="/lab1/sw1/0",
        )
        device._paramiko = mock_paramiko

        logged_messages = []

        def mock_info(msg, *args):
            try:
                formatted = msg % args
            except Exception:
                formatted = msg
            logged_messages.append(formatted)

        live_device_mod = sys.modules["cliq.connection.live_device"]
        with patch.object(live_device_mod.log, "info", side_effect=mock_info):
            with patch("time.sleep", return_value=None):
                device.reattach(reason="client_reconnect")

    timing_logs = [m for m in logged_messages if "[TIMING]" in m]
    assert any("is_reattach=true" in m for m in timing_logs), "Expected is_reattach=true in timing log"
    assert any("stage=gateway_ssh_start" in m for m in timing_logs)
    assert any("stage=cml_proxy_command_end" in m for m in timing_logs)


# ---------------------------------------------------------------------------
# 2. Plaintext JWT & Secret Redaction
# ---------------------------------------------------------------------------

def test_redact_jwt_tokens():
    """Verify plaintext JWTs (header.payload.signature) are redacted unconditionally."""
    sample_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    text = f"User session token is {sample_jwt} for active user"
    cleaned = obs.redact(text)
    assert sample_jwt not in cleaned
    assert "<redacted>" in cleaned


def test_redact_cisco_enable_secrets_and_passwords():
    """Verify Cisco enable secret, enable password, and line passwords are redacted."""
    cisco_config = """
    hostname R2
    enable secret 5 $1$mERr$9cTjUIEqNGurQiFU.ZeCi1
    enable password cisco_secret_pass
    line vty 0 4
     password vty_password_123
     login
    """
    cleaned = obs.redact(cisco_config)
    assert "$1$mERr$9cTjUIEqNGurQiFU.ZeCi1" not in cleaned
    assert "cisco_secret_pass" not in cleaned
    assert "vty_password_123" not in cleaned
    assert "enable secret 5 <redacted>" in cleaned
    assert "enable password <redacted>" in cleaned
    assert "password <redacted>" in cleaned


# ---------------------------------------------------------------------------
# 3. Diagnostic Export Security (token_cache.json exclusion)
# ---------------------------------------------------------------------------

def test_export_diagnostic_bundle_excludes_token_cache(tmp_path: Path):
    """Verify diagnostic zip bundle strictly excludes token_cache.json and redacts logs."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)

    # Legitimate log file with sensitive JWT and secrets
    app_log = log_dir / "cml.log"
    app_log.write_text(
        "2026-11-01 [AUTH] token=eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ.abc123def456\n"
        "2026-11-01 [CONFIG] enable secret 5 $1$abc$secret_hash\n",
        encoding="utf-8",
    )

    # Sensitive token cache files that MUST be excluded
    token_cache = log_dir / "token_cache.json"
    token_cache.write_text('{"token": "SUPER_SECRET_TOKEN", "expiry": 999999}', encoding="utf-8")

    sub_cache = log_dir / ".token_cache"
    sub_cache.write_text("secret_jwt_data", encoding="utf-8")

    target_zip = tmp_path / "diagnostics.zip"
    exported_path = obs.export_diagnostic_bundle(target_path=target_zip, log_dir=log_dir)

    assert exported_path.exists()
    with zipfile.ZipFile(exported_path, "r") as zf:
        namelist = zf.namelist()
        assert "token_cache.json" not in namelist
        assert ".token_cache" not in namelist
        assert "cml.log" in namelist

        content = zf.read("cml.log").decode("utf-8")
        assert "eyJhbGciOiJIUzI1NiJ9" not in content
        assert "$1$abc$secret_hash" not in content
        assert "<redacted>" in content


# ---------------------------------------------------------------------------
# 4. Multi-Device Isolation During Reattachment
# ---------------------------------------------------------------------------

def test_multi_device_isolation_during_reattach():
    """Verify reattaching R1 leaves R2, SW1, SW2 completely isolated and untouched."""
    devices = {}
    with patch.dict(sys.modules, {"paramiko": _mock_paramiko}):
        for dev_name in ["R1", "R2", "SW1", "SW2"]:
            mock_paramiko = MagicMock()
            mock_client = MagicMock()
            mock_transport = MagicMock()
            mock_chan = MagicMock()
            mock_paramiko.SSHClient.return_value = mock_client
            mock_client.get_transport.return_value = mock_transport
            mock_transport.open_session.return_value = mock_chan

            dev = CMLConsoleDevice(
                gateway="10.10.20.161",
                username="admin",
                password="test_password",
                path=f"/lab1/{dev_name.lower()}/0",
            )
            dev._paramiko = mock_paramiko
            dev._connected = True
            dev._current_prompt = f"{dev_name}#"
            dev._chan = mock_chan
            dev._client = mock_client
            devices[dev_name] = dev

    # Now reattach R1 only
    r1_responses = [b"consoles> ", b"Connected to CML terminalserver\r\n", b"\r\nR1#"]
    devices["R1"]._chan.recv.side_effect = lambda n: r1_responses.pop(0) if r1_responses else b""

    with patch("time.sleep", return_value=None):
        mode = devices["R1"].reattach(reason="tab_switch")

    assert mode == "enable"
    assert devices["R1"].current_prompt == "R1#"

    # Verify R2, SW1, SW2 state is 100% intact
    for other in ["R2", "SW1", "SW2"]:
        assert devices[other].is_connected is True
        assert devices[other].current_prompt == f"{other}#"
        # Channels of other devices were not touched or closed
        devices[other]._chan.close.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Clean Page Prompt Deduplication
# ---------------------------------------------------------------------------

def test_clean_page_single_prompt_on_empty_enter():
    """Verify empty enter output cleanly pops prompt without leaving duplicate artifacts."""
    # When Enter is pressed on an empty prompt, Cisco IOS returns: \r\nR2#
    raw_ios_reply = "\r\nR2#"
    cleaned = _clean_page(raw_ios_reply, "")
    assert cleaned == "", f"Expected empty body for empty enter prompt echo, got: {cleaned!r}"


def test_clean_page_command_output_removes_trailing_prompt():
    """Verify command output preserves body lines but strips trailing prompt."""
    raw_output = "show clock\r\n*23:15:00.123 UTC Sun Nov 1 2026\r\nR2#"
    cleaned = _clean_page(raw_output, "show clock")
    assert cleaned == "*23:15:00.123 UTC Sun Nov 1 2026"
    assert "R2#" not in cleaned
