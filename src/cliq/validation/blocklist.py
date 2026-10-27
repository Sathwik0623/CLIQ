#!/usr/bin/env python3
"""
blocklist.py — Hardcoded dangerous-command blocklist.

These commands are ALWAYS blocked, regardless of mode, platform, or context.
The list is a frozenset to prevent runtime modification.
Immune to prompt injection and user override.
"""

# Commands that are categorically dangerous and should be blocked.
# Each entry is a command PREFIX (lowercase) — matching is case-insensitive, prefix-based.
DANGEROUS_COMMAND_PREFIXES = frozenset({
    "reload",              # reboot device
    "write erase",         # erase running config
    "erase startup-config", # erase saved config
    "format",              # format disk (destructive)
    "delete",              # mass deletion (context-dependent, but risky)
    "shutdown",            # shut down interface/device
    "clear",               # various clear operations (often dangerous in production)
    "debug",               # debug commands (can impact performance)
    "configure replace",   # replace entire running config
    "no shutdown",         # mass enable (if targeted at global, can be risky)
})

# Criticality map: some dangerous commands are CRITICAL (must block).
CRITICAL_COMMANDS = frozenset({
    "reload",
    "write erase",
    "erase startup-config",
    "format",
    "configure replace",
})


def is_blocklisted(command: str) -> tuple[bool, str]:
    """
    Check if a command is on the dangerous blocklist.

    Args:
        command: The command to check (will be normalized to lowercase).

    Returns:
        A tuple of (is_blocklisted, severity_level).
        is_blocklisted: True if the command is on the blocklist.
        severity_level: "critical" for the most dangerous, "warning" for risky commands.
    """
    normalized = command.strip().lower()

    # Handle do-prefixed commands: check the remainder.
    if normalized.startswith("do "):
        normalized = normalized[3:].strip()

    # Check against the blocklist (prefix matching).
    for dangerous_prefix in DANGEROUS_COMMAND_PREFIXES:
        if normalized.startswith(dangerous_prefix):
            severity = "critical" if normalized in CRITICAL_COMMANDS else "warning"
            return True, severity

    return False, ""


def get_blocklist_reason(command: str) -> str:
    """
    Return a human-readable reason why a command is blocklisted.

    Args:
        command: The command to explain.

    Returns:
        A human-readable reason.
    """
    is_blocked, severity = is_blocklisted(command)
    if not is_blocked:
        return ""

    normalized = command.strip().lower()
    if normalized.startswith("do "):
        normalized = normalized[3:].strip()

    if normalized.startswith("reload"):
        return "reload is disabled — it would reboot the device and interrupt production traffic."
    if normalized.startswith("write erase") or normalized.startswith("erase startup-config"):
        return "erase/write commands are disabled — they would destroy the running and saved configuration."
    if normalized.startswith("format"):
        return "format is disabled — it would erase disk contents and render the device unusable."
    if normalized.startswith("configure replace"):
        return "configure replace is disabled — it would replace the entire running configuration."
    if normalized.startswith("clear"):
        return (
            "clear is disabled in this context — many clear operations can disrupt active connections. "
            "Use read-only 'show' commands instead."
        )
    if normalized.startswith("debug"):
        return "debug is disabled — debug output can impact device performance in production."
    if normalized.startswith("delete"):
        return "delete is disabled — destructive file operations must be reviewed manually."
    if normalized.startswith("shutdown"):
        return "shutdown is disabled — it would disable interfaces and disrupt connectivity."

    return f"{severity.upper()}: This command is considered risky in a production environment."
