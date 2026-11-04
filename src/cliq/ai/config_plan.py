"""
CLIQ M1.4 — Autonomous network configuration planning.

Turns natural-language network requirements into a structured, reviewable
multi-device configuration plan. Plans are evidence-gated: interface mappings
are validated against live topology and device evidence, and anything missing
or contradictory becomes an explicit operator question instead of an
assumption. Plans never execute here — execution lives in
:mod:`cliq.ai.config_exec` behind plan-bound approval.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from cliq.ai.device_exec import (
    PLATFORM_CISCO_IOS,
    PLATFORM_LINUX,
    classify_platform,
    execute_device_command,
)

log = logging.getLogger("cliq.ai.config_plan")

# ── Intent extraction patterns ───────────────────────────────────────────────

_INTF_RE = re.compile(
    r"\b((?:GigabitEthernet|TenGigabitEthernet|FastEthernet|Ethernet|"
    r"Gi|Te|Fa|Eth|Et|eth)\s*\d[\d/]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_LINUX_INTF_RE = re.compile(r"\beth\d+\b", re.IGNORECASE)
_VLAN_NAME_RE = re.compile(r"\bvlan\s*(\d{1,4})\s*\(\s*([^)]{1,32}?)\s*\)", re.IGNORECASE)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\b")
_NETMASK_RE = re.compile(r"\bnetmask\s+(\d{1,3}(?:\.\d{1,3}){3})\b", re.IGNORECASE)
_GATEWAY_RE = re.compile(r"\bgateway\s+(\d{1,3}(?:\.\d{1,3}){3})\b", re.IGNORECASE)
#: 802.1Q/802.1X/802.3 terminology: never a VLAN identifier. Scrubbed before
#: VLAN number extraction so "802.1Q trunk" cannot create VLAN 802.
_DOT1_RE = re.compile(r"\b802\.1[QqXx]?\b|\b802\.3\b")
_BOTH_ENDS_RE = re.compile(r"\bboth\s+(ends|switches|sides|devices)\b", re.IGNORECASE)
_LINK_SENTENCE_RE = re.compile(
    r"(\w+)\s+(" + r"(?:GigabitEthernet|TenGigabitEthernet|FastEthernet|Ethernet|"
    r"Gi|Te|Fa|Eth|Et|eth)\s*\d[\d/]*(?:\.\d+)?" + r")\s+connected\s+to\s+"
    r"(\w+)\s+(" + r"(?:GigabitEthernet|TenGigabitEthernet|FastEthernet|Ethernet|"
    r"Gi|Te|Fa|Eth|Et|eth)\s*\d[\d/]*(?:\.\d+)?" + r")",
    re.IGNORECASE,
)
_REACH_RE = re.compile(r"\b(\w+)\s+should\s+(?:reach|ping)\s+(\w+)\b", re.IGNORECASE)
_TRUNK_PEER_RE = re.compile(r"\btrunk\s+(?:to|toward|towards|uplink to)\s+(\w+)\b", re.IGNORECASE)
_ALLOWED_VLAN_RE = re.compile(r"\ballowed\s+(?:vlan\s+)?([\d,\s]+(?:and\s+\d+)?)", re.IGNORECASE)

_INTF_ABBREV = (
    ("gigabitethernet", "gigabitethernet"), ("gi", "gigabitethernet"),
    ("tengigabitethernet", "tengigabitethernet"), ("te", "tengigabitethernet"),
    ("fastethernet", "fastethernet"), ("fa", "fastethernet"),
    ("ethernet", "ethernet"), ("eth", "ethernet"), ("et", "ethernet"),
)


def normalize_interface(name: str) -> str:
    """Canonical interface key for evidence matching (``Gi0/1`` == ``gigabitethernet0/1``)."""
    compact = re.sub(r"\s+", "", (name or "").strip().lower())
    for abbrev, full in sorted(_INTF_ABBREV, key=lambda p: -len(p[0])):
        if compact.startswith(abbrev):
            return full + compact[len(abbrev):]
    return compact


def _device_role(platform: str, node_type: str) -> str:
    if platform == PLATFORM_LINUX:
        return "endpoint"
    norm = (node_type or "").lower()
    if "l2" in norm or "switch" in norm or "nxos" in norm or "ioll2" in norm:
        return "switch"
    if norm in ("iosv", "csr1000v", "cat8000v", "iosxrv", "iosxrv9000"):
        return "router"
    return "switch" if "sw" in norm else "router"


# ── Plan model ───────────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    step_id: str = ""
    device_id: str = ""
    command: str = ""
    purpose: str = ""
    read_only: bool = False
    depends_on: List[str] = field(default_factory=list)
    critical: bool = True
    target: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DevicePlan:
    device_id: str = ""
    device_label: str = ""
    platform: str = ""
    role: str = ""
    node_type: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    unverified: bool = False
    endpoint_specs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() if isinstance(s, PlanStep) else s for s in self.steps]
        return d


@dataclass
class PlanQuestion:
    qid: str = ""
    kind: str = ""
    message: str = ""
    blocking: bool = True
    device_id: str = ""
    options: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    resolved: bool = False
    resolution: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectivityExpectation:
    exp_id: str = ""
    src_device: str = ""
    dst: str = ""
    dst_ip: str = ""
    kind: str = "ping"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VlanSpec:
    vlan_id: int = 0
    name: str = ""


@dataclass
class AccessSpec:
    interface: str = ""
    vlan: int = 0


@dataclass
class TrunkSpec:
    interface: str = ""
    vlans: List[int] = field(default_factory=list)
    peer: str = ""


@dataclass
class EndpointIPSpec:
    interface: str = ""
    address: str = ""
    prefix: int = 0
    gateway: str = ""


@dataclass
class ConfigPlan:
    plan_id: str = ""
    owner_token: str = ""
    lab_id: str = ""
    lab_title: str = ""
    title: str = ""
    summary: str = ""
    prompt: str = ""
    version: int = 1
    fingerprint: str = ""
    status: str = "draft"
    devices: List[DevicePlan] = field(default_factory=list)
    questions: List[PlanQuestion] = field(default_factory=list)
    connectivity: List[ConnectivityExpectation] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "lab_id": self.lab_id,
            "lab_title": self.lab_title,
            "title": self.title,
            "summary": self.summary,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "devices": [d.to_dict() for d in self.devices],
            "questions": [q.to_dict() for q in self.questions],
            "connectivity": [c.to_dict() for c in self.connectivity],
            "assumptions": list(self.assumptions),
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "blocking_open": [q.qid for q in self.questions if q.blocking and not q.resolved],
        }


def fingerprint_plan(lab_id: str, devices: List[DevicePlan]) -> str:
    """Content hash binding approval to the exact lab/devices/commands/order."""
    canonical = {
        "lab_id": lab_id,
        "devices": [
            {
                "device_id": d.device_id,
                "platform": d.platform,
                "commands": [s.command for s in d.steps],
                "depends": [s.depends_on for s in d.steps],
            }
            for d in sorted(devices, key=lambda x: x.device_id)
        ],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


# ── In-memory plan store ─────────────────────────────────────────────────────

_plan_lock = threading.Lock()
_plans: Dict[str, ConfigPlan] = {}


def get_plan(plan_id: str) -> Optional[ConfigPlan]:
    with _plan_lock:
        return _plans.get(plan_id)


def _store_plan(plan: ConfigPlan) -> None:
    with _plan_lock:
        _plans[plan_id_key(plan)] = plan


def plan_id_key(plan: ConfigPlan) -> str:
    return plan.plan_id


# ── Inventory + topology evidence ────────────────────────────────────────────

def _get_server_module():
    from cliq.web import server
    return server


def _owned_inventory(session_token: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Return (caller's device inventory, error_code). Fail-closed on bad token."""
    server = _get_server_module()
    if not session_token or session_token not in server.SESSIONS:
        return {}, "unauthorized"
    return dict(server.DEVICES_BY_TOKEN.get(session_token) or {}), ""


async def _fetch_topology(session_token: str, lab_id: str) -> Optional[Dict[str, Any]]:
    """Fetch CML topology for evidence. Returns None when unavailable (SSH sessions)."""
    server = _get_server_module()
    session = server.SESSIONS.get(session_token)
    auth = getattr(session, "auth", None) if session else None
    if auth is None or not hasattr(auth, "get_topology"):
        return None
    try:
        return await asyncio.to_thread(auth.get_topology, lab_id)
    except Exception as exc:
        log.warning("topology fetch failed for %s: %s", lab_id, exc)
        return None


def _topology_links(topology: Optional[Dict[str, Any]]) -> List[Tuple[str, str, str, str, str, str]]:
    """Raw link pairs: (node_a, intf_a_raw, node_b, intf_b_raw, key_a, key_b)."""
    pairs: List[Tuple[str, str, str, str, str, str]] = []
    for link in (topology or {}).get("links", []) or []:
        node_a, node_b = link.get("node_a", ""), link.get("node_b", "")
        ia_raw = str(link.get("node_a_intf") or "")
        ib_raw = str(link.get("node_b_intf") or "")
        if node_a and node_b and ia_raw and ib_raw:
            pairs.append((node_a, ia_raw, node_b, ib_raw,
                          normalize_interface(ia_raw), normalize_interface(ib_raw)))
    return pairs


def _topology_interfaces(topology: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Map node_id -> {normalized_interface: peer_node_id} from topology links."""
    out: Dict[str, Dict[str, str]] = {}
    for link in (topology or {}).get("links", []) or []:
        node_a, node_b = link.get("node_a", ""), link.get("node_b", "")
        intf_a = normalize_interface(link.get("node_a_intf") or "")
        intf_b = normalize_interface(link.get("node_b_intf") or "")
        if node_a and intf_a:
            out.setdefault(node_a, {})[intf_a] = node_b
        if node_b and intf_b:
            out.setdefault(node_b, {})[intf_b] = node_a
    return out


_IOS_STATUS_INTF_RE = re.compile(r"(?m)^\s*([A-Za-z]+\d[\d/\.]*)\s+")
_LINUX_IFCONFIG_RE = re.compile(r"(?m)^(\S+)\s+Link encap")


async def _live_interfaces(session_token: str, lab_id: str, device_id: str,
                           platform: str) -> Tuple[List[str], str]:
    """Read-only interface enumeration. Returns (names, raw_or_error)."""
    cmd = "show interfaces status" if platform == PLATFORM_CISCO_IOS else "ifconfig -a"
    res = await execute_device_command(session_token, lab_id, device_id, cmd)
    if not res.success:
        return [], res.error or res.error_code
    if platform == PLATFORM_CISCO_IOS:
        return _IOS_STATUS_INTF_RE.findall(res.stdout or ""), res.stdout or ""
    return _LINUX_IFCONFIG_RE.findall(res.stdout or ""), res.stdout or ""


# ── Intent parsing ───────────────────────────────────────────────────────────

@dataclass
class ParsedIntent:
    device_names: List[str] = field(default_factory=list)
    vlans: Dict[int, str] = field(default_factory=dict)
    access: List[Tuple[str, Optional[int], str]] = field(default_factory=list)
    trunks: List[Tuple[Optional[str], Optional[str]]] = field(default_factory=list)
    endpoint_ips: List[Tuple[str, str, int, str]] = field(default_factory=list)
    reach: List[Tuple[str, str]] = field(default_factory=list)
    allowed_vlans: List[int] = field(default_factory=list)
    dot1q: bool = False
    both_ends: bool = False
    link_sentences: List[Tuple[str, str, str, str]] = field(default_factory=list)


def parse_config_intent(prompt: str) -> ParsedIntent:
    """Extract structured requirements from natural language. Pure function."""
    intent = ParsedIntent()
    text = prompt or ""
    if _DOT1_RE.search(text):
        intent.dot1q = True
    if _BOTH_ENDS_RE.search(text):
        intent.both_ends = True
    for m in _LINK_SENTENCE_RE.finditer(text):
        intent.link_sentences.append(
            (m.group(1), " ".join(m.group(2).split()).rstrip("."),
             m.group(3), " ".join(m.group(4).split()).rstrip(".")))
    scrubbed_text = _DOT1_RE.sub(" ", text)
    for m in _VLAN_NAME_RE.finditer(scrubbed_text):
        try:
            vid = int(m.group(1))
            if 1 <= vid <= 4094:
                intent.vlans[vid] = m.group(2).strip()[:32]
        except ValueError:
            continue
    for clause in re.split(r"[;\n]|\.(?:\s|$)", scrubbed_text):
        if "vlan" not in clause.lower():
            continue
        scrubbed = _INTF_RE.sub(" ", clause)
        scrubbed = _IP_RE.sub(" ", scrubbed)
        for num in re.findall(r"\b(\d{1,4})\b", scrubbed):
            vid = int(num)
            if 1 <= vid <= 4094 and vid not in intent.vlans:
                intent.vlans[vid] = ""
    for m in _ALLOWED_VLAN_RE.finditer(scrubbed_text):
        for num in re.findall(r"\b(\d{1,4})\b", m.group(1)):
            vid = int(num)
            if 1 <= vid <= 4094 and vid not in intent.allowed_vlans:
                intent.allowed_vlans.append(vid)
    for m in re.finditer(
            r"(" + _INTF_RE.pattern + r")\s*:?\s*(?:as\s+)?access"
            r"(?:\s+(?:port\s+)?(?:in\s+)?(?:to\s+)?(?:vlan\s+)?(\d{1,4}))?",
            text, re.IGNORECASE):
        intf = " ".join(m.group(1).split()).rstrip(".")
        vlan = int(m.group(3)) if m.group(3) else None
        intent.access.append((intf, vlan, m.group(0).strip()[:80]))
    for m in re.finditer(
            r"access(?:\s+port)?\s+(" + _INTF_RE.pattern + r")"
            r"(?:\s+(?:in\s+|to\s+)?(?:vlan\s+)?(\d{1,4}))?",
            text, re.IGNORECASE):
        intf = " ".join(m.group(1).split()).rstrip(".")
        vlan = int(m.group(3)) if m.group(3) else None
        entry = (intf, vlan, m.group(0).strip()[:80])
        if entry not in intent.access:
            intent.access.append(entry)
    for m in re.finditer(
            r"(" + _INTF_RE.pattern + r")\s*:?\s*(?:as\s+)?trunk\b", text, re.IGNORECASE):
        intent.trunks.append((" ".join(m.group(1).split()).rstrip("."), None))
    for m in re.finditer(r"\btrunk\s+(?:port\s+)?(" + _INTF_RE.pattern + r")",
                         text, re.IGNORECASE):
        entry = (" ".join(m.group(1).split()).rstrip("."), None)
        if entry not in intent.trunks:
            intent.trunks.append(entry)
    for m in _TRUNK_PEER_RE.finditer(text):
        intent.trunks.append((None, m.group(1)))
    for clause in re.split(r"[;\n]|\.(?:\s|$)", text):
        if "trunk" not in clause.lower():
            continue
        for m in _INTF_RE.finditer(clause):
            intf = " ".join(m.group(1).split()).rstrip(".")
            hint_m = re.search(r"(\w+)\s*$", clause[:m.start()])
            hint = hint_m.group(1) if hint_m else None
            if hint and hint.lower() in ("trunk", "port", "interface", "the", "a", "on", "for"):
                hint = None
            if hint is None:
                fwd_m = re.match(r"\s*(?:on|for)\s+(\w+)", clause[m.end():])
                if fwd_m:
                    hint = fwd_m.group(1)
            if not any((e[0] or "").lower() == intf.lower()
                       and (e[1] or "").lower() == (hint or "").lower()
                       for e in intent.trunks):
                intent.trunks.append((intf, hint))
    # Bare "trunk" outside read-only show commands only: "show interfaces
    # trunk" is discovery, never a trunk-configuration order.
    non_show = re.sub(r"\bshow\b[^;\n.]*", "", text, flags=re.IGNORECASE)
    if re.search(r"\btrunk\b", non_show, re.IGNORECASE) and not intent.trunks:
        intent.trunks.append((None, None))
    for m in _REACH_RE.finditer(text):
        intent.reach.append((m.group(1), m.group(2)))
    return intent


def _match_devices(names: List[str], inventory: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Map mentioned names to device ids (label or id, case-insensitive)."""
    resolved: Dict[str, str] = {}
    for name in names:
        low = name.strip().lower()
        for dev_id, meta in inventory.items():
            if low in (str(meta.get("label") or "").lower(), dev_id.lower()):
                resolved[name] = dev_id
                break
    return resolved


def _mentioned_devices(text: str, inventory: Dict[str, Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    for dev_id, meta in inventory.items():
        for key in (str(meta.get("label") or ""), dev_id):
            if key and re.search(rf"\b{re.escape(key)}\b", text or "", re.IGNORECASE):
                found.append(dev_id)
                break
    return found


# ── Negation / exclusion handling (H1) ───────────────────────────────────────
# Natural-language exclusions ("R1 and R2 are excluded", "No VLAN 802",
# "Only SW1 and SW2") remove targets — they must never add them.

_NEG_SCOPE_END_RE = re.compile(r"\b(?:but|yet|however|except|although)\b", re.IGNORECASE)
_NO_ROUTER_RE = re.compile(r"\bno\b[^.\n;]{0,40}\brouters?\b", re.IGNORECASE)
_ROUTER_EXCLUDED_RE = re.compile(r"\brouters?\b[^.\n;]{0,40}\bexclud\w*\b", re.IGNORECASE)
_NO_GATEWAY_RE = re.compile(r"\bno\b[^.\n;]{0,40}\b(?:default\s+)?gateways?\b", re.IGNORECASE)
_WITHOUT_GATEWAY_RE = re.compile(
    r"\bwithout\b[^.\n;]{0,40}\b(?:any\s+|a\s+)?gateways?\b", re.IGNORECASE)
_ONLY_INLINE_RE = re.compile(r"\bonly\s+([A-Za-z0-9][^.\n;]*)", re.IGNORECASE)
_ONLY_SECTION_RE = re.compile(r"\bonly\s+these\s+devices\b", re.IGNORECASE)
_DEVICE_NEG_CUES = (
    re.compile(r"\bexclud\w*\b", re.IGNORECASE),
    re.compile(r"\bexcept\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:on|for|in)\b", re.IGNORECASE),
    re.compile(r"\bno\b[^.\n;]{0,40}\btargets?\b", re.IGNORECASE),
    re.compile(r"\bno\b[^.\n;]{0,40}\bconfig\w*\b[^.\n;]{0,20}\b(?:on|for)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:do\s+not|don['\u2019]t|never)\s+config\w*\b", re.IGNORECASE),
)
_VLAN_NEG_CUE_RE = re.compile(
    r"\b(?:no|without|prohibit\w*|forbid\w*|ban\w*|exclud\w*|"
    r"never\s+(?:create|add|configure)|not\s+allowed)\b", re.IGNORECASE)


@dataclass
class Negations:
    """Explicitly excluded scope parsed from negative requirements."""
    excluded_devices: List[str] = field(default_factory=list)
    only_devices: List[str] = field(default_factory=list)
    prohibited_vlans: List[int] = field(default_factory=list)
    no_gateways: bool = False
    no_routers: bool = False


def _devices_in_scope(scope: str, inventory: Dict[str, Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    for dev_id, meta in inventory.items():
        for key in (str(meta.get("label") or ""), dev_id):
            if key and re.search(rf"\b{re.escape(key)}\b", scope or "", re.IGNORECASE):
                found.append(dev_id)
                break
    return found


def _negated_vlans_in_clause(clause: str) -> List[int]:
    """VLAN IDs under a negation cue ("No VLAN 2 or VLAN 802" -> [2, 802])."""
    found: List[int] = []
    if "vlan" not in clause.lower():
        return found
    for cue in _VLAN_NEG_CUE_RE.finditer(clause):
        end_m = _NEG_SCOPE_END_RE.search(clause, cue.end())
        scope = clause[cue.end():end_m.start() if end_m else len(clause)]
        scope = _DOT1_RE.sub(" ", scope)
        scope = _INTF_RE.sub(" ", scope)
        scope = _IP_RE.sub(" ", scope)
        for num in re.findall(r"\b(\d{1,4})\b", scope):
            vid = int(num)
            if 1 <= vid <= 4094 and vid not in found:
                found.append(vid)
    return found


def extract_negations(text: str, inventory: Dict[str, Dict[str, Any]]) -> Negations:
    """Parse exclusion/prohibition scope from natural language. Pure function."""
    neg = Negations()
    text = text or ""
    clauses = re.split(r"[;\n]|\.(?:\s|$)", text)
    for clause in clauses:
        if any(cue.search(clause) for cue in _DEVICE_NEG_CUES):
            for dev_id in _devices_in_scope(clause, inventory):
                if dev_id not in neg.excluded_devices:
                    neg.excluded_devices.append(dev_id)
        for vid in _negated_vlans_in_clause(clause):
            if vid not in neg.prohibited_vlans:
                neg.prohibited_vlans.append(vid)
    if _NO_ROUTER_RE.search(text) or _ROUTER_EXCLUDED_RE.search(text):
        neg.no_routers = True
    if _NO_GATEWAY_RE.search(text) or _WITHOUT_GATEWAY_RE.search(text):
        neg.no_gateways = True
    for m in _ONLY_INLINE_RE.finditer(text):
        for dev_id in _devices_in_scope(m.group(1), inventory):
            if dev_id not in neg.only_devices:
                neg.only_devices.append(dev_id)
    for m in _ONLY_SECTION_RE.finditer(text):
        # "ONLY THESE DEVICES:" followed by a device list; stop at blank
        # lines, new headings, or negation cues (never swallow exclusions).
        for line in text[m.end():].splitlines()[1:11]:
            stripped = line.strip()
            if not stripped or re.match(r"^[A-Z][A-Z /-]{2,}:\s*$", stripped):
                break
            if any(cue.search(stripped) for cue in _DEVICE_NEG_CUES):
                break
            for dev_id in _devices_in_scope(stripped, inventory):
                if dev_id not in neg.only_devices:
                    neg.only_devices.append(dev_id)
    return neg


# ── Multi-turn structured requirements (H2) ──────────────────────────────────
# Requirements stated in earlier turns of the same conversation+lab stay
# available to later builds. Never merged across sessions, conversations,
# or labs. Validation-only turns never read or write this store.

@dataclass
class PlanRequirements:
    lab_id: str = ""
    devices: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    only: List[str] = field(default_factory=list)
    vlans: Dict[int, str] = field(default_factory=dict)
    prohibited_vlans: List[int] = field(default_factory=list)
    access: List[Tuple[str, Optional[int], str]] = field(default_factory=list)
    trunks: List[Tuple[Optional[str], Optional[str]]] = field(default_factory=list)
    allowed: List[int] = field(default_factory=list)
    endpoint_specs: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    no_gateways: bool = False
    no_routers: bool = False
    dot1q: bool = False
    both_ends: bool = False
    link_sentences: List[Tuple[str, str, str, str]] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)


_req_lock = threading.Lock()
_CONV_REQUIREMENTS: Dict[Tuple[str, str, str], PlanRequirements] = {}


def _load_prior_requirements(session_token: Optional[str],
                             conversation_id: Optional[str],
                             lab_param: Optional[str],
                             mentioned: List[str],
                             inventory: Dict[str, Dict[str, Any]],
                             ) -> Optional[PlanRequirements]:
    """Return carried-over requirements, or None when scoping is unsafe."""
    if not session_token or not conversation_id:
        return None
    with _req_lock:
        labs = [lab for (s, c, lab) in _CONV_REQUIREMENTS
                if s == session_token and c == conversation_id]
    if lab_param:
        if lab_param not in labs:
            return None
        target = lab_param
    elif len(labs) == 1:
        target = labs[0]
    else:
        return None
    with _req_lock:
        prior = copy.deepcopy(_CONV_REQUIREMENTS[(session_token, conversation_id, target)])
    if mentioned:
        mentioned_labs = {inventory[d].get("lab_id", "") for d in mentioned
                          if d in inventory}
        if mentioned_labs - {target}:
            return None
    return prior


def _merge_intent(intent: ParsedIntent, neg: Negations,
                  prior: Optional[PlanRequirements]
                  ) -> Tuple[ParsedIntent, Negations]:
    """Combine the current-turn parse with carried-over requirements."""
    if prior is None:
        return intent, neg
    merged = ParsedIntent(
        device_names=list(intent.device_names),
        vlans={**prior.vlans, **intent.vlans},
        access=list(prior.access) + [a for a in intent.access if a not in prior.access],
        trunks=list(prior.trunks) + [tr for tr in intent.trunks if tr not in prior.trunks],
        endpoint_ips=list(intent.endpoint_ips),
        reach=list(intent.reach),
        allowed_vlans=list(dict.fromkeys(list(prior.allowed) + list(intent.allowed_vlans))),
        dot1q=intent.dot1q or prior.dot1q,
        both_ends=intent.both_ends or prior.both_ends,
        link_sentences=(list(prior.link_sentences)
                        + [ls for ls in intent.link_sentences
                           if ls not in prior.link_sentences]),
    )
    merged_neg = Negations(
        excluded_devices=list(dict.fromkeys(list(prior.excluded)
                                            + list(neg.excluded_devices))),
        only_devices=list(neg.only_devices) or list(prior.only),
        prohibited_vlans=list(dict.fromkeys(list(prior.prohibited_vlans)
                                            + list(neg.prohibited_vlans))),
        no_gateways=neg.no_gateways or prior.no_gateways,
        no_routers=neg.no_routers or prior.no_routers,
    )
    return merged, merged_neg


def _save_requirements(session_token: Optional[str],
                       conversation_id: Optional[str],
                       lab_id: str,
                       wanted: List[str],
                       vlans: Dict[int, str],
                       intent: ParsedIntent,
                       neg: Negations,
                       ip_specs: Dict[str, List[Any]],
                       prompt: str) -> None:
    """Snapshot merged requirements for later turns of this conversation."""
    if not session_token or not conversation_id or not lab_id:
        return
    with _req_lock:
        old = _CONV_REQUIREMENTS.get((session_token, conversation_id, lab_id))
        prompts = list(old.prompts) if old else []
    prompts.append((prompt or "")[:2000])
    req = PlanRequirements(
        lab_id=lab_id,
        devices=list(wanted),
        excluded=list(neg.excluded_devices),
        only=list(neg.only_devices),
        vlans=dict(vlans),
        prohibited_vlans=list(neg.prohibited_vlans),
        access=[(a[0], a[1], a[2][:80]) for a in intent.access],
        trunks=[(tr[0], tr[1]) for tr in intent.trunks],
        allowed=list(intent.allowed_vlans),
        endpoint_specs={d: [asdict(s) for s in specs]
                        for d, specs in (ip_specs or {}).items() if specs},
        no_gateways=neg.no_gateways,
        no_routers=neg.no_routers,
        dot1q=intent.dot1q,
        both_ends=intent.both_ends,
        link_sentences=list(intent.link_sentences),
        prompts=prompts[-3:],
    )
    with _req_lock:
        _CONV_REQUIREMENTS[(session_token, conversation_id, lab_id)] = req


def latest_plan_for_session(session_token: Optional[str],
                            lab_id: Optional[str] = None) -> Optional[ConfigPlan]:
    """Most recently created owned plan (validation fallback)."""
    if not session_token:
        return None
    with _plan_lock:
        cands = [pl for pl in _plans.values()
                 if pl.owner_token == session_token
                 and (lab_id is None or pl.lab_id == lab_id)]
    if not cands:
        return None
    return max(cands, key=lambda pl: pl.created_at)


# ── Requirements coverage ────────────────────────────────────────────────────

_ACCESS_Q_KINDS = frozenset({"ambiguous_target", "missing_access_vlan", "unknown_interface"})
_TRUNK_Q_KINDS = frozenset({"ambiguous_target", "missing_trunk_interface", "unknown_interface",
                            "unknown_peer", "missing_link", "ambiguous_link",
                            "missing_vlan_ids"})
_ENDPOINT_Q_KINDS = frozenset({"ambiguous_endpoint", "missing_prefix", "invalid_address",
                               "missing_capability"})


def _verify_coverage(intent: ParsedIntent,
                     access_specs: Dict[str, List[Any]],
                     trunk_specs: Dict[str, List[Any]],
                     ip_specs: Dict[str, List[Any]],
                     trunk_requested: int,
                     ep_requested: int,
                     ep_rendered: int,
                     plan: ConfigPlan) -> None:
    """Blocking errors for requested work that produced neither steps nor questions."""
    open_kinds = {q.kind for q in plan.questions if q.blocking and not q.resolved}
    req_access = len(intent.access)
    got_access = sum(len(v) for v in access_specs.values())
    if got_access < req_access and not (open_kinds & _ACCESS_Q_KINDS):
        plan.questions.append(PlanQuestion(
            qid=_qid("cov"), kind="missing_coverage", blocking=True,
            message=(f"{req_access - got_access} access-port assignment(s) were requested "
                     f"but only {got_access} rendered. Name each access port and VLAN explicitly."),
            context={"category": "access", "requested": req_access, "rendered": got_access}))
    got_trunk = sum(len(v) for v in trunk_specs.values())
    if got_trunk < trunk_requested and not (open_kinds & _TRUNK_Q_KINDS):
        plan.questions.append(PlanQuestion(
            qid=_qid("cov"), kind="missing_coverage", blocking=True,
            message=(f"{trunk_requested - got_trunk} trunk(s) were requested but only "
                     f"{got_trunk} rendered. Name each trunk port explicitly."),
            context={"category": "trunk", "requested": trunk_requested,
                     "rendered": got_trunk}))
    trunked_devices = sum(1 for v in trunk_specs.values() if v)
    if (intent.both_ends and trunked_devices < 2
            and not (open_kinds & (_TRUNK_Q_KINDS | {"missing_trunk_end"}))):
        plan.questions.append(PlanQuestion(
            qid=_qid("trk"), kind="missing_trunk_end", blocking=True,
            message=("Both trunk ends were requested but fewer than two trunk ports "
                     "resolved. Name both ends explicitly."),
            context={"category": "trunk", "rendered_devices": trunked_devices}))
    if ep_rendered < ep_requested and not (open_kinds & _ENDPOINT_Q_KINDS):
        plan.questions.append(PlanQuestion(
            qid=_qid("cov"), kind="missing_coverage", blocking=True,
            message=(f"{ep_requested - ep_rendered} endpoint address(es) were requested "
                     f"but only {ep_rendered} rendered. Pair each address with its endpoint."),
            context={"category": "endpoint", "requested": ep_requested,
                     "rendered": ep_rendered}))


# ── Command renderers ────────────────────────────────────────────────────────

def render_ios_switch(vlans: Dict[int, str], access: List[AccessSpec],
                      trunks: List[TrunkSpec]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Render ordered (command, purpose, target) tuples for a Cisco switch."""
    steps: List[Tuple[str, str, Dict[str, Any]]] = [("configure terminal", "Enter configuration mode", {})]
    for vid in sorted(vlans):
        steps.append((f"vlan {vid}", f"Create VLAN {vid}", {"vlan": vid}))
        if vlans[vid]:
            steps.append((f"name {vlans[vid]}", f"Name VLAN {vid}", {"vlan": vid}))
        steps.append(("exit", "Exit VLAN submode", {}))
    for spec in access:
        steps.append((f"interface {spec.interface}", f"Select {spec.interface}", {"interface": spec.interface}))
        steps.append(("switchport mode access", f"Set {spec.interface} to access mode", {"interface": spec.interface}))
        steps.append((f"switchport access vlan {spec.vlan}",
                      f"Assign {spec.interface} to VLAN {spec.vlan}",
                      {"interface": spec.interface, "vlan": spec.vlan}))
        steps.append(("no shutdown", f"Ensure {spec.interface} is up", {"interface": spec.interface}))
        steps.append(("exit", "Exit interface submode", {}))
    for spec in trunks:
        allowed = ",".join(str(v) for v in sorted(spec.vlans))
        steps.append((f"interface {spec.interface}", f"Select {spec.interface}", {"interface": spec.interface}))
        steps.append(("switchport mode trunk", f"Set {spec.interface} to trunk mode", {"interface": spec.interface}))
        steps.append((f"switchport trunk allowed vlan {allowed}",
                      f"Allow VLANs {allowed} on {spec.interface}",
                      {"interface": spec.interface}))
        steps.append(("no shutdown", f"Ensure {spec.interface} is up", {"interface": spec.interface}))
        steps.append(("exit", "Exit interface submode", {}))
    steps.append(("end", "Return to privileged EXEC", {}))
    return steps


#: Applets probed on Linux endpoints before rendering configuration.
_CAP_PROBE_APPLETS = ("ifconfig", "route", "arp", "ip", "sudo")


async def probe_endpoint_caps(session_token: str, lab_id: str,
                              device_id: str) -> Tuple[Optional[Dict[str, bool]], str]:
    """Establish which applets a Linux endpoint actually supports (read-only).

    Returns (caps, error): caps maps applet -> available, or None when the
    probe itself failed (no evidence at all).
    """
    res = await execute_device_command(
        session_token, lab_id, device_id, "which " + " ".join(_CAP_PROBE_APPLETS))
    if not res.success:
        return None, res.error or res.error_code
    found = {a: False for a in _CAP_PROBE_APPLETS}
    for line in (res.stdout or "").splitlines():
        base = line.strip().rsplit("/", 1)[-1].strip().lower()
        if base in found:
            found[base] = True
    return found, ""


def endpoint_tooling(spec: EndpointIPSpec,
                     caps: Optional[Dict[str, bool]]) -> Tuple[List[Tuple[str, str, Dict[str, Any]]], List[str]]:
    """Select address/gateway commands from verified applet evidence.

    Prefers ifconfig/route (BusyBox baseline), falls back to `ip` when that is
    what the endpoint actually has, and reports problems instead of guessing.
    Never emits sudo: privilege escalation is never assumed.
    """
    caps = caps or {}
    commands: List[Tuple[str, str, Dict[str, Any]]] = []
    problems: List[str] = []
    if caps.get("ifconfig"):
        try:
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{spec.prefix}", strict=False).netmask)
        except ValueError:
            mask = "255.255.255.0"
        commands.append((f"ifconfig {spec.interface} {spec.address} netmask {mask}",
                         f"Assign {spec.address}/{spec.prefix} to {spec.interface}",
                         {"interface": spec.interface}))
    elif caps.get("ip"):
        commands.append((f"ip addr add {spec.address}/{spec.prefix} dev {spec.interface}",
                         f"Assign {spec.address}/{spec.prefix} to {spec.interface}",
                         {"interface": spec.interface}))
    else:
        problems.append("no verified address-assignment command (need ifconfig or ip evidence)")
    if spec.gateway:
        if caps.get("route"):
            commands.append((f"route add default gw {spec.gateway}",
                             f"Set default gateway {spec.gateway}", {}))
        elif caps.get("ip"):
            commands.append((f"ip route add default via {spec.gateway}",
                             f"Set default gateway {spec.gateway}", {}))
        else:
            problems.append("no verified gateway command (need route or ip evidence)")
    return commands, problems


def render_linux_endpoint(specs: List[EndpointIPSpec],
                          caps: Optional[Dict[str, bool]]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Render endpoint steps using only capability-verified applets."""
    steps: List[Tuple[str, str, Dict[str, Any]]] = []
    for spec in specs:
        commands, _problems = endpoint_tooling(spec, caps)
        steps.extend(commands)
    return steps


# ── Plan builder ─────────────────────────────────────────────────────────────

def _qid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def build_plan(session_token: str, prompt: str,
                     lab_id: Optional[str] = None,
                     device_ids: Optional[List[str]] = None,
                     conversation_id: Optional[str] = None,
                     ) -> Tuple[Optional[ConfigPlan], str]:
    """Build a validated multi-device plan. Returns (plan, error_code).

    ``("", ...)`` error with a plan means success (plan may carry blocking
    questions). Runs read-only evidence commands only — never configures.
    """
    inventory, err = _owned_inventory(session_token)
    if err:
        return None, "unauthorized"
    if not inventory:
        return None, "no_devices"
    intent = parse_config_intent(prompt or "")
    neg = extract_negations(prompt or "", inventory)
    mentioned = _mentioned_devices(prompt or "", inventory)
    # Multi-turn carryover: prior requirements for this conversation+lab seed
    # the wanted set when the current turn names no conflicting scope.
    prior = _load_prior_requirements(session_token, conversation_id, lab_id,
                                     mentioned, inventory)
    wanted = list(device_ids or []) or mentioned
    if prior is not None:
        for d in prior.devices:
            if d not in wanted:
                wanted.append(d)
    # Keep only session-owned devices; silently drop foreign ids (no leak).
    wanted = [d for d in wanted if d in inventory]
    # Explicit only-scope wins over mentions; exclusions always apply.
    only_scope = neg.only_devices or (prior.only if prior else [])
    if only_scope:
        wanted = [d for d in wanted if d in only_scope]
    excluded_ids = set(neg.excluded_devices) | (set(prior.excluded) if prior else set())
    if neg.no_routers or (prior is not None and prior.no_routers):
        excluded_ids |= {d for d in wanted
                         if _device_role(classify_platform(str(inventory[d].get("type") or "")),
                                          str(inventory[d].get("type") or "")) == "router"}
    dropped = [d for d in wanted if d in excluded_ids]
    wanted = [d for d in wanted if d not in excluded_ids]
    intent, neg = _merge_intent(intent, neg, prior)
    if not wanted:
        plan = ConfigPlan(plan_id=f"cfg-{uuid.uuid4().hex[:10]}", owner_token=session_token,
                          lab_id=lab_id or "",
                          title="Configuration plan", summary="",
                          prompt=(prompt or "")[:2000],
                          created_at=time.time(), updated_at=time.time())
        if dropped:
            labels = ", ".join(str(inventory.get(d, {}).get("label") or d) for d in dropped)
            msg = (f"All mentioned devices were excluded ({labels}). Name devices to "
                   "configure or adjust the exclusions.")
        else:
            msg = ("No target devices identified. Name the lab devices to configure "
                   "(e.g. SW1, PC1).")
        plan.questions.append(PlanQuestion(
            qid=_qid("devices"), kind="missing_devices", blocking=True, message=msg))
        plan.fingerprint = fingerprint_plan(plan.lab_id, plan.devices)
        _save_requirements(session_token, conversation_id, lab_id or "", wanted,
                           dict(intent.vlans), intent, neg, {}, prompt or "")
        _store_plan(plan)
        return plan, ""
    _dropped_labels = [str(inventory.get(d, {}).get("label") or d) for d in dropped]
    _only_labels = ([str(inventory.get(d, {}).get("label") or d) for d in only_scope]
                    if only_scope else [])
    labs = {inventory[d].get("lab_id", "") for d in wanted}
    if lab_id and lab_id not in labs:
        return None, "wrong_lab"
    target_lab = lab_id or (next(iter(labs)) if len(labs) == 1 else "")
    plan = ConfigPlan(plan_id=f"cfg-{uuid.uuid4().hex[:10]}", owner_token=session_token,
                      lab_id=target_lab,
                      title="Network configuration plan",
                      prompt=(prompt or "")[:2000],
                      created_at=time.time(), updated_at=time.time())
    if not target_lab:
        plan.questions.append(PlanQuestion(
            qid=_qid("lab"), kind="ambiguous_lab", blocking=True,
            message="Mentioned devices span multiple labs. Specify one target lab.",
            options=sorted(labs)))
        plan.fingerprint = fingerprint_plan("", plan.devices)
        _store_plan(plan)
        return plan, ""
    wanted = [d for d in wanted if inventory[d].get("lab_id") == target_lab]
    try:
        from cliq.web.server import SESSIONS
        session = SESSIONS.get(session_token)
        plan.lab_title = target_lab
        _ = session
    except Exception:
        pass

    topology = await _fetch_topology(session_token, target_lab)
    topo_intfs = _topology_interfaces(topology)
    evidence: Dict[str, Any] = {
        "topology_available": topology is not None,
        "topology_links": len((topology or {}).get("links", []) or []),
    }
    live_names: Dict[str, List[str]] = {}
    for dev_id in wanted:
        meta = inventory[dev_id]
        platform = classify_platform(str(meta.get("type") or ""),
                                     str(meta.get("ssh_device_type") or ""))
        if not platform:
            plan.questions.append(PlanQuestion(
                qid=_qid("plat"), kind="unsupported_platform", blocking=True,
                device_id=dev_id,
                message=(f"Device '{meta.get('label')}' has unsupported platform "
                         f"'{meta.get('type')}' for configuration.")))
            continue
        names, _raw = await _live_interfaces(session_token, target_lab, dev_id, platform)
        live_names[dev_id] = names
    evidence["live_interfaces"] = {d: live_names.get(d, []) for d in wanted}
    evidence["endpoint_caps"] = {}
    plan.evidence = evidence

    vlans: Dict[int, str] = dict(intent.vlans)
    for vid in neg.prohibited_vlans:
        if vid in vlans:
            plan.assumptions.append(f"VLAN {vid} is prohibited per request — excluded.")
            vlans.pop(vid)
    allowed = ([v for v in intent.allowed_vlans if v not in neg.prohibited_vlans]
               or sorted(vlans))
    for label in _dropped_labels:
        plan.assumptions.append(f"{label} excluded per request — not targeted.")
    if _only_labels:
        plan.assumptions.append(
            "Device scope limited per request: " + ", ".join(_only_labels) + ".")
    if neg.no_gateways:
        plan.assumptions.append("Default gateways suppressed per request.")
    plan.evidence["prohibited_vlans"] = list(neg.prohibited_vlans)
    if intent.trunks and not allowed:
        plan.questions.append(PlanQuestion(
            qid=_qid("vlan"), kind="missing_vlan_ids", blocking=True,
            message=("A trunk was requested but no VLAN IDs were found. "
                     "Specify the VLANs (e.g. 'VLANs 10 and 20').")))
    if not vlans and not intent.access and not intent.trunks and not _IP_RE.search(prompt or ""):
        plan.questions.append(PlanQuestion(
            qid=_qid("req"), kind="missing_requirements", blocking=True,
            message=("No VLANs, ports, trunks, or endpoint addresses found in the request. "
                     "Describe the required VLANs, ports, and addresses.")))

    access_specs: Dict[str, List[AccessSpec]] = {d: [] for d in wanted}
    trunk_specs: Dict[str, List[TrunkSpec]] = {d: [] for d in wanted}
    ip_specs: Dict[str, List[EndpointIPSpec]] = {d: [] for d in wanted}

    access_targets = _assign_specs_to_devices(intent.access, wanted, inventory, prompt or "")
    for (intf, vlan, _raw), dev_id in access_targets:
        if dev_id is None:
            plan.questions.append(PlanQuestion(
                qid=_qid("tgt"), kind="ambiguous_target", blocking=True,
                message=f"Access port {intf} matches no single target device. Name the device.",
                context={"interface": intf}))
            continue
        if vlan is None:
            if len(vlans) == 1:
                vlan = next(iter(vlans))
                plan.assumptions.append(f"{intf} on {inventory[dev_id].get('label')} assigned to the only mentioned VLAN {vlan}.")
            else:
                plan.questions.append(PlanQuestion(
                    qid=_qid("acc"), kind="missing_access_vlan", blocking=True,
                    device_id=dev_id,
                    message=f"No VLAN given for access port {intf}. Specify the VLAN ID.",
                    context={"interface": intf}))
                continue
        if vlan in neg.prohibited_vlans:
            plan.questions.append(PlanQuestion(
                qid=_qid("proh"), kind="prohibited_vlan", blocking=True,
                device_id=dev_id,
                message=(f"Access port {intf} requests VLAN {vlan}, which is prohibited. "
                         "Choose an allowed VLAN."),
                context={"interface": intf, "vlan": vlan}))
            continue
        if vlan not in vlans:
            vlans[vlan] = ""
        ok, why = _validate_interface(dev_id, intf, topo_intfs, live_names,
                                      topology is not None)
        if not ok:
            plan.questions.append(PlanQuestion(
                qid=_qid("intf"), kind="unknown_interface", blocking=True,
                device_id=dev_id,
                message=f"Interface {intf} on {inventory[dev_id].get('label')}: {why}",
                context={"interface": intf, "vlan": vlan}))
            continue
        if why == "unverified":
            plan.assumptions.append(
                f"{intf} on {inventory[dev_id].get('label')} could not be verified against evidence.")
        access_specs[dev_id].append(AccessSpec(interface=intf, vlan=vlan))

    if intent.dot1q and intent.trunks:
        plan.assumptions.append(
            "802.1Q trunk encapsulation noted; trunks carry tagged 802.1Q frames.")
    trunk_entries = _expand_trunk_endpoints(
        intent, wanted, inventory, topology, plan)
    for trunk_intf, peer, preferred in trunk_entries:
        dev_id = _resolve_trunk_device(trunk_intf, peer, wanted, inventory,
                                       topo_intfs, prompt or "", plan,
                                       preferred=preferred)
        if dev_id is None:
            continue
        intf = trunk_intf
        if intf is None and peer:
            intf = _peer_link_interface(dev_id, peer, wanted, inventory, topo_intfs, plan)
            if intf is None:
                continue
        if intf is None:
            plan.questions.append(PlanQuestion(
                qid=_qid("trk"), kind="missing_trunk_interface", blocking=True,
                device_id=dev_id,
                message=(f"No trunk interface identified on {inventory[dev_id].get('label')}. "
                         "Name the trunk port (e.g. Gi0/24).")))
            continue
        ok, why = _validate_interface(dev_id, intf, topo_intfs, live_names,
                                      topology is not None)
        if not ok:
            plan.questions.append(PlanQuestion(
                qid=_qid("intf"), kind="unknown_interface", blocking=True,
                device_id=dev_id,
                message=f"Interface {intf} on {inventory[dev_id].get('label')}: {why}",
                context={"interface": intf, "trunk": True}))
            continue
        trunk_specs[dev_id].append(TrunkSpec(interface=intf, vlans=list(allowed), peer=peer or ""))

    no_gateways = neg.no_gateways
    ep_requested, ep_rendered = _extract_endpoint_ips(
        prompt or "", wanted, inventory, ip_specs, plan, no_gateways=no_gateways)
    if prior is not None:
        for dev_id, specs in (prior.endpoint_specs or {}).items():
            if dev_id not in ip_specs:
                continue
            have = {(s.address, s.interface) for s in ip_specs[dev_id]}
            for s in specs:
                spec = EndpointIPSpec(
                    interface=s.get("interface", "eth0"),
                    address=s.get("address", ""),
                    prefix=int(s.get("prefix", 0) or 0),
                    gateway="" if no_gateways else s.get("gateway", ""))
                if spec.address and (spec.address, spec.interface) not in have:
                    ip_specs[dev_id].append(spec)
                    have.add((spec.address, spec.interface))
                    ep_requested += 1
                    ep_rendered += 1

    endpoint_caps: Dict[str, Optional[Dict[str, bool]]] = {}
    for dev_id in wanted:
        specs = ip_specs.get(dev_id, [])
        if not specs:
            continue
        meta = inventory[dev_id]
        if classify_platform(str(meta.get("type") or "")) != PLATFORM_LINUX:
            continue
        label = str(meta.get("label") or dev_id)
        found, perr = await probe_endpoint_caps(session_token, target_lab, dev_id)
        endpoint_caps[dev_id] = found
        evidence["endpoint_caps"][dev_id] = {"caps": found, "attested": False,
                                             "error": perr}
        if found is None:
            plan.questions.append(PlanQuestion(
                qid=_qid("cap"), kind="missing_capability", blocking=True,
                device_id=dev_id,
                message=(f"Cannot establish which commands {label} supports ({perr}). "
                         "Name the available applets (e.g. 'ifconfig and route only') "
                         "or reconnect the endpoint."),
                context={"specs": [asdict(s) for s in specs]}))
            continue
        for spec in specs:
            _cmds, problems = endpoint_tooling(spec, found)
            for prob in problems:
                plan.questions.append(PlanQuestion(
                    qid=_qid("cap"), kind="missing_capability", blocking=True,
                    device_id=dev_id,
                    message=(f"{label}: {prob}. Evidence: {', '.join(sorted(a for a, ok in found.items() if ok)) or 'none'}. "
                             "Confirm available applets or install the missing ones."),
                    context={"specs": [asdict(s) for s in specs]}))

    for dev_id in wanted:
        meta = inventory[dev_id]
        platform = classify_platform(str(meta.get("type") or ""),
                                     str(meta.get("ssh_device_type") or ""))
        if not platform:
            continue
        role = _device_role(platform, str(meta.get("type") or ""))
        dp = DevicePlan(device_id=dev_id, device_label=str(meta.get("label") or dev_id),
                        platform=platform, role=role,
                        node_type=str(meta.get("type") or ""))
        if platform == PLATFORM_CISCO_IOS and role == "endpoint":
            role = "switch"
            dp.role = role
        if platform == PLATFORM_CISCO_IOS and role != "switch":
            plan.questions.append(PlanQuestion(
                qid=_qid("rtr"), kind="router_target", blocking=True,
                device_id=dev_id,
                message=(f"{dp.device_label} is a router ({dp.node_type}); this planner "
                         "renders Layer-2 switch configuration only. Answer 'remove' to drop "
                         "it from the plan, or re-submit without router targets."),
                context={"role": role, "node_type": dp.node_type}))
            plan.devices.append(dp)
            continue
        if platform == PLATFORM_CISCO_IOS:
            rendered = render_ios_switch(
                {v: vlans.get(v, "") for v in sorted(set(list(vlans) + allowed))} if (vlans or allowed) else {},
                access_specs.get(dev_id, []), trunk_specs.get(dev_id, []))
            # Skip bare configure/end wrapper when nothing changes on this device.
            if (not vlans and not allowed and not access_specs.get(dev_id)
                    and not trunk_specs.get(dev_id)):
                rendered = []
        elif platform == PLATFORM_LINUX:
            rendered = render_linux_endpoint(ip_specs.get(dev_id, []),
                                              endpoint_caps.get(dev_id))
            dp.endpoint_specs = [asdict(s) for s in ip_specs.get(dev_id, [])]
        else:
            rendered = []
        prev_id = ""
        for idx, (cmd, purpose, target) in enumerate(rendered, 1):
            sid = f"{dev_id}:{idx}"
            dp.steps.append(PlanStep(step_id=sid, device_id=dev_id, command=cmd,
                                     purpose=purpose, read_only=False,
                                     depends_on=[prev_id] if prev_id else [],
                                     critical=True, target=target))
            prev_id = sid
        plan.devices.append(dp)

    _verify_coverage(intent, access_specs, trunk_specs, ip_specs,
                     len(trunk_entries), ep_requested, ep_rendered, plan)
    _wire_cross_device_deps(plan, inventory, topo_intfs)
    _build_connectivity(plan, inventory, ip_specs, intent)

    if not any(d.steps for d in plan.devices) and not plan.questions:
        plan.questions.append(PlanQuestion(
            qid=_qid("req"), kind="missing_requirements", blocking=True,
            message="No configuration changes could be derived. Describe VLANs, ports, or addresses."))
    plan.title = _plan_title(plan, vlans)
    plan.summary = _plan_summary(plan, vlans)
    plan.fingerprint = fingerprint_plan(plan.lab_id, plan.devices)
    _save_requirements(session_token, conversation_id, target_lab, wanted, vlans,
                       intent, neg, ip_specs, prompt or "")
    _store_plan(plan)
    return plan, ""


def _assign_specs_to_devices(specs: List[Tuple[str, Optional[int], str]],
                             wanted: List[str],
                             inventory: Dict[str, Dict[str, Any]],
                             prompt: str) -> List[Tuple[Tuple[str, Optional[int], str], Optional[str]]]:
    """Assign each access spec to a target switch. Single-switch plans bind directly."""
    switches = [d for d in wanted
                if _device_role(classify_platform(str(inventory[d].get("type") or "")),
                                str(inventory[d].get("type") or "")) == "switch"]
    out = []
    low_prompt = prompt.lower()
    occ_cache: Dict[str, List[int]] = {}
    seen_count: Dict[str, int] = {}
    for spec in specs:
        if len(switches) == 1:
            out.append((spec, switches[0]))
            continue
        # Multi-switch: bind by nearest device mention to this spec's own
        # interface occurrence, else ambiguous.
        intf = spec[0]
        key = intf.lower()
        if key not in occ_cache:
            occ_cache[key] = [m.start() for m in re.finditer(re.escape(key), low_prompt)]
        occs = occ_cache[key]
        idx = seen_count.get(key, 0)
        seen_count[key] = idx + 1
        pos = occs[min(idx, len(occs) - 1)] if occs else None
        best = None
        if pos is not None:
            # Explicit attachment ("Gi0/2 access vlan 10 on SW1") wins.
            tail = low_prompt[pos:pos + 32]
            am = re.search(r"\b(?:on|for)\s+(\w+)", tail)
            if am and am.start() <= 24 and not re.search(r"[.;\n]", tail[:am.start()]):
                attached = _resolve_name(am.group(1), inventory)
                if attached in switches:
                    best = attached
            if best is None:
                # Otherwise the owning section header (nearest preceding
                # mention); fall back to the nearest following mention.
                best_pre, pre_dist = None, None
                best_post, post_dist = None, None
                for dev_id in switches:
                    for name in (str(inventory[dev_id].get("label") or ""), dev_id):
                        if not name:
                            continue
                        for m in re.finditer(re.escape(name.lower()), low_prompt):
                            if m.start() <= pos:
                                dist = pos - m.start()
                                if pre_dist is None or dist < pre_dist:
                                    best_pre, pre_dist = dev_id, dist
                            else:
                                dist = m.start() - pos
                                if post_dist is None or dist < post_dist:
                                    best_post, post_dist = dev_id, dist
                best = best_pre if best_pre is not None else best_post
        out.append((spec, best))
    return out


def _validate_interface(device_id: str, interface: str,
                        topo_intfs: Dict[str, Dict[str, str]],
                        live_names: Dict[str, List[str]],
                        topology_available: bool) -> Tuple[bool, str]:
    """Validate an interface against topology + live evidence.

    Returns (ok, reason): ok with "verified"/"unverified", or not-ok with the
    explanation for the operator question.
    """
    key = normalize_interface(interface)
    topo_hit = key in (topo_intfs.get(device_id) or {})
    live_hit = any(normalize_interface(n) == key for n in live_names.get(device_id, []))
    if topo_hit or live_hit:
        return True, "verified"
    if not topology_available and not live_names.get(device_id):
        return False, ("no topology or live evidence is available to confirm it "
                       "(device offline or unreachable evidence). Provide the exact "
                       "interface or reconnect the device.")
    return False, "it matches no interface in topology or live device evidence."


def _is_switch(dev_id: str, inventory: Dict[str, Dict[str, Any]]) -> bool:
    """True only for Layer-2 switch roles (routers excluded)."""
    meta = inventory.get(dev_id) or {}
    return _device_role(
        classify_platform(str(meta.get("type") or "")),
        str(meta.get("type") or "")) == "switch"


def _expand_trunk_endpoints(intent: ParsedIntent, wanted: List[str],
                            inventory: Dict[str, Dict[str, Any]],
                            topology: Optional[Dict[str, Any]],
                            plan: ConfigPlan) -> List[Tuple[Optional[str], Optional[str], Optional[str]]]:
    """Expand trunk intent to both ends when the requirement names them.

    Returns (interface, peer, preferred_device) triples. A bare "trunk" plus a
    named switch link ("SW1 Gi0/1 connected to SW2 Gi0/1") binds both ends
    explicitly; "both ends/switches" phrasing mirrors each end across its
    topology link. Single-end phrasing is left untouched (preferred None).
    """
    pairs = _topology_links(topology)
    if intent.trunks == [(None, None)] and intent.link_sentences:
        expanded: List[Tuple[Optional[str], Optional[str], Optional[str]]] = []
        for dev1, intf1, dev2, intf2 in intent.link_sentences:
            d1 = _resolve_name(dev1, inventory)
            d2 = _resolve_name(dev2, inventory)
            if (d1 in wanted and d2 in wanted and d1 is not None and d2 is not None
                    and _is_switch(d1, inventory) and _is_switch(d2, inventory)):
                expanded.append((intf1, dev2, d1))
                expanded.append((intf2, dev1, d2))
        if expanded:
            plan.assumptions.append(
                "Trunk applied to both ends of the named switch link(s).")
            return expanded
        return [(None, None, None)]
    out: List[Tuple[Optional[str], Optional[str], Optional[str]]] = []
    for intf, peer in intent.trunks:
        mirrored = False
        if intent.both_ends and intf:
            key = normalize_interface(intf)
            for (node_a, ia_raw, node_b, ib_raw, key_a, key_b) in pairs:
                if key != key_a and key != key_b:
                    continue
                if not (node_a in wanted and node_b in wanted):
                    continue
                if not (_is_switch(node_a, inventory) and _is_switch(node_b, inventory)):
                    continue
                if key == key_a:
                    out.append((ia_raw, None, node_a))
                    out.append((ib_raw, None, node_b))
                else:
                    out.append((ib_raw, None, node_b))
                    out.append((ia_raw, None, node_a))
                mirrored = True
        if not mirrored:
            out.append((intf, peer, None))
    if len(out) > len(intent.trunks):
        plan.assumptions.append("Trunk mirrored to both ends of the link.")
    seen, deduped = set(), []
    for entry in out:
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    return deduped


def _resolve_trunk_device(trunk_intf: Optional[str], peer: Optional[str],
                          wanted: List[str], inventory: Dict[str, Dict[str, Any]],
                          topo_intfs: Dict[str, Dict[str, str]],
                          prompt: str, plan: ConfigPlan,
                          preferred: Optional[str] = None) -> Optional[str]:
    if preferred and preferred in wanted:
        return preferred
    if trunk_intf and peer:
        hint = _resolve_name(peer, inventory)
        if (hint in wanted
                and normalize_interface(trunk_intf) in (topo_intfs.get(hint) or {})):
            return hint
    if trunk_intf:
        holders = [d for d in wanted
                   if normalize_interface(trunk_intf) in (topo_intfs.get(d) or {})]
        if len(holders) == 1:
            return holders[0]
    switches = [d for d in wanted
                if _device_role(classify_platform(str(inventory[d].get("type") or "")),
                                str(inventory[d].get("type") or "")) == "switch"]
    if len(switches) == 1:
        return switches[0]
    plan.questions.append(PlanQuestion(
        qid=_qid("tgt"), kind="ambiguous_target", blocking=True,
        message="Trunk target is ambiguous. Name the switch for the trunk port.",
        context={"peer": peer or ""}))
    return None


def _peer_link_interface(dev_id: str, peer: str, wanted: List[str],
                         inventory: Dict[str, Dict[str, Any]],
                         topo_intfs: Dict[str, Dict[str, str]],
                         plan: ConfigPlan) -> Optional[str]:
    peer_id = None
    low = peer.lower()
    for cand in wanted:
        if low in (str(inventory[cand].get("label") or "").lower(), cand.lower()):
            peer_id = cand
            break
    if peer_id is None:
        plan.questions.append(PlanQuestion(
            qid=_qid("peer"), kind="unknown_peer", blocking=True, device_id=dev_id,
            message=f"Trunk peer '{peer}' matches no lab device. Name a valid peer.",
            context={"peer": peer}))
        return None
    links = [i for i, p in (topo_intfs.get(dev_id) or {}).items() if p == peer_id]
    if len(links) == 1:
        return links[0]
    if not links:
        plan.questions.append(PlanQuestion(
            qid=_qid("peer"), kind="missing_link", blocking=True, device_id=dev_id,
            message=(f"No topology link found between {inventory[dev_id].get('label')} and "
                     f"{inventory[peer_id].get('label')}. Name the trunk interface explicitly."),
            context={"peer": peer}))
        return None
    plan.questions.append(PlanQuestion(
        qid=_qid("peer"), kind="ambiguous_link", blocking=True, device_id=dev_id,
        message=(f"Multiple links connect {inventory[dev_id].get('label')} to "
                 f"{inventory[peer_id].get('label')}. Choose the trunk interface."),
        options=sorted(links), context={"peer": peer}))
    return None


def _extract_endpoint_ips(prompt: str, wanted: List[str],
                          inventory: Dict[str, Dict[str, Any]],
                          ip_specs: Dict[str, List[EndpointIPSpec]],
                          plan: ConfigPlan,
                          no_gateways: bool = False) -> Tuple[int, int]:
    endpoints = [d for d in wanted
                 if classify_platform(str(inventory[d].get("type") or "")) == PLATFORM_LINUX]
    if not endpoints:
        return 0, 0
    requested = 0
    rendered = 0
    gateway = ""
    gm = None if no_gateways else _GATEWAY_RE.search(prompt or "")
    if (gm):
        gateway = gm.group(1)
    clauses = re.split(r"[;\n]|\.(?:\s|$)", prompt or "")
    for clause in clauses:
        ips = _IP_RE.findall(clause)
        if not ips:
            continue
        requested += sum(1 for ip in ips if not (gateway and ip == gateway))
        owners = [d for d in endpoints
                  if re.search(rf"\b{re.escape(str(inventory[d].get('label') or d))}\b",
                               clause, re.IGNORECASE) or
                  re.search(rf"\b{re.escape(d)}\b", clause, re.IGNORECASE)]
        if not owners:
            if len(endpoints) == 1 and ("address" in clause.lower() or "ip" in clause.lower()
                                        or "/" in clause):
                owners = list(endpoints)
            else:
                plan.questions.append(PlanQuestion(
                    qid=_qid("ep"), kind="ambiguous_endpoint", blocking=True,
                    message=(f"Address {', '.join(ips)} matches no single endpoint owner. "
                             "Name the endpoint explicitly (e.g. 'PC1 192.168.10.11/24')."),
                    context={"ips": list(ips), "clause": clause[:160]}))
                continue
        iface_m = _LINUX_INTF_RE.search(clause)
        iface = iface_m.group(0).lower() if iface_m else "eth0"
        if not iface_m:
            plan.assumptions.append(
                f"{inventory[owners[0]].get('label')} address assigned to eth0 (no interface named).")
        for raw_ip in ips:
            if gateway and raw_ip == gateway:
                continue
            if "/" in raw_ip:
                addr, prefix_s = raw_ip.split("/", 1)
                try:
                    prefix = int(prefix_s)
                    ipaddress.IPv4Address(addr)
                    if not 0 <= prefix <= 32:
                        raise ValueError
                except ValueError:
                    plan.questions.append(PlanQuestion(
                        qid=_qid("ip"), kind="invalid_address", blocking=True,
                        device_id=owners[0],
                        message=f"'{raw_ip}' is not a valid IPv4 address/prefix.",
                        context={"value": raw_ip}))
                    continue
            else:
                nm = _NETMASK_RE.search(clause)
                if nm:
                    try:
                        prefix = ipaddress.IPv4Network(f"0.0.0.0/{nm.group(1)}").prefixlen
                    except ValueError:
                        prefix = 0
                    addr = raw_ip
                else:
                    try:
                        ipaddress.IPv4Address(raw_ip)
                    except ValueError:
                        plan.questions.append(PlanQuestion(
                            qid=_qid("ip"), kind="invalid_address", blocking=True,
                            device_id=owners[0],
                            message=(f"'{raw_ip}' is not a valid IPv4 address. "
                                     "Correct the address for "
                                     f"{inventory[owners[0]].get('label')}."),
                            context={"value": raw_ip, "interface": iface}))
                        continue
                    plan.questions.append(PlanQuestion(
                        qid=_qid("ip"), kind="missing_prefix", blocking=True,
                        device_id=owners[0],
                        message=(f"No prefix/netmask given for {raw_ip} on "
                                 f"{inventory[owners[0]].get('label')}. Specify e.g. '{raw_ip}/24'."),
                        context={"address": raw_ip, "interface": iface, "gateway": gateway}))
                    continue
                try:
                    ipaddress.IPv4Address(addr)
                except ValueError:
                    plan.questions.append(PlanQuestion(
                        qid=_qid("ip"), kind="invalid_address", blocking=True,
                        device_id=owners[0],
                        message=(f"'{raw_ip}' is not a valid IPv4 address. "
                                 "Correct the address for "
                                 f"{inventory[owners[0]].get('label')}."),
                        context={"value": raw_ip, "interface": iface}))
                    continue
            for dev_id in owners:
                ip_specs[dev_id].append(EndpointIPSpec(interface=iface, address=addr,
                                                       prefix=prefix, gateway=gateway))
                rendered += 1
    # Gateway-only mention for endpoints without addresses: not actionable.
    if gateway and not any(ip_specs.values()) and endpoints:
        plan.assumptions.append(f"Gateway {gateway} noted; no endpoint addresses to pair it with.")
    return requested, rendered


def _wire_cross_device_deps(plan: ConfigPlan, inventory: Dict[str, Dict[str, Any]],
                            topo_intfs: Dict[str, Dict[str, str]]) -> None:
    """Endpoint IP steps depend on the peer switch's access-port steps."""
    by_device: Dict[str, DevicePlan] = {d.device_id: d for d in plan.devices}
    for dp in plan.devices:
        if dp.platform != PLATFORM_LINUX or not dp.steps:
            continue
        peers = topo_intfs.get(dp.device_id) or {}
        for _iface, peer_id in peers.items():
            peer_dp = by_device.get(peer_id)
            if not peer_dp or not peer_dp.steps:
                continue
            access_steps = [s.step_id for s in peer_dp.steps
                            if s.target.get("vlan") and s.target.get("interface")]
            if access_steps:
                first = dp.steps[0]
                for dep in access_steps:
                    if dep not in first.depends_on:
                        first.depends_on.append(dep)


def _build_connectivity(plan: ConfigPlan, inventory: Dict[str, Dict[str, Any]],
                        ip_specs: Dict[str, List[EndpointIPSpec]],
                        intent: ParsedIntent) -> None:
    addr_of: Dict[str, str] = {}
    for dev_id, specs in ip_specs.items():
        if specs:
            addr_of[dev_id] = specs[0].address
    label_of = {d: str(inventory[d].get("label") or d) for d in inventory}
    seen = set()
    for src_name, dst_name in intent.reach:
        src = _resolve_name(src_name, inventory)
        dst_ip = _IP_RE.search(dst_name)
        dst = _resolve_name(dst_name, inventory) if not dst_ip else ""
        if src is None:
            continue
        pair = (src, dst_ip.group(1) if dst_ip else dst_name)
        if pair in seen:
            continue
        seen.add(pair)
        plan.connectivity.append(ConnectivityExpectation(
            exp_id=f"ping-{len(plan.connectivity) + 1}", src_device=src,
            dst=label_of.get(dst, dst_name) if dst else dst_name,
            dst_ip=dst_ip.group(1) if dst_ip else addr_of.get(dst, "")))
    for dev_id, specs in ip_specs.items():
        for spec in specs:
            if spec.gateway and (dev_id, spec.gateway) not in seen:
                seen.add((dev_id, spec.gateway))
                plan.connectivity.append(ConnectivityExpectation(
                    exp_id=f"ping-{len(plan.connectivity) + 1}", src_device=dev_id,
                    dst=f"gateway {spec.gateway}", dst_ip=spec.gateway))
    eps = [d for d in ip_specs if ip_specs[d]]
    for i in range(len(eps)):
        for j in range(i + 1, len(eps)):
            a, b = eps[i], eps[j]
            if len([e for e in plan.connectivity if e.kind == "ping"]) >= 6:
                break
            if (a, addr_of[b]) not in seen:
                seen.add((a, addr_of[b]))
                plan.connectivity.append(ConnectivityExpectation(
                    exp_id=f"ping-{len(plan.connectivity) + 1}", src_device=a,
                    dst=label_of.get(b, b), dst_ip=addr_of[b]))


def _resolve_name(name: str, inventory: Dict[str, Dict[str, Any]]) -> Optional[str]:
    low = name.lower()
    for dev_id, meta in inventory.items():
        if low in (str(meta.get("label") or "").lower(), dev_id.lower()):
            return dev_id
    return None


def _plan_title(plan: ConfigPlan, vlans: Dict[int, str]) -> str:
    parts = []
    if vlans:
        parts.append("VLANs " + ",".join(str(v) for v in sorted(vlans)))
    n_cmds = sum(len(d.steps) for d in plan.devices)
    devs = ",".join(d.device_label for d in plan.devices if d.steps)
    if devs:
        parts.append(f"on {devs}")
    return ("Configure " + " ".join(parts) + f" ({n_cmds} commands)") if parts else "Configuration plan"


def _plan_summary(plan: ConfigPlan, vlans: Dict[int, str]) -> str:
    lines = []
    if vlans:
        lines.append("VLANs: " + ", ".join(
            f"{v} ({n})" if n else str(v) for v, n in sorted(vlans.items())))
    for dp in plan.devices:
        if dp.steps:
            lines.append(f"{dp.device_label} [{dp.role}/{dp.node_type}]: {len(dp.steps)} commands")
    if not lines:
        return "No configuration changes derived yet."
    return "; ".join(lines)


# ── Operator answers ─────────────────────────────────────────────────────────

def apply_answers(plan_id: str, answers: Dict[str, str],
                  session_token: str = "") -> Tuple[Optional[ConfigPlan], str]:
    """Apply operator answers, re-render affected commands, bump version.

    Any accepted change bumps ``version`` and recomputes the fingerprint, which
    invalidates outstanding approval tickets/tokens bound to the old version.
    """
    with _plan_lock:
        plan = _plans.get(plan_id)
        if plan is None:
            return None, "not_found"
        if not session_token or plan.owner_token != session_token:
            return None, "forbidden"
        if plan.status in ("executing", "completed"):
            return None, "locked"
        changed = False
        for q in plan.questions:
            if q.resolved or q.qid not in (answers or {}):
                continue
            value = str(answers[q.qid] or "").strip()
            if not value:
                continue
            ok, note = _apply_single_answer(plan, q, value)
            if ok:
                q.resolved = True
                q.resolution = value[:200]
                changed = True
            else:
                q.message = note
        if changed:
            plan.version += 1
            plan.status = "draft"
            plan.fingerprint = fingerprint_plan(plan.lab_id, plan.devices)
            plan.updated_at = time.time()
        return plan, ""


def _apply_single_answer(plan: ConfigPlan, q: PlanQuestion, value: str) -> Tuple[bool, str]:
    if q.kind in ("unknown_interface", "missing_trunk_interface", "ambiguous_link"):
        intf = " ".join(value.split())
        if not _INTF_RE.search(intf) or len(intf) > 64:
            return False, f"'{value}' is not a valid interface name. {q.message}"
        _retarget_steps(plan, q, intf)
        return True, ""
    if q.kind == "missing_access_vlan":
        try:
            vid = int(value)
            if not 1 <= vid <= 4094:
                raise ValueError
        except ValueError:
            return False, f"'{value}' is not a valid VLAN ID (1-4094). {q.message}"
        if vid in (plan.evidence.get("prohibited_vlans") or []):
            return False, f"VLAN {vid} is prohibited for this plan. {q.message}"
        _set_access_vlan(plan, q, vid)
        return True, ""
    if q.kind == "missing_vlan_ids":
        vids = [int(n) for n in re.findall(r"\b(\d{1,4})\b", value) if 1 <= int(n) <= 4094]
        if not vids:
            return False, f"No valid VLAN IDs in '{value}'. {q.message}"
        banned = [v for v in vids if v in (plan.evidence.get("prohibited_vlans") or [])]
        if banned:
            return False, f"VLAN(s) {banned} are prohibited for this plan. {q.message}"
        _set_trunk_vlans(plan, vids)
        return True, ""
    if q.kind == "missing_prefix":
        addr = q.context.get("address", "")
        prefix = value.lstrip("/").strip()
        try:
            net = ipaddress.IPv4Network(f"{addr}/{prefix}", strict=False)
        except ValueError:
            return False, f"'{value}' is not a valid prefix for {addr}. {q.message}"
        caps = (plan.evidence.get("endpoint_caps") or {}).get(q.device_id, {}).get("caps")
        if not (caps or {}).get("ifconfig") and not (caps or {}).get("ip"):
            return False, ("No verified address-assignment command on this endpoint. "
                           "Resolve the capability question first.")
        _add_endpoint_ip(plan, q, addr, net.prefixlen, caps)
        return True, ""
    if q.kind == "missing_capability":
        attested = {a for a in _CAP_PROBE_APPLETS
                    if re.search(rf"\b{re.escape(a)}\b", value.lower())}
        if not attested:
            return False, (f"No known applets in '{value}'. Name the supported commands "
                           f"(e.g. 'ifconfig and route only'). {q.message}")
        caps = {a: (a in attested) for a in _CAP_PROBE_APPLETS}
        plan.evidence.setdefault("endpoint_caps", {})[q.device_id] = {
            "caps": caps, "attested": True, "error": ""}
        _rerender_endpoint_steps(plan, q.device_id, caps)
        # Resolve sibling capability questions for the same device: one
        # attestation covers the whole endpoint.
        for other in plan.questions:
            if (other.kind == "missing_capability" and other.device_id == q.device_id
                    and not other.resolved):
                other.resolved = True
                other.resolution = value[:200]
        return True, ""
    if q.kind == "router_target":
        if value.strip().lower() in ("remove", "drop", "yes"):
            plan.devices = [d for d in plan.devices if d.device_id != q.device_id]
            return True, ""
        return False, f"Answer 'remove' to drop the router target. {q.message}"
    if q.kind in ("ambiguous_target", "missing_devices", "ambiguous_lab",
                  "unknown_peer", "missing_link", "missing_requirements",
                  "invalid_address", "unsupported_platform",
                  "ambiguous_endpoint", "missing_coverage", "missing_trunk_end",
                  "prohibited_vlan"):
        # Free-text answers to open questions are recorded for the next
        # planning turn; the operator re-submits refined requirements.
        return True, ""
    if q.kind == "evidence_missing":
        return True, ""
    return False, q.message


def _device_plan(plan: ConfigPlan, device_id: str) -> Optional[DevicePlan]:
    for dp in plan.devices:
        if dp.device_id == device_id:
            return dp
    return None


def _retarget_steps(plan: ConfigPlan, q: PlanQuestion, new_intf: str) -> None:
    dp = _device_plan(plan, q.device_id)
    if dp is None:
        return
    old = q.context.get("interface", "")
    replaced = False
    for step in dp.steps:
        if old and old in step.command:
            step.command = step.command.replace(old, new_intf)
            step.purpose = step.purpose.replace(old, new_intf)
            if step.target.get("interface") == old:
                step.target["interface"] = new_intf
            replaced = True
    if q.kind == "missing_trunk_interface":
        _append_trunk_steps(dp, new_intf, q)
    elif not replaced:
        # Unknown interfaces were never rendered: append the intended block.
        q.context["interface"] = new_intf
        if q.context.get("trunk"):
            _append_trunk_steps(dp, new_intf, q)
        elif isinstance(q.context.get("vlan"), int):
            _set_access_vlan(plan, q, q.context["vlan"])
    dp.unverified = True


def _append_trunk_steps(dp: DevicePlan, intf: str, q: PlanQuestion) -> None:
    vlan_ids = sorted({s.target.get("vlan") for s in dp.steps
                       if isinstance(s.target.get("vlan"), int)} or [10])
    base = len(dp.steps)
    block = [
        (f"interface {intf}", f"Select {intf}", {"interface": intf}),
        ("switchport mode trunk", f"Set {intf} to trunk mode", {"interface": intf}),
        (f"switchport trunk allowed vlan {','.join(str(v) for v in vlan_ids)}",
         f"Allow VLANs on {intf}", {"interface": intf}),
        ("no shutdown", f"Ensure {intf} is up", {"interface": intf}),
        ("exit", "Exit interface submode", {}),
    ]
    # Insert before trailing "end" when present.
    at = base - 1 if dp.steps and dp.steps[-1].command == "end" else base
    prev = dp.steps[at - 1].step_id if at > 0 else ""
    fresh: List[PlanStep] = []
    for i, (cmd, purpose, target) in enumerate(block):
        sid = f"{dp.device_id}:{at + i + 1}x"
        fresh.append(PlanStep(step_id=sid, device_id=dp.device_id, command=cmd,
                              purpose=purpose, depends_on=[prev] if prev else [],
                              target=target))
        prev = sid
    dp.steps[at:at] = fresh
    # Renumber to keep step ids dense and dependency chains intact.
    id_map: Dict[str, str] = {}
    for i, s in enumerate(dp.steps, 1):
        new_id = f"{dp.device_id}:{i}"
        id_map[s.step_id] = new_id
        s.step_id = new_id
    for s in dp.steps:
        s.depends_on = [id_map.get(d, d) for d in s.depends_on]


def _set_access_vlan(plan: ConfigPlan, q: PlanQuestion, vid: int) -> None:
    dp = _device_plan(plan, q.device_id)
    if dp is None:
        return
    intf = q.context.get("interface", "")
    # Ensure the VLAN exists, then append a full access-port block.
    cmds = [s.command for s in dp.steps]
    at = len(dp.steps) - 1 if cmds and cmds[-1] == "end" else len(dp.steps)
    block = [(f"vlan {vid}", f"Create VLAN {vid}", {"vlan": vid}),
             ("exit", "Exit VLAN submode", {})]
    if f"vlan {vid}" in cmds:
        block = []
    block += [(f"interface {intf}", f"Select {intf}", {"interface": intf}),
              ("switchport mode access", f"Set {intf} to access mode", {"interface": intf}),
              (f"switchport access vlan {vid}", f"Assign {intf} to VLAN {vid}",
               {"interface": intf, "vlan": vid}),
              ("no shutdown", f"Ensure {intf} is up", {"interface": intf}),
              ("exit", "Exit interface submode", {})]
    prev = dp.steps[at - 1].step_id if at > 0 else ""
    fresh = []
    for cmd, purpose, target in block:
        sid = f"{dp.device_id}:a{len(dp.steps) + len(fresh) + 1}"
        fresh.append(PlanStep(step_id=sid, device_id=dp.device_id, command=cmd,
                              purpose=purpose, depends_on=[prev] if prev else [],
                              target=target))
        prev = sid
    dp.steps[at:at] = fresh
    id_map = {}
    for i, s in enumerate(dp.steps, 1):
        new_id = f"{dp.device_id}:{i}"
        id_map[s.step_id] = new_id
        s.step_id = new_id
    for s in dp.steps:
        s.depends_on = [id_map.get(d, d) for d in s.depends_on]


def _set_trunk_vlans(plan: ConfigPlan, vids: List[int]) -> None:
    for dp in plan.devices:
        for step in dp.steps:
            if step.command.startswith("switchport trunk allowed vlan"):
                step.command = f"switchport trunk allowed vlan {','.join(str(v) for v in sorted(vids))}"
                step.purpose = f"Allow VLANs on {step.target.get('interface', 'trunk')}"


def _add_endpoint_ip(plan: ConfigPlan, q: PlanQuestion, addr: str, prefix: int,
                     caps: Optional[Dict[str, bool]]) -> None:
    dp = _device_plan(plan, q.device_id)
    if dp is None:
        return
    iface = q.context.get("interface", "eth0")
    gw = q.context.get("gateway", "")
    spec = EndpointIPSpec(interface=iface, address=addr, prefix=prefix, gateway=gw)
    commands, _problems = endpoint_tooling(spec, caps)
    prev = dp.steps[-1].step_id if dp.steps else ""
    for cmd, purpose, target in commands:
        sid = f"{dp.device_id}:{len(dp.steps) + 1}"
        dp.steps.append(PlanStep(
            step_id=sid, device_id=dp.device_id, command=cmd, purpose=purpose,
            depends_on=[prev] if prev else [], target=target))
        prev = sid
    dp.endpoint_specs.append(asdict(spec))


def _rerender_endpoint_steps(plan: ConfigPlan, device_id: str,
                             caps: Dict[str, bool]) -> None:
    """Re-render an endpoint's steps from stored specs after attestation.

    Endpoint steps are leaves (nothing depends on them), so replacement only
    preserves the first step's inbound (cross-device) dependencies.
    """
    dp = _device_plan(plan, device_id)
    if dp is None:
        return
    inbound = list(dp.steps[0].depends_on) if dp.steps else []
    specs = [EndpointIPSpec(**s) for s in dp.endpoint_specs]
    fresh: List[PlanStep] = []
    prev = ""
    for spec in specs:
        for cmd, purpose, target in render_linux_endpoint([spec], caps):
            sid = f"{device_id}:{len(fresh) + 1}"
            fresh.append(PlanStep(
                step_id=sid, device_id=device_id, command=cmd, purpose=purpose,
                depends_on=[prev] if prev else [], target=target))
            prev = sid
    if fresh:
        fresh[0].depends_on = inbound
    dp.steps = fresh
