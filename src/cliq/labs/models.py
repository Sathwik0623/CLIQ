"""
CLIQ Lab Blueprint models (M1).

Strict Pydantic v2 models for natural-language lab planning. All models
forbid extra fields so the LLM planner and API callers fail closed on
unknown keys instead of silently dropping them.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BLUEPRINT_VERSION = "1.0"

#: SDK-verified controller state markers (virl2_client 2.9.0:
#: ``Lab.is_active`` ⟺ ``STARTED``; ``Node.is_booted`` ⟺ ``BOOTED``;
#: node active set ``{STARTED, QUEUED, BOOTED}``).
STATE_STARTED = "STARTED"
STATE_BOOTED = "BOOTED"
NODE_ACTIVE_STATES = frozenset({"STARTED", "QUEUED", "BOOTED"})

_NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_HOSTNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_INTERFACE_PATTERN = r"^[A-Za-z]+[0-9][A-Za-z0-9/.\-]*$"

_MAX_NODES = 64
_MAX_LINKS = 256
_MAX_LABEL = 64


class LabNode(BaseModel):
    """One simulated node in a lab blueprint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=_NODE_ID_PATTERN, description="Unique node id (e.g. 'R1').")
    label: str = Field(
        min_length=1, max_length=_MAX_LABEL, pattern=_HOSTNAME_PATTERN,
        description="Hostname / display label (e.g. 'R1').",
    )
    node_definition: str = Field(
        min_length=1, max_length=64,
        description="CML node definition (e.g. 'iosv', 'iosvl2', 'server').",
    )
    image_definition: Optional[str] = Field(
        default=None, max_length=128,
        description="Optional CML image definition override.",
    )
    x: int = Field(default=0, ge=-10000, le=10000)
    y: int = Field(default=0, ge=-10000, le=10000)
    vcpus: Optional[int] = Field(default=None, ge=1, le=32)
    ram_mb: Optional[int] = Field(default=None, ge=128, le=131072)

    @field_validator("node_definition")
    @classmethod
    def _normalize_node_definition(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("image_definition")
    @classmethod
    def _normalize_image_definition(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class LabLink(BaseModel):
    """One point-to-point link between two blueprint nodes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=_NODE_ID_PATTERN, description="Unique link id (e.g. 'L1').")
    node_a: str = Field(pattern=_NODE_ID_PATTERN)
    node_b: str = Field(pattern=_NODE_ID_PATTERN)
    interface_a: str = Field(
        min_length=2, max_length=32, pattern=_INTERFACE_PATTERN,
        description="Interface label on node_a (e.g. 'Gi0/0').",
    )
    interface_b: str = Field(
        min_length=2, max_length=32, pattern=_INTERFACE_PATTERN,
        description="Interface label on node_b (e.g. 'Gi0/1').",
    )


class LabBlueprint(BaseModel):
    """A complete, validated lab plan. Read-only: never provisioned in M1."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=128)
    nodes: list[LabNode] = Field(min_length=1, max_length=_MAX_NODES)
    links: list[LabLink] = Field(default_factory=list, max_length=_MAX_LINKS)
    notes: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_references(self) -> LabBlueprint:
        node_ids = [n.id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node id in blueprint")
        labels = [n.label for n in self.nodes]
        if len(set(labels)) != len(labels):
            raise ValueError("duplicate node label in blueprint")

        link_ids = [link.id for link in self.links]
        if len(set(link_ids)) != len(link_ids):
            raise ValueError("duplicate link id in blueprint")

        known = set(node_ids)
        seen_interfaces: set[tuple[str, str]] = set()
        for link in self.links:
            if link.node_a not in known:
                raise ValueError(f"link '{link.id}' references unknown node '{link.node_a}'")
            if link.node_b not in known:
                raise ValueError(f"link '{link.id}' references unknown node '{link.node_b}'")
            if link.node_a == link.node_b:
                raise ValueError(f"link '{link.id}' is a self-link on '{link.node_a}'")
            for endpoint in ((link.node_a, link.interface_a), (link.node_b, link.interface_b)):
                if endpoint in seen_interfaces:
                    raise ValueError(
                        f"interface '{endpoint[1]}' on node '{endpoint[0]}' is used more than once"
                    )
                seen_interfaces.add(endpoint)
        return self


class ControllerSnapshot(BaseModel):
    """Optional read-only controller capacity snapshot for validation.

    Supplied by the caller from live read-only CML API data. M1 never fetches
    this itself and never writes to CML.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    node_definitions: Optional[list[str]] = Field(default=None, max_length=512)
    image_definitions: Optional[list[str]] = Field(default=None, max_length=1024)
    cpu_total: Optional[int] = Field(default=None, ge=1, le=100000)
    cpu_used: Optional[int] = Field(default=None, ge=0, le=100000)
    ram_total_mb: Optional[int] = Field(default=None, ge=1, le=100000000)
    ram_used_mb: Optional[int] = Field(default=None, ge=0, le=100000000)


class ValidationIssue(BaseModel):
    """One validation error or warning."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    node_id: Optional[str] = None
    link_id: Optional[str] = None


class PerNodeResource(BaseModel):
    """Estimated resources for one node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    node_definition: str
    vcpus: int
    ram_mb: int


class ResourceEstimate(BaseModel):
    """Total estimated controller resources for a blueprint."""

    model_config = ConfigDict(extra="forbid")

    total_nodes: int
    total_links: int
    total_vcpus: int
    total_ram_mb: int
    per_node: list[PerNodeResource] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """Outcome of read-only blueprint validation."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    resource_estimate: ResourceEstimate
