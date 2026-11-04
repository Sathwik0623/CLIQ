"""
Lab Copilot REST router (M1 + M1.1).

Endpoints (all read-only, no CML writes):
- ``POST /api/labs/plan`` — natural-language prompt → strict blueprint.
- ``POST /api/labs/validate`` — blueprint (+ optional controller snapshot,
  or ``live=true`` for the caller's live controller data) → verdict.
- ``POST /api/labs/preview`` — blueprint → ReactFlow-compatible nodes/links.
- ``GET /api/labs/catalog`` — live read-only controller catalog for the
  caller's own session (definitions, images, stats, health).
- ``POST /api/labs/deploy/prepare|confirm`` + ``GET /deploy/{job_id}`` —
  approval-gated deployment (confirm is the only CML-writing endpoint).
- ``GET /api/labs/available`` — live lab states, counts, boot progress.
- ``POST /api/labs/{lab_id}/start|stop`` — one lifecycle PUT per
  user-confirmed request; poll ``GET /api/labs/start/{op_id}``.
- ``GET /api/labs/{lab_id}/operation`` — latest tracked operation for one
  lab (read-only recovery after a dead poller).

All endpoints require a valid CLIQ session via the existing
:func:`cliq.web.server.require_session` gate (401 fail-closed) and only ever
use the caller's OWN controller session (session ownership).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cliq.labs import deploy as _deploy
from cliq.labs import lifecycle as _lifecycle
from cliq.labs.deploy import DeploymentError
from cliq.labs.discovery import (
    ControllerCatalog,
    DiscoveryError,
    _definition_ids,
    discover_controller_catalog,
    list_labs_available,
    parse_converged,
    parse_element_nodes,
    validate_definition_id,
    validate_lab_id,
)
from cliq.labs.lifecycle import DASHBOARD_OP_STATUSES, LabSnapshot
from cliq.labs.models import (
    ControllerSnapshot,
    LabBlueprint,
    ValidationIssue,
)
from cliq.labs.planner import PlannerError, plan_lab
from cliq.labs.preview import blueprint_to_reactflow
from cliq.labs.validator import validate_blueprint

router = APIRouter(prefix="/api/labs", tags=["labs"])


def _require_session_dep(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Delegate to the existing ``require_session`` gate.

    Imported lazily because ``cliq.web.server`` imports this router at module
    load; at request time both modules are fully initialized, so this is the
    exact same gate, store, and 401 behavior — not a copy.
    """
    from cliq.web.server import require_session

    return require_session(request, authorization)


def _owned_cml_session(token: str) -> Any:
    """Return the caller's OWN CML session (host + authenticated manager).

    Session ownership: only ``SESSIONS[token]`` is ever touched. SSH-backed
    sessions carry no controller authentication and are rejected with 409.
    """
    from cliq.web.server import SESSIONS

    session = SESSIONS.get(token)
    if session is None or getattr(session, "auth", None) is None:
        raise HTTPException(
            status_code=409,
            detail="session has no authenticated CML controller session",
        )
    return session


def _cml_get_json(auth_manager: Any) -> Callable[[str], Any]:
    """Adapt a ``CMLAuthManager`` to a GET-only JSON getter.

    Reuses the existing authenticated session (Bearer token handling,
    TLS/timeout behavior). Only GET is ever issued; transport failures are
    converted to :class:`DiscoveryError` with short, secret-free messages
    (tokens travel in headers, never in URLs or these messages).
    """

    def _get(path: str) -> Any:
        try:
            return auth_manager._get(path).json()
        except DiscoveryError:
            raise
        except Exception as exc:
            raise DiscoveryError(
                f"CML GET {path} failed: {type(exc).__name__}: {str(exc)[:120]}"
            ) from exc

    return _get


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=3, max_length=4000)
    provider: str = Field(default="meta", min_length=1, max_length=64)
    model: Optional[str] = Field(default=None, max_length=128)
    live: bool = False


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: LabBlueprint
    snapshot: Optional[ControllerSnapshot] = None
    live: bool = False


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: LabBlueprint


@router.post("/plan")
async def plan_endpoint(
    payload: PlanRequest,
    token: str = Depends(_require_session_dep),
) -> dict:
    snapshot = None
    live_meta: dict[str, Any] = {"live": False}
    if payload.live:
        session = _owned_cml_session(token)
        get_json = _cml_get_json(session.auth)
        try:
            catalog = discover_controller_catalog(get_json)
        except DiscoveryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        snapshot = catalog.to_snapshot()
        live_meta = {"live": True, **catalog.summary()}
    try:
        result = await plan_lab(
            prompt=payload.prompt,
            provider_id=payload.provider,
            model=payload.model,
            snapshot=snapshot,
        )
    except PlannerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "blueprint": result.blueprint.model_dump(),
        "warnings": result.warnings,
        "provider": result.provider,
        "model": result.model,
        "mock": result.mock,
        "interfaces_verified": result.interfaces_verified,
        **live_meta,
    }


@router.post("/validate")
def validate_endpoint(
    payload: ValidateRequest,
    token: str = Depends(_require_session_dep),
) -> dict:
    snapshot = payload.snapshot
    live_meta: dict[str, Any] = {"live": False}
    if payload.live:
        session = _owned_cml_session(token)
        get_json = _cml_get_json(session.auth)
        try:
            catalog = discover_controller_catalog(get_json)
        except DiscoveryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        snapshot = catalog.to_snapshot()
        live_meta = {"live": True, **catalog.summary()}
        result = validate_blueprint(payload.blueprint, snapshot=snapshot)
        _check_per_definition_images(payload.blueprint, catalog, get_json, result)
        dumped = result.model_dump()
        dumped["live"] = live_meta
        return dumped
    result = validate_blueprint(payload.blueprint, snapshot=snapshot)
    dumped = result.model_dump()
    dumped["live"] = live_meta
    return dumped


def _check_per_definition_images(
    blueprint: LabBlueprint,
    catalog: ControllerCatalog,
    get_json: Callable[[str], Any],
    result: Any,
) -> None:
    """Refine live validation with per-definition image allowlists.

    ``GET node_definitions/{id}/image_definitions`` is SDK-verified. Each
    referenced definition id is path-validated before use. A failed sub-fetch
    degrades to an explicit warning (global image check still applies) —
    never a silent pass, never a silent deploy signal.
    """
    wanted = sorted({n.node_definition for n in blueprint.nodes if n.image_definition})
    for definition_id in wanted:
        try:
            safe_id = validate_definition_id(definition_id)
            payload = get_json(f"/node_definitions/{safe_id}/image_definitions")
        except DiscoveryError as exc:
            result.warnings.append(ValidationIssue(
                code="per_definition_unavailable",
                message=f"per-definition images unavailable for '{definition_id}': {exc}",
            ))
            continue
        allowed = {i.strip().lower() for i in _definition_ids(payload)}
        for node in blueprint.nodes:
            if node.node_definition != definition_id or not node.image_definition:
                continue
            if node.image_definition.strip().lower() not in allowed:
                result.errors.append(ValidationIssue(
                    code="image_not_valid_for_definition",
                    message=(
                        f"image '{node.image_definition}' is not valid for "
                        f"node_definition '{definition_id}' on this controller"
                    ),
                    node_id=node.id,
                ))
    if any(e.code == "image_not_valid_for_definition" for e in result.errors):
        result.valid = False


@router.post("/preview")
def preview_endpoint(
    payload: PreviewRequest,
    token: str = Depends(_require_session_dep),
) -> dict:
    _ = token
    return blueprint_to_reactflow(payload.blueprint)


@router.get("/catalog")
def catalog_endpoint(
    token: str = Depends(_require_session_dep),
) -> dict:
    """Live read-only controller catalog for the caller's own session."""
    session = _owned_cml_session(token)
    get_json = _cml_get_json(session.auth)
    try:
        catalog = discover_controller_catalog(get_json)
    except DiscoveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    snapshot = catalog.to_snapshot()
    return {
        "snapshot": snapshot.model_dump(),
        "summary": catalog.summary(),
        "node_definitions": catalog.node_definitions[:512],
        "image_definitions": catalog.image_definitions[:1024],
        "system_health": catalog.system_health,
        "system_information": catalog.system_information,
    }


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: LabBlueprint
    snapshot: Optional[ControllerSnapshot] = None
    live: bool = False
    lab_title: Optional[str] = Field(default=None, max_length=128)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket_id: str = Field(min_length=1, max_length=64)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    lab_title: Optional[str] = Field(default=None, max_length=128)
    allow_offline: bool = False


@router.post("/deploy/prepare")
def deploy_prepare_endpoint(
    payload: PrepareRequest,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Validate a blueprint and mint an approval ticket. No CML writes."""
    snapshot = payload.snapshot
    if payload.live:
        session = _owned_cml_session(token)
        get_json = _cml_get_json(session.auth)
        try:
            catalog = discover_controller_catalog(get_json)
        except DiscoveryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        snapshot = catalog.to_snapshot()
    try:
        ticket = _deploy.prepare_deployment(
            token, payload.blueprint, live=payload.live,
            snapshot=snapshot, lab_title=payload.lab_title,
        )
    except DeploymentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ticket.to_dict()


@router.post("/deploy/confirm", status_code=202)
def deploy_confirm_endpoint(
    payload: ConfirmRequest,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Confirm an approval ticket and launch the deployment job.

    The ONLY endpoint that can cause CML writes, and only after explicit
    approval. Returns 202 with the job; poll ``GET /deploy/{job_id}``.
    """
    try:
        import virl2_client  # noqa: F401 - availability probe, real use in worker
    except ImportError as exc:
        raise HTTPException(
            status_code=501, detail="deployment requires virl2_client==2.9.0"
        ) from exc
    session = _owned_cml_session(token)
    try:
        job, created = _deploy.confirm_deployment(
            token, payload.ticket_id,
            idempotency_key=payload.idempotency_key,
            lab_title=payload.lab_title,
            allow_offline=payload.allow_offline,
            client_factory=_deploy.build_client_library,
            session_host=session.host,
            session_username=session.username,
            session_password=session.password,
        )
    except DeploymentError as exc:
        message = str(exc)
        if message == "approval ticket not found":
            raise HTTPException(status_code=404, detail=message) from exc
        if message == "approval ticket expired":
            raise HTTPException(status_code=410, detail=message) from exc
        raise HTTPException(status_code=422, detail=message) from exc
    body = job.to_dict()
    body["created"] = created
    return body


@router.get("/deploy/{job_id}")
def deploy_status_endpoint(
    job_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Poll a deployment job. Ownership-scoped: foreign jobs read as 404."""
    job = _deploy.get_job(token, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="deployment job not found")
    return job.to_dict()


@router.get("/available")
def available_labs_endpoint(
    token: str = Depends(_require_session_dep),
) -> dict:
    """List the caller's controller labs with states and device counts."""
    session = _owned_cml_session(token)
    get_json = _cml_get_json(session.auth)
    try:
        labs = list_labs_available(get_json)
    except DiscoveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    out = []
    for lab in labs:
        latest = _lifecycle.latest_operation_for_lab(token, lab.id)
        # Only in-flight/failed ops overlay the dashboard; completed ops
        # defer to fresh reads so verdicts can never go stale.
        overlay = (latest.status if latest is not None else None)
        if overlay not in DASHBOARD_OP_STATUSES:
            overlay = None
        out.append({
            **lab.to_dict(),
            "state_display": _lifecycle.display_state(
                lab.state, overlay, converged=lab.converged,
                booted=lab.nodes_booted, active=lab.nodes_active,
                total=lab.nodes_total),
        })
    return {"count": len(out), "labs": out}


@router.get("/{lab_id}/operation")
def lab_operation_endpoint(
    lab_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Latest tracked lifecycle operation for one lab (read-only recovery).

    Lets the UI resume tracking an in-flight operation after the original
    polling request died (timeout, closed dialog, transient error) WITHOUT
    issuing a new start/stop. 404 when this session never tracked an
    operation for the lab — including after a backend restart, since
    operation records are in-memory by design (fresh ``/available`` reads
    govern the dashboard in that case).
    """
    try:
        safe_id = validate_lab_id(lab_id)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _owned_cml_session(token)
    latest = _lifecycle.latest_operation_for_lab(token, safe_id)
    if latest is None:
        raise HTTPException(
            status_code=404, detail="no lifecycle operation for this lab")
    return latest.to_dict()


def _lab_snapshot(get_json: Callable[[str], Any], safe_id: str) -> LabSnapshot:
    """Build one verified read snapshot (state required, extras optional).

    ``/state`` must read or the whole snapshot fails (callers need at least
    the raw state); convergence and element reads degrade to ``None``.
    """
    state_payload = get_json(f"/labs/{safe_id}/state")
    raw = state_payload if isinstance(state_payload, str) else str(state_payload)
    try:
        converged = parse_converged(get_json(f"/labs/{safe_id}/check_if_converged"))
    except Exception:
        converged = None
    try:
        booted, active, total = parse_element_nodes(
            get_json(f"/labs/{safe_id}/lab_element_state"))
    except Exception:
        booted, active, total = None, None, None
    return LabSnapshot(raw_state=raw, converged=converged,
                       booted=booted, active=active, total=total)


@router.post("/{lab_id}/start", status_code=202)
def start_lab_endpoint(
    lab_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Start a stopped lab (one PUT per user-confirmed request, then poll).

    Uses the SDK-verified ``Lab.start(wait=False)`` operation only. Poll
    ``GET /api/labs/start/{op_id}`` for the tracked outcome.
    """
    try:
        safe_id = validate_lab_id(lab_id)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        import virl2_client  # noqa: F401 - availability probe, real use in worker
    except ImportError as exc:
        raise HTTPException(
            status_code=501, detail="lab start requires virl2_client==2.9.0"
        ) from exc
    session = _owned_cml_session(token)
    if not session.auth.is_lab_accessible(safe_id):
        raise HTTPException(
            status_code=404, detail="lab not found or not accessible")
    get_json = _cml_get_json(session.auth)

    # Resolve a title for display (best-effort; id fallback).
    try:
        details = get_json(f"/labs/{safe_id}")
        title = str(details.get("lab_title", safe_id)) \
            if isinstance(details, dict) else safe_id
    except DiscoveryError:
        title = safe_id

    try:
        op, created = _lifecycle.start_lab(
            token, safe_id, title,
            client_factory=_deploy.build_client_library,
            session_host=session.host,
            session_username=session.username,
            session_password=session.password,
            get_snapshot=lambda: _lab_snapshot(get_json, safe_id),
        )
    except _lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    body = op.to_dict()
    body["created"] = created
    return body


@router.post("/{lab_id}/stop", status_code=202)
def stop_lab_endpoint(
    lab_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Stop a running lab (one PUT per user-confirmed request, then poll).

    Uses the SDK-verified ``Lab.stop(wait=False)`` operation only. Poll
    ``GET /api/labs/start/{op_id}`` for the tracked outcome.
    """
    try:
        safe_id = validate_lab_id(lab_id)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        import virl2_client  # noqa: F401 - availability probe, real use in worker
    except ImportError as exc:
        raise HTTPException(
            status_code=501, detail="lab stop requires virl2_client==2.9.0"
        ) from exc
    session = _owned_cml_session(token)
    if not session.auth.is_lab_accessible(safe_id):
        raise HTTPException(
            status_code=404, detail="lab not found or not accessible")
    get_json = _cml_get_json(session.auth)

    try:
        details = get_json(f"/labs/{safe_id}")
        title = str(details.get("lab_title", safe_id)) \
            if isinstance(details, dict) else safe_id
    except DiscoveryError:
        title = safe_id

    try:
        op, created = _lifecycle.stop_lab(
            token, safe_id, title,
            client_factory=_deploy.build_client_library,
            session_host=session.host,
            session_username=session.username,
            session_password=session.password,
            get_snapshot=lambda: _lab_snapshot(get_json, safe_id),
        )
    except _lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    body = op.to_dict()
    body["created"] = created
    return body


@router.get("/start/{op_id}")
def start_status_endpoint(
    op_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Poll a start/stop operation (read-only controller state refresh)."""
    if not op_id or len(op_id) > 64 or not re.match(
            r"^[A-Za-z0-9._\-]+$", op_id):
        raise HTTPException(status_code=422, detail="invalid operation id")
    session = _owned_cml_session(token)
    get_json = _cml_get_json(session.auth)
    op = _lifecycle.get_operation(token, op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="start operation not found")

    refreshed = _lifecycle.refresh_operation(
        token, op_id, lambda: _lab_snapshot(get_json, op.lab_id))
    if refreshed is None:  # pragma: no cover - just observed above
        raise HTTPException(status_code=404, detail="start operation not found")
    return refreshed.to_dict()


@router.get("/deploy/{job_id}/topology")
def deploy_topology_endpoint(
    job_id: str,
    token: str = Depends(_require_session_dep),
) -> dict:
    """Live read-only topology of a successfully deployed lab."""
    job = _deploy.get_job(token, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="deployment job not found")
    if job.status != "succeeded" or not job.lab_id:
        raise HTTPException(
            status_code=409, detail="deployment has no live lab to show")
    session = _owned_cml_session(token)
    try:
        topo = session.auth.get_topology(job.lab_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"topology read failed: {str(exc)[:160]}"
        ) from exc
    return {
        "lab_id": job.lab_id,
        "lab_title": job.lab_title,
        "lab_state": job.lab_state,
        "nodes": [
            {
                "id": n["id"],
                "position": {"x": n.get("x", 0), "y": n.get("y", 0)},
                "data": {
                    "label": n.get("label", n["id"]),
                    "type": n.get("type", "unknown"),
                    "node_state": n.get("state", "unknown"),
                    "cli_connected": False,
                },
            }
            for n in topo.get("nodes", [])
        ],
        "links": [
            {
                "id": link["id"],
                "label": link.get("label", link["id"]),
                "source": link.get("node_a", ""),
                "target": link.get("node_b", ""),
                "sourceHandle": link.get("node_a_intf", ""),
                "targetHandle": link.get("node_b_intf", ""),
            }
            for link in topo.get("links", [])
        ],
    }
