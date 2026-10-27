#!/usr/bin/env python3
"""
ntc_harvest.py — Harvest CLEAN, structured command output from CML devices
using Netmiko + ntc-templates (TextFSM). Windows-native, no pyATS required.

Runs a curated list of KNOWN commands and returns structured data via TextFSM.
Falls back to raw text for commands without a template.

Output:
  ntc_harvest.json   — structured parsed data per command
  ntc_raw/           — raw text output per command (fallback / unparsed)

USAGE:

"""

import argparse
import getpass
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from netmiko import ConnectHandler
from ntc_templates.parse import parse_output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ntc_harvest")

# ---------------------------------------------------------------------------
# Curated command list. Start SMALL with high-value 'show' commands that
# ntc-templates has parsers for. Expand once verified.
# ---------------------------------------------------------------------------
COMMANDS_TO_HARVEST = [
    # --- System / Platform (will parse well) ---
    "show version",
    "show inventory",
    "show processes cpu",
    "show processes memory",
    "show memory statistics",
    "show clock",
    "show users",
    "show privilege",
    "show running-config",
    "show startup-config",
    "show boot",
    "show file systems",
    "show env all",

    # --- Interfaces (will parse well) ---
    "show ip interface brief",
    "show interfaces",
    "show interfaces status",
    "show interfaces description",
    "show ipv6 interface brief",
    "show interfaces switchport",
    "show interfaces trunk",
    "show controllers",

    # --- IP / Routing ---
    "show ip route",
    "show ip route summary",
    "show ipv6 route",
    "show ip protocols",
    "show ip arp",
    "show arp",
    "show ip cef",
    "show ip ospf",
    "show ip ospf neighbor",
    "show ip ospf interface brief",
    "show ip eigrp neighbors",
    "show ip eigrp topology",
    "show ip bgp",
    "show ip bgp summary",
    "show ip bgp neighbors",

    # --- L2 / Switching (may be empty on router) ---
    "show vlan",
    "show vlan brief",
    "show spanning-tree",
    "show spanning-tree summary",
    "show mac address-table",
    "show etherchannel summary",

    # --- Discovery / Neighbors ---
    "show cdp neighbors",
    "show cdp neighbors detail",
    "show lldp neighbors",
    "show lldp neighbors detail",

    # --- Services / Misc ---
    "show ntp status",
    "show ntp associations",
    "show snmp",
    "show logging",
    "show access-lists",
    "show ip access-lists",
    "show ip nat translations",
    "show ip dhcp binding",
    "show standby brief",
    "show vrrp brief",
    "show ip ssh",
    "show crypto isakmp sa",
]


# ---------------------------------------------------------------------------
# CML Connection — reuses your hardened, WORKING gateway approach
# ---------------------------------------------------------------------------
class CMLConnection:
    def __init__(self, host, username, password, device_path, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.device_path = device_path
        self.port = port
        self.conn = None

    def connect(self):
        log.info(f"Connecting to gateway {self.host}...")
        self.conn = ConnectHandler(
            device_type="terminal_server",
            host=self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            fast_cli=False,
        )
        # Disable transport keepalives -> prevents ^@ NULL-byte leakage
        try:
            self.conn.remote_conn.transport.set_keepalive(0)
        except Exception:
            log.warning("Could not disable keepalive (non-fatal).")

        # Navigate the CML gateway to the device console
        log.info(f"Opening device console: {self.device_path}")
        self.conn.write_channel(f"open {self.device_path}\n")
        time.sleep(3)
        self.conn.write_channel("\r\n")  # wake the console
        time.sleep(1.5)
        self._read()  # flush

        # Enter enable + disable paging
        self.conn.write_channel("enable\n")
        time.sleep(1)
        self.conn.write_channel("\n")  # in case of password prompt
        time.sleep(0.5)
        self.conn.write_channel("terminal length 0\n")
        time.sleep(0.5)
        self._read()  # flush

        # CRITICAL: tell Netmiko what the device prompt looks like now that
        # we're past the gateway, so send_command/TextFSM works correctly.
        try:
            self.conn.set_base_prompt()
            log.info(f"Base prompt detected: {self.conn.base_prompt!r}")
        except Exception as e:
            log.warning(f"Could not auto-detect base prompt: {e}")

        log.info("Console ready.")

    def _read(self):
        time.sleep(0.2)
        raw = self.conn.read_channel()
        return raw.replace("\x00", "").replace("^@", "")

    def run_structured(self, command):
        """Run command, return (parsed_or_None, raw_text)."""
        # Raw text (always reliable)
        raw = self.conn.send_command(
            command,
            read_timeout=60,            # bumped up (show processes cpu is slow)
            strip_prompt=True,
            strip_command=True,
            expect_string=r"R1#",       # explicit prompt fixes the timeout
        )
        raw = raw.replace("\x00", "").replace("^@", "")

        # Structured via TextFSM — FORCE cisco_ios platform
        parsed = None
        try:
            from ntc_templates.parse import parse_output
            structured = parse_output(
                platform="cisco_ios",       # <-- the critical fix
                command=command,
                data=raw,
            )
            if isinstance(structured, list) and len(structured) > 0:
                parsed = structured
        except Exception as e:
            log.debug(f"TextFSM parse failed for {command!r}: {e}")

        return parsed, raw

    def disconnect(self):
        if self.conn:
            try:
                self.conn.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Harvest loop
# ---------------------------------------------------------------------------
def harvest(conn, commands, raw_dir):
    results = {}
    os.makedirs(raw_dir, exist_ok=True)

    for cmd in commands:
        log.info(f"Harvesting: {cmd!r}")
        entry = {"command": cmd, "parsed": None,
                 "parse_status": None, "raw_saved_to": None}
        try:
            parsed, raw = conn.run_structured(cmd)

            # Save raw text
            safe = cmd.replace(" ", "_").replace("/", "_")
            raw_path = os.path.join(raw_dir, f"{safe}.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw)
            entry["raw_saved_to"] = raw_path

            if parsed is not None:
                entry["parsed"] = parsed
                entry["parse_status"] = "parsed_ok"
                log.info(f"  -> parsed OK ({len(parsed)} rows)")
            else:
                entry["parse_status"] = "no_template (raw saved)"
                log.info("  -> no TextFSM template (raw text saved)")

        except Exception as e:
            entry["parse_status"] = f"error: {type(e).__name__}: {e}"
            log.warning(f"  -> error: {e}")

        results[cmd] = entry
        time.sleep(0.3)  # gentle on the console

    return results


def main():
    parser = argparse.ArgumentParser(description="Netmiko + ntc-templates harvester")
    parser.add_argument("--host", required=True, help="CML gateway hostname")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None,
                        help="Omit to be prompted securely (RECOMMENDED).")
    parser.add_argument("--device-path", required=True,
                        help="CML console path, e.g. /cli_test1/iosv-0/0")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--output", default="ntc_harvest.json")
    parser.add_argument("--raw-dir", default="ntc_raw")
    args = parser.parse_args()

    if not args.password:
        args.password = getpass.getpass("Device password: ")
    if not args.password:
        log.error("No password provided. Aborting.")
        sys.exit(1)

    conn = CMLConnection(args.host, args.username, args.password,
                         args.device_path, args.port)
    try:
        conn.connect()
        results = harvest(conn, COMMANDS_TO_HARVEST, args.raw_dir)

        out = {
            "metadata": {
                "harvested_at": datetime.now(timezone.utc).isoformat(),
                "method": "netmiko_ntc_templates",
                "host": args.host,
                "device_path": args.device_path,
                "command_count": len(COMMANDS_TO_HARVEST),
            },
            "results": results,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)

        parsed_ok = sum(1 for r in results.values()
                        if r["parse_status"] == "parsed_ok")
        log.info(f"Done. {parsed_ok}/{len(results)} commands parsed cleanly.")
        log.info(f"Structured -> {args.output}  |  Raw -> {args.raw_dir}/")

    except Exception as e:
        log.error(f"Harvest failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        conn.disconnect()


if __name__ == "__main__":
    main()