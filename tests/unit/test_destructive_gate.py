#!/usr/bin/env python3
"""Unit + integration tests for the BLOCKING destructive-action confirmation gate.

The gate (``cliq.intelligence.advisory_router.ConfirmationGate``) is the active,
stateful counterpart to the passive red advisory. It HOLDS a destructive command
on Enter and forwards it to the device ONLY after an explicit 'y'. These tests
exercise the whole state machine offline and prove the exact deliverable
behaviours against a fake device whose running-config is inspected:

  * ``no ip address …`` + Enter + 'y'  -> command forwarded, IP removed.
  * ``no ip address …`` + Enter + 'n'  -> command discarded, IP still present
    (verified via ``show running-config``).
  * ``no ip address …`` + Enter + random key -> re-prompts, command still held.

Advisory-only guarantee: the gate NEVER forwards a command on its own; the fake
device (standing in for the real device) performs the removal only when the test
harness — mirroring the server wiring — forwards the released command on 'y'.
"""

from __future__ import annotations

import pytest

from cliq.intelligence.advisory_router import (
    ANSI_BOLD_RED,
    ANSI_RESET,
    CONFIRM_DISCARD,
    CONFIRM_FORWARD,
    CONFIRM_IDLE,
    CONFIRM_REPROMPT,
    ConfirmationGate,
    HeldCommand,
    build_destructive_confirmation,
    is_destructive_command,
    resolve_confirmation_answer,
)


# ---------------------------------------------------------------------------
# Detection — the configurable/extensible destructive-command list.
# ---------------------------------------------------------------------------
class TestDetection:
    @pytest.mark.parametrize(
        "command",
        [
            "no ip address 192.168.1.1 255.255.255.0",
            "no shutdown",
            "no router ospf 1",
            "shutdown",
            "clear ip route *",
            "clear counters",
            "erase startup-config",
            "reload",
            "write erase",
            "delete flash:test.cfg",
            "format flash:",
        ],
    )
    def test_destructive_commands_detected(self, command):
        assert is_destructive_command(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "show ip interface brief",
            "interface GigabitEthernet0/1",
            "ip address 10.0.0.1 255.255.255.0",
            "configure terminal",
            "",
            "   ",
        ],
    )
    def test_safe_commands_not_detected(self, command):
        assert is_destructive_command(command) is False


# ---------------------------------------------------------------------------
# Answer classification.
# ---------------------------------------------------------------------------
class TestResolveAnswer:
    @pytest.mark.parametrize("ans", ["y", "Y", "yes", "YES", " y "])
    def test_yes_forwards(self, ans):
        assert resolve_confirmation_answer(ans) == CONFIRM_FORWARD

    @pytest.mark.parametrize("ans", ["n", "N", "no", "NO", " n "])
    def test_no_discards(self, ans):
        assert resolve_confirmation_answer(ans) == CONFIRM_DISCARD

    @pytest.mark.parametrize("ans", ["", "\r", "x", "z", "yy", "maybe", "1"])
    def test_other_reprompts(self, ans):
        assert resolve_confirmation_answer(ans) == CONFIRM_REPROMPT


# ---------------------------------------------------------------------------
# The red confirmation advisory names the exact parsed object.
# ---------------------------------------------------------------------------
class TestConfirmationAdvisory:
    def test_names_ip_and_location_in_red(self):
        adv = build_destructive_confirmation(
            "no ip address 192.168.1.1 255.255.255.0", "config-if"
        )
        assert adv is not None
        assert adv.danger is True
        assert "DELETE" in adv.message
        assert "192.168.1.1 255.255.255.0" in adv.message
        assert "on this interface" in adv.message
        assert "confirm? (y/n)" in adv.message
        # Rendered in bold terminal red for the raw console write.
        assert adv.ansi_message.startswith(ANSI_BOLD_RED)
        assert adv.ansi_message.endswith(ANSI_RESET)

    def test_none_for_safe_command(self):
        assert build_destructive_confirmation("show version", "exec") is None


# ---------------------------------------------------------------------------
# ConfirmationGate state machine (no device I/O).
# ---------------------------------------------------------------------------
class TestConfirmationGate:
    def test_intercept_holds_destructive_and_returns_advisory(self):
        gate = ConfirmationGate()
        adv = gate.intercept("dev1", "no ip address 10.0.0.1 255.0.0.0", "config-if")
        assert adv is not None
        assert gate.is_pending("dev1") is True
        held = gate.held("dev1")
        assert isinstance(held, HeldCommand)
        assert held.command == "no ip address 10.0.0.1 255.0.0.0"

    def test_intercept_passes_safe_command_through(self):
        gate = ConfirmationGate()
        adv = gate.intercept("dev1", "show running-config", "config-if")
        assert adv is None
        assert gate.is_pending("dev1") is False

    def test_intercept_ignores_raw_inserts(self):
        gate = ConfirmationGate()
        adv = gate.intercept("dev1", "no shutdown", "config-if", raw=True)
        assert adv is None
        assert gate.is_pending("dev1") is False

    def test_resolve_yes_forwards_and_clears(self):
        gate = ConfirmationGate()
        gate.intercept("dev1", "no ip address 10.0.0.1 255.0.0.0", "config-if")
        outcome, held, reprompt = gate.resolve("dev1", "y")
        assert outcome == CONFIRM_FORWARD
        assert held.command == "no ip address 10.0.0.1 255.0.0.0"
        assert reprompt is None
        assert gate.is_pending("dev1") is False

    def test_resolve_no_discards_and_clears(self):
        gate = ConfirmationGate()
        gate.intercept("dev1", "no ip address 10.0.0.1 255.0.0.0", "config-if")
        outcome, held, reprompt = gate.resolve("dev1", "n")
        assert outcome == CONFIRM_DISCARD
        assert held.command == "no ip address 10.0.0.1 255.0.0.0"
        assert gate.is_pending("dev1") is False

    def test_resolve_other_reprompts_and_keeps_held(self):
        gate = ConfirmationGate()
        gate.intercept("dev1", "no ip address 10.0.0.1 255.0.0.0", "config-if")
        outcome, held, reprompt = gate.resolve("dev1", "x")
        assert outcome == CONFIRM_REPROMPT
        assert gate.is_pending("dev1") is True  # STILL held
        assert reprompt is not None
        assert "10.0.0.1" in reprompt.message  # re-names the held object
        assert reprompt.ansi_message.startswith(ANSI_BOLD_RED)

    def test_resolve_bare_enter_reprompts(self):
        gate = ConfirmationGate()
        gate.intercept("dev1", "no shutdown", "config-if")
        outcome, _held, reprompt = gate.resolve("dev1", "\r")
        assert outcome == CONFIRM_REPROMPT
        assert gate.is_pending("dev1") is True

    def test_resolve_with_nothing_held_is_idle(self):
        gate = ConfirmationGate()
        outcome, held, reprompt = gate.resolve("dev1", "y")
        assert outcome == CONFIRM_IDLE
        assert held is None

    def test_gate_is_per_device(self):
        gate = ConfirmationGate()
        gate.intercept("dev1", "reload", "enable")
        gate.intercept("dev2", "erase startup-config", "enable")
        assert gate.is_pending("dev1") and gate.is_pending("dev2")
        gate.resolve("dev1", "n")
        assert gate.is_pending("dev1") is False
        assert gate.is_pending("dev2") is True  # unaffected


# ---------------------------------------------------------------------------
# Integration — mirror the server wiring against a fake device whose
# running-config is inspected, proving forward/discard/reprompt end-to-end.
# ---------------------------------------------------------------------------
class _FakeDevice:
    """A minimal stand-in for the live device.

    Holds a running-config with an IP on an interface; ``execute`` applies
    ``no ip address …`` (removes the IP) exactly like the real device would when
    the command is forwarded. Records every command it is actually asked to run
    so the tests can assert the gate never forwarded a discarded/held command.
    """

    def __init__(self) -> None:
        self.running_config = (
            "interface GigabitEthernet0/1\n"
            " ip address 192.168.1.1 255.255.255.0\n"
        )
        self.executed: list[str] = []

    def execute(self, command: str) -> str:
        self.executed.append(command)
        low = command.strip().lower()
        if low.startswith("no ip address"):
            # Device removes the address line from the interface.
            self.running_config = "interface GigabitEthernet0/1\n"
            return ""
        if low.startswith("show running-config"):
            return self.running_config
        return ""


def _drive(gate: ConfirmationGate, device: _FakeDevice, device_id: str,
           command: str, mode: str):
    """Mimic the server: intercept on Enter; forward to the device only on 'y'.

    Returns the advisory produced (or None if the command was forwarded because
    it was not destructive).
    """
    advisory = gate.intercept(device_id, command, mode)
    if advisory is None:
        # Not destructive — forwarded straight to the device.
        device.execute(command)
    return advisory


def _answer(gate: ConfirmationGate, device: _FakeDevice, device_id: str, key: str):
    """Mimic the server confirm_response handler for one keystroke."""
    outcome, held, reprompt = gate.resolve(device_id, key)
    if outcome == CONFIRM_FORWARD:
        device.execute(held.command)  # DEVICE performs the action
    return outcome, held, reprompt


class TestGateIntegrationAgainstDevice:
    CMD = "no ip address 192.168.1.1 255.255.255.0"

    def test_enter_then_y_forwards_and_removes_ip(self):
        gate, device = ConfirmationGate(), _FakeDevice()
        # Enter — the command is HELD, NOT forwarded.
        adv = _drive(gate, device, "d1", self.CMD, "config-if")
        assert adv is not None
        assert device.executed == []  # nothing reached the device yet
        assert "192.168.1.1" in device.execute("show running-config")

        # 'y' — released to the device; the device removes the IP.
        outcome, _held, _rp = _answer(gate, device, "d1", "y")
        assert outcome == CONFIRM_FORWARD
        assert self.CMD in device.executed
        assert "192.168.1.1" not in device.execute("show running-config")
        assert gate.is_pending("d1") is False

    def test_enter_then_n_discards_ip_still_present(self):
        gate, device = ConfirmationGate(), _FakeDevice()
        adv = _drive(gate, device, "d1", self.CMD, "config-if")
        assert adv is not None
        assert device.executed == []

        # 'n' — discarded; the command NEVER reaches the device.
        outcome, _held, _rp = _answer(gate, device, "d1", "n")
        assert outcome == CONFIRM_DISCARD
        assert self.CMD not in device.executed
        # Verify via show running-config — the IP is still present.
        assert "ip address 192.168.1.1 255.255.255.0" in device.execute(
            "show running-config"
        )
        assert gate.is_pending("d1") is False

    def test_enter_then_random_key_reprompts_and_holds(self):
        gate, device = ConfirmationGate(), _FakeDevice()
        adv = _drive(gate, device, "d1", self.CMD, "config-if")
        assert adv is not None

        # A random key re-prompts; the command is still held and NOT forwarded.
        outcome, _held, reprompt = _answer(gate, device, "d1", "x")
        assert outcome == CONFIRM_REPROMPT
        assert reprompt is not None
        assert self.CMD not in device.executed
        assert gate.is_pending("d1") is True
        assert "192.168.1.1" in device.execute("show running-config")

        # A subsequent 'y' still releases the SAME held command.
        outcome, _held, _rp = _answer(gate, device, "d1", "y")
        assert outcome == CONFIRM_FORWARD
        assert self.CMD in device.executed
        assert "192.168.1.1" not in device.execute("show running-config")
