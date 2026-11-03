"""
Live read-only CML controller discovery (M1.1).

Queries ONLY SDK-verified GET endpoints through the caller's own
authenticated ``CMLAuthManager`` session:

- ``GET node_definitions`` — virl2_client
  ``NodeImageDefinitions.node_definitions()``
- ``GET image_definitions`` — ``image_definitions()``
- ``GET node_definitions/{id}/image_definitions`` —
  ``image_definitions_for_node_definition()``
- ``GET system_stats`` — ``ClientLibrary.get_system_stats()``
- ``GET system_health`` — ``ClientLibrary.get_system_health()``
- ``GET system_information`` — ``ClientLibrary.system_info()``

Strictly read-only: this module issues GET requests only. It performs no
lab/node/link creation, no device configuration, and no destructive calls.
Response shapes are parsed defensively (raw payloads are preserved) because
exact controller response schemas can only be confirmed against a live
controller, which M1.1 has not contacted yet.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cliq.labs.models import (
    NODE_ACTIVE_STATES,
    STATE_BOOTED,
    ControllerSnapshot,
)

log = logging.getLogger("cliq.labs.discovery")

#: Path-segment guard for definition ids interpolated into request URLs.
_DEF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,128}$")

#: Candidate keys for a definition's identifier, in preference order. The SDK
#: returns raw dicts without documenting the id key, so extraction is
#: defensive and the raw dict is always preserved alongside.
_ID_KEYS = ("id", "definition_id", "name")

#: Per-lab bundle budget and overall dashboard budget for ``/available``.
#: Individual controller GETs already time out at 10s; these bounds keep one
#: slow lab (or a large fleet) from stalling the dashboard past the
#: frontend's 120s ceiling. Labs that exceed the budget are returned as
#: Unknown WITH the timeout as their error — never dropped, never Defined.
PER_LAB_TIMEOUT_SECONDS = 55.0
AVAILABLE_OVERALL_TIMEOUT_SECONDS = 100.0


class DiscoveryError(RuntimeError):
    """Raised when live controller discovery fails (transport or shape)."""


def validate_definition_id(definition_id: str) -> str:
    """Validate a definition id for safe use as a URL path segment."""
    cleaned = (definition_id or "").strip()
    if not _DEF_ID_RE.match(cleaned) or ".." in cleaned or "/" in cleaned:
        raise DiscoveryError(f"invalid definition id: {definition_id!r}")
    return cleaned


def validate_lab_id(lab_id: str) -> str:
    """Validate a controller lab id for safe use as a URL path segment."""
    cleaned = (lab_id or "").strip()
    if not _DEF_ID_RE.match(cleaned) or ".." in cleaned or "/" in cleaned:
        raise DiscoveryError(f"invalid lab id: {lab_id!r}")
    return cleaned


def _definition_ids(payload: Any) -> list[str]:
    """Extract definition ids from a definitions-list payload, defensively."""
    items = payload if isinstance(payload, list) else []
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _ID_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
                break
    return ids


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _capacity_from_stats(stats: Any) -> tuple[Optional[int], Optional[int],
                                             Optional[int], Optional[int]]:
    """Best-effort (cpu_total, cpu_used, ram_total_mb, ram_used_mb) extraction.

    The ``system_stats`` schema is SDK-opaque (returned raw); only
    unambiguous integer fields are read, otherwise capacity stays unknown
    (None) rather than invented.
    """
    if not isinstance(stats, dict):
        return None, None, None, None
    lowered = {str(k).lower(): v for k, v in stats.items()}
    cpu_total = _as_int(lowered.get("cpu_total", lowered.get("cpus_total")))
    cpu_used = _as_int(lowered.get("cpu_used", lowered.get("cpus_used")))
    ram_total = _as_int(lowered.get("ram_total_mb", lowered.get("memory_total_mb")))
    ram_used = _as_int(lowered.get("ram_used_mb", lowered.get("memory_used_mb")))
    return cpu_total, cpu_used, ram_total, ram_used


def parse_converged(payload: Any) -> Optional[bool]:
    """Parse ``GET .../check_if_converged`` (SDK returns the raw boolean)."""
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, dict):
        inner = payload.get("converged")
        return inner if isinstance(inner, bool) else None
    return None


def parse_element_nodes(payload: Any) -> tuple[Optional[int], Optional[int],
                                               Optional[int]]:
    """Parse ``GET .../lab_element_state`` into (booted, active, total).

    Shape per SDK ``sync_states``: ``{nodes: {id: state}, ...}`` with plain
    state strings. Unparseable payloads yield ``(None, None, None)``.
    """
    if not isinstance(payload, dict):
        return None, None, None
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return None, None, None
    states = [str(s).strip().upper() for s in nodes.values()]
    booted = sum(1 for s in states if s == STATE_BOOTED)
    active = sum(1 for s in states if s in NODE_ACTIVE_STATES)
    return booted, active, len(states)


@dataclass
class LabSummary:
    """One controller lab with raw state passthrough + best-effort extras.

    ``error`` carries the controller/transport failure when the lab could
    not be read (state is then ``""`` so downstream renders Unknown —
    errors are never converted to Defined). ``nodes_active`` counts nodes
    in the SDK active set (STARTED/QUEUED/BOOTED) for lab-level Running
    evidence; ``nodes_booted`` stays the separate device-readiness signal.
    """

    id: str
    title: str
    state: str
    owner: str = ""
    device_count: Optional[int] = None
    converged: Optional[bool] = None
    nodes_booted: Optional[int] = None
    nodes_active: Optional[int] = None
    nodes_total: Optional[int] = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "owner": self.owner,
            "device_count": self.device_count,
            "converged": self.converged,
            "nodes_booted": self.nodes_booted,
            "nodes_active": self.nodes_active,
            "nodes_total": self.nodes_total,
            "error": self.error,
        }


def _clean_state(value: Any) -> str:
    """Normalize a controller state string; missing/placeholder → ``""``."""
    text = value if isinstance(value, str) else str(value)
    if not text.strip() or text.strip().lower() == "unknown":
        return ""
    return text


def _fetch_lab_bundle(get_json: Callable[[str], Any],
                      lab_id: str) -> LabSummary:
    """Fetch one lab's detail bundle, never dropping the lab silently.

    State comes from the dedicated ``.../state`` endpoint (the same value
    the SDK ``is_active`` check and the CML dashboard reflect), falling back
    to the details ``state`` field when ``/state`` is unreadable. Failures
    are preserved in ``error`` with an empty state (Unknown downstream).
    """
    try:
        safe = validate_lab_id(lab_id)
    except DiscoveryError as exc:
        return LabSummary(id=str(lab_id), title=str(lab_id), state="",
                          error=f"invalid lab id: {exc}")
    try:
        details = get_json(f"/labs/{safe}")
    except Exception as exc:
        return LabSummary(id=safe, title=safe, state="",
                          error=f"lab details unreadable: {exc}")
    if not isinstance(details, dict):
        return LabSummary(id=safe, title=safe, state="",
                          error="lab details unreadable: unexpected shape")
    try:
        state = _clean_state(get_json(f"/labs/{safe}/state"))
    except Exception:
        state = ""
    if not state:
        state = _clean_state(details.get("state", ""))
    error = "" if state else "no readable lab state"
    try:
        nodes_payload = get_json(f"/labs/{safe}/nodes")
    except Exception:
        count: Optional[int] = None
    else:
        if isinstance(nodes_payload, list):
            count = len(nodes_payload)
        elif isinstance(nodes_payload, dict):
            count = len(nodes_payload.keys())
        else:
            count = None
    try:
        converged = parse_converged(get_json(f"/labs/{safe}/check_if_converged"))
    except Exception:
        converged = None
    try:
        booted, active, total = parse_element_nodes(
            get_json(f"/labs/{safe}/lab_element_state"))
    except Exception:
        booted, active, total = None, None, None
    return LabSummary(
        id=safe,
        title=str(details.get("lab_title", safe)),
        state=state,
        owner=str(details.get("owner_username") or details.get("owner", "")),
        device_count=count,
        converged=converged,
        nodes_booted=booted,
        nodes_active=active,
        nodes_total=total,
        error=error,
    )


def list_labs_available(
    get_json: Callable[[str], Any],
    max_workers: int = 4,
    *,
    per_lab_timeout_s: float = PER_LAB_TIMEOUT_SECONDS,
    overall_timeout_s: float = AVAILABLE_OVERALL_TIMEOUT_SECONDS,
) -> list[LabSummary]:
    """List labs with states, counts, convergence and boot progress.

    ``GET /labs`` → per lab ``GET /labs/{id}`` (title/owner/fallback state)
    plus ``.../state`` (authoritative state) and best-effort ``.../nodes``
    (count), ``.../check_if_converged`` and ``.../lab_element_state`` (boot
    progress). Per-lab bundles fetch in parallel (order-preserving) under a
    per-lab timeout and an overall deadline, so one slow lab — or a large
    fleet — cannot stall the dashboard; over-budget labs are returned as
    Unknown WITH the timeout as their error.
    """
    try:
        payload = get_json("/labs")
    except Exception as exc:
        raise DiscoveryError(f"controller lab list failed: {exc}") from exc
    if isinstance(payload, list):
        ids = [i for i in payload if isinstance(i, str)]
    elif isinstance(payload, dict):
        ids = [k for k in payload.keys() if isinstance(k, str)]
    else:
        ids = []

    if not ids:
        return []
    deadline = time.time() + overall_timeout_s
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {lab_id: pool.submit(_fetch_lab_bundle, get_json, lab_id)
                   for lab_id in ids}
        out: list[LabSummary] = []
        for lab_id in ids:  # order-preserving
            remaining = min(per_lab_timeout_s,
                            max(0.1, deadline - time.time()))
            try:
                out.append(futures[lab_id].result(timeout=remaining))
            except concurrent.futures.TimeoutError:
                log.warning("lab bundle timed out: %s", lab_id)
                if remaining < per_lab_timeout_s:
                    detail = "dashboard time budget exhausted"
                else:
                    detail = (f"lab read timed out after "
                              f"{per_lab_timeout_s:.0f}s")
                out.append(LabSummary(id=str(lab_id), title=str(lab_id),
                                      state="", error=detail))
            except Exception as exc:  # never 500 one lab's bundle
                log.warning("lab bundle failed: %s: %s", lab_id, exc)
                out.append(LabSummary(id=str(lab_id), title=str(lab_id),
                                      state="",
                                      error=f"lab read failed: {exc}"))
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@dataclass
class ControllerCatalog:
    """Live read-only controller catalog for one caller session."""

    node_definitions: list[dict[str, Any]] = field(default_factory=list)
    image_definitions: list[dict[str, Any]] = field(default_factory=list)
    images_by_node_definition: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    system_stats: dict[str, Any] = field(default_factory=dict)
    system_health: dict[str, Any] = field(default_factory=dict)
    system_information: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> ControllerSnapshot:
        cpu_total, cpu_used, ram_total, ram_used = _capacity_from_stats(
            self.system_stats
        )
        return ControllerSnapshot(
            node_definitions=_definition_ids(self.node_definitions) or None,
            image_definitions=_definition_ids(self.image_definitions) or None,
            cpu_total=cpu_total,
            cpu_used=cpu_used,
            ram_total_mb=ram_total,
            ram_used_mb=ram_used,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "node_definition_count": len(self.node_definitions),
            "image_definition_count": len(self.image_definitions),
            "controller_version": self.system_information.get("version"),
            "controller_ready": self.system_information.get("ready"),
        }


def discover_controller_catalog(
    get_json: Callable[[str], Any],
    *,
    include_per_definition_images: bool = False,
    max_definitions: int = 512,
) -> ControllerCatalog:
    """Fetch the live catalog using only GET requests.

    :param get_json: ``path -> parsed JSON`` adapter bound to the caller's
        authenticated controller session (e.g. around
        ``CMLAuthManager._get``). Must only ever issue GET.
    :param include_per_definition_images: also fetch
        ``node_definitions/{id}/image_definitions`` per definition.
    """
    try:
        node_defs = get_json("/node_definitions")
        image_defs = get_json("/image_definitions")
        try:
            stats = get_json("/system_stats")
        except Exception:
            stats = {}
        try:
            health = get_json("/system_health")
        except Exception:
            health = {}
        try:
            sysinfo = get_json("/system_information")
        except Exception:
            sysinfo = {}
    except DiscoveryError:
        raise
    except Exception as exc:
        raise DiscoveryError(f"controller discovery failed: {exc}") from exc

    catalog = ControllerCatalog(
        node_definitions=node_defs if isinstance(node_defs, list) else [],
        image_definitions=image_defs if isinstance(image_defs, list) else [],
        system_stats=stats if isinstance(stats, dict) else {},
        system_health=health if isinstance(health, dict) else {},
        system_information=sysinfo if isinstance(sysinfo, dict) else {},
    )

    if include_per_definition_images:
        for definition_id in _definition_ids(catalog.node_definitions)[:max_definitions]:
            safe_id = validate_definition_id(definition_id)
            try:
                payload = get_json(f"/node_definitions/{safe_id}/image_definitions")
            except Exception as exc:
                raise DiscoveryError(
                    f"controller discovery failed for {safe_id}: {exc}"
                ) from exc
            catalog.images_by_node_definition[safe_id] = (
                payload if isinstance(payload, list) else []
            )
    return catalog
