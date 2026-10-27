"""
safety_gates.py — CLIQ Safety Gate Checks

Evaluates commands BEFORE execution to decide whether they should be:
  - ALLOWED  : execute normally
  - WARN     : show a confirmation prompt first
  - BLOCK    : refuse outright and log the reason

Used by app.py _execute_ok() to wrap all live-device commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GateResult(str, Enum):
    ALLOW = "allow"
    WARN  = "warn"
    BLOCK = "block"


@dataclass
class GateDecision:
    result: GateResult
    reason: str = ""
    suggestion: str = ""


# Commands that wipe ALL config — never run without explicit confirmation
_BLOCK_COMMANDS = {
    "write erase",
    "erase startup-config",
    "erase nvram",
    "reload",
    "format",
    "delete nvram",
    "delete flash",
}

# Commands that modify config destructively — require a yes/no prompt
_WARN_COMMANDS = {
    "shutdown",
    "no ip address",
    "no interface",
    "no router",
    "no access-list",
    "clear ip route",
    "clear arp",
    "clear counters",
    "debug all",
    "undebug all",
    "terminal monitor",
}


def evaluate(command: str, mode: str) -> GateDecision:
    """
    Evaluate a command against safety gates.

    Returns a GateDecision indicating ALLOW / WARN / BLOCK.
    Called before every live-device execution.
    """
    cmd_lower = command.strip().lower()

    # Check absolute blocks first
    for blocked in _BLOCK_COMMANDS:
        if cmd_lower == blocked or cmd_lower.startswith(blocked + " "):
            return GateDecision(
                result=GateResult.BLOCK,
                reason=f"'{command}' can cause unrecoverable data loss and is blocked in CLIQ.",
                suggestion="Use the device's native console for irreversible operations.",
            )

    # Check warn-level commands
    for warned in _WARN_COMMANDS:
        if cmd_lower == warned or cmd_lower.startswith(warned + " "):
            return GateDecision(
                result=GateResult.WARN,
                reason=f"'{command}' is a potentially disruptive operation.",
            )

    # debug <something> (but not 'no debug') always gets a warning
    if cmd_lower.startswith("debug ") and not cmd_lower.startswith("no debug"):
        return GateDecision(
            result=GateResult.WARN,
            reason="Enabling debug can overwhelm the console. Confirm before proceeding.",
            suggestion="Use 'undebug all' to stop all debugs.",
        )

    return GateDecision(result=GateResult.ALLOW)
