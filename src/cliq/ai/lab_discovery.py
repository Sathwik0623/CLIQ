"""
CLIQ Copilot — autonomous CML controller discovery (read-only).

Lets Copilot resolve the caller's already-authenticated controller session and
reuse the existing discovery services (catalog, system information, labs,
topology) during Lab Creation. Never invents data: unavailable sections are
reported as unavailable with typed errors. Session-scoped: one token can only
ever read its own controller session. No secrets are logged or returned.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cliq.labs.discovery import DiscoveryError, discover_controller_catalog

log = logging.getLogger("cliq.ai.lab_discovery")

NO_SESSION = "no_session"
NON_CML_SESSION = "non_cml_session"
AUTH_FAILURE = "auth_failure"
CONTROLLER_UNAVAILABLE = "controller_unavailable"
DISCOVERY_ERROR = "discovery_error"

_UNAVAILABLE_TYPES = frozenset({
    "ConnectionError", "ConnectTimeout", "Timeout", "ReadTimeout",
    "ConnectError", "URLError", "gaierror", "NewConnectionError",
    "MaxRetryError", "NameResolutionError",
})

_SECRET_RE = re.compile(
    r"Bearer\s+[^\s,;]+|[?&]token=[^&\s,;]+|\"password\"\s*:\s*\"[^\"]*\"",
    re.IGNORECASE,
)

MAX_DEF_NAMES = 60
MAX_LABS = 30
MAX_TOPOLOGY_NODES = 40
MAX_TOPOLOGY_LINKS = 40
MAX_TOPOLOGY_INTERFACES = 40
MAX_SUMMARY_NODE_LABELS = 20
MAX_SUMMARY_LINKS = 20


def _scrub(text: Any) -> str:
    """Truncate and redact an error message (never leak credentials)."""
    return _SECRET_RE.sub("[REDACTED]", str(text or ""))[:300]


def classify_error(exc: Exception) -> str:
    """Best-effort classification of a discovery transport failure."""
    name = type(exc).__name__
    text = f"{name}: {exc}"
    low = text.lower()
    if (name in _UNAVAILABLE_TYPES or "failed to establish" in low
            or "name or service not known" in low or "timed out" in low):
        return CONTROLLER_UNAVAILABLE
    if ("not authenticated" in low or "expired" in low or "login" in low
            or "401" in low or "403" in low or "unauthorized" in low
            or "forbidden" in low):
        return AUTH_FAILURE
    return DISCOVERY_ERROR


@dataclass
class DiscoveryResult:
    """Outcome of one read-only discovery pass over the owned controller."""
    ok: bool = False
    partial: bool = False
    error_code: str = ""
    message: str = ""
    host: str = ""
    version: str = ""
    ready: Optional[bool] = None
    node_definitions: Optional[List[str]] = None
    image_definitions: Optional[List[str]] = None
    resources: Optional[Dict[str, Any]] = None
    labs: Optional[List[Dict[str, Any]]] = None
    topology: Optional[Dict[str, Any]] = None
    lab_title: str = ""
    unavailable: List[str] = field(default_factory=list)
    section_errors: Dict[str, str] = field(default_factory=dict)
    collected_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "partial": self.partial,
            "error_code": self.error_code,
            "message": self.message,
            "host": self.host,
            "version": self.version,
            "ready": self.ready,
            "node_definitions": self.node_definitions,
            "image_definitions": self.image_definitions,
            "resources": self.resources,
            "labs": self.labs,
            "topology": self.topology,
            "lab_title": self.lab_title,
            "unavailable": list(self.unavailable),
            "section_errors": dict(self.section_errors),
            "collected_at": self.collected_at,
        }


def _names(items: Any) -> List[str]:
    out: List[str] = []
    if isinstance(items, list):
        for entry in items:
            if isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
            elif isinstance(entry, dict):
                name = entry.get("id") or entry.get("name") or entry.get("definition")
                if name:
                    out.append(str(name))
    elif isinstance(items, dict):
        out.extend(str(k) for k in items.keys())
    return out


def resolve_lab_title(labs: Any, hint: Optional[str]) -> Optional[str]:
    """Resolve a natural-language lab title to a lab UUID.

    Matches known lab titles from ``get_labs()`` data as case-insensitive
    substrings of the turn text (longest title first). Returns the lab id or
    None when no title is named. Never guesses: unknown titles stay unknown.
    """
    if not hint or not isinstance(labs, list):
        return None
    low = str(hint).lower()
    cands = [lab for lab in labs
             if isinstance(lab, dict) and str(lab.get("title") or "").strip()]
    for lab in sorted(cands, key=lambda l: len(str(l.get("title"))), reverse=True):
        title = str(lab.get("title") or "").strip()
        if len(title) >= 3 and title.lower() in low:
            return str(lab.get("id") or "") or None
    return None


def discover_environment(session_token: Optional[str],
                         lab_id: Optional[str] = None,
                         lab_title_hint: Optional[str] = None) -> DiscoveryResult:
    """Discover the caller's own controller. Read-only; never fabricates.

    :param session_token: CLIQ session token owning a CML controller session.
    :param lab_id: when set, also fetch that lab's live topology.
    :param lab_title_hint: turn text; a named lab title resolves to its UUID
        from ``get_labs()`` data for the topology fetch.
    """
    from cliq.web import server  # lazy: avoid circular import at module load

    if not session_token or session_token not in server.SESSIONS:
        return DiscoveryResult(
            ok=False, error_code=NO_SESSION, collected_at=time.time(),
            message=("No controller session is available for this chat session. "
                     "Connect to a CML controller in the CLIQ IDE first."))
    session = server.SESSIONS[session_token]
    auth = getattr(session, "auth", None)
    if auth is None or getattr(session, "connection_type", "cml") != "cml":
        return DiscoveryResult(
            ok=False, error_code=NON_CML_SESSION, collected_at=time.time(),
            message=("This chat session is not backed by a CML controller session "
                     "(SSH sessions have no controller). Connect to a CML controller "
                     "first."))
    host = str(getattr(session, "host", "") or "")
    res = DiscoveryResult(collected_at=time.time(), host=host)

    def _get(path: str) -> Any:
        try:
            return auth._get(path).json()
        except DiscoveryError:
            raise
        except Exception as exc:
            raise DiscoveryError(
                f"CML GET {path} failed: {type(exc).__name__}: {_scrub(exc)}"
            ) from exc

    # 1. Controller identity / version / readiness.
    try:
        info = auth.get_system_information() or {}
        version = str(info.get("version") or "")
        ready = info.get("ready")
        if version:
            res.version = version
        else:
            res.unavailable.append("version")
        res.ready = bool(ready) if ready is not None else None
        if ready is None:
            res.unavailable.append("readiness")
    except Exception as exc:
        code = classify_error(exc)
        res.section_errors["system_information"] = code
        res.unavailable.append("system_information")
        log.warning("discovery system_information failed: %s", _scrub(exc))

    # 2. Node/image definitions + resources via the shared catalog service.
    try:
        catalog = discover_controller_catalog(_get)
        res.node_definitions = _names(catalog.node_definitions)
        res.image_definitions = _names(catalog.image_definitions)
        snap = catalog.to_snapshot()
        resources: Dict[str, Any] = {}
        if snap.cpu_total is not None:
            resources["cpu_total"] = snap.cpu_total
            resources["cpu_used"] = snap.cpu_used
        if snap.ram_total_mb is not None:
            resources["ram_total_mb"] = snap.ram_total_mb
            resources["ram_used_mb"] = snap.ram_used_mb
        if resources:
            res.resources = resources
        else:
            res.unavailable.append("resources")
        if not res.version:
            version = str((catalog.system_information or {}).get("version") or "")
            if version:
                res.version = version
        if res.ready is None and "ready" in (catalog.system_information or {}):
            res.ready = bool(catalog.system_information.get("ready"))
    except Exception as exc:
        code = classify_error(exc)
        res.section_errors["catalog"] = code
        res.unavailable.extend(["node_definitions", "image_definitions", "resources"])
        log.warning("discovery catalog failed: %s", _scrub(exc))

    # 3. Existing labs.
    try:
        labs_out: List[Dict[str, Any]] = []
        for lab in auth.get_labs() or []:
            labs_out.append({
                "id": str(getattr(lab, "lab_id", getattr(lab, "id", "")) or ""),
                "title": str(getattr(lab, "title", "") or ""),
                "state": str(getattr(lab, "state", "") or ""),
                "owner": str(getattr(lab, "owner", "") or ""),
            })
        res.labs = labs_out
    except Exception as exc:
        code = classify_error(exc)
        res.section_errors["labs"] = code
        res.unavailable.append("labs")
        log.warning("discovery labs failed: %s", _scrub(exc))

    # 3b. Resolve a named lab title to its UUID for the topology fetch.
    if not lab_id and lab_title_hint and res.labs:
        lab_id = resolve_lab_title(res.labs, lab_title_hint)
    if lab_id and res.labs:
        for lab in res.labs:
            if lab.get("id") == lab_id and lab.get("title"):
                res.lab_title = str(lab.get("title"))
                break

    # 4. Live topology for the selected lab (only when requested).
    if lab_id:
        try:
            topo = auth.get_topology(lab_id) or {}
            res.topology = {
                "lab_id": lab_id,
                "nodes": topo.get("nodes", []) if isinstance(topo, dict) else [],
                "links": topo.get("links", []) if isinstance(topo, dict) else [],
                "interfaces": (topo.get("interfaces", [])
                               if isinstance(topo, dict) else []),
            }
        except Exception as exc:
            code = classify_error(exc)
            res.section_errors["topology"] = code
            res.unavailable.append("topology")
            log.warning("discovery topology failed: %s", _scrub(exc))

    failed = list(res.section_errors)
    if not failed:
        res.ok = True
        res.message = f"Controller discovery succeeded on {host or 'the controller'}."
    else:
        succeeded = 3 + (1 if lab_id else 0) - len(failed)
        first_code = res.section_errors[failed[0]]
        if succeeded > 0:
            res.partial = True
            res.error_code = first_code
            res.message = (f"Partial controller discovery on {host or 'the controller'}: "
                           f"{', '.join(failed)} unavailable ({first_code}).")
        else:
            res.error_code = first_code
            res.message = (f"Controller discovery failed on {host or 'the controller'}: "
                           f"{first_code}.")
    return res


def _link_endpoints(link: Dict[str, Any],
                    node_labels: Dict[str, str]) -> tuple:
    """Human-readable "NODE:interface" endpoints for a topology link.

    Falls back from enriched labels to node-id lookup to "?" — unknown
    references stay visibly unknown, never invented.
    """
    eps = []
    for side in ("a", "b"):
        node_id = str(link.get(f"node_{side}") or "")
        intf = str(link.get(f"node_{side}_intf") or "")
        label = (str(link.get(f"node_{side}_label") or "")
                 or node_labels.get(node_id, ""))
        if label and intf:
            eps.append(f"{label}:{intf}")
        elif label:
            eps.append(label)
        elif intf:
            eps.append(f"?:{intf}")
        else:
            eps.append("?")
    return eps[0], eps[1]


def format_discovery_for_prompt(result: DiscoveryResult) -> str:
    """Render discovery as bounded LLM context. Only actual results appear."""
    if result.error_code in (NO_SESSION, NON_CML_SESSION):
        return ("--- CML CONTROLLER DISCOVERY ---\n"
                f"Status: unavailable ({result.error_code}). {result.message}")
    lines = ["--- CML CONTROLLER DISCOVERY (LIVE, READ-ONLY) ---"]
    ident = f"Controller: {result.host or 'unknown controller'}"
    if result.version:
        ident += f" | version {result.version}"
    if result.ready is not None:
        ident += f" | ready: {result.ready}"
    lines.append(ident)
    if result.node_definitions is not None:
        names = result.node_definitions[:MAX_DEF_NAMES]
        lines.append(f"Node definitions ({len(result.node_definitions)}): "
                     + (", ".join(names) or "none reported"))
    if result.image_definitions is not None:
        names = result.image_definitions[:MAX_DEF_NAMES]
        lines.append(f"Image definitions ({len(result.image_definitions)}): "
                     + (", ".join(names) or "none reported"))
    if result.resources is not None:
        parts = []
        if "cpu_total" in result.resources:
            parts.append(f"CPU {result.resources.get('cpu_used')}/"
                         f"{result.resources.get('cpu_total')}")
        if "ram_total_mb" in result.resources:
            parts.append(f"RAM {result.resources.get('ram_used_mb')}/"
                         f"{result.resources.get('ram_total_mb')} MB")
        lines.append("Resources: " + ("; ".join(parts) if parts else "reported"))
    if result.labs is not None:
        lines.append(f"Existing labs ({len(result.labs)}):")
        for lab in result.labs[:MAX_LABS]:
            lines.append(f"  - {lab.get('title') or lab.get('id')} "
                         f"[{lab.get('state') or 'unknown'}]")
    if result.topology is not None:
        nodes = [n for n in result.topology.get("nodes", []) if isinstance(n, dict)]
        links = [lk for lk in result.topology.get("links", []) if isinstance(lk, dict)]
        ifaces = [i for i in result.topology.get("interfaces", [])
                  if isinstance(i, dict)]
        node_labels = {str(n.get("id") or ""): str(n.get("label") or "")
                       for n in nodes if n.get("id")}
        lab_ref = result.lab_title or result.topology.get("lab_id")
        lines.append(f"Topology for lab {lab_ref}: "
                     f"{len(nodes)} nodes, {len(links)} links, "
                     f"{len(ifaces)} interfaces.")
        shown = nodes[:MAX_TOPOLOGY_NODES]
        for node in shown:
            label = node.get("label") or node.get("id") or "?"
            ntype = node.get("type") or node.get("node_definition") or "unknown"
            state = node.get("state") or "unknown"
            lines.append(f"  - {label} [{ntype}, {state}] "
                         f"(id: {node.get('id') or '?'})")
        if len(nodes) > len(shown):
            lines.append(f"  ... and {len(nodes) - len(shown)} more nodes.")
        shown_links = links[:MAX_TOPOLOGY_LINKS]
        for link in shown_links:
            ep_a, ep_b = _link_endpoints(link, node_labels)
            lines.append(f"  - link {link.get('id') or '?'}: "
                         f"{ep_a} <-> {ep_b}")
        if len(links) > len(shown_links):
            lines.append(f"  ... and {len(links) - len(shown_links)} more links.")
        shown_ifaces = ifaces[:MAX_TOPOLOGY_INTERFACES]
        for iface in shown_ifaces:
            owner = (iface.get("node_label")
                     or node_labels.get(str(iface.get("node") or ""), "")
                     or "?")
            lines.append(f"  - {owner}:{iface.get('label') or '?'} "
                         f"(id: {iface.get('id') or '?'})")
        if len(ifaces) > len(shown_ifaces):
            lines.append(f"  ... and {len(ifaces) - len(shown_ifaces)} "
                         "more interfaces.")
    if result.unavailable:
        lines.append("Unavailable (not invented): " + ", ".join(result.unavailable) + ".")
    if result.error_code:
        lines.append(f"Discovery status: {result.error_code}. {result.message}")
        lines.append("Relay only the results above; do not invent missing sections.")
    else:
        lines.append("Instruct the engineer from these live results; "
                     "never ask for data already shown here.")
    return "\n".join(lines)


def format_prior_summary(summary: Dict[str, Any]) -> str:
    """Render a retained multi-turn summary as bounded LLM context."""
    summary = summary or {}
    lines = ["--- CML CONTROLLER DISCOVERY (RETAINED FROM AN EARLIER TURN) ---"]
    ident = f"Controller: {summary.get('host') or 'unknown controller'}"
    if summary.get("version"):
        ident += f" | version {summary['version']}"
    if summary.get("ready") is not None:
        ident += f" | ready: {summary['ready']}"
    lines.append(ident)
    for key, label in (("node_definition_count", "Node definitions"),
                       ("image_definition_count", "Image definitions"),
                       ("lab_count", "Existing labs")):
        if summary.get(key) is not None:
            lines.append(f"{label}: {summary[key]} (see earlier turn for names)")
    if summary.get("topology_lab"):
        lab_ref = summary.get("topology_lab_title") or summary["topology_lab"]
        lines.append(f"Topology retained for lab {lab_ref}.")
        if summary.get("topology_node_labels"):
            lines.append("Nodes: " + ", ".join(summary["topology_node_labels"]))
        if summary.get("topology_links"):
            lines.append("Links: " + ", ".join(summary["topology_links"]))
    if summary.get("unavailable"):
        lines.append("Unavailable: " + ", ".join(summary["unavailable"]) + ".")
    if summary.get("error_code"):
        lines.append(f"Earlier discovery status: {summary['error_code']}. "
                     "Do not treat missing sections as discovered.")
    lines.append("Ask for fresh discovery if the controller may have changed.")
    return "\n".join(lines)


def summarize_for_metadata(result: DiscoveryResult) -> Dict[str, Any]:
    """Small multi-turn summary (counts and status, not full payloads)."""
    topo_nodes = (result.topology or {}).get("nodes", []) or []
    node_labels = [str(n.get("label") or n.get("id") or "?")
                   for n in topo_nodes[:MAX_SUMMARY_NODE_LABELS]
                   if isinstance(n, dict)]
    topo_links = [lk for lk in ((result.topology or {}).get("links", []) or [])
                  if isinstance(lk, dict)]
    id_labels = {str(n.get("id") or ""): str(n.get("label") or "")
                 for n in topo_nodes if isinstance(n, dict) and n.get("id")}
    link_eps = []
    for lk in topo_links[:MAX_SUMMARY_LINKS]:
        ep_a, ep_b = _link_endpoints(lk, id_labels)
        link_eps.append(f"{ep_a}<->{ep_b}")
    return {
        "host": result.host,
        "version": result.version,
        "ready": result.ready,
        "node_definition_count": (len(result.node_definitions)
                                  if result.node_definitions is not None else None),
        "image_definition_count": (len(result.image_definitions)
                                   if result.image_definitions is not None else None),
        "lab_count": len(result.labs) if result.labs is not None else None,
        "topology_lab": (result.topology or {}).get("lab_id"),
        "topology_lab_title": result.lab_title or None,
        "topology_node_count": len(topo_nodes) if result.topology else None,
        "topology_node_labels": node_labels or None,
        "topology_link_count": len(topo_links) if result.topology else None,
        "topology_links": link_eps or None,
        "ok": result.ok,
        "partial": result.partial,
        "error_code": result.error_code,
        "unavailable": list(result.unavailable),
        "collected_at": result.collected_at,
    }
