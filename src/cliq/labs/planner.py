"""
Natural-language lab planning (M1).

Uses the existing AI provider abstraction (:mod:`cliq.ai.provider` /
:mod:`cliq.ai.registry`) to translate a prompt into a strict
:class:`~cliq.labs.models.LabBlueprint`. Read-only: the planner never touches
CML and never configures devices.

Offline behavior: when ``CLIQ_LABS_MOCK=1`` or ``CLIQ_AI_MOCK=1`` is set, or
when no provider credentials are configured, planning falls back to a small
deterministic heuristic so M1 stays usable air-gapped and in tests without
burning API credits.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import ValidationError

from cliq.ai.provider import ChatMessage, ChatRole
from cliq.labs.models import LabBlueprint

log = logging.getLogger("cliq.labs.planner")

LAB_PLANNER_SYSTEM_PROMPT = """You are CLIQ Lab Planner. Translate the operator's natural-language lab request into a lab blueprint.

Output ONLY a single JSON object, no markdown fences, no prose. The JSON MUST match this schema:
{"version": "1.0", "title": string, "nodes": [{"id": string, "label": string, "node_definition": string, "image_definition": null|string, "x": int, "y": int}], "links": [{"id": string, "node_a": string, "node_b": string, "interface_a": string, "interface_b": string}], "notes": null|string}

Rules:
- version is exactly "1.0".
- Node ids and link ids are unique, matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$.
- Node labels are unique hostnames (e.g. R1, SW1).
- node_definition is a CML node definition in lowercase: one of iosv, iosvl2, csr1000v, cat8000v, iosxrv, iosxrv9000, nxosv, nxosv9000, ioll2, unmanaged_switch, server, desktop, external_connector.
- Routers use iosv; switches use iosvl2; hosts/servers use server. Never invent definitions.
- Every link references two DIFFERENT existing node ids; each node+interface pair is used at most once.
- Interface labels look like Gi0/0, Gi0/1, Fa0/1, Eth0/0 (letters then digits).
- Keep topologies small (<= 12 nodes) unless the request explicitly asks for more.
- notes briefly describes the topology in one or two sentences.
"""


class PlannerError(ValueError):
    """Raised when a prompt cannot be turned into a valid blueprint."""


@dataclass
class PlanResult:
    blueprint: LabBlueprint
    warnings: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    mock: bool = False


def _mock_enabled() -> bool:
    for var in ("CLIQ_LABS_MOCK", "CLIQ_AI_MOCK"):
        if os.environ.get(var, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


_COUNT_RE = re.compile(
    r"(\d+)\s*(router|rtr|switch|sw|switchport|firewall|fw|server|srv|host|pc|node)s?",
    re.IGNORECASE,
)


def parse_lab_prompt_fallback(prompt: str) -> LabBlueprint:
    """Deterministic offline blueprint heuristic for mock mode and tests.

    Recognizes counts such as "3 routers", "2 switches", "2 servers/hosts".
    Defaults to 2 routers + 1 switch when no counts are found.
    """
    text = (prompt or "").strip()
    if not text:
        raise PlannerError("prompt must not be empty")

    routers = switches = servers = 0
    for match in _COUNT_RE.finditer(text):
        count = max(1, min(int(match.group(1)), 16))
        kind = match.group(2).lower()
        if kind in ("router", "rtr"):
            routers += count
        elif kind in ("switch", "sw", "switchport"):
            switches += count
        elif kind in ("server", "srv", "host", "pc"):
            servers += count
        elif kind in ("node",):
            routers += count
        elif kind in ("firewall", "fw"):
            routers += count
    if routers == 0 and switches == 0 and servers == 0:
        lowered = text.lower()
        if "switch" in lowered:
            switches = 2
            routers = 1
        elif "server" in lowered or "host" in lowered or "pc" in lowered:
            routers = 1
            servers = 2
        else:
            routers = 2
            switches = 1

    routers = min(routers, 16)
    switches = min(switches, 16)
    servers = min(servers, 16)

    nodes: list[dict] = []
    for i in range(1, routers + 1):
        nodes.append({"id": f"R{i}", "label": f"R{i}", "node_definition": "iosv", "x": 0, "y": 0})
    for i in range(1, switches + 1):
        nodes.append({"id": f"SW{i}", "label": f"SW{i}", "node_definition": "iosvl2", "x": 0, "y": 0})
    for i in range(1, servers + 1):
        nodes.append({"id": f"SRV{i}", "label": f"SRV{i}", "node_definition": "server", "x": 0, "y": 0})

    links: list[dict] = []
    link_no = 1

    def _add_link(a: str, b: str, if_a: str, if_b: str) -> None:
        nonlocal link_no
        links.append({
            "id": f"L{link_no}", "node_a": a, "node_b": b,
            "interface_a": if_a, "interface_b": if_b,
        })
        link_no += 1

    # Chain routers, attach each switch to R1 (or first router), attach servers to SW1/R1.
    for i in range(1, routers):
        _add_link(f"R{i}", f"R{i + 1}", f"Gi0/{i}", "Gi0/0")
    first_router = "R1" if routers else None
    for i in range(1, switches + 1):
        if first_router:
            _add_link(first_router, f"SW{i}", f"Gi0/{routers + i - 1}", "Gi0/0")
        elif i < switches:
            _add_link(f"SW{i}", f"SW{i + 1}", f"Gi0/{i}", "Gi0/0")
    uplink = "SW1" if switches else first_router
    for i in range(1, servers + 1):
        if uplink:
            _add_link(uplink, f"SRV{i}", f"Gi0/{i}", "Eth0/0")

    title = " ".join(text.split())[:96] or "Planned lab"
    notes = (
        f"Offline mock plan: {routers} router(s), {switches} switch(es), "
        f"{servers} server(s). Review before any future provisioning step."
    )
    return LabBlueprint.model_validate({
        "version": "1.0", "title": title, "nodes": nodes, "links": links, "notes": notes,
    })


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def parse_blueprint_json(text: str) -> LabBlueprint:
    """Parse strict blueprint JSON produced by the LLM."""
    cleaned = _strip_code_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PlannerError("planner returned no JSON object")
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PlannerError(f"planner returned invalid JSON: {exc}") from exc
    try:
        return LabBlueprint.model_validate(payload)
    except ValidationError as exc:
        raise PlannerError(f"planner output failed blueprint validation: {exc}") from exc


async def plan_lab(
    prompt: str,
    provider_id: str = "meta",
    model: Optional[str] = None,
) -> PlanResult:
    """Plan a lab blueprint from natural language via the AI provider abstraction."""
    text = (prompt or "").strip()
    if len(text) < 3:
        raise PlannerError("prompt must be at least 3 characters")
    if len(text) > 4000:
        raise PlannerError("prompt must be at most 4000 characters")

    warnings: list[str] = []
    if _mock_enabled():
        blueprint = parse_lab_prompt_fallback(text)
        return PlanResult(
            blueprint=blueprint,
            warnings=["mock_mode: deterministic offline plan (no LLM call)"],
            provider=provider_id, model=model or "mock", mock=True,
        )

    from cliq.ai.registry import get_provider

    try:
        provider = get_provider(provider_id)
    except KeyError as exc:
        raise PlannerError(str(exc)) from exc

    if not provider.is_configured():
        blueprint = parse_lab_prompt_fallback(text)
        warnings.append(
            f"provider '{provider_id}' is not configured; used deterministic offline plan"
        )
        return PlanResult(
            blueprint=blueprint, warnings=warnings,
            provider=provider_id, model=model or provider.get_default_model(), mock=True,
        )

    chosen_model = model or provider.get_default_model()
    messages = [
        ChatMessage(role=ChatRole.DEVELOPER, content=LAB_PLANNER_SYSTEM_PROMPT),
        ChatMessage(role=ChatRole.USER, content=text),
    ]
    try:
        response = await provider.chat(messages=messages, model=chosen_model, temperature=0.2)
    except Exception as exc:
        raise PlannerError(f"AI provider error: {exc}") from exc
    blueprint = parse_blueprint_json(response.content)
    return PlanResult(
        blueprint=blueprint, warnings=warnings,
        provider=provider_id, model=chosen_model, mock=False,
    )
