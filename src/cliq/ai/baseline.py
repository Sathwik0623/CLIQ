"""
CLIQ M1.4 — Post-configuration healthy-baseline collection.

Collects fresh read-only evidence from every affected device through the M1.2
execution service, runs the planned connectivity checks, and renders an
honest verdict: HEALTHY only when every required check passes, DEGRADED when
evidence is complete but a state expectation mismatches, FAILED when evidence
is missing or connectivity fails. Telemetry is never fabricated — every check
cites the actual command output it was derived from.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cliq.ai.config_plan import get_plan, normalize_interface
from cliq.ai.device_exec import (
    PLATFORM_CISCO_IOS,
    PLATFORM_LINUX,
    classify_platform,
    execute_device_command,
)

log = logging.getLogger("cliq.ai.baseline")

BASELINE_IOS_COMMANDS = [
    "show vlan brief",
    "show interfaces trunk",
    "show interfaces status",
    "show mac address-table",
]
BASELINE_LINUX_COMMANDS = ["ifconfig -a", "route -n", "arp -n"]
DEFAULT_STP_VLANS = [10, 20]
MAX_STORED_CHARS = 32768

_lock = threading.Lock()
_baselines: Dict[str, Dict[str, Any]] = {}
_baseline_owners: Dict[str, str] = {}


def get_baseline(baseline_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        rec = _baselines.get(baseline_id)
        return dict(rec) if rec else None


def get_baseline_owner(baseline_id: str) -> Optional[str]:
    """Owning session token for a baseline (None when unknown)."""
    with _lock:
        return _baseline_owners.get(baseline_id)


@dataclass
class BaselineCheck:
    check_id: str = ""
    device_id: str = ""
    device_label: str = ""
    description: str = ""
    status: str = ""
    detail: str = ""
    source_command: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Output parsers (pure functions) ──────────────────────────────────────────

def parse_ios_ping(output: str) -> Tuple[bool, str]:
    """Parse IOS ping statistics. Pass requires an actual success rate >= 80%."""
    m = re.search(r"Success rate is (\d+)\s*percent", output or "", re.IGNORECASE)
    if not m:
        return False, "no ping statistics found in output"
    rate = int(m.group(1))
    bangs = (output or "").count("!")
    if rate >= 80 and bangs > 0:
        return True, f"success rate {rate}% ({bangs} replies)"
    return False, f"success rate {rate}% ({bangs} replies)"


def parse_linux_ping(output: str) -> Tuple[bool, str]:
    """Parse BusyBox ping summary. Pass requires zero packet loss."""
    text = output or ""
    m = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?received",
                  text, re.IGNORECASE)
    if not m:
        if re.search(r"command not found|unknown host|network is unreachable|bad address",
                     text, re.IGNORECASE):
            return False, "ping failed: destination unreachable or command error"
        return False, "no ping summary found in output"
    sent, received = int(m.group(1)), int(m.group(2))
    if sent > 0 and received == sent:
        return True, f"{received}/{sent} packets received"
    return False, f"{received}/{sent} packets received"


def vlan_present(output: str, vlan_id: int) -> bool:
    return re.search(rf"(?m)^\s*{vlan_id}\s+\S+", output or "") is not None


def trunk_operational(output: str, interface: str) -> bool:
    key = normalize_interface(interface)
    for line in (output or "").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        lowered = [t.lower() for t in tokens]
        if normalize_interface(tokens[0]) == key and "trunking" in lowered:
            return True
    return False


def access_assigned(output: str, interface: str, vlan: int) -> Tuple[bool, str]:
    key = normalize_interface(interface)
    for line in (output or "").splitlines():
        tokens = line.split()
        if not tokens or normalize_interface(tokens[0]) != key:
            continue
        if str(vlan) in tokens:
            return True, line.strip()[:160]
        return False, f"{interface} row does not show VLAN {vlan}: {line.strip()[:160]}"
    return False, f"{interface} not found in interface status output"


def stp_healthy(output: str, vlan_id: int) -> Tuple[bool, str]:
    text = output or ""
    if not re.search(rf"VLAN0*{vlan_id}\b", text, re.IGNORECASE):
        return False, f"no spanning-tree instance found for VLAN {vlan_id}"
    if "executing" not in text.lower():
        return False, f"spanning-tree not executing for VLAN {vlan_id}"
    if re.search(r"forwarding|FWD", text):
        return True, f"VLAN {vlan_id} spanning-tree running with forwarding ports"
    return False, f"VLAN {vlan_id} spanning-tree has no forwarding ports"


def address_present(output: str, address: str) -> bool:
    return re.search(rf"\b{re.escape(address)}\b", output or "") is not None


# ── Collection ───────────────────────────────────────────────────────────────

def _expected_state(plan: Optional[Any]) -> Dict[str, Any]:
    """Derive verifiable expectations from plan steps (VLANs, ports, addresses)."""
    expected: Dict[str, Any] = {"vlans": {}, "access": [], "trunks": [],
                                "addresses": [], "gateways": []}
    if plan is None:
        return expected
    for dp in plan.devices:
        for step in dp.steps:
            cmd = step.command
            m = re.match(r"vlan\s+(\d+)$", cmd.strip(), re.IGNORECASE)
            if m:
                expected["vlans"].setdefault(dp.device_id, set()).add(int(m.group(1)))
            m = re.match(r"switchport access vlan\s+(\d+)$", cmd.strip(), re.IGNORECASE)
            if m and step.target.get("interface"):
                expected["access"].append((dp.device_id, step.target["interface"], int(m.group(1))))
            m = re.match(r"switchport trunk allowed vlan\s+(.+)$", cmd.strip(), re.IGNORECASE)
            if m and step.target.get("interface"):
                vids = [int(n) for n in re.findall(r"\d+", m.group(1))]
                expected["trunks"].append((dp.device_id, step.target["interface"], vids))
            m = re.match(r"ifconfig\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)", cmd.strip(), re.IGNORECASE)
            if m:
                expected["addresses"].append((dp.device_id, m.group(1), m.group(2)))
            m = re.match(r"route add default gw\s+(\d+\.\d+\.\d+\.\d+)", cmd.strip(), re.IGNORECASE)
            if m:
                expected["gateways"].append((dp.device_id, m.group(1)))
    expected["vlans"] = {d: sorted(v) for d, v in expected["vlans"].items()}
    return expected


async def collect_baseline(session_token: str, lab_id: str,
                           device_ids: List[str],
                           plan_id: str = "",
                           stp_vlans: Optional[List[int]] = None,
                           ) -> Tuple[Dict[str, Any], str]:
    """Collect evidence + connectivity and render the health verdict."""
    from cliq.web import server
    if not session_token or session_token not in server.SESSIONS:
        return {}, "unauthorized"
    inventory = server.DEVICES_BY_TOKEN.get(session_token) or {}
    owned = [d for d in device_ids if d in inventory
             and (inventory[d].get("lab_id") or "") == lab_id]
    if not owned:
        return {}, "no_devices"
    plan = get_plan(plan_id) if plan_id else None
    expected = _expected_state(plan)
    vlan_union = sorted({v for vs in expected["vlans"].values() for v in vs})
    stp_list = list(stp_vlans) if stp_vlans else (vlan_union or list(DEFAULT_STP_VLANS))

    devices_out: Dict[str, Any] = {}
    errors: List[str] = []
    for dev_id in owned:
        meta = inventory[dev_id]
        label = str(meta.get("label") or dev_id)
        platform = classify_platform(str(meta.get("type") or ""),
                                     str(meta.get("ssh_device_type") or ""))
        if platform == PLATFORM_CISCO_IOS:
            commands = list(BASELINE_IOS_COMMANDS) + [f"show spanning-tree vlan {v}" for v in stp_list]
        elif platform == PLATFORM_LINUX:
            commands = list(BASELINE_LINUX_COMMANDS)
        else:
            errors.append(f"{label}: unsupported platform for baseline")
            continue
        outputs: Dict[str, Any] = {}
        for cmd in commands:
            res = await execute_device_command(session_token, lab_id, dev_id, cmd)
            outputs[cmd] = {
                "stdout": (res.stdout or "")[:MAX_STORED_CHARS],
                "truncated": len(res.stdout or "") > MAX_STORED_CHARS,
                "exit_status": res.exit_status,
                "error": res.error,
                "error_code": res.error_code,
                "latency_ms": round(res.latency_ms, 2),
                "success": res.success,
            }
            if not res.success:
                errors.append(f"{label} '{cmd}': {res.error or res.error_code}")
        devices_out[dev_id] = {"device_id": dev_id, "device_label": label,
                               "platform": platform, "outputs": outputs}

    connectivity = list(plan.connectivity) if plan else []
    ping_results: List[Dict[str, Any]] = []
    for exp in connectivity:
        if exp.src_device not in owned or not exp.dst_ip:
            ping_results.append({"exp_id": exp.exp_id, "src_device": exp.src_device,
                                 "dst": exp.dst, "dst_ip": exp.dst_ip,
                                 "status": "failed",
                                 "detail": "missing source device or destination address evidence"})
            continue
        src_meta = inventory[exp.src_device]
        src_platform = classify_platform(str(src_meta.get("type") or ""),
                                         str(src_meta.get("ssh_device_type") or ""))
        ping_cmd = (f"ping -c 4 {exp.dst_ip}" if src_platform == PLATFORM_LINUX
                    else f"ping {exp.dst_ip}")
        res = await execute_device_command(session_token, lab_id, exp.src_device, ping_cmd)
        if not res.success:
            ping_results.append({"exp_id": exp.exp_id, "src_device": exp.src_device,
                                 "dst": exp.dst, "dst_ip": exp.dst_ip,
                                 "status": "failed",
                                 "detail": f"ping command failed: {res.error or res.error_code}",
                                 "stdout": (res.stdout or "")[:4000]})
            continue
        ok, detail = (parse_linux_ping(res.stdout) if src_platform == PLATFORM_LINUX
                      else parse_ios_ping(res.stdout))
        ping_results.append({"exp_id": exp.exp_id, "src_device": exp.src_device,
                             "dst": exp.dst, "dst_ip": exp.dst_ip,
                             "status": "passed" if ok else "failed", "detail": detail,
                             "stdout": (res.stdout or "")[:4000]})

    checks = _evaluate_checks(owned, inventory, devices_out, expected, ping_results)
    verdict, explanation = _render_verdict(checks, ping_results, errors)
    record = {
        "baseline_id": f"base-{uuid.uuid4().hex[:10]}",
        "plan_id": plan_id,
        "lab_id": lab_id,
        "collected_at": time.time(),
        "devices": devices_out,
        "checks": [c.to_dict() for c in checks],
        "connectivity": ping_results,
        "verdict": verdict,
        "explanation": explanation,
        "audit": {
            "log": str(Path.home() / ".cliq" / "copilot_exec_audit.jsonl"),
            "session_prefix": (session_token or "")[:8],
            "commands_run": sum(len(d["outputs"]) for d in devices_out.values()) + len(
                [p for p in ping_results if p.get("stdout")]),
        },
    }
    with _lock:
        _baselines[record["baseline_id"]] = record
        _baseline_owners[record["baseline_id"]] = session_token
    return dict(record), ""


def _out(devices_out: Dict[str, Any], dev_id: str, cmd: str) -> str:
    return ((devices_out.get(dev_id) or {}).get("outputs") or {}).get(cmd, {}).get("stdout", "")


def _ok(devices_out: Dict[str, Any], dev_id: str, cmd: str) -> bool:
    return bool(((devices_out.get(dev_id) or {}).get("outputs") or {}).get(cmd, {}).get("success"))


def _label(inventory: Dict[str, Any], dev_id: str) -> str:
    return str((inventory.get(dev_id) or {}).get("label") or dev_id)


def _evaluate_checks(owned: List[str], inventory: Dict[str, Any],
                     devices_out: Dict[str, Any], expected: Dict[str, Any],
                     ping_results: List[Dict[str, Any]]) -> List[BaselineCheck]:
    checks: List[BaselineCheck] = []
    seq = 0

    def add(dev_id: str, desc: str, ok: bool, detail: str, cmd: str = "") -> None:
        nonlocal seq
        seq += 1
        checks.append(BaselineCheck(
            check_id=f"chk-{seq}", device_id=dev_id,
            device_label=_label(inventory, dev_id), description=desc,
            status="passed" if ok else "failed", detail=detail[:500],
            source_command=cmd))

    for dev_id in owned:
        for cmd, outs in (devices_out.get(dev_id, {}).get("outputs") or {}).items():
            if cmd.startswith("ping "):
                continue
            add(dev_id, f"evidence collected: {cmd}", bool(outs.get("success")),
                outs.get("error") or f"{len((outs.get('stdout') or '').splitlines())} lines", cmd)
    for dev_id, vlans in (expected.get("vlans") or {}).items():
        out = _out(devices_out, dev_id, "show vlan brief")
        for vid in vlans:
            add(dev_id, f"VLAN {vid} present", vlan_present(out, vid),
                f"VLAN {vid} {'found' if vlan_present(out, vid) else 'missing'} in vlan brief",
                "show vlan brief")
    for dev_id, intf, vid in expected.get("access", []):
        out = _out(devices_out, dev_id, "show interfaces status")
        ok, detail = access_assigned(out, intf, vid)
        add(dev_id, f"{intf} access VLAN {vid}", ok, detail, "show interfaces status")
    for dev_id, intf, _vids in expected.get("trunks", []):
        out = _out(devices_out, dev_id, "show interfaces trunk")
        ok = trunk_operational(out, intf)
        add(dev_id, f"{intf} trunking", ok,
            f"{intf} {'is trunking' if ok else 'is not trunking'}", "show interfaces trunk")
    stp_vlans = sorted({v for vs in (expected.get("vlans") or {}).values() for v in vs} or [10, 20])
    for dev_id in owned:
        if not any(c.startswith("show spanning-tree") for c in
                   (devices_out.get(dev_id, {}).get("outputs") or {})):
            continue
        for vid in stp_vlans:
            cmd = f"show spanning-tree vlan {vid}"
            if not _ok(devices_out, dev_id, cmd):
                continue
            ok, detail = stp_healthy(_out(devices_out, dev_id, cmd), vid)
            add(dev_id, f"spanning-tree VLAN {vid} healthy", ok, detail, cmd)
    for dev_id, iface, addr in expected.get("addresses", []):
        out = _out(devices_out, dev_id, "ifconfig -a")
        ok = address_present(out, addr)
        add(dev_id, f"{iface} has {addr}", ok,
            f"{addr} {'present' if ok else 'missing'} in ifconfig output", "ifconfig -a")
    for dev_id, gw in expected.get("gateways", []):
        out = _out(devices_out, dev_id, "route -n")
        ok = address_present(out, gw)
        add(dev_id, f"default gateway {gw} in routing table", ok,
            f"{gw} {'present' if ok else 'missing'} in route output", "route -n")
    for pr in ping_results:
        add(pr["src_device"], f"ping {pr['dst']} ({pr['dst_ip'] or 'no address'})",
            pr["status"] == "passed", pr["detail"], "ping")
    return checks


def _render_verdict(checks: List[BaselineCheck], ping_results: List[Dict[str, Any]],
                    errors: List[str]) -> Tuple[str, str]:
    failed = [c for c in checks if c.status != "passed"]
    if not checks:
        return "FAILED", "No baseline checks could be evaluated (no evidence collected)."
    evidence_failed = [c for c in failed if c.description.startswith("evidence collected")]
    ping_failed = [p for p in ping_results if p["status"] != "passed"]
    vlan_failed = [c for c in failed if "VLAN" in c.description and "present" in c.description]
    if evidence_failed or ping_failed or vlan_failed:
        reasons = []
        if evidence_failed:
            reasons.append(f"{len(evidence_failed)} evidence collection(s) failed: " +
                           "; ".join(f"{c.device_label} {c.source_command}" for c in evidence_failed[:4]))
        if vlan_failed:
            reasons.append("expected VLAN(s) missing: " +
                           "; ".join(f"{c.device_label} {c.description}" for c in vlan_failed[:4]))
        if ping_failed:
            reasons.append("connectivity failed: " +
                           "; ".join(f"{p['src_device']}->{p['dst']}: {p['detail']}" for p in ping_failed[:4]))
        return "FAILED", "Baseline FAILED. " + " ".join(reasons)
    if failed:
        return ("DEGRADED",
                "Baseline DEGRADED: evidence complete and connectivity passed, but " +
                "; ".join(f"{c.device_label} {c.description}: {c.detail}" for c in failed[:6]))
    return "HEALTHY", f"Baseline HEALTHY: all {len(checks)} checks passed."
