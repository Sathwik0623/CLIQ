#!/usr/bin/env python3
"""
build_db.py — Compile the curated YAML Command Database into a queryable SQLite DB.

Reads all YAML files under command_db/, validates them, and produces command_db.sqlite.

USAGE:
  python build_db.py
"""

import glob
import json
import logging
import os
import sqlite3
import sys

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_db")

DB_DIR = "command_db"
OUTPUT_DB = "command_db.sqlite"

REQUIRED_FIELDS = {"id", "full_command", "tokens", "mode", "description", "risk_class"}
VALID_RISK = {"informational", "configuration", "disruptive", "catastrophic"}


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_command(cmd, source_file):
    """Validate a single command entry. Returns list of errors."""
    errors = []
    missing = REQUIRED_FIELDS - set(cmd.keys())
    if missing:
        errors.append(f"{source_file}: '{cmd.get('id', '?')}' missing fields: {missing}")
    if cmd.get("risk_class") not in VALID_RISK:
        errors.append(f"{source_file}: '{cmd.get('id')}' invalid risk_class: {cmd.get('risk_class')}")
    if not isinstance(cmd.get("tokens", []), list):
        errors.append(f"{source_file}: '{cmd.get('id')}' tokens must be a list")
    return errors


def build():
    if not os.path.isdir(DB_DIR):
        log.error(f"Directory '{DB_DIR}/' not found. Create it and add YAML files.")
        sys.exit(1)

    # 1) Load modes + arg_types (reference data)
    modes = load_yaml(os.path.join(DB_DIR, "modes.yaml")).get("modes", {})
    arg_types = load_yaml(os.path.join(DB_DIR, "arg_types.yaml")).get("arg_types", {})
    log.info(f"Loaded {len(modes)} modes, {len(arg_types)} arg types.")

    # 2) Load all command YAML files (exec/ and config/)
    all_commands = []
    all_errors = []
    for path in glob.glob(os.path.join(DB_DIR, "**", "*.yaml"), recursive=True):
        base = os.path.basename(path)
        if base in ("modes.yaml", "arg_types.yaml", "schema_version.yaml"):
            continue
        data = load_yaml(path)
        # Skip empty files or files with no 'commands' key
        if not data or not isinstance(data, dict):
            log.warning(f"Skipping empty/invalid YAML: {base}")
            continue
        commands = data.get("commands")
        if not commands:
            log.warning(f"No 'commands' in {base} (skipping)")
            continue
        for cmd in commands:
            errs = validate_command(cmd, base)
            all_errors.extend(errs)
            if not errs:
                cmd["_source"] = base
                all_commands.append(cmd)

    if all_errors:
        log.error("Validation errors found:")
        for e in all_errors:
            log.error(f"  {e}")
        sys.exit(1)

    # Check for duplicate IDs
    ids = [c["id"] for c in all_commands]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        log.error(f"Duplicate command IDs: {dupes}")
        sys.exit(1)

    log.info(f"Loaded {len(all_commands)} valid commands.")

    # 3) Build SQLite
    if os.path.exists(OUTPUT_DB):
        os.remove(OUTPUT_DB)
    conn = sqlite3.connect(OUTPUT_DB)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE commands (
            id TEXT PRIMARY KEY,
            full_command TEXT NOT NULL,
            tokens TEXT NOT NULL,
            abbreviations TEXT,
            mode TEXT NOT NULL,
            description TEXT,
            risk_class TEXT NOT NULL,
            is_destructive INTEGER DEFAULT 0,
            arguments TEXT,
            examples TEXT,
            related_commands TEXT,
            platforms TEXT,
            source_file TEXT
        )
    """)
    c.execute("CREATE INDEX idx_mode ON commands(mode)")
    c.execute("CREATE INDEX idx_full_command ON commands(full_command)")

    c.execute("CREATE TABLE modes (name TEXT PRIMARY KEY, data TEXT)")
    for name, data in modes.items():
        c.execute("INSERT INTO modes VALUES (?, ?)", (name, json.dumps(data)))

    c.execute("CREATE TABLE arg_types (name TEXT PRIMARY KEY, data TEXT)")
    for name, data in arg_types.items():
        c.execute("INSERT INTO arg_types VALUES (?, ?)", (name, json.dumps(data)))

    for cmd in all_commands:
        c.execute("""
            INSERT INTO commands
            (id, full_command, tokens, abbreviations, mode, description,
             risk_class, is_destructive, arguments, examples, related_commands, platforms, source_file)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cmd["id"],
            cmd["full_command"],
            json.dumps(cmd.get("tokens", [])),
            json.dumps(cmd.get("abbreviations", [])),
            cmd["mode"],
            cmd.get("description", ""),
            cmd["risk_class"],
            1 if cmd.get("is_destructive") else 0,
            json.dumps(cmd.get("arguments", [])),
            json.dumps(cmd.get("examples", [])),
            json.dumps(cmd.get("related_commands", [])),
            json.dumps(cmd.get("platforms", [])),
            cmd["_source"],
        ))

    conn.commit()
    conn.close()

    # Summary
    log.info(f"✅ Built {OUTPUT_DB}")
    log.info(f"   Commands: {len(all_commands)}")
    by_mode = {}
    for cmd in all_commands:
        by_mode[cmd["mode"]] = by_mode.get(cmd["mode"], 0) + 1
    for mode, count in sorted(by_mode.items()):
        log.info(f"   {mode}: {count}")
    destructive = sum(1 for c in all_commands if c.get("is_destructive"))
    log.info(f"   Destructive commands: {destructive}")


if __name__ == "__main__":
    build()