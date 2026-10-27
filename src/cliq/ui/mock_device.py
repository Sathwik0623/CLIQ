#!/usr/bin/env python3
"""
mock_device.py — A fake device that returns realistic output.

Uses your REAL harvested outputs from data/harvest/ntc_raw/ when available,
so the mock feels authentic. Falls back to generic responses otherwise.
"""

import os

# Resolve path to harvested raw outputs
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_RAW_DIR = os.path.join(_ROOT, "data", "harvest", "ntc_raw")
# Fallback if you kept ntc_raw at project root
_RAW_DIR_ALT = os.path.join(_ROOT, "ntc_raw")


class MockDevice:
    def __init__(self):
        self.raw_dir = _RAW_DIR if os.path.isdir(_RAW_DIR) else _RAW_DIR_ALT
        self.mode = "exec"

    def execute(self, command: str) -> str:
        """Return canned output for a command, from real harvest if available."""
        safe = command.replace(" ", "_").replace("/", "_")
        path = os.path.join(self.raw_dir, f"{safe}.txt")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        # Generic fallbacks for common commands
        if command.startswith("ping"):
            target = command.split()[-1] if len(command.split()) > 1 else "8.8.8.8"
            return (f"Sending 5, 100-byte ICMP Echos to {target}, timeout is 2 seconds:\n"
                    f"!!!!!\nSuccess rate is 100 percent (5/5), "
                    f"round-trip min/avg/max = 1/2/4 ms")
        if command in ("configure terminal", "conf t"):
            return "Enter configuration commands, one per line.  End with CNTL/Z."
        return f"[mock] Executed: {command}\n(no canned output available)"