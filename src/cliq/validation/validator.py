#!/usr/bin/env python3
"""
validator.py — Main safety validation logic.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from cliq.validation.blocklist import get_blocklist_reason, is_blocklisted
from cliq.validation.schema import SafetyVerdict, Severity, ValidationContext, VerdictType

log = logging.getLogger("cliq.validation")


class SafetyValidator:
    """Validates commands for safety before execution."""

    # Valid modes for CLI operation. config-subif (subinterface) and the other
    # config sub-modes are first-class modes: a subinterface legitimately
    # accepts interface-level commands, so it must NOT be rejected as an
    # "unknown mode" nor flagged wrong-mode for 'ip address' / 'no ip address'.
    VALID_MODES = frozenset({
        "exec", "config", "config-if", "config-subif", "config-router",
        "config-line", "config-vlan", "enable",
    })

    # Modes that support 'do' prefix (all config sub-modes).
    DO_PREFIX_MODES = frozenset({
        "config", "config-if", "config-subif", "config-router",
        "config-line", "config-vlan",
    })

    # Bug 4 — modes that inherit the interface-level command set. A subinterface
    # (config-subif) is a child/sibling of config-if for command validation, so
    # interface commands ('ip address', 'no ip address', 'shutdown', ...) are
    # valid there. Kept as a set so more inheriting modes can be added later.
    INTERFACE_LEVEL_MODES = frozenset({"config-if", "config-subif"})

    def validate(self, ctx: ValidationContext) -> SafetyVerdict:
        """
        Validate a command for safety.

        Args:
            ctx: Validation context (command, device_platform, current_mode, etc.)

        Returns:
            A SafetyVerdict with validation result, reason, and guidance.
        """
        ctx.validate()
        start = time.monotonic()

        # Step 0: Check if do-prefixed in config mode (pre-normalization check for validity)
        command_lower = ctx.command.strip().lower()
        do_is_valid = False
        if ctx.current_mode in self.DO_PREFIX_MODES and command_lower.startswith("do "):
            do_is_valid = True

        # Step 1: Normalize command (handle do prefix, etc.)
        normalized_cmd, do_removed = self._normalize_command(ctx.command, ctx.current_mode)

                # Step 2: Validate target platform and device metadata
        plat = (ctx.device_platform or "").strip().lower()
        if not plat or plat in ("unknown", "missing", "unspecified", "none"):
            return SafetyVerdict(
                valid=False,
                verdict_type=VerdictType.UNKNOWN_COMMAND,
                reason="Target device platform or metadata is missing/unknown. Cannot verify compatibility.",
                severity=Severity.WARNING,
                normalized_command=normalized_cmd,
                suggested_action="Specify target device platform and verify compatibility before insertion.",
            )

        # Step 2b: Multi-vendor check (e.g. Juniper device receiving Cisco syntax)
        vendor = (ctx.vendor or "").strip().lower()
        if vendor in ("juniper", "junos") or plat in ("junos", "juniper", "juniper_vrr", "vmx") or "juniper" in plat:
            cisco_markers = (
                "show ip interface brief", "show running-config", "show run", "show ip route",
                "configure terminal", "conf t", "switchport", "router ospf", "router bgp",
                "ip route", "enable", "wr mem", "copy run start",
            )
            if any(normalized_cmd.startswith(m) for m in cisco_markers) or "switchport" in normalized_cmd:
                return SafetyVerdict(
                    valid=False,
                    verdict_type=VerdictType.WRONG_DEVICE,
                    reason=f"Command '{ctx.command}' is Cisco IOS-XE syntax and is incompatible with Juniper ({plat}).",
                    severity=Severity.WARNING,
                    normalized_command=normalized_cmd,
                    suggested_action="Use native Juniper Junos syntax (e.g. 'show interfaces terse', 'show route').",
                )

        # Step 2c: Device Role validation (Router vs Switch - e.g. Cisco switchport commands)
        role = (ctx.device_role or "").strip().lower()
        is_router = (role == "router" or plat in ("iosv", "iosxr", "cisco_router") or (ctx.device_id and bool(re.match(r"^r\d+$", str(ctx.device_id), re.I))))
        is_switch = (role == "switch" or plat in ("iosvl2", "cat9k", "cat9500", "nexus", "cisco_switch") or (ctx.device_id and bool(re.match(r"^sw\d+$", str(ctx.device_id), re.I))))

        # Check for switchport configuration command (never allow on router)
        is_switchport_cmd = (
            normalized_cmd.startswith("switchport")
            or " switchport" in normalized_cmd
            or normalized_cmd.startswith("no switchport")
        )
        if is_switchport_cmd and is_router and not is_switch:
            dev_name = ctx.device_id or ctx.device_platform
            return SafetyVerdict(
                valid=False,
                verdict_type=VerdictType.WRONG_DEVICE,
                reason=f"Switchport configuration commands ('{ctx.command}') are only valid on Layer 2/3 switches and are not supported on router '{dev_name}'.",
                severity=Severity.WARNING,
                normalized_command=normalized_cmd,
                suggested_action="Configure Layer 3 routed interfaces or subinterfaces on the router instead of switchports.",
            )

        # Step 3: Check blocklist
        is_blocked, severity_str = is_blocklisted(normalized_cmd)
        if is_blocked:
            elapsed_ms = (time.monotonic() - start) * 1000
            severity = Severity.CRITICAL if severity_str == "critical" else Severity.WARNING
            reason = get_blocklist_reason(normalized_cmd)
            log.warning(f"Blocklisted command: {normalized_cmd} ({severity_str})")
            return SafetyVerdict(
                valid=False,
                verdict_type=VerdictType.BLOCKLISTED,
                reason=reason,
                severity=severity,
                normalized_command=normalized_cmd,
                suggested_action="This command is disabled for safety. Use read-only 'show' commands instead.",
            )

        # Step 4: Check mode correctness (skip if do-prefixed valid command)
        if not do_is_valid:
            is_valid_mode, mode_reason = self._check_mode_correctness(
                normalized_cmd, ctx.current_mode
            )
            if not is_valid_mode:
                elapsed_ms = (time.monotonic() - start) * 1000
                log.warning(f"Mode mismatch: {normalized_cmd} in mode {ctx.current_mode}")
                # Provide better suggestion for exec commands in config mode
                suggested = "Switch to exec mode or use 'do' prefix." if "exec" in mode_reason.lower() else "Switch to the appropriate mode first."
                return SafetyVerdict(
                    valid=False,
                    verdict_type=VerdictType.WRONG_MODE,
                    reason=mode_reason,
                    severity=Severity.WARNING,
                    normalized_command=normalized_cmd,
                    suggested_action=suggested,
                )

        # Step 5: All checks passed
        elapsed_ms = (time.monotonic() - start) * 1000
        log.info(f"Command validated: {normalized_cmd} ({elapsed_ms:.1f}ms)")
        return SafetyVerdict(
            valid=True,
            verdict_type=VerdictType.OK,
            reason=f"Command is valid for platform {ctx.device_platform} in mode {ctx.current_mode}.",
            severity=Severity.INFO,
            normalized_command=normalized_cmd if do_removed else ctx.command,
            suggested_action=None,
        )

    def _normalize_command(self, command: str, current_mode: str) -> tuple[str, bool]:
        """
        Normalize a command by removing the 'do' prefix if present.

        Args:
            command: The raw command input.
            current_mode: The current CLI mode.

        Returns:
            A tuple of (normalized_command, was_do_prefix_removed).
        """
        normalized = command.strip().lower()

        # Check if we're in a mode that supports 'do' prefix
        if current_mode in self.DO_PREFIX_MODES and normalized.startswith("do "):
            normalized = normalized[3:].strip()
            return normalized, True

        return normalized, False

    def _check_mode_correctness(self, command: str, current_mode: str) -> tuple[bool, str]:
        """
        Check if a command is valid in the current mode.

        Args:
            command: The normalized command.
            current_mode: The current CLI mode.

        Returns:
            A tuple of (is_valid, reason_if_invalid).
        """
        normalized = command.strip().lower()

        # Validate current mode
        if current_mode not in self.VALID_MODES:
            return False, f"Unknown mode '{current_mode}'."

        # Config-mode-only commands
        config_only_prefixes = frozenset({
            "interface",
            "router",
            "line",
            "access-list",
            "route-map",
            "policy-map",
            "class-map",
            "vlan",
            "ip route",
            "username",
            "enable password",
        })

        # Exec-mode-only commands
        exec_only_prefixes = frozenset({
            "show",
            "debug",
            "ping",
            "traceroute",
            "telnet",
            "ssh",
            "reload",
            "exit",
            "enable",
        })

        # Check if a config-only command is being run in exec mode
        for prefix in config_only_prefixes:
            if normalized.startswith(prefix):
                if current_mode == "exec":
                    return False, f"'{prefix}' commands are only valid in config mode."

        # Check if an interface-level command is run in exec/enable mode
        interface_only_prefixes = (
            "switchport", "ip address", "no shutdown", "channel-group", "spanning-tree",
        )
        for prefix in interface_only_prefixes:
            if normalized.startswith(prefix):
                if current_mode in ("exec", "enable"):
                    return False, f"'{prefix}' is an interface configuration command. Currently in '{current_mode}' mode."
                elif current_mode not in self.INTERFACE_LEVEL_MODES and current_mode == "config":
                    return False, f"'{prefix}' is an interface configuration command. Enter interface configuration mode ('interface <name>') first."

        # Check if an exec-only command is being run in config mode (without 'do')
        for prefix in exec_only_prefixes:
            if normalized.startswith(prefix):
                if current_mode in self.DO_PREFIX_MODES:
                    return False, f"'{prefix}' is an exec-mode command. Use 'do {command}' in config mode."

        return True, ""
