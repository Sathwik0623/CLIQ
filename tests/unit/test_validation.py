#!/usr/bin/env python3
"""Unit tests for the validation layer."""

import pytest

from cliq.validation.blocklist import get_blocklist_reason, is_blocklisted
from cliq.validation.platform_rules import (
    command_capability,
    command_suits_platform,
    get_device_type,
    is_valid_for_platform,
    platform_capabilities,
)
from cliq.validation.schema import SafetyVerdict, Severity, ValidationContext, VerdictType
from cliq.validation.validator import SafetyValidator


class TestBlocklist:
    """Test dangerous-command blocklist."""

    def test_reload_is_blocklisted(self):
        """reload should be blocklisted as critical."""
        is_blocked, severity = is_blocklisted("reload")
        assert is_blocked
        assert severity == "critical"

    def test_write_erase_is_blocklisted(self):
        """write erase should be blocklisted as critical."""
        is_blocked, severity = is_blocklisted("write erase")
        assert is_blocked
        assert severity == "critical"

    def test_clear_is_blocklisted(self):
        """clear commands should be blocklisted as warning."""
        is_blocked, severity = is_blocklisted("clear interface counters")
        assert is_blocked
        assert severity == "warning"

    def test_do_prefix_blocklist_check(self):
        """'do reload' should also be blocklisted (do prefix stripped)."""
        is_blocked, severity = is_blocklisted("do reload")
        assert is_blocked
        assert severity == "critical"

    def test_safe_command_not_blocklisted(self):
        """show command should not be blocklisted."""
        is_blocked, _ = is_blocklisted("show ip route")
        assert not is_blocked

    def test_blocklist_reason_is_clear(self):
        """Blocklist reasons should be human-readable."""
        reason = get_blocklist_reason("reload")
        assert "reboot" in reason.lower()
        assert reason != ""


class TestPlatformRules:
    """Device capability is ADVISORY-only — the device is the source of truth.

    CLIQ must never hard-block a command on a hardcoded platform assumption, so
    ``is_valid_for_platform`` is always permissive. The capability classifiers
    are used only for SOFT suggestion ranking.
    """

    def test_capability_check_never_blocks(self):
        """is_valid_for_platform is permissive for every command/platform pair."""
        for cmd, plat in [
            ("vlan 10", "iosvl2"),
            ("vlan 10", "iosxr"),          # would previously be 'invalid'
            ("ip ospf 1 area 0", "iosxr"),
            ("router ospf 1", "iosvl2"),   # would previously be 'invalid'
            ("switchport mode access", "iosxr"),
            ("show vlan brief", "cat8000v"),
            ("do vlan 10", "iosxr"),
        ]:
            is_valid, reason = is_valid_for_platform(cmd, plat)
            assert is_valid
            assert reason == ""

    def test_command_capability_classification(self):
        """Commands classify as routing / switching / both for ranking."""
        assert command_capability("vlan 10") == "switching"
        assert command_capability("switchport mode access") == "switching"
        assert command_capability("show vlan brief") == "switching"
        assert command_capability("ip ospf 1 area 0") == "routing"
        assert command_capability("router bgp 65000") == "routing"
        assert command_capability("show version") == "both"
        assert command_capability("show ip interface brief") == "both"

    def test_platform_capabilities(self):
        """Modern IOS XE platforms route AND switch; device decides, not a table."""
        assert platform_capabilities("cat8000v") == frozenset({"routing", "switching"})
        assert platform_capabilities("iosxe") == frozenset({"routing", "switching"})
        assert platform_capabilities("iosvl2") == frozenset({"switching"})
        assert platform_capabilities("iosv") == frozenset({"routing"})
        assert platform_capabilities("iosxr") == frozenset({"routing"})
        # Unknown platforms default to permissive (both).
        assert platform_capabilities("unknown") == frozenset({"routing", "switching"})
        assert platform_capabilities("") == frozenset({"routing", "switching"})

    def test_command_suits_platform_soft_ranking(self):
        """Soft ranking: matching capability ranks first, but nothing is blocked."""
        # Switching command suits a switch and a both-capable device.
        assert command_suits_platform("vlan 10", "iosvl2")
        assert command_suits_platform("vlan 10", "cat8000v")
        # Switching command does not 'suit' a pure router (ranked lower, not blocked).
        assert not command_suits_platform("vlan 10", "iosxr")
        # Routing command suits a router and a both-capable device.
        assert command_suits_platform("ip ospf 1 area 0", "iosxr")
        assert command_suits_platform("ip ospf 1 area 0", "cat8000v")
        # Capability-neutral commands suit everything.
        assert command_suits_platform("show version", "iosvl2")
        assert command_suits_platform("show version", "iosxr")

    def test_device_type_classification(self):
        """Device types reflect real capability, including 'both'."""
        assert get_device_type("iosvl2") == "switch"
        assert get_device_type("iosxe") == "both"
        assert get_device_type("cat8000v") == "both"
        assert get_device_type("iosxr") == "router"
        assert get_device_type("iosv") == "router"
        assert get_device_type("unknown") == "unknown"


class TestValidationContext:
    """Test validation context."""

    def test_valid_context(self):
        """Valid context should not raise."""
        ctx = ValidationContext(
            command="show ip route",
            device_platform="iosxr",
            current_mode="exec",
        )
        ctx.validate()  # Should not raise

    def test_empty_command_invalid(self):
        """Empty command should be invalid."""
        ctx = ValidationContext(command="", device_platform="iosxr")
        with pytest.raises(ValueError, match="command cannot be empty"):
            ctx.validate()

    def test_empty_platform_invalid(self):
        """Empty platform should be invalid."""
        ctx = ValidationContext(command="show ip route", device_platform="")
        with pytest.raises(ValueError, match="device_platform cannot be empty"):
            ctx.validate()


class TestSafetyValidator:
    """Test the main validator."""

    def test_valid_command_returns_ok(self):
        """A valid command should return verdict type OK."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="show ip route",
            device_platform="iosxr",
            current_mode="exec",
        )
        result = validator.validate(ctx)
        assert result.valid
        assert result.verdict_type == VerdictType.OK
        assert result.severity == Severity.INFO

    def test_blocklisted_command_returns_blocklisted(self):
        """A blocklisted command should return verdict type BLOCKLISTED."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="reload",
            device_platform="iosxr",
            current_mode="exec",
        )
        result = validator.validate(ctx)
        assert not result.valid
        assert result.verdict_type == VerdictType.BLOCKLISTED
        assert result.severity == Severity.CRITICAL

    def test_capability_mismatch_is_not_blocked(self):
        """A capability 'mismatch' must NOT be blocked — the device is the truth.

        'vlan 10' on a platform we'd guess is a router used to return
        WRONG_DEVICE. Since the device is the source of truth (and many IOS XE
        platforms both route and switch), the validator no longer blocks it.
        Truly invalid input is surfaced by the device's own error instead.
        """
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="vlan 10",
            device_platform="iosxr",
            current_mode="config",
        )
        result = validator.validate(ctx)
        assert result.verdict_type != VerdictType.WRONG_DEVICE

    def test_do_prefix_in_config_mode(self):
        """'do show ip route' in config mode should be valid and normalized."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="do show ip route",
            device_platform="iosxr",
            current_mode="config",
        )
        result = validator.validate(ctx)
        assert result.valid
        assert result.verdict_type == VerdictType.OK
        assert "do" not in result.normalized_command or result.normalized_command == "do show ip route"

    def test_config_command_in_exec_mode(self):
        """'interface GigabitEthernet0/0' in exec mode should fail mode check."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="interface GigabitEthernet0/0",
            device_platform="iosxr",
            current_mode="exec",
        )
        result = validator.validate(ctx)
        assert not result.valid
        assert result.verdict_type == VerdictType.WRONG_MODE

    def test_exec_command_in_config_mode_without_do(self):
        """'show ip route' in config mode without 'do' should fail mode check."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="show ip route",
            device_platform="iosxr",
            current_mode="config",
        )
        result = validator.validate(ctx)
        assert not result.valid
        assert result.verdict_type == VerdictType.WRONG_MODE
        assert "do" in result.suggested_action.lower() or "exec" in result.suggested_action.lower()

    def test_normalized_command_in_result(self):
        """Normalized command should be in result."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="SHOW IP ROUTE",
            device_platform="iosxr",
            current_mode="exec",
        )
        result = validator.validate(ctx)
        assert result.valid
        assert "show ip route" in result.normalized_command.lower()

    def test_verdict_summary_is_readable(self):
        """Verdict summary should be human-readable."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="reload",
            device_platform="iosxr",
            current_mode="exec",
        )
        result = validator.validate(ctx)
        summary = result.summary()
        assert "✗" in summary  # Failure marker
        assert result.verdict_type.value in summary

    def test_velocity_less_than_50ms(self):
        """Validation should complete in less than 50ms."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="show ip route",
            device_platform="iosxr",
            current_mode="exec",
        )
        import time
        start = time.monotonic()
        validator.validate(ctx)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50, f"Validation took {elapsed_ms:.1f}ms, expected < 50ms"

    def test_case_insensitive_validation(self):
        """Validation should be case-insensitive."""
        validator = SafetyValidator()
        ctx1 = ValidationContext(
            command="RELOAD",
            device_platform="iosxr",
            current_mode="exec",
        )
        ctx2 = ValidationContext(
            command="reload",
            device_platform="iosxr",
            current_mode="exec",
        )
        result1 = validator.validate(ctx1)
        result2 = validator.validate(ctx2)
        assert result1.verdict_type == result2.verdict_type
        assert result1.valid == result2.valid


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
