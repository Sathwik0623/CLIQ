#!/usr/bin/env python3
"""
output_cleaner.py — Strip IOS terminal cruft from raw device output.

Netmiko returns the raw byte stream from the SSH channel which includes:
  - Echo of the command itself (first line)
  - The device prompt at the end of the output
  - ANSI escape codes from terminals that have `terminal length 0` not set
  - Cisco MORE prompts if pagination wasn't disabled
  - Trailing blank lines

This module cleans all of that so the UI receives readable text.
"""

from __future__ import annotations

import re

# ANSI escape sequences (colour, cursor movement, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Cisco MORE prompt
_MORE_RE = re.compile(r"--More--|---\(MORE\)---", re.IGNORECASE)

# IOS / IOS-XE prompt patterns  (Router>, Router#, Router(config)#, …)
_PROMPT_RE = re.compile(
    r"^[\w\-]+(\([^\)]+\))?[#>]\s*$",
    re.MULTILINE,
)


def clean(raw: str, command: str | None = None) -> str:
    """
    Clean raw device output.

    Parameters
    ----------
    raw:
        The full string returned by the SSH channel / netmiko send_command().
    command:
        The command that was sent (used to strip the echo if present).

    Returns
    -------
    Cleaned string, ready for display.
    """
    if not raw:
        return ""

    # 1. Strip ANSI escape sequences
    text = _ANSI_RE.sub("", raw)

    # 2. Normalise line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.splitlines()

    # 3. Strip command echo (netmiko usually does this already, but just in case)
    if command and lines and lines[0].strip().lower() == command.strip().lower():
        lines = lines[1:]

    # 4. Strip trailing prompt lines
    while lines and _PROMPT_RE.match(lines[-1].strip()):
        lines.pop()

    # 5. Strip MORE prompts embedded mid-output (incomplete pagination)
    lines = [l for l in lines if not _MORE_RE.search(l)]

    # 6. Strip leading / trailing blank lines, keep internal blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)
