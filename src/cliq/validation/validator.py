#!/usr/bin/env python3
"""
validator.py — Main safety validation logic.
"""

from __future__ import annotations

import logging
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

        # Step 2: Device-capability is ADVISORY only — never block here.
        # The device is the source of truth for what it accepts (a Cat8000v both
        # routes and switches, so 'vlan'/'switchport'/'ip ospf' are all valid on
        # it). Capability now only influences SOFT suggestion ranking elsewhere;
        # truly invalid commands are surfaced by the device's own error message.

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

        # Check if an exec-only command is being run in config mode (without 'do')
        for prefix in exec_only_prefixes:
            if normalized.startswith(prefix):
                if current_mode in self.DO_PREFIX_MODES:
                    return False, f"'{prefix}' is an exec-mode command. Use 'do {command}' in config mode."

        return True, ""
