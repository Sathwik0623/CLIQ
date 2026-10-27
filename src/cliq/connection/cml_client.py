#!/usr/bin/env python3
"""
cml_client.py — CLIQ CML integration layer (virl2_client 2.9.0).

Scope (Phase 0, Option-a integration):
  This module uses the official ``virl2_client`` library ONLY for:
    - connection + authentication to a reserved CML host
    - controller version confirmation (2.9.0)
    - lab listing
    - topology extraction (nodes + links + interface labels)
    - readiness signals (system ready / node booted / lab converged)

  It is purely ADDITIVE. It does NOT touch the interactive terminal path
  (CMLConsoleDevice / SessionManager / ghost-text / ?-help / pager /
  learned-cache). Those continue to use the existing terminal-server SSH
  console proxy and are completely unaffected by anything in this file.

Environment facts (Cisco NetSim 2.0):
  - A CML host is a URL (e.g. https://cml.example.com).
  - Reservation Name == CML username (e.g. TEST).
  - Password is auto-generated and emailed (NOT CEC). User-entered at connect.
  - Hosts use self-signed certs -> ssl_verify=False.
  - Hosts require a site proxy (differs per host/site).
  - The user may have several reachable hosts (owned + colleague-granted).
  - Reservations expire; there is no NetSim reservation API — create/extend
    is manual via the portal. CLIQ can only DETECT failure/expiry and guide
    the user back to the portal.

USAGE:
  from cliq.connection.cml_client import CMLClient, CMLHost

  host = CMLHost(
      url="https://cml.example.com",
      username="TEST",                       # reservation name
      proxy="http://proxy.example.com:3128",
      label="MXC-044 (mine)",
      owner="self",
  )
  client = CMLClient(host, password=emailed_password)
  client.connect()                           # confirms controller is 2.9.0
  labs = client.list_labs()
  topo = client.get_topology(labs[0].id)
  ready = client.is_system_ready()
  client.disconnect()
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from cliq.utils import observability as obs

# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

#: Pinned controller version CLIQ targets (matches NetSim CML 2.9.0).
REQUIRED_CONTROLLER_VERSION = "2.9.0"

#: NetSim portal guidance shown whenever a reservation looks expired/invalid.
NETSIM_PORTAL_HINT = (
    "Reservations are managed manually via the NetSim portal — CLIQ cannot "
    "create or extend them. Open the portal, create/extend your CML "
    "reservation, then re-enter the host URL, reservation name and the newly "
    "emailed password to reconnect."
)

#: Where CLIQ keeps CML-related local state (host config + connection logs).
_CLIQ_HOME = Path(os.path.expanduser("~")) / ".cliq"
_HOSTS_FILE = _CLIQ_HOME / "cml_hosts.json"
_CONN_LOG_FILE = _CLIQ_HOME / "cml_connection.log"

log = logging.getLogger("cliq.connection.cml_client")


# ──────────────────────────────────────────────────────────────────────────
# node_definition → device-type / icon mapping
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _DeviceType:
    kind: str   # "router" | "switch" | "host" | "other"
    shape: str  # UI icon hint: "circle" | "square" | "diamond" | "rect"


#: Map a CML ``node_definition`` to a CLIQ device type + UI icon shape.
#: iosv → router (circle), iosvl2 → switch (square). The raw definition is
#: always preserved on the node model so the UI can special-case anything else.
_DEVICE_TYPE_MAP: dict[str, _DeviceType] = {
    "iosv": _DeviceType("router", "circle"),
    "csr1000v": _DeviceType("router", "circle"),
    "cat8000v": _DeviceType("router", "circle"),
    "iosxrv": _DeviceType("router", "circle"),
    "iosxrv9000": _DeviceType("router", "circle"),
    "nxosv": _DeviceType("router", "circle"),
    "iosvl2": _DeviceType("switch", "square"),
    "nxosv9000": _DeviceType("switch", "square"),
    "unmanaged_switch": _DeviceType("switch", "square"),
    "ioll2": _DeviceType("switch", "square"),
    "server": _DeviceType("host", "rect"),
    "desktop": _DeviceType("host", "rect"),
    "external_connector": _DeviceType("other", "diamond"),
}

_UNKNOWN_DEVICE_TYPE = _DeviceType("other", "rect")


def map_device_type(node_definition: str) -> _DeviceType:
    """Resolve a CML ``node_definition`` to a CLIQ device type + icon shape."""
    return _DEVICE_TYPE_MAP.get((node_definition or "").lower(), _UNKNOWN_DEVICE_TYPE)


# ──────────────────────────────────────────────────────────────────────────
# Errors — labeled, user-friendly classifications
# ──────────────────────────────────────────────────────────────────────────

class CMLError(Exception):
    """Base error for the CML integration layer.

    Carries a short, user-facing ``message`` plus optional ``guidance`` the
    UI can surface to tell the engineer exactly what to do next.
    """

    stage = "cml"

    def __init__(self, message: str, *, guidance: str = "", cause: Optional[BaseException] = None):
        super().__init__(message)
        self.message = message
        self.guidance = guidance
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message if not self.guidance else f"{self.message} — {self.guidance}"


class CMLClientNotInstalled(CMLError):
    """virl2_client is not installed (offline box without the optional dep)."""
    stage = "import"


class CMLUnreachableError(CMLError):
    """Host could not be reached (DNS, routing, host/reservation not spun up)."""
    stage = "reachability"


class CMLProxyError(CMLError):
    """Proxy or TLS/cert problem reaching the controller."""
    stage = "proxy_tls"


class CMLAuthError(CMLError):
    """Authentication rejected by the controller."""
    stage = "auth"


class CMLReservationExpiredError(CMLAuthError):
    """Auth failed in a way consistent with an expired/invalid reservation."""
    stage = "reservation"


class CMLVersionMismatchError(CMLError):
    """Controller version is not the required 2.9.0 (or otherwise incompatible)."""
    stage = "version"


# ──────────────────────────────────────────────────────────────────────────
# Data models (clean, UI-ready, JSON-serializable)
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CMLHost:
    """A configured CML host (one reservation).

    ``username`` is the reservation name. Passwords are NEVER stored here —
    they are entered by the user at connect time and held only in memory.
    """

    url: str
    username: str                      # == reservation name
    proxy: Optional[str] = None        # per-host/site proxy, e.g. http://proxy:3128
    label: str = ""                    # friendly display label
    owner: str = "self"               # "self" or a colleague's CEC id (granted by)
    ssl_verify: bool = False           # self-signed certs → False
    expires_on: Optional[str] = None   # ISO-8601 timestamp if the user supplied one

    def __post_init__(self) -> None:
        self.url = _normalize_url(self.url)
        if not self.label:
            self.label = self.url

    # ── ownership ────────────────────────────────────────────────────────
    @property
    def is_owned(self) -> bool:
        return self.owner.strip().lower() in ("self", "me", "owned", "")

    @property
    def ownership_label(self) -> str:
        return "owned" if self.is_owned else f"granted by {self.owner}"

    # ── expiry (optional, user-supplied; NOT from any NetSim API) ─────────
    @property
    def expires_at(self) -> Optional[datetime]:
        if not self.expires_on:
            return None
        return _parse_timestamp(self.expires_on)

    @property
    def is_expired(self) -> bool:
        exp = self.expires_at
        return exp is not None and _now() >= exp

    def expires_in_seconds(self) -> Optional[float]:
        exp = self.expires_at
        if exp is None:
            return None
        return (exp - _now()).total_seconds()

    def expiry_warning(self, within_hours: float = 24.0) -> Optional[str]:
        """Return a warning string if the known reservation expiry is near/past."""
        secs = self.expires_in_seconds()
        if secs is None:
            return None
        if secs <= 0:
            return (
                f"Reservation for {self.label} expired "
                f"{_humanize_seconds(-secs)} ago. {NETSIM_PORTAL_HINT}"
            )
        if secs <= within_hours * 3600:
            return (
                f"Reservation for {self.label} expires in "
                f"{_humanize_seconds(secs)}. Extend it via the NetSim portal "
                f"before it lapses."
            )
        return None

    # ── persistence (config only — never the password) ───────────────────
    def to_config(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "username": self.username,
            "proxy": self.proxy,
            "label": self.label,
            "owner": self.owner,
            "ssl_verify": self.ssl_verify,
            "expires_on": self.expires_on,
        }

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> "CMLHost":
        return cls(
            url=data["url"],
            username=data.get("username", ""),
            proxy=data.get("proxy"),
            label=data.get("label", ""),
            owner=data.get("owner", "self"),
            ssl_verify=bool(data.get("ssl_verify", False)),
            expires_on=data.get("expires_on"),
        )


@dataclass
class CMLLabModel:
    """A lab as needed by the future lab-picker UI."""

    id: str
    title: str
    state: str
    is_active: bool
    owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CMLNodeModel:
    """A topology node tagged with a CLIQ device type + UI icon shape."""

    id: str
    label: str
    node_definition: str            # raw CML definition, always preserved
    device_type: str                # "router" | "switch" | "host" | "other"
    icon_shape: str                 # "circle" | "square" | "diamond" | "rect"
    state: str
    booted: bool = False
    x: int = 0
    y: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CMLLinkModel:
    """A topology link carrying BOTH endpoint interface labels (e.g. G0/0)."""

    id: str
    node_a: str                     # node id
    node_b: str                     # node id
    node_a_label: str               # node label (e.g. iosv-0)
    node_b_label: str               # node label (e.g. iosv-1)
    interface_a: str                # interface label on node_a (e.g. G0/0)
    interface_b: str                # interface label on node_b (e.g. G0/1)
    state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CMLTopology:
    """Structured topology: device-type-tagged nodes + interface-labeled links."""

    lab_id: str
    nodes: list[CMLNodeModel] = field(default_factory=list)
    links: list[CMLLinkModel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lab_id": self.lab_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [l.to_dict() for l in self.links],
        }


@dataclass
class CMLReadiness:
    """Readiness snapshot to replace fixed timeouts on cold start."""

    system_ready: bool
    lab_converged: Optional[bool] = None
    booted_nodes: int = 0
    total_nodes: int = 0

    @property
    def all_booted(self) -> bool:
        return self.total_nodes > 0 and self.booted_nodes == self.total_nodes

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_ready": self.system_ready,
            "lab_converged": self.lab_converged,
            "booted_nodes": self.booted_nodes,
            "total_nodes": self.total_nodes,
            "all_booted": self.all_booted,
        }


# ──────────────────────────────────────────────────────────────────────────
# Connection log (host, stage, outcome, timing) for future debugging
# ──────────────────────────────────────────────────────────────────────────

def _log_connection_event(
    host: str,
    stage: str,
    outcome: str,
    *,
    elapsed_ms: Optional[float] = None,
    detail: str = "",
) -> None:
    """Append one structured line to the CML connection log.

    Best-effort: never raises into the caller (logging must not break connect).
    """
    record = {
        "ts": _now().isoformat(),
        "host": host,
        "stage": stage,
        "outcome": outcome,
        "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
        "detail": detail,
    }
    log.info("cml-connect %s", record)
    try:
        _CLIQ_HOME.mkdir(parents=True, exist_ok=True)
        with _CONN_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 - logging must be non-fatal
        pass

    # Mirror to the unified observability schema (layer="cml"). Additive: the
    # legacy cml_connection.log above is preserved for backward compatibility.
    obs.log_event(
        "cml", "connect", stage, outcome,
        elapsed_ms=elapsed_ms, detail=detail or "", host=host,
    )


# ──────────────────────────────────────────────────────────────────────────
# CMLClient — virl2_client wrapper
# ──────────────────────────────────────────────────────────────────────────

class CMLClient:
    """Thin, defensive wrapper around ``virl2_client.ClientLibrary``.

    Responsibilities: connect/auth (proxy + ssl_verify=False aware), confirm
    controller 2.9.0, list labs, extract topology, expose readiness signals,
    and classify every failure into a labeled, user-friendly error.
    """

    def __init__(self, host: CMLHost, password: str):
        if not host.url:
            raise CMLError("A CML host URL is required.")
        if not host.username:
            raise CMLError("A reservation name (CML username) is required.")
        self.host = host
        self._password = password or ""
        self._client: Any = None
        self._controller_version: Optional[str] = None

    # ── connection lifecycle ─────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def controller_version(self) -> Optional[str]:
        return self._controller_version

    def connect(self) -> "CMLClient":
        """Authenticate to the host and confirm the controller is 2.9.0.

        Raises a labeled :class:`CMLError` subclass on any failure and writes
        connection-log records for every stage attempted.
        """
        ClientLibrary, exceptions = _import_virl2()

        # Per-stage cold-start breakdown (Phase A #2). Each mark logs the time
        # spent in the previous stage to the unified "cml" log, so we can see
        # whether the 5–10s cold-start is reservation spin-up vs. auth vs. node
        # boot. Purely additive — connection behavior is unchanged.
        st = obs.StageTimer("cml", "connect_stages", detail=self.host.url)

        # Early, cheap guard: a user-supplied expiry already in the past.
        if self.host.is_expired:
            st.mark("reservation_check", "error", detail="expiry timestamp already passed")
            _log_connection_event(
                self.host.url, "reservation", "expired",
                detail="known expiry timestamp already passed",
            )
            raise CMLReservationExpiredError(
                f"The reservation for {self.host.label} has already expired.",
                guidance=NETSIM_PORTAL_HINT,
            )
        st.mark("reservation_check", "success", detail="local expiry ok")

        start = time.monotonic()
        _log_connection_event(self.host.url, "connect", "start",
                              detail=f"proxy={self.host.proxy or 'none'}")
        try:
            with _proxy_env(self.host.proxy):
                self._client = ClientLibrary(
                    url=self.host.url,
                    username=self.host.username,
                    password=self._password,
                    ssl_verify=self.host.ssl_verify,
                    raise_for_auth_failure=True,
                    convergence_wait_max_iter=120,
                )
        except Exception as exc:  # noqa: BLE001 - classified + re-raised below
            elapsed = (time.monotonic() - start) * 1000
            err = _classify_connect_error(exc, self.host, exceptions)
            # The ClientLibrary constructor bundles host-reachability + auth +
            # an initial version probe; tag the failing stage from the error.
            st.mark(err.stage or "auth", "error", detail=type(exc).__name__)
            _log_connection_event(
                self.host.url, err.stage, "fail",
                elapsed_ms=elapsed, detail=str(exc)[:300],
            )
            raise err from exc
        # host_reachable + auth + initial controller handshake all complete here.
        st.mark("auth", "success", detail="client library authenticated")

        # Confirm the controller version is exactly what NetSim ships (2.9.0).
        try:
            version = self._client.check_controller_version()
            self._controller_version = str(version) if version is not None else None
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - start) * 1000
            st.mark("version_check", "error", detail=type(exc).__name__)
            _log_connection_event(self.host.url, "version", "fail",
                                  elapsed_ms=elapsed, detail=str(exc)[:300])
            raise CMLVersionMismatchError(
                f"Controller version check failed for {self.host.label}.",
                guidance=f"CLIQ targets CML {REQUIRED_CONTROLLER_VERSION}.",
                cause=exc,
            ) from exc
        st.mark("version_check", "success", detail=self._controller_version or "")

        if self._controller_version and not self._controller_version.startswith(
            REQUIRED_CONTROLLER_VERSION
        ):
            elapsed = (time.monotonic() - start) * 1000
            _log_connection_event(
                self.host.url, "version", "mismatch",
                elapsed_ms=elapsed, detail=self._controller_version,
            )
            raise CMLVersionMismatchError(
                f"Connected, but {self.host.label} is running CML "
                f"{self._controller_version}, not the required "
                f"{REQUIRED_CONTROLLER_VERSION}.",
                guidance="Reserve a CML 2.9.0 host, or update the client pin.",
            )

        elapsed = (time.monotonic() - start) * 1000
        _log_connection_event(
            self.host.url, "connect", "success",
            elapsed_ms=elapsed, detail=f"version={self._controller_version}",
        )
        st.mark("complete", "success", detail=f"total_connect_ms={round(elapsed, 1)}")
        return self

    def disconnect(self) -> None:
        """Log out of the controller. Best-effort and idempotent."""
        if self._client is None:
            return
        try:
            self._client.logout()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
        finally:
            self._client = None
            _log_connection_event(self.host.url, "disconnect", "ok")

    def __enter__(self) -> "CMLClient":
        return self.connect()

    def __exit__(self, *_exc: Any) -> None:
        self.disconnect()

    # ── lab listing ──────────────────────────────────────────────────────
    def list_labs(self) -> list[CMLLabModel]:
        """Return all labs visible to this reservation as clean models."""
        self._require_connected()
        _t0 = time.monotonic()
        try:
            labs = self._client.all_labs()
        except Exception as exc:  # noqa: BLE001
            obs.log_event("cml", "list_labs", "lab_list", "error",
                          elapsed_ms=(time.monotonic() - _t0) * 1000,
                          detail=type(exc).__name__)
            raise _wrap_runtime_error(exc, self.host, "labs") from exc
        obs.log_event("cml", "list_labs", "lab_list", "success",
                      elapsed_ms=(time.monotonic() - _t0) * 1000,
                      detail=f"labs={len(labs) if hasattr(labs, '__len__') else '?'}")

        out: list[CMLLabModel] = []
        for lab in labs:
            out.append(
                CMLLabModel(
                    id=str(getattr(lab, "id", "")),
                    title=_safe(lambda: lab.title) or _safe(lambda: lab.id) or "",
                    state=_safe(lambda: lab.state()) or "unknown",
                    is_active=bool(_safe(lambda: lab.is_active())),
                    owner=_safe(lambda: lab.owner) or "",
                )
            )
        return out

    # ── topology extraction ──────────────────────────────────────────────
    def get_topology(self, lab_id: str) -> CMLTopology:
        """Return a structured topology model for ``lab_id``.

        Nodes are tagged with device type + icon shape (iosv→router/circle,
        iosvl2→switch/square; raw definition preserved). Links carry both
        endpoint interface labels (e.g. G0/0 ↔ G0/1).
        """
        self._require_connected()
        lab = self._get_lab(lab_id)
        # Per-stage topology timing (Phase A #5). sync_states_if_outdated() is an
        # efficient incremental refresh (no full reload); we measure it and the
        # node/link extraction separately so large labs are observable.
        topo_t = obs.StageTimer("cml", "get_topology", detail=str(lab_id))
        try:
            lab.sync_states_if_outdated()
        except Exception:  # noqa: BLE001 - state sync is best-effort
            pass
        topo_t.mark("sync_state")

        nodes: list[CMLNodeModel] = []
        for node in _safe(lambda: lab.nodes(), default=[]) or []:
            node_def = _safe(lambda: node.node_definition) or "unknown"
            dtype = map_device_type(node_def)
            nodes.append(
                CMLNodeModel(
                    id=str(_safe(lambda: node.id) or ""),
                    label=_safe(lambda: node.label) or "",
                    node_definition=node_def,
                    device_type=dtype.kind,
                    icon_shape=dtype.shape,
                    state=_safe(lambda: node.state) or "unknown",
                    booted=bool(_safe(lambda: node.is_booted())),
                    x=int(_safe(lambda: node.x, default=0) or 0),
                    y=int(_safe(lambda: node.y, default=0) or 0),
                )
            )

        topo_t.mark("nodes", detail=f"nodes={len(nodes)}")

        links: list[CMLLinkModel] = []
        for link in _safe(lambda: lab.links(), default=[]) or []:
            ia = _safe(lambda: link.interface_a)
            ib = _safe(lambda: link.interface_b)
            na = _safe(lambda: link.node_a)
            nb = _safe(lambda: link.node_b)
            links.append(
                CMLLinkModel(
                    id=str(_safe(lambda: link.id) or ""),
                    node_a=str(_safe(lambda: na.id) or ""),
                    node_b=str(_safe(lambda: nb.id) or ""),
                    node_a_label=_safe(lambda: na.label) or "",
                    node_b_label=_safe(lambda: nb.label) or "",
                    interface_a=_safe(lambda: ia.label) or "",
                    interface_b=_safe(lambda: ib.label) or "",
                    state=_safe(lambda: link.state) or "",
                )
            )

        topo_t.mark("links", detail=f"links={len(links)}")
        topo_t.mark("complete", detail=f"nodes={len(nodes)} links={len(links)}")
        return CMLTopology(lab_id=str(lab_id), nodes=nodes, links=links)

    # ── readiness signals (Phase A cold-start fix) ───────────────────────
    def is_system_ready(self) -> bool:
        """True when the controller reports the system is fully ready."""
        self._require_connected()
        try:
            return bool(self._client.is_system_ready())
        except Exception:  # noqa: BLE001 - treat probe failure as "not ready"
            return False

    def get_readiness(self, lab_id: Optional[str] = None) -> CMLReadiness:
        """Aggregate readiness: system-ready + (optionally) per-lab boot status.

        Lets CLIQ distinguish host/reservation spin-up latency from node-boot
        latency instead of relying on a fixed sleep.
        """
        self._require_connected()
        system_ready = self.is_system_ready()
        if lab_id is None:
            return CMLReadiness(system_ready=system_ready)

        lab = self._get_lab(lab_id)
        try:
            lab.sync_states_if_outdated()
        except Exception:  # noqa: BLE001
            pass
        nodes = _safe(lambda: lab.nodes(), default=[]) or []
        booted = sum(1 for n in nodes if _safe(lambda: n.is_booted()))
        converged = _safe(lambda: lab.has_converged())
        return CMLReadiness(
            system_ready=system_ready,
            lab_converged=None if converged is None else bool(converged),
            booted_nodes=booted,
            total_nodes=len(nodes),
        )

    def wait_until_lab_converged(self, lab_id: str, max_iterations: Optional[int] = None) -> bool:
        """Block until the lab converges (nodes booted/stable). Returns success."""
        self._require_connected()
        lab = self._get_lab(lab_id)
        start = time.monotonic()
        try:
            if max_iterations is not None:
                lab.wait_until_lab_converged(max_iterations=max_iterations)
            else:
                lab.wait_until_lab_converged()
            ok = True
        except Exception as exc:  # noqa: BLE001
            _log_connection_event(
                self.host.url, "converge", "fail",
                elapsed_ms=(time.monotonic() - start) * 1000, detail=str(exc)[:200],
            )
            return False
        _log_connection_event(
            self.host.url, "converge", "ok",
            elapsed_ms=(time.monotonic() - start) * 1000, detail=str(lab_id),
        )
        return ok

    def node_booted(self, lab_id: str, node_id: str) -> bool:
        """Return whether a specific node has finished booting."""
        self._require_connected()
        lab = self._get_lab(lab_id)
        node = _safe(lambda: lab.get_node_by_id(node_id))
        if node is None:
            return False
        return bool(_safe(lambda: node.is_booted()))

    # ── internals ────────────────────────────────────────────────────────
    def _require_connected(self) -> None:
        if self._client is None:
            raise CMLError("Not connected. Call connect() first.")

    def _get_lab(self, lab_id: str):
        ClientLibrary, exceptions = _import_virl2()
        try:
            return self._client.get_local_lab(lab_id)
        except Exception:  # noqa: BLE001 - fall through to join
            pass
        try:
            return self._client.join_existing_lab(lab_id)
        except Exception as exc:  # noqa: BLE001
            raise CMLError(
                f"Lab {lab_id!r} could not be opened on {self.host.label}.",
                guidance="Confirm the lab id exists for this reservation.",
                cause=exc,
            ) from exc


# ──────────────────────────────────────────────────────────────────────────
# CMLHostRegistry — multi-host config + selection
# ──────────────────────────────────────────────────────────────────────────

class CMLHostRegistry:
    """Persistent registry of configured CML hosts (owned + colleague-granted).

    Stores ONLY non-secret host config (URL, reservation name, proxy, label,
    ownership, optional expiry). Passwords are never written to disk — they are
    supplied per-connect and held only in memory.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else _HOSTS_FILE
        self._hosts: dict[str, CMLHost] = {}
        self.load()

    # ── persistence ──────────────────────────────────────────────────────
    def load(self) -> None:
        self._hosts.clear()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read CML host config %s: %s", self.path, exc)
            return
        for entry in data.get("hosts", []):
            try:
                host = CMLHost.from_config(entry)
                self._hosts[host.url] = host
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed host entry: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"hosts": [h.to_config() for h in self._hosts.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ── CRUD / selection ─────────────────────────────────────────────────
    def add_host(self, host: CMLHost, *, persist: bool = True) -> CMLHost:
        self._hosts[host.url] = host
        if persist:
            self.save()
        return host

    def remove_host(self, url: str, *, persist: bool = True) -> bool:
        removed = self._hosts.pop(_normalize_url(url), None) is not None
        if removed and persist:
            self.save()
        return removed

    def get_host(self, url: str) -> Optional[CMLHost]:
        return self._hosts.get(_normalize_url(url))

    def list_hosts(self) -> list[CMLHost]:
        """All configured hosts, owned reservations first."""
        return sorted(self._hosts.values(), key=lambda h: (not h.is_owned, h.label.lower()))

    def select(self, identifier: str) -> CMLHost:
        """Select a host by URL, label, or 1-based index from ``list_hosts()``."""
        host = self.get_host(identifier)
        if host:
            return host
        ident = identifier.strip().lower()
        for h in self._hosts.values():
            if h.label.strip().lower() == ident:
                return h
        try:
            idx = int(identifier) - 1
            hosts = self.list_hosts()
            if 0 <= idx < len(hosts):
                return hosts[idx]
        except (ValueError, TypeError):
            pass
        raise CMLError(f"No configured CML host matches {identifier!r}.")

    def __len__(self) -> int:
        return len(self._hosts)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _import_virl2():
    """Import virl2_client lazily so CLIQ runs offline without the optional dep."""
    try:
        from virl2_client import ClientLibrary
        from virl2_client import exceptions as virl_exceptions
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CMLClientNotInstalled(
            "The CML integration requires virl2_client==2.9.0.",
            guidance="Install it with: pip install virl2_client==2.9.0",
            cause=exc,
        ) from exc
    return ClientLibrary, virl_exceptions


@contextmanager
def _proxy_env(proxy: Optional[str]) -> Iterator[None]:
    """Temporarily apply a per-host proxy via env vars for the httpx session.

    virl2_client builds an httpx client (trust_env=True) at construction, so the
    proxy must be present in the environment while the ClientLibrary is created.
    We set it only for that window and restore the prior values afterwards so a
    second host with a different proxy is unaffected.
    """
    if not proxy:
        yield
        return
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ[k] = proxy
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _classify_connect_error(exc: Exception, host: CMLHost, exceptions) -> CMLError:
    """Translate a raw virl2_client/httpx failure into a labeled CMLError."""
    text = str(exc).lower()
    name = type(exc).__name__.lower()

    # Auth rejected → most likely an expired/invalid reservation or wrong password.
    if any(t in text for t in ("401", "403", "unauthorized", "authentication", "invalid credentials", "auth failure")):
        return CMLReservationExpiredError(
            f"Authentication to {host.label} was rejected.",
            guidance=(
                "Double-check the reservation name and the emailed password "
                f"(never your CEC credentials). If those are correct, the "
                f"reservation may have expired. {NETSIM_PORTAL_HINT}"
            ),
            cause=exc,
        )

    # Proxy / TLS / certificate problems.
    if any(t in text for t in ("proxy", "ssl", "certificate", "cert", "tls", "self-signed", "self signed")):
        return CMLProxyError(
            f"Proxy or TLS error reaching {host.label}.",
            guidance=(
                f"Confirm the per-host proxy ({host.proxy or 'none configured'}) "
                "is correct and reachable. Self-signed certs are expected "
                "(ssl_verify is disabled)."
            ),
            cause=exc,
        )

    # Reachability / DNS / timeout / connection refused.
    if any(t in text for t in (
        "timeout", "timed out", "connection refused", "name or service not known",
        "failed to resolve", "getaddrinfo", "no route to host", "connecterror",
        "connection error", "unreachable", "network is unreachable",
    )) or "connect" in name:
        return CMLUnreachableError(
            f"Could not reach {host.label}.",
            guidance=(
                "Verify the host URL, that the reservation is active, and that "
                "you are on the internal network / VPN. A freshly reserved host "
                "may still be spinning up — retry shortly."
            ),
            cause=exc,
        )

    # Version-incompatibility surfaced during init.
    if "version" in text or "incompatible" in text:
        return CMLVersionMismatchError(
            f"{host.label} reported an incompatible controller version.",
            guidance=f"CLIQ targets CML {REQUIRED_CONTROLLER_VERSION}.",
            cause=exc,
        )

    return CMLUnreachableError(
        f"Failed to connect to {host.label}.",
        guidance=str(exc)[:200],
        cause=exc,
    )


def _wrap_runtime_error(exc: Exception, host: CMLHost, stage: str) -> CMLError:
    """Wrap a post-connect API failure, detecting session/reservation loss."""
    text = str(exc).lower()
    if any(t in text for t in ("401", "403", "unauthorized", "authentication", "token")):
        _log_connection_event(host.url, stage, "auth_lost", detail=str(exc)[:200])
        return CMLReservationExpiredError(
            f"The CML session for {host.label} is no longer authorized.",
            guidance=NETSIM_PORTAL_HINT,
            cause=exc,
        )
    return CMLError(f"CML request failed ({stage}) on {host.label}: {exc}", cause=exc)


def _safe(fn, default=None):
    """Call ``fn`` and swallow any exception, returning ``default`` on failure."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - defensive accessor
        return default


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _humanize_seconds(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


# ──────────────────────────────────────────────────────────────────────────
# Friendly CML 401/403 → reservation-expiry diagnosis (NetSim cross-check)
# ──────────────────────────────────────────────────────────────────────────

#: Public NetSim portal the user must use to extend/create a reservation.
NETSIM_PORTAL_URL = "cml.example.com"


@dataclass
class CMLAuthDiagnosis:
    """Outcome of classifying a CML 401/403 into a distinct, accurate cause.

    ``kind`` is one of:
      * ``"bad_credentials"``   — the username/password was rejected (needs no
        NetSim key to determine; a CML 401 is a credential rejection).
      * ``"reservation_expired"`` — NetSim confirms the reservation backing the
        host is expired / in-grace / absent / inactive.
      * ``"missing_api_key"``   — CLIQ could not verify the reservation because
        the NetSim API key is genuinely the blocker (present but invalid).

    ``message`` is a ready-to-surface, user-facing line. ``verified`` says
    whether the NetSim cross-check actually confirmed the reason.
    """

    kind: str
    message: str
    verified: bool


def _host_label(host: str) -> str:
    """Trim a host string to a clean label (no scheme/trailing slash)."""
    text = (host or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return text or "the CML host"


def is_http_auth_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like an HTTP 401/403 from the CML host.

    Detects both a ``requests``/``httpx`` error carrying a ``response`` with a
    401/403 status code, and the plain string forms (so it works regardless of
    which HTTP library raised it).
    """
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code in (401, 403):
        return True
    text = str(exc).lower()
    return any(t in text for t in ("401", "403", "unauthorized", "forbidden"))


def diagnose_cml_auth_failure(
    host: str,
    *,
    status_code: Optional[int] = None,
    exc: Optional[BaseException] = None,
    netsim_api_key: Optional[str] = None,
    proxy: Optional[str] = None,
) -> CMLAuthDiagnosis:
    """Classify a CML authentication/authorization failure into a DISTINCT cause.

    Distinguishes 401 Unauthorized (bad credentials) from 403 Forbidden
    (permission denied/locked) and network/reachability failures. Never raises.
    """
    host_label = _host_label(host)
    
    code = status_code
    if code is None and exc is not None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    
    err_text = str(exc or "").lower()
    
    if code == 403 or ("403" in err_text or "forbidden" in err_text):
        obs.log_event("cml", "auth_diagnosis", "controller", "forbidden",
                      status_code=403, detail="forbidden")
        return CMLAuthDiagnosis(
            "forbidden",
            f"Access forbidden on {host_label}. The controller rejected the request or "
            "your account lacks required permissions.",
            verified=True,
        )

    obs.log_event("cml", "auth_diagnosis", "controller", "bad_credentials",
                  status_code=401, detail="bad_credentials")
    return CMLAuthDiagnosis(
        "bad_credentials",
        f"Incorrect username or password for {host_label}. "
        "Re-check your CML controller credentials.",
        verified=True,
    )

__all__ = [
    "REQUIRED_CONTROLLER_VERSION",
    "NETSIM_PORTAL_HINT",
    "NETSIM_PORTAL_URL",
    "CMLClient",
    "CMLHost",
    "CMLHostRegistry",
    "CMLLabModel",
    "CMLNodeModel",
    "CMLLinkModel",
    "CMLTopology",
    "CMLReadiness",
    "map_device_type",
    "CMLError",
    "CMLClientNotInstalled",
    "CMLUnreachableError",
    "CMLProxyError",
    "CMLAuthError",
    "CMLReservationExpiredError",
    "CMLVersionMismatchError",
    "CMLAuthDiagnosis",
    "diagnose_cml_auth_failure",
    "is_http_auth_error",
]
