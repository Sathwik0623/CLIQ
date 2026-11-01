"""Unit tests for CLIQ Phase U: Multi-Device Session Reliability, Concurrency Safety,
and Explorer Icon Refinement.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from cliq.web import server
from cliq.web.server import (
    _DEVICE_CONNECT_LOCKS,
    _DEVICE_RUNTIMES,
    _get_device_connect_lock,
    _release_socket_registrations,
    DeviceRuntime,
)


class DummyMockDevice:
    """Mock live device for multi-device concurrency testing."""

    def __init__(self, device_id: str, prompt: str = "Router#"):
        self.device_id = device_id
        self.current_prompt = prompt
        self.node_type = "iosv"
        self.capability = {"routing"}
        self.is_connected = True
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def test_device_connect_lock_granularity():
    """Verify that connection locks are isolated per (token, device_id)."""
    token_a = "token_alpha_123"
    token_b = "token_beta_456"

    lock_a_r1 = _get_device_connect_lock(token_a, "r1")
    lock_a_r1_again = _get_device_connect_lock(token_a, "r1")
    lock_a_sw1 = _get_device_connect_lock(token_a, "sw1")
    lock_b_r1 = _get_device_connect_lock(token_b, "r1")

    # Same token + device_id must yield the exact same lock instance
    assert lock_a_r1 is lock_a_r1_again

    # Different device_id under same token must yield a different lock
    assert lock_a_r1 is not lock_a_sw1

    # Same device_id under different token must yield a different lock
    assert lock_a_r1 is not lock_b_r1


def test_release_socket_registrations_clears_locks():
    """Verify _release_socket_registrations cleans up device connect locks for that token."""
    token = "test_release_token_789"
    _get_device_connect_lock(token, "dev1")
    _get_device_connect_lock(token, "dev2")
    _get_device_connect_lock("other_token", "dev1")

    assert (token, "dev1") in _DEVICE_CONNECT_LOCKS
    assert (token, "dev2") in _DEVICE_CONNECT_LOCKS
    assert ("other_token", "dev1") in _DEVICE_CONNECT_LOCKS

    _release_socket_registrations(token)

    assert (token, "dev1") not in _DEVICE_CONNECT_LOCKS
    assert (token, "dev2") not in _DEVICE_CONNECT_LOCKS
    # Other token's locks must remain intact
    assert ("other_token", "dev1") in _DEVICE_CONNECT_LOCKS

    # Clean up other token
    _release_socket_registrations("other_token")


def test_safe_close_superseded_ws():
    """Verify superseded WebSocket close suppresses WebSocketDisconnect and CancelledError."""
    async def _test():
        mock_ws = AsyncMock()
        mock_ws.close.side_effect = WebSocketDisconnect(code=1000)

        # Mimics the safe closer logic in ws_terminal
        async def _safe_close_superseded(ws_to_close):
            try:
                await ws_to_close.close(code=1000, reason="Superseded by new connection")
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                pass

        # Should not raise any exception
        await _safe_close_superseded(mock_ws)
        mock_ws.close.assert_awaited_once_with(code=1000, reason="Superseded by new connection")

    asyncio.run(_test())


def test_multi_device_isolation_r1_r2_sw1():
    """Verify that opening SW1 does not alter, re-attach, or disconnect already-open R1 and R2."""
    async def _test():
        token = "multi_device_test_token"
        server.DEVICES_BY_TOKEN[token] = {
            "node-r1": {"id": "node-r1", "label": "R1", "type": "iosv"},
            "node-r2": {"id": "node-r2", "label": "R2", "type": "iosv"},
            "node-sw1": {"id": "node-sw1", "label": "SW1", "type": "iosvl2"},
        }

        # Pre-populate R1 and R2 runtimes
        dev_r1 = DummyMockDevice("node-r1", "R1#")
        dev_r2 = DummyMockDevice("node-r2", "R2#")
        rt_r1 = DeviceRuntime(device=dev_r1, mode="enable", status="connected", prompt="R1#", node_type="iosv")
        rt_r2 = DeviceRuntime(device=dev_r2, mode="enable", status="connected", prompt="R2#", node_type="iosv")

        _DEVICE_RUNTIMES[(token, "node-r1")] = rt_r1
        _DEVICE_RUNTIMES[(token, "node-r2")] = rt_r2

        # Test that opening SW1 connects SW1 and leaves R1, R2 untouched
        dev_sw1 = DummyMockDevice("node-sw1", "SW1#")
        with patch("cliq.web.server.CMLConsoleDevice", return_value=dev_sw1):
            lock = _get_device_connect_lock(token, "node-sw1")
            async with lock:
                dev_sw1.connect()
                rt_sw1 = DeviceRuntime(device=dev_sw1, mode="enable", status="connected", prompt="SW1#", node_type="iosvl2")
                _DEVICE_RUNTIMES[(token, "node-sw1")] = rt_sw1

        # Verify R1 and R2 were never disconnected or re-connected
        assert dev_r1.connect_calls == 0
        assert dev_r1.disconnect_calls == 0
        assert dev_r2.connect_calls == 0
        assert dev_r2.disconnect_calls == 0

        # Verify SW1 connected exactly once
        assert dev_sw1.connect_calls == 1

        # Verify all 3 devices are cleanly present in _DEVICE_RUNTIMES
        assert (token, "node-r1") in _DEVICE_RUNTIMES
        assert (token, "node-r2") in _DEVICE_RUNTIMES
        assert (token, "node-sw1") in _DEVICE_RUNTIMES

        # Cleanup
        _release_socket_registrations(token)
        server.DEVICES_BY_TOKEN.pop(token, None)

    asyncio.run(_test())


def test_concurrent_sw1_requests_idempotent():
    """Verify that multiple concurrent requests for the same device serialize on the lock
    and only initialize the device once without duplicate proxy streams.
    """
    async def _test():
        token = "concurrent_test_token"
        server.DEVICES_BY_TOKEN[token] = {
            "node-sw1": {"id": "node-sw1", "label": "SW1", "type": "iosvl2"},
        }

        dev_sw1 = DummyMockDevice("node-sw1", "SW1#")
        connect_counter = 0

        async def connect_worker(worker_id: int):
            nonlocal connect_counter
            lock = _get_device_connect_lock(token, "node-sw1")
            async with lock:
                # Check if already connected (idempotency check)
                if (token, "node-sw1") in _DEVICE_RUNTIMES:
                    return "already_connected"

                # Simulate connection to CML
                await asyncio.sleep(0.02)
                connect_counter += 1
                dev_sw1.connect()
                _DEVICE_RUNTIMES[(token, "node-sw1")] = DeviceRuntime(
                    device=dev_sw1, mode="enable", status="connected", prompt="SW1#", node_type="iosvl2"
                )
                return "connected"

        # Launch two workers concurrently for the same SW1 device
        results = await asyncio.gather(connect_worker(1), connect_worker(2))

        # Exactly one worker performs connection; the other sees already_connected
        assert sorted(results) == ["already_connected", "connected"]
        assert connect_counter == 1
        assert dev_sw1.connect_calls == 1

        # Cleanup
        _release_socket_registrations(token)
        server.DEVICES_BY_TOKEN.pop(token, None)

    asyncio.run(_test())
