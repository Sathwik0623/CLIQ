#!/usr/bin/env python3
"""
validate_commands.py — Run Command DB entries against a real device.

Usage:
  python tools/validate_commands.py
"""
import sys, time
sys.path.insert(0, 'src')
from cliq.connection.live_device import CMLConsoleDevice

GATEWAY = 'cml.example.com'
USER    = 'cli_test'
PASS    = 'zyq6L6nNX1xb'
PATH    = '/cli_testing/iosv-0/0'

dev = CMLConsoleDevice(GATEWAY, USER, PASS, PATH)
dev.connect()
print("Connected. Prompt:", repr(dev.current_prompt))

# --- go to enable ---
dev.execute("enable")
time.sleep(0.3)

CONFIG_CMDS = [
    ("config", "configure terminal"),
    ("config", "hostname r0"),          # restore original name
    ("config", "ip route 0.0.0.0 0.0.0.0 192.168.1.1"),
    ("config", "ip access-list extended CLIQ_TEST"),
    ("config", "no ip access-list extended CLIQ_TEST"),
    ("config", "ntp server 1.1.1.1"),
    ("config", "logging host 10.0.0.1"),
    ("config", "ip default-gateway 192.168.1.1"),
    ("config", "vtp mode server"),
    ("config", "vtp domain CLIQTEST"),
    ("config", "spanning-tree vlan 1 priority 4096"),
    ("config", "username cliq_test privilege 15 secret Cisco123"),
    ("config", "no username cliq_test"),
    ("config", "end"),
    ("enable", "show ip ospf"),
    ("enable", "show ip bgp"),
    ("enable", "show ip nat translations"),
    ("enable", "show access-lists"),
    ("enable", "show vlan brief"),
    ("enable", "copy running-config startup-config"),
]

results = []
current_mode = "enable"

for expected_mode, cmd in CONFIG_CMDS:
    try:
        out = dev.execute(cmd)
        first_line = out.strip().splitlines()[0] if out and out.strip() else ""
        is_err = first_line.startswith("% ")
        results.append((cmd, expected_mode, not is_err, first_line[:70]))
    except Exception as e:
        results.append((cmd, expected_mode, False, str(e)[:70]))
    time.sleep(0.3)

dev.disconnect()

print()
print("COMMAND DB VALIDATION RESULTS")
print("=" * 80)
for cmd, mode, ok, info in results:
    status = "OK   " if ok else "FAIL "
    print(f"  {status} [{mode:<10}] {cmd:<45} {info[:30]}")

# Report missing DB entries
print()
from cliq.db.query import CommandDB
db = CommandDB()
check_ids = [
    "banner_motd", "ntp_server", "logging_host", "ip_default_gateway",
    "vtp_mode", "vtp_domain", "spanning_tree_priority", "spanning_tree_portfast",
    "shutdown", "no_shutdown",
]
print("COMMAND DB COVERAGE:")
for cid in check_ids:
    cmd = next((c for c in db.commands if c.id == cid), None)
    print(f"  {'FOUND ' if cmd else 'MISSING'} {cid}")
