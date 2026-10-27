#!/usr/bin/env python3
"""
schema.py — Data contracts for the validation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


class VerdictType(str, Enum):
    """Validation verdict categories."""

    OK = "ok"
    WRONG_DEVICE = "wrong_device"
    BLOCKLISTED = "blocklisted"
    WRONG_MODE = "wrong_mode"
    UNKNOWN_COMMAND = "unknown_command"


class Severity(str, Enum):
    """Verdict severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class SafetyVerdict:
    """The result of safety validation for a command."""

    valid: bool
    """True if the command passed all safety checks."""

    verdict_type: VerdictType
    """Category of the verdict (ok, wrong_device, blocklisted, wrong_mode, unknown_command)."""

    reason: str
    """Human-readable explanation of the verdict."""

    severity: Severity = Severity.INFO
    """Severity level: info, warning, or critical."""

    normalized_command: str = ""
    """The command after normalization (e.g., after do-prefix stripping)."""

    suggested_action: Optional[str] = None
    """Guidance for the user if the verdict is not 'ok' (e.g., 'switch to config mode')."""

    def summary(self) -> str:
        """One-line summary of the verdict."""
        icon = "✓" if self.valid else "✗"
        return f"{icon} [{self.verdict_type.value}] {self.reason}"


@dataclass
class ValidationContext:
    """Input context for safety validation."""

    command: str
    """The command to validate (e.g., 'show ip route', 'vlan 10', 'reload')."""

    device_platform: str
    """Target device platform (iosxe, iosxr, iosv, iosvl2)."""

    current_mode: str = "exec"
    """Current CLI mode (exec, config, config-if, enable, etc.)."""

    command_metadata: Optional[dict] = None
    """Optional metadata from knowledge base (platform, mode, syntax, etc.)."""

    def validate(self) -> None:
        """Raise ValueError if context is invalid."""
        if not self.command or not self.command.strip():
            raise ValueError("command cannot be empty")
        if not self.device_platform or not self.device_platform.strip():
            raise ValueError("device_platform cannot be empty")
        if not self.current_mode or not self.current_mode.strip():
            raise ValueError("current_mode cannot be empty")
