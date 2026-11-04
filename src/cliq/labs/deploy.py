"""
Approval-gated CML deployment (M1.1).

Flow: ``prepare`` (validate → approval ticket) → explicit user approval →
``confirm`` (ticket → background job) → poll job status. Every step uses ONLY
SDK-verified methods:

- ``ClientLibrary.create_lab(title, description, notes)``
- ``Lab.create_node(label, node_definition, x, y, wait=False, ...)``
- ``Lab.create_interface(node, slot=None, wait=False)``
- ``Lab.create_link(iface_a, iface_b, wait=False)``
- ``ClientLibrary.find_labs_by_title(title)`` (read-only collision check)
- ``Lab.state()`` / ``Lab.details()`` (read-only final status)

Hard exclusions (also enforced by tests): no ``start``/``stop``/``wipe``/
``remove``/``delete`` calls, no ``configuration`` payloads, no device
configuration of any kind, no automatic retries of uncertain writes. A failed
step halts the job immediately; already-created elements are reported exactly
(no silent partial state, no automatic cleanup deletes).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cliq.labs.models import ControllerSnapshot, LabBlueprint, ValidationResult
from cliq.labs.validator import validate_blueprint

log = logging.getLogger("cliq.labs.deploy")

#: Approval tickets expire after 15 minutes.
TICKET_TTL_SECONDS = 15 * 60

#: Per-session caps to bound in-memory stores.
MAX_TICKETS_PER_SESSION = 50
MAX_JOBS_PER_SESSION = 50

#: Maximum lab-title length. Aligned with ``LabBlueprint.title`` (128) so a
#: planned blueprint title can never fail deployment preparation on length
#: alone. The CML SDK itself imposes no title limit: ``create_lab`` passes
#: the title straight into the JSON request body, so any controller-side
#: limit would surface as a clean job failure with the controller's message.
LAB_TITLE_MAX_LENGTH = 128

#: Explicit punctuation allowed in lab titles, beyond Unicode letters/digits.
#: Titles travel ONLY in JSON request bodies (lab addressing uses UUIDs), so
#: these characters can never alter a URL path, shell command, or file path.
LAB_TITLE_ALLOWED_PUNCT = frozenset(" -_.&(),:;!'+@#")

#: Unicode dashes normalized to ASCII "-" before validation.
_UNICODE_DASHES = {
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2212: "-",  # minus sign
}

#: Machine-token charset for idempotency keys (strict by design).
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


class DeploymentError(RuntimeError):
    """Raised for expected deployment failures (invalid, expired, refused)."""


def normalize_lab_title(title: str) -> str:
    """Normalize and validate a human-readable controller lab title.

    Normalization (visible to the user via ``lab_title`` /
    ``lab_title_original`` on the approval ticket): strip and collapse
    whitespace runs, map Unicode dashes to "-". Everything else is preserved
    exactly.

    Rejected with :class:`DeploymentError`: non-string/empty input, control
    characters other than tab/newline/CR (which collapse to a space), titles
    longer than 128 characters, anything containing "/" or "\\\\" or ".."
    (path traversal can never be valid in a title), any character outside
    letters/digits plus ``LAB_TITLE_ALLOWED_PUNCT``, and a leading character
    that is not a letter or digit.
    """
    if not isinstance(title, str):
        raise DeploymentError("invalid lab title: title must be a string")
    controls = sorted({ch for ch in title
                       if ch not in "\t\n\r"
                       and unicodedata.category(ch) in ("Cc", "Cf")})
    if controls:
        shown = ", ".join(f"U+{ord(ch):04X}" for ch in controls)
        raise DeploymentError(
            f"invalid lab title: {title!r} "
            f"(control character(s) not allowed: {shown})"
        )
    cleaned = " ".join(title.translate(_UNICODE_DASHES).split())
    if not cleaned:
        raise DeploymentError("invalid lab title: title must not be empty")
    if len(cleaned) > LAB_TITLE_MAX_LENGTH:
        raise DeploymentError(
            "invalid lab title: exceeds 128 characters "
            f"({len(cleaned)} given)"
        )
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise DeploymentError(
            f"invalid lab title: {title!r} "
            "(must not contain path separators or '..')"
        )
    bad = sorted({ch for ch in cleaned
                  if not ch.isalnum() and ch not in LAB_TITLE_ALLOWED_PUNCT})
    if bad:
        shown = ", ".join(repr(ch) for ch in bad)
        raise DeploymentError(
            f"invalid lab title: {title!r} "
            f"(unsupported character(s): {shown})"
        )
    if not cleaned[0].isalnum():
        raise DeploymentError(
            f"invalid lab title: {title!r} "
            "(must start with a letter or digit)"
        )
    return cleaned


def validate_lab_title(title: str) -> str:
    """Validate a lab title (normalizing it). Kept for existing callers."""
    return normalize_lab_title(title)


def validate_idempotency_key(key: str) -> str:
    cleaned = (key or "").strip()
    if not IDEMPOTENCY_KEY_RE.match(cleaned):
        raise DeploymentError("invalid idempotency key")
    return cleaned


def blueprint_hash(blueprint: LabBlueprint) -> str:
    canonical = json.dumps(blueprint.model_dump(), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _truncate(text: str, limit: int = 300) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class ApprovalTicket:
    ticket_id: str
    lab_title: str
    blueprint: LabBlueprint
    digest: str
    validation: ValidationResult
    live: bool
    created_at: float
    expires_at: float
    consumed_by_job_id: Optional[str] = None
    #: Pre-normalization title when normalize_lab_title changed it (so the
    #: approval UI can show exactly what was adjusted); else None.
    lab_title_original: Optional[str] = None

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "lab_title": self.lab_title,
            "lab_title_original": self.lab_title_original,
            "blueprint_hash": self.digest,
            "blueprint": self.blueprint.model_dump(),
            "validation": self.validation.model_dump(),
            "live": self.live,
            "expires_at": self.expires_at,
        }


@dataclass
class DeployStep:
    seq: int
    kind: str  # lab | node | interface | link | status
    label: str
    status: str = "pending"  # pending | running | succeeded | failed | skipped
    controller_id: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "controller_id": self.controller_id,
            "detail": self.detail,
        }


@dataclass
class DeployJob:
    job_id: str
    ticket_id: str
    lab_title: str
    idempotency_key: Optional[str]
    blueprint_hash: str
    status: str = "queued"  # queued | running | succeeded | failed
    steps: list[DeployStep] = field(default_factory=list)
    lab_id: Optional[str] = None
    lab_state: Optional[str] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Requested interface label -> controller-assigned label, per link end.
    interface_map: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ticket_id": self.ticket_id,
            "lab_title": self.lab_title,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "lab_id": self.lab_id,
            "lab_state": self.lab_state,
            "error": self.error,
            "blueprint_hash": self.blueprint_hash,
            "interface_map": dict(self.interface_map),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_LOCK = threading.Lock()
_TICKETS: dict[tuple[str, str], ApprovalTicket] = {}
_JOBS: dict[tuple[str, str], DeployJob] = {}
_JOBS_BY_KEY: dict[tuple[str, str], str] = {}


def _evict_oldest(store: dict, token: str, limit: int) -> None:
    owned = sorted(
        (k for k in store if k[0] == token),
        key=lambda k: getattr(store[k], "created_at", 0.0),
    )
    while len(owned) >= limit:
        store.pop(owned.pop(0), None)


def prepare_deployment(
    token: str,
    blueprint: LabBlueprint,
    *,
    live: bool = False,
    snapshot: Optional[ControllerSnapshot] = None,
    lab_title: Optional[str] = None,
) -> ApprovalTicket:
    """Validate a blueprint and mint an approval ticket. No CML writes.

    Raises :class:`DeploymentError` if the blueprint is invalid — an invalid
    blueprint is never ticketed and can therefore never be deployed.
    """
    result = validate_blueprint(blueprint, snapshot=snapshot)
    if not result.valid:
        first = result.errors[0] if result.errors else None
        detail = first.message if first else "blueprint invalid"
        raise DeploymentError(
            f"refusing to ticket invalid blueprint: {_truncate(detail)}"
        )
    raw_title = lab_title or blueprint.title
    title = validate_lab_title(raw_title)
    stripped = raw_title.strip()
    original = stripped if stripped != title else None
    now = time.time()
    ticket = ApprovalTicket(
        ticket_id=_new_id("ticket"),
        lab_title=title,
        lab_title_original=original,
        blueprint=blueprint,
        digest=blueprint_hash(blueprint),
        validation=result,
        live=live,
        created_at=now,
        expires_at=now + TICKET_TTL_SECONDS,
    )
    with _LOCK:
        _evict_oldest(_TICKETS, token, MAX_TICKETS_PER_SESSION)
        _TICKETS[(token, ticket.ticket_id)] = ticket
    return ticket


def get_ticket(token: str, ticket_id: str) -> Optional[ApprovalTicket]:
    with _LOCK:
        return _TICKETS.get((token, ticket_id))


def get_job(token: str, job_id: str) -> Optional[DeployJob]:
    with _LOCK:
        job = _JOBS.get((token, job_id))
        if job is None:
            return None
        # Return a snapshot copy so callers cannot mutate the store.
        snapshot = DeployJob(
            job_id=job.job_id, ticket_id=job.ticket_id, lab_title=job.lab_title,
            idempotency_key=job.idempotency_key, blueprint_hash=job.blueprint_hash,
            status=job.status, lab_id=job.lab_id, lab_state=job.lab_state,
            error=job.error, created_at=job.created_at, updated_at=job.updated_at,
        )
        snapshot.steps = [DeployStep(**s.to_dict()) for s in job.steps]
        snapshot.interface_map = dict(job.interface_map)
        return snapshot


def _update_job(token: str, job: DeployJob) -> None:
    job.updated_at = time.time()
    with _LOCK:
        _JOBS[(token, job.job_id)] = job


def build_client_library(host: str, username: str, password: str) -> Any:
    """Build a real SDK client for the caller's controller session.

    Uses only the credentials already held in memory for this CLIQ session.
    Raises :class:`DeploymentError` when the SDK is unavailable.
    """
    try:
        from virl2_client import ClientLibrary
    except ImportError as exc:
        raise DeploymentError(
            "deployment requires virl2_client==2.9.0 in the backend environment"
        ) from exc
    return ClientLibrary(
        url=f"https://{host}",
        username=username,
        password=password,
        ssl_verify=False,
        raise_for_auth_failure=True,
    )


ClientFactory = Callable[[str, str, str], Any]


def confirm_deployment(
    token: str,
    ticket_id: str,
    *,
    idempotency_key: Optional[str] = None,
    lab_title: Optional[str] = None,
    allow_offline: bool = False,
    client_factory: ClientFactory,
    session_host: str,
    session_username: str,
    session_password: str,
) -> tuple[DeployJob, bool]:
    """Confirm a ticket and launch the deployment job.

    Returns ``(job, created)``: ``created`` is False when an existing job is
    returned for a repeated idempotency key or an already-consumed ticket —
    in which case NO new lab is created.
    """
    key = validate_idempotency_key(idempotency_key) if idempotency_key else None
    with _LOCK:
        if key is not None and (token, key) in _JOBS_BY_KEY:
            existing = _JOBS.get((token, _JOBS_BY_KEY[(token, key)]))
            if existing is not None:
                return existing, False
        ticket = _TICKETS.get((token, ticket_id))
        if ticket is None:
            raise DeploymentError("approval ticket not found")
        if ticket.expired():
            raise DeploymentError("approval ticket expired")
        if ticket.consumed_by_job_id is not None:
            existing = _JOBS.get((token, ticket.consumed_by_job_id))
            if existing is not None:
                return existing, False
            raise DeploymentError("approval ticket already consumed")

    title = validate_lab_title(lab_title or ticket.lab_title)
    if not ticket.live and not allow_offline:
        raise DeploymentError(
            "deployment requires live-controller validation; this ticket was "
            "prepared offline. Re-validate against the live controller first."
        )
    if not ticket.live and allow_offline:
        log.warning(
            "OFFLINE DEPLOYMENT OVERRIDE ticket=%s title=%s",
            ticket.ticket_id, title,
        )
    job = DeployJob(
        job_id=_new_id("job"),
        ticket_id=ticket.ticket_id,
        lab_title=title,
        idempotency_key=key,
        blueprint_hash=ticket.digest,
    )
    with _LOCK:
        _evict_oldest(_JOBS, token, MAX_JOBS_PER_SESSION)
        _JOBS[(token, job.job_id)] = job
        if key is not None:
            _JOBS_BY_KEY[(token, key)] = job.job_id
        ticket.consumed_by_job_id = job.job_id

    worker = threading.Thread(
        target=_run_job,
        args=(token, job.job_id, ticket, title, client_factory,
              session_host, session_username, session_password),
        daemon=True,
        name=f"cliq-deploy-{job.job_id}",
    )
    worker.start()
    snapshot = get_job(token, job.job_id)
    assert snapshot is not None
    return snapshot, True


def _step(job: DeployJob, kind: str, label: str) -> DeployStep:
    step = DeployStep(seq=len(job.steps) + 1, kind=kind, label=label,
                      status="running")
    job.steps.append(step)
    return step


def _fail(job: DeployJob, step: Optional[DeployStep], message: str) -> None:
    if step is not None:
        step.status = "failed"
        step.detail = _truncate(message)
    job.status = "failed"
    job.error = _truncate(message)


def _run_job(
    token: str,
    job_id: str,
    ticket: ApprovalTicket,
    lab_title: str,
    client_factory: ClientFactory,
    host: str,
    username: str,
    password: str,
) -> None:
    """Execute the deployment. Any failure halts immediately; no retries."""
    with _LOCK:
        job = _JOBS.get((token, job_id))
        if job is None:
            return
        job.status = "running"

    # Defense in depth: re-normalize the title before any controller contact.
    # Confirm already normalized it, so this is normally a no-op; a failure
    # here fails the job WITHOUT creating anything.
    try:
        lab_title = normalize_lab_title(lab_title)
    except DeploymentError as exc:
        with _LOCK:
            job = _JOBS.get((token, job_id))
            if job is None:
                return
        _fail(job, None, f"refusing to deploy: {exc}")
        _update_job(token, job)
        return

    try:
        try:
            client = client_factory(host, username, password)
        except DeploymentError as exc:
            _fail(job, None, str(exc))
            return
        except Exception as exc:
            _fail(job, None, f"controller connection failed: {exc}")
            return
        finally:
            _update_job(token, job)

        # 1. Collision check (read-only): never create a duplicate-titled lab.
        step = _step(job, "lab", f"check title '{lab_title}'")
        _update_job(token, job)
        try:
            existing = client.find_labs_by_title(lab_title)
        except Exception as exc:
            _fail(job, step, f"lab title check failed: {exc}")
            _update_job(token, job)
            return
        if existing:
            _fail(job, step,
                  f"lab titled '{lab_title}' already exists; choose another title")
            _update_job(token, job)
            return
        step.status = "succeeded"
        step.detail = "title available"
        _update_job(token, job)

        # 2. Create the lab.
        step = _step(job, "lab", f"create lab '{lab_title}'")
        _update_job(token, job)
        try:
            lab = client.create_lab(
                title=lab_title,
                description="Created by CLIQ Lab Copilot",
                notes=f"CLIQ deploy job {job_id}",
            )
        except Exception as exc:
            _fail(job, step, f"lab creation failed: {exc}")
            _update_job(token, job)
            return
        job.lab_id = lab.id
        step.status = "succeeded"
        step.controller_id = lab.id
        _update_job(token, job)

        # 3. Create nodes in blueprint order.
        node_by_blueprint: dict[str, Any] = {}
        for node_spec in ticket.blueprint.nodes:
            step = _step(job, "node", f"create node '{node_spec.label}'")
            _update_job(token, job)
            kwargs: dict[str, Any] = {"tags": ["cliq"]}
            if node_spec.image_definition:
                kwargs["image_definition"] = node_spec.image_definition
            if node_spec.vcpus is not None:
                kwargs["cpus"] = node_spec.vcpus
            if node_spec.ram_mb is not None:
                kwargs["ram"] = node_spec.ram_mb
            try:
                node = lab.create_node(
                    node_spec.label, node_spec.node_definition,
                    node_spec.x, node_spec.y, wait=False, **kwargs)
            except Exception as exc:
                _fail(job, step, f"node '{node_spec.label}' failed: {exc}")
                _update_job(token, job)
                return
            node_by_blueprint[node_spec.id] = node
            step.status = "succeeded"
            step.controller_id = node.id
            _update_job(token, job)

        # 4. Create one interface per link endpoint, then the link.
        for link_spec in ticket.blueprint.links:
            node_a = node_by_blueprint.get(link_spec.node_a)
            node_b = node_by_blueprint.get(link_spec.node_b)
            if node_a is None or node_b is None:  # pragma: no cover - validated
                _fail(job, None, f"link '{link_spec.id}' has unknown endpoint")
                _update_job(token, job)
                return
            step = _step(job, "interface",
                         f"create {link_spec.node_a}:{link_spec.interface_a}")
            _update_job(token, job)
            try:
                iface_a = lab.create_interface(node_a, wait=False)
            except Exception as exc:
                _fail(job, step, f"interface for '{link_spec.id}' failed: {exc}")
                _update_job(token, job)
                return
            step.status = "succeeded"
            step.controller_id = iface_a.id
            job.interface_map[f"{link_spec.id}:a"] = iface_a.label
            _update_job(token, job)

            step = _step(job, "interface",
                         f"create {link_spec.node_b}:{link_spec.interface_b}")
            _update_job(token, job)
            try:
                iface_b = lab.create_interface(node_b, wait=False)
            except Exception as exc:
                _fail(job, step, f"interface for '{link_spec.id}' failed: {exc}")
                _update_job(token, job)
                return
            step.status = "succeeded"
            step.controller_id = iface_b.id
            job.interface_map[f"{link_spec.id}:b"] = iface_b.label
            _update_job(token, job)

            step = _step(job, "link", f"create link '{link_spec.id}'")
            _update_job(token, job)
            try:
                link = lab.create_link(iface_a, iface_b, wait=False)
            except Exception as exc:
                _fail(job, step, f"link '{link_spec.id}' failed: {exc}")
                _update_job(token, job)
                return
            step.status = "succeeded"
            step.controller_id = link.id
            _update_job(token, job)

        # 5. Final read-only status (best-effort; never fails the job).
        step = _step(job, "status", "read lab state")
        _update_job(token, job)
        try:
            job.lab_state = lab.state()
            step.detail = f"state={job.lab_state}"
        except Exception as exc:
            step.detail = f"state unreadable: {_truncate(str(exc), 120)}"
        step.status = "succeeded"
        job.status = "succeeded"
        _update_job(token, job)
    except Exception as exc:  # pragma: no cover - defensive
        log.error("deploy job %s crashed: %s", job_id, exc)
        with _LOCK:
            job = _JOBS.get((token, job_id))
            if job is not None and job.status in ("queued", "running"):
                _fail(job, None, f"deployment error: {exc}")
                _update_job(token, job)
