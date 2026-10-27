#!/usr/bin/env python3
"""
cliq.validation — Phase 3 Safety & Validation Layer.

Before any command reaches a device, this layer validates:
  1. Platform capability (e.g., vlan on a switch, ospf on a router)
  2. Dangerous-command blocklist (reload, write erase, format, etc.)
  3. CLI mode correctness (exec, config, config-if) and do-prefix handling
  4. Unknown-command detection

All validation is local, offline, and non-blocking on the device.
The verdict is structured, human-readable, and safety-focused.
"""

from cliq.validation.blocklist import (
    CRITICAL_COMMANDS,
    DANGEROUS_COMMAND_PREFIXES,
    get_blocklist_reason,
    is_blocklisted,
)
from cliq.validation.platform_rules import (
    ROUTER_ONLY_COMMAND_PREFIXES,
    ROUTER_PREFERRED_PREFIXES,
    SWITCH_ONLY_COMMAND_PREFIXES,
    VALID_PLATFORMS,
    command_capability,
    command_suits_platform,
    get_device_type,
    is_valid_for_platform,
    platform_capabilities,
)
from cliq.validation.schema import SafetyVerdict, Severity, ValidationContext, VerdictType
from cliq.validation.validator import SafetyValidator

__all__ = [
    # Schema
    "SafetyVerdict",
    "Severity",
    "ValidationContext",
    "VerdictType",
    # Validator
    "SafetyValidator",
    # Blocklist
    "CRITICAL_COMMANDS",
    "DANGEROUS_COMMAND_PREFIXES",
    "get_blocklist_reason",
    "is_blocklisted",
    # Platform rules (capability classification — advisory only)
    "ROUTER_ONLY_COMMAND_PREFIXES",
    "ROUTER_PREFERRED_PREFIXES",
    "SWITCH_ONLY_COMMAND_PREFIXES",
    "VALID_PLATFORMS",
    "command_capability",
    "command_suits_platform",
    "get_device_type",
    "is_valid_for_platform",
    "platform_capabilities",
]
