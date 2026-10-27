#!/usr/bin/env python3
"""
query.py — Command Database lookup interface for CLIQ.

Loads command_db.sqlite into memory for fast (<50ms) lookups.
This is the interface the parser/intelligence layers use.

USAGE (standalone test):
  python query.py
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("db_query")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
DEFAULT_DB = os.path.join(_PROJECT_ROOT, "command_db.sqlite")


# Bug 2 — head verbs of config commands that ENTER/SELECT a config sub-context
# (interface, router, line, vlan). In IOS these are re-selectable from within a
# child config sub-mode, so they must be surfaced in every config sub-mode's
# command pool — not just base 'config'.
_CONTEXT_ENTRY_HEADS = frozenset({"interface", "router", "line", "vlan"})


def _is_context_entry(cmd) -> bool:
    """True for a base-config command that selects a new config sub-context."""
    parts = (getattr(cmd, "full_command", "") or "").split()
    return bool(parts) and parts[0].lower() in _CONTEXT_ENTRY_HEADS


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Argument:
    name: str
    type: str
    required: bool = False
    position: Optional[int] = None


@dataclass
class Command:
    id: str
    full_command: str
    tokens: list
    abbreviations: list
    mode: str
    description: str
    risk_class: str
    is_destructive: bool
    arguments: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    related_commands: list = field(default_factory=list)
    platforms: list = field(default_factory=list)  # empty = all platforms


# ---------------------------------------------------------------------------
# The Query Engine
# ---------------------------------------------------------------------------
class CommandDB:
    """In-memory command database for fast lookups."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self.commands: list[Command] = []
        self.modes: dict = {}
        self.arg_types: dict = {}
        self._by_id: dict[str, Command] = {}
        self._by_mode: dict[str, list[Command]] = {}
        self._load()

    def _load(self):
        """Load entire DB into memory once at startup."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Load commands
        for row in c.execute("SELECT * FROM commands"):
            args = [Argument(**a) for a in json.loads(row["arguments"] or "[]")]
            cmd = Command(
                id=row["id"],
                full_command=row["full_command"],
                tokens=json.loads(row["tokens"]),
                abbreviations=json.loads(row["abbreviations"] or "[]"),
                mode=row["mode"],
                description=row["description"],
                risk_class=row["risk_class"],
                is_destructive=bool(row["is_destructive"]),
                arguments=args,
                examples=json.loads(row["examples"] or "[]"),
                related_commands=json.loads(row["related_commands"] or "[]"),
                platforms=json.loads(row["platforms"] or "[]") if "platforms" in row.keys() else [],
            )
            self.commands.append(cmd)
            self._by_id[cmd.id] = cmd
            self._by_mode.setdefault(cmd.mode, []).append(cmd)

        # Load modes + arg_types
        for row in c.execute("SELECT * FROM modes"):
            self.modes[row["name"]] = json.loads(row["data"])
        for row in c.execute("SELECT * FROM arg_types"):
            self.arg_types[row["name"]] = json.loads(row["data"])

        conn.close()
        log.info(f"Loaded {len(self.commands)} commands, "
                 f"{len(self.modes)} modes, {len(self.arg_types)} arg types.")

    # --- Lookup methods (what the parser will call) ---

    def get_by_id(self, cmd_id: str) -> Optional[Command]:
        return self._by_id.get(cmd_id)

    def get_by_full_command(self, full_command: str) -> Optional[Command]:
        for cmd in self.commands:
            if cmd.full_command == full_command:
                return cmd
        return None

    def commands_in_mode(self, mode: str) -> list[Command]:
        """All commands valid in a given CLI mode.

        Privileged EXEC ('enable') inherits every user-EXEC command, matching
        real Cisco IOS semantics — anything you can run from `Router>` is also
        valid from `Router#`.
        """
        pool = list(self._by_mode.get(mode, []))
        if mode == "enable":
            seen = {c.id for c in pool}
            for c in self._by_mode.get("exec", []):
                if c.id not in seen:
                    pool.append(c)
        # Bug 4 — config-subif inherits the interface-level command set.
        # A subinterface (config-subif) LEGITIMATELY accepts every interface
        # command a physical interface does ('ip address' / 'no ip address',
        # 'shutdown', 'description', ...). In the CLIQ mode model these commands
        # are stored once under 'config-if', so config-subif must inherit them —
        # otherwise a valid 'no ip address ...' on a subinterface is wrongly
        # flagged "Wrong mode — requires 'config-if'". config-subif is treated as
        # a child/sibling of config-if for command validation.
        if mode == "config-subif":
            seen = {c.id for c in pool}
            for c in self._by_mode.get("config-if", []):
                if c.id not in seen:
                    seen.add(c.id)
                    pool.append(c)
        # Mode-navigation commands (exit/end) are valid in every config
        # sub-mode (config-if, config-line, ...), but are stored once under the
        # base 'config' mode. Surface them in every sub-mode so prefix matching
        # and ghost text work consistently.
        if mode.startswith("config") and mode != "config":
            seen = {c.id for c in pool}
            for c in self._by_mode.get("config", []):
                if c.id in {"exit", "end"} and c.id not in seen:
                    pool.append(c)
                # Bug 2 — context-entry commands (interface / router / line /
                # vlan) can be RE-SELECTED from within a child config sub-mode:
                # IOS lets 'C8k(config-if)# interface g0/3' jump straight to
                # another interface (the parser auto-exits the current context).
                # These are stored once under base 'config', so surface them in
                # every sub-mode; otherwise a valid re-select is wrongly rejected
                # as "wrong mode — requires 'config'".
                elif c.id not in seen and _is_context_entry(c):
                    pool.append(c)
        return pool

    def find_prefix_matches(self, partial: str, mode: str = None) -> list[Command]:
        """
        Find commands whose full_command starts with the partial input.
        Optionally filter by mode. Used for suggestions/ghost text.
        """
        partial = partial.strip().lower()
        results = []
        pool = self.commands_in_mode(mode) if mode else self.commands
        for cmd in pool:
            if cmd.full_command.lower().startswith(partial):
                results.append(cmd)
        return results

    def resolve_abbreviation(self, abbrev: str, mode: str = None) -> Optional[Command]:
        """Match an abbreviation (e.g. 'sh int br') to a command."""
        abbrev = abbrev.strip().lower()
        pool = self.commands_in_mode(mode) if mode else self.commands
        for cmd in pool:
            if abbrev in [a.lower() for a in cmd.abbreviations]:
                return cmd
        return None

    def all_full_commands(self, mode: str = None) -> list[str]:
        """List of all full command strings (for typo correction candidates)."""
        pool = self.commands_in_mode(mode) if mode else self.commands
        return [cmd.full_command for cmd in pool]

    def get_arg_type(self, type_name: str) -> Optional[dict]:
        return self.arg_types.get(type_name)

    def get_mode(self, mode_name: str) -> Optional[dict]:
        return self.modes.get(mode_name)

    def destructive_commands(self) -> list[Command]:
        return [c for c in self.commands if c.is_destructive]


# ---------------------------------------------------------------------------
# Standalone test / smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    db = CommandDB()

    print("\n=== All commands ===")
    for cmd in db.commands:
        flag = " ⚠️ DESTRUCTIVE" if cmd.is_destructive else ""
        print(f"  [{cmd.mode:12}] {cmd.full_command:30} ({cmd.risk_class}){flag}")

    print("\n=== Commands in 'config-if' mode ===")
    for cmd in db.commands_in_mode("config-if"):
        print(f"  {cmd.full_command}")

    print("\n=== Prefix match: 'show ip' ===")
    for cmd in db.find_prefix_matches("show ip"):
        print(f"  {cmd.full_command}")

    print("\n=== Resolve abbreviation: 'sh int br' ===")
    result = db.resolve_abbreviation("sh int br")
    print(f"  -> {result.full_command if result else 'NOT FOUND'}")

    print("\n=== Resolve abbreviation: 'conf t' ===")
    result = db.resolve_abbreviation("conf t")
    print(f"  -> {result.full_command if result else 'NOT FOUND'}")

    print("\n=== Destructive commands ===")
    for cmd in db.destructive_commands():
        print(f"  {cmd.full_command} — {cmd.description}")

    print("\n=== 'ip address' argument details ===")
    cmd = db.get_by_id("ip_address")
    if cmd:
        for arg in cmd.arguments:
            req = "required" if arg.required else "optional"
            at = db.get_arg_type(arg.type)
            example = at.get("example", "?") if at else "?"
            print(f"  {arg.name} ({arg.type}, {req}) e.g. {example}")








