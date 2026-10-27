#!/usr/bin/env python3
"""
test_validation.py — Interactive test/preview tool for the safety validation layer.

Allows testing validation logic against sample commands and device contexts.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / ".." / "src"))

from cliq.validation.schema import ValidationContext
from cliq.validation.validator import SafetyValidator


def interactive_mode(validator: SafetyValidator) -> None:
    """Run interactive validation loop."""
    print("\n=== Safety Validation Preview ===")
    print("Commands:")
    print("  <command>                - Validate in exec mode on iosxr")
    print("  --platform <p> <cmd>     - Validate on platform (iosxe/iosxr/iosv/iosvl2)")
    print("  --mode <m> <cmd>         - Validate in mode (exec/config/config-if)")
    print("  help                     - Show this help")
    print("  quit, exit               - Exit\n")

    while True:
        try:
            user_input = input("> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("help", "--help", "-h"):
                print("Validate a command: <command>")
                print("Flags: --platform <p>, --mode <m>")
                print("Example: --platform iosvl2 --mode config vlan 10\n")
                continue
            if user_input.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            # Parse flags and command
            parts = user_input.split()
            platform = "iosxr"
            mode = "exec"
            command_text = ""

            i = 0
            while i < len(parts):
                if parts[i] == "--platform" and i + 1 < len(parts):
                    platform = parts[i + 1]
                    i += 2
                elif parts[i] == "--mode" and i + 1 < len(parts):
                    mode = parts[i + 1]
                    i += 2
                else:
                    command_text = " ".join(parts[i:])
                    break

            if not command_text:
                print("No command provided.")
                continue

            # Validate
            ctx = ValidationContext(
                command=command_text,
                device_platform=platform,
                current_mode=mode,
            )
            try:
                ctx.validate()
            except ValueError as e:
                print(f"Invalid context: {e}\n")
                continue

            result = validator.validate(ctx)

            # Display result
            print()
            print(f"Command: {command_text}")
            print(f"Platform: {platform} | Mode: {mode}")
            print(f"Status: {'✓ VALID' if result.valid else '✗ BLOCKED'}")
            print(f"Verdict: {result.verdict_type.value}")
            print(f"Severity: {result.severity.value}")
            print(f"Reason: {result.reason}")
            if result.normalized_command and result.normalized_command != command_text:
                print(f"Normalized: {result.normalized_command}")
            if result.suggested_action:
                print(f"Action: {result.suggested_action}")
            print()

        except KeyboardInterrupt:
            print("\nInterrupted. Goodbye!")
            break
        except Exception as e:
            print(f"Error: {e}\n")
            continue


def batch_mode(validator: SafetyValidator, tests: list[tuple], json_output: bool = False) -> None:
    """Run validation tests from command-line arguments or predefined set."""
    if not tests:
        # Default test cases
        tests = [
            ("show ip route", "iosxr", "exec", True),
            ("vlan 10", "iosvl2", "config", True),
            ("vlan 10", "iosxr", "config", False),  # wrong device
            ("reload", "iosxr", "exec", False),  # blocklisted
            ("do show ip route", "iosxr", "config", True),
            ("show ip route", "iosxr", "config", False),  # wrong mode
            ("ospf 1 area 0", "iosxr", "config", True),
        ]

    validator_obj = SafetyValidator()
    results = []

    for test in tests:
        if len(test) == 4:
            command, platform, mode, expected_valid = test
        else:
            command, platform, mode = test
            expected_valid = None

        ctx = ValidationContext(
            command=command,
            device_platform=platform,
            current_mode=mode,
        )
        try:
            ctx.validate()
        except ValueError:
            continue

        result = validator_obj.validate(ctx)

        if json_output:
            results.append({
                "command": command,
                "platform": platform,
                "mode": mode,
                "valid": result.valid,
                "verdict_type": result.verdict_type.value,
                "reason": result.reason,
                "severity": result.severity.value,
                "normalized_command": result.normalized_command,
                "suggested_action": result.suggested_action,
                "expected_valid": expected_valid,
                "matches_expectation": result.valid == expected_valid if expected_valid is not None else None,
            })
        else:
            status = "✓" if result.valid else "✗"
            match = ""
            if expected_valid is not None:
                match_str = "✓" if result.valid == expected_valid else "✗"
                match = f" [{match_str}]"
            print(
                f"{status} {command:40} [{platform:6} {mode:10}] "
                f"{result.verdict_type.value:15}{match}"
            )

    if json_output:
        print(json.dumps(results, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test/preview the safety validation layer."
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Run interactive mode (default if no tests specified)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "tests",
        nargs="*",
        help="Test commands (format: 'command:platform:mode' or just 'command')",
    )

    args = parser.parse_args()

    validator = SafetyValidator()

    # Parse test cases if provided
    parsed_tests = []
    if args.tests:
        for test_spec in args.tests:
            parts = test_spec.split(":")
            if len(parts) == 1:
                parsed_tests.append((parts[0], "iosxr", "exec"))
            elif len(parts) == 2:
                parsed_tests.append((parts[0], parts[1], "exec"))
            elif len(parts) == 3:
                parsed_tests.append((parts[0], parts[1], parts[2]))

    # Run batch or interactive
    if parsed_tests or args.json:
        batch_mode(validator, parsed_tests, json_output=args.json)
    else:
        interactive_mode(validator)


if __name__ == "__main__":
    main()
