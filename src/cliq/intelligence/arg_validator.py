#!/usr/bin/env python3
"""
arg_validator.py — Value-level argument validation against arg_types.yaml.

The matcher checks "did the user provide enough tokens?" — this checks
"are the tokens themselves shaped correctly?" using the regex/range
metadata stored in the command DB.

USAGE:
  python -m cliq.intelligence.arg_validator
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ArgValidationError:
    arg_name: str
    arg_type: str
    provided: str
    reason: str
    example: str = ""


class ArgValidator:
    def __init__(self, command_db):
        self.db = command_db

    def validate(self, cmd, provided_tokens: list) -> Optional[ArgValidationError]:
        """
        Validate the values supplied for a matched command's positional arguments.

        provided_tokens = the user's full token list (command + args, expanded).
        Returns the first ArgValidationError, or None if all values are valid /
        not yet supplied.
        """
        base_len = len(cmd.tokens)
        for arg in cmd.arguments:
            pos = arg.position if arg.position is not None else base_len
            if pos >= len(provided_tokens):
                continue
            value = provided_tokens[pos]
            if value == "":
                continue
            err = self._validate_value(arg, value)
            if err:
                return err
        return None

    def _validate_value(self, arg, value: str) -> Optional[ArgValidationError]:
        spec = self.db.get_arg_type(arg.type) or {}
        example = spec.get("example", "")

        rng = spec.get("range")
        if rng and isinstance(rng, list) and len(rng) == 2:
            if not value.isdigit():
                return ArgValidationError(
                    arg.name, arg.type, value,
                    f"{arg.name} must be a number in [{rng[0]}-{rng[1]}]",
                    example,
                )
            ival = int(value)
            if ival < rng[0] or ival > rng[1]:
                return ArgValidationError(
                    arg.name, arg.type, value,
                    f"{arg.name} must be between {rng[0]} and {rng[1]}",
                    example,
                )

        regex = spec.get("regex")
        if regex:
            try:
                if not re.match(regex, value):
                    return ArgValidationError(
                        arg.name, arg.type, value,
                        self._friendly_regex_error(arg, value, spec),
                        example,
                    )
            except re.error:
                return None
        return None

    @staticmethod
    def _friendly_regex_error(arg, value: str, spec: dict) -> str:
        desc = spec.get("description", arg.type)
        example = spec.get("example", "")
        if arg.type in ("ipv4_address", "subnet_mask", "hostname_or_ip"):
            parts = value.split(".")
            if len(parts) != 4:
                return f"{arg.name} must have 4 octets (got {len(parts)})"
            for p in parts:
                if not p.isdigit() or not 0 <= int(p) <= 255:
                    return f"{arg.name} octet '{p}' must be 0-255"
            return f"{arg.name} '{value}' is not a valid {desc}"
        if arg.type == "interface_name":
            return f"{arg.name} '{value}' is not a valid interface (e.g. {example})"
        return f"{arg.name} '{value}' does not match {desc} (e.g. {example})"


if __name__ == "__main__":
    from cliq.db.query import CommandDB

    db = CommandDB()
    v = ArgValidator(db)

    cmd = db.get_by_id("ip_address")
    print("--- ip_address valid ---")
    print(v.validate(cmd, ["ip", "address", "192.168.1.1", "255.255.255.0"]))
    print("--- ip_address bad ip ---")
    print(v.validate(cmd, ["ip", "address", "1.11.1", "255.255.255.0"]))
    print("--- ping good ---")
    print(v.validate(db.get_by_id("ping"), ["ping", "8.8.8.8"]))
