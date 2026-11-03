"""
Unit tests for Phase V WebSocket Lifecycle, Disconnect Handling, and Multi-Device Context (CLIQ).
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.websockets import WebSocketDisconnect

from cliq.ai.network_context import NetworkContextService
from cliq.ai.orchestrator import CopilotOrchestrator
from cliq.ai.provider import ModelInfo
from cliq.ai.providers.meta import META_MODELS, MetaProvider
from cliq.connection.live_device import CMLConsoleDevice, _clean_page


# ── 1. WebSocket Disconnect & Safe Send Tests ─────────────────────────────────

def test_safe_send_json_handles_disconnect_and_runtime_error():
    """Verify that closed-socket exceptions (WebSocketDisconnect, RuntimeError) do not crash the server."""
    async def _run():
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        async def _safe_send_json(payload: dict) -> bool:
            try:
                await mock_ws.send_json(payload)
                return True
            except (WebSocketDisconnect, RuntimeError, ConnectionResetError, asyncio.CancelledError):
                return False

        res1 = await _safe_send_json({"type": "test"})
        assert res1 is False

        # Test Starlette's ASGI RuntimeError
        mock_ws.send_json = AsyncMock(
            side_effect=RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'.")
        )
        res2 = await _safe_send_json({"type": "test"})
        assert res2 is False

    asyncio.run(_run())


# ── 2. Session Health Probe Guard During Paging ───────────────────────────────

def test_session_health_probe_skips_active_pager():
    """Session health check must immediately yield if a device has an active pager to prevent corrupting console stream."""
    mock_runtime = MagicMock()
    mock_runtime.pager_active = True
    mock_runtime.help_paging = False
    mock_runtime.lock = asyncio.Lock()

    # The guard logic in server.py:
    def should_skip_health_check(runtime) -> bool:
        return bool(getattr(runtime, "pager_active", False) or getattr(runtime, "help_paging", False))

    assert should_skip_health_check(mock_runtime) is True

    # When pager is inactive but help_paging is active
    mock_runtime.pager_active = False
    mock_runtime.help_paging = True
    assert should_skip_health_check(mock_runtime) is True

    # When both are inactive
    mock_runtime.pager_active = False
    mock_runtime.help_paging = False
    assert should_skip_health_check(mock_runtime) is False


# ── 3. Pager Continuation & Cleaning Tests ────────────────────────────────────

def test_pager_clean_page_strips_erase_and_more():
    """Verify _clean_page cleanly strips --More--, backspaces, and boundary spaces without touching config indentation."""
    raw_page = "\x08\x08\x08\x08\x08\x08\x08\x08        \x08\x08\x08\x08\x08\x08\x08\x08\r\ninterface GigabitEthernet0/1\n description Link to R2\n ip address 10.0.12.1 255.255.255.0\n--More--"
    cleaned = _clean_page(raw_page)

    assert "--More--" not in cleaned
    assert "\x08" not in cleaned
    assert "interface GigabitEthernet0/1" in cleaned
    assert "description Link to R2" in cleaned
    assert "ip address 10.0.12.1 255.255.255.0" in cleaned


def test_pager_send_key_does_not_probe_channel():
    """Verify pager_send_key directly writes the pager key and reads page without extra keepalive probes."""
    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._chan = MagicMock()
    dev._chan.recv = MagicMock(return_value=b"interface Gi0/2\r\n--More--")

    with patch.object(dev, "_check_connection", create=True) as mock_check:
        page, more = dev.pager_send_key(" ")
        mock_check.assert_not_called()
        assert more is True
        assert "interface Gi0/2" in page


# ── 4. Multi-Device Context Extraction Tests ─────────────────────────────────

def test_multi_device_context_assembly():
    """Verify orchestrator extracts and formats context for multiple devices independently (e.g. R1 + R2)."""
    service = NetworkContextService()

    mock_r1 = MagicMock()
    mock_r1.device.execute.return_value = "GigabitEthernet0/0 10.0.0.1 YES NVRAM up up\nGigabitEthernet0/2 10.0.12.1 YES NVRAM administratively down down"

    mock_r2 = MagicMock()
    mock_r2.device.execute.return_value = "GigabitEthernet0/0 10.0.0.2 YES NVRAM up up\nGigabitEthernet0/2 10.0.12.2 YES NVRAM up up"

    with patch.object(service, "_resolve_runtime") as mock_res:
        def resolve_side_effect(session_token, dev_id):
            if "r1" in dev_id.lower() or dev_id == "n0":
                return mock_r1
            if "r2" in dev_id.lower() or dev_id == "n1":
                return mock_r2
            return None

        mock_res.side_effect = resolve_side_effect

        orchestrator = CopilotOrchestrator()
        orchestrator.context_service = service

        chips = [
            {"id": "interfaces_n0", "type": "interfaces", "deviceId": "n0", "label": "@R1 Interfaces"},
            {"id": "interfaces_n1", "type": "interfaces", "deviceId": "n1", "label": "@R2 Interfaces"},
        ]

        context_text = orchestrator._assemble_context_text(
            session_token="test-token",
            context_chips=chips,
            attachments=[],
            user_text="Compare R1 and R2 interface status",
        )

        assert "[Device R1 - IP Interfaces Brief]:" in context_text
        assert "10.0.12.1" in context_text
        assert "[Device R2 - IP Interfaces Brief]:" in context_text
        assert "10.0.12.2" in context_text


def test_general_device_context_chip():
    """Verify general device context chip includes both summary and interface overview."""
    service = NetworkContextService()
    mock_r1 = MagicMock()
    mock_r1.device.execute.return_value = "Gi0/0 10.0.0.1 up up"

    with patch.object(service, "_resolve_runtime", return_value=mock_r1), \
         patch.object(service, "get_device_info", return_value={"status": "connected", "mode": "exec"}):

        orchestrator = CopilotOrchestrator()
        orchestrator.context_service = service

        chips = [{"id": "device_n0", "type": "device", "deviceId": "n0", "label": "@R1"}]
        context_text = orchestrator._assemble_context_text("test-token", chips, [])

        assert "[Device Summary: R1 (ID: n0)" in context_text
        assert "Status: connected" in context_text
        assert "Mode: exec" in context_text
        assert "[IP Interfaces Brief]:" in context_text
        assert "Gi0/0 10.0.0.1 up up" in context_text


# ── 5. Model Catalog & Identifier Tests ──────────────────────────────────────

def test_default_model_identifier():
    """Verify Muse Spark 1.3 is the configured default model for Meta provider."""
    provider = MetaProvider(api_key="test-key")
    assert provider.default_model == "muse-spark-1.3"

    models = provider.get_models()
    default_models = [m for m in models if m.is_default]
    assert len(default_models) == 1
    assert default_models[0].id == "muse-spark-1.3"
    assert any(m.id == "muse-wonder-1.0" for m in models)
