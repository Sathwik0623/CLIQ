#!/usr/bin/env python3
"""
test_fixes.py — Validates the three bug fixes:

  1. "sh ip int bq"  → status="typo", suggests "show ip interface brief"
  2. "ip address" in exec mode → wrong_mode with fix offer
  3. ConnectionLostError raised when channel is closed; _reconnect called

Run:
  python tools/test_fixes.py
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Make the src/ tree importable without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.db.query import CommandDB
from cliq.intelligence.engine import IntelligenceEngine


class TestBug1KeywordTypo(unittest.TestCase):
    """Bug 1: 'sh ip int bq' should resolve to typo, not invalid_arg."""

    @classmethod
    def setUpClass(cls):
        cls.db = CommandDB()
        cls.engine = IntelligenceEngine(cls.db)

    def test_sh_ip_int_bq_is_typo(self):
        analysis = self.engine.analyze("sh ip int bq", mode="enable", is_executing=True)
        self.assertEqual(
            analysis.status, "typo",
            f"Expected status='typo', got '{analysis.status}'. "
            f"expanded='{analysis.expanded_command}'"
        )

    def test_sh_ip_int_bq_suggests_brief(self):
        analysis = self.engine.analyze("sh ip int bq", mode="enable", is_executing=True)
        suggested = analysis.expanded_command.lower()
        self.assertIn(
            "brief", suggested,
            f"Expected suggestion containing 'brief', got '{suggested}'"
        )

    def test_valid_abbreviation_not_flagged_as_typo(self):
        """'sh ip int br' is a valid abbreviation — must NOT be flagged as typo."""
        analysis = self.engine.analyze("sh ip int br", mode="enable", is_executing=True)
        self.assertNotEqual(
            analysis.status, "typo",
            f"'sh ip int br' should NOT be a typo, got status='{analysis.status}'"
        )

    def test_missing_arg_still_works(self):
        """'ip address' with no args in exec mode → wrong_mode (not a typo)."""
        analysis = self.engine.analyze("ip address", mode="exec", is_executing=True)
        self.assertIn(
            analysis.status, ("wrong_mode", "missing_arg", "unknown"),
            f"Expected wrong_mode/missing_arg/unknown, got '{analysis.status}'"
        )


class TestBug2WrongModeFixOffer(unittest.TestCase):
    """Bug 2: 'ip address' in exec mode → wrong_mode with available fix path."""

    @classmethod
    def setUpClass(cls):
        cls.db = CommandDB()
        cls.engine = IntelligenceEngine(cls.db)

    def test_ip_address_exec_is_wrong_mode(self):
        analysis = self.engine.analyze("ip address", mode="exec", is_executing=True)
        self.assertIn(
            analysis.status, ("wrong_mode", "wrong_mode_do"),
            f"Expected wrong_mode, got '{analysis.status}'"
        )

    def test_wrong_mode_has_explanation(self):
        analysis = self.engine.analyze("ip address", mode="exec", is_executing=True)
        if analysis.status in ("wrong_mode", "wrong_mode_do"):
            self.assertIsNotNone(
                analysis.explanation,
                "wrong_mode analysis must carry an explanation"
            )

    def test_wrong_mode_do_from_config(self):
        """'show ip int br' from config mode → wrong_mode_do with 'do' suggestion."""
        analysis = self.engine.analyze("show ip interface brief", mode="config", is_executing=True)
        self.assertEqual(
            analysis.status, "wrong_mode_do",
            f"Expected wrong_mode_do, got '{analysis.status}'"
        )
        self.assertIn("do ", analysis.do_suggestion.lower())


class TestBug3Reconnect(unittest.TestCase):
    """Bug 3: ConnectionLostError raised on dead channel; _reconnect called."""

    def _make_device(self):
        from cliq.connection.live_device import CMLConsoleDevice
        dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
        dev.gateway = "fake-host"
        dev.username = "u"
        dev.password = "p"
        dev.path = "/lab/node/0"
        dev.timeout = 20
        dev._client = None
        dev._chan = None
        dev._connected = True
        dev._current_prompt = "R1#"
        dev._paramiko = MagicMock()
        return dev

    def test_is_alive_false_when_chan_none(self):
        from cliq.connection.live_device import CMLConsoleDevice
        dev = self._make_device()
        dev._chan = None
        self.assertFalse(dev._is_alive())

    def test_is_alive_false_when_chan_closed(self):
        dev = self._make_device()
        chan = MagicMock()
        chan.closed = True
        dev._chan = chan
        self.assertFalse(dev._is_alive())

    def test_check_connection_raises_on_dead_chan(self):
        from cliq.connection.live_device import ConnectionLostError
        dev = self._make_device()
        dev._chan = None
        with self.assertRaises(ConnectionLostError):
            dev._check_connection()

    def test_execute_calls_reconnect_on_lost_connection(self):
        from cliq.connection.live_device import ConnectionLostError
        dev = self._make_device()
        # Provide a non-None channel so is_connected passes the initial guard
        chan = MagicMock()
        chan.closed = False
        dev._chan = chan
        # Make _check_connection always raise ConnectionLostError
        dev._check_connection = MagicMock(side_effect=ConnectionLostError("dead"))
        # Make _reconnect succeed silently
        reconnect_calls = []

        def fake_reconnect():
            # After first reconnect, make _check_connection pass
            dev._check_connection = MagicMock()
            dev._send = MagicMock()
            dev._read_until = MagicMock(return_value="R1#")
            reconnect_calls.append(1)

        dev._reconnect = fake_reconnect

        from cliq.connection.output_cleaner import clean as real_clean
        with patch("cliq.connection.live_device.clean", return_value=""):
            try:
                dev.execute("show version")
            except ConnectionLostError:
                pass  # All retries failed path is also valid

        self.assertGreater(len(reconnect_calls), 0, "_reconnect was never called")

    def test_connection_lost_error_is_connection_error_subclass(self):
        from cliq.connection.live_device import ConnectionError, ConnectionLostError
        self.assertTrue(issubclass(ConnectionLostError, ConnectionError))


class TestBug4PlatformCompatibility(unittest.TestCase):
    """Bug 4: show vlan / show vlan brief should be wrong_device on router."""

    @classmethod
    def setUpClass(cls):
        cls.db = CommandDB()
        cls.engine = IntelligenceEngine(cls.db)

    def test_show_vlan_wrong_device_on_router(self):
        analysis = self.engine.analyze(
            "show vlan brief", mode="exec", is_executing=True, device_type="iosv"
        )
        self.assertEqual(
            analysis.status, "wrong_device",
            f"Expected 'wrong_device' for 'show vlan brief' on iosv, got '{analysis.status}'"
        )

    def test_show_vlan_ok_on_switch(self):
        analysis = self.engine.analyze(
            "show vlan brief", mode="exec", is_executing=True, device_type="iosvl2"
        )
        self.assertNotEqual(
            analysis.status, "wrong_device",
            "Expected 'show vlan brief' to be allowed on iosvl2"
        )

    def test_show_ip_route_no_platform_restriction(self):
        # show ip route has no platforms list — should be OK on any device
        analysis = self.engine.analyze(
            "show ip route", mode="exec", is_executing=True, device_type="iosv"
        )
        self.assertEqual(
            analysis.status, "ok",
            f"Expected 'ok' for 'show ip route' on iosv, got '{analysis.status}'"
        )

    def test_platforms_field_loaded_from_db(self):
        cmd = self.db.get_by_id("show_vlan_brief")
        self.assertIsNotNone(cmd, "show_vlan_brief not found in DB")
        self.assertIn("iosvl2", cmd.platforms,
                      f"Expected 'iosvl2' in platforms, got {cmd.platforms}")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestBug1KeywordTypo))
    suite.addTests(loader.loadTestsFromTestCase(TestBug2WrongModeFixOffer))
    suite.addTests(loader.loadTestsFromTestCase(TestBug3Reconnect))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
