"""
Read-only CML validation (M1).

Validates a :class:`~cliq.labs.models.LabBlueprint` against structural rules,
known CML 2.9 node/image definitions, and optional controller capacity — using
only in-memory data. This module performs no network I/O, imports no CML
client, and can never create labs, nodes, links, or device configuration.
"""

from __future__ import annotations

from typing import Optional

from cliq.labs.models import (
    ControllerSnapshot,
    LabBlueprint,
    PerNodeResource,
    ResourceEstimate,
    ValidationIssue,
    ValidationResult,
)

#: Node definitions known to exist on CML 2.9 controllers (mirrors the
#: device-type map in cliq.connection.cml_client, read-only copy).
KNOWN_NODE_DEFINITIONS: frozenset[str] = frozenset({
    "iosv", "csr1000v", "cat8000v", "iosxrv", "iosxrv9000", "nxosv",
    "iosvl2", "nxosv9000", "unmanaged_switch", "ioll2",
    "server", "desktop", "external_connector",
})

#: Conservative default compute per node definition (vcpus, ram_mb).
DEFAULT_NODE_RESOURCES: dict[str, tuple[int, int]] = {
    "iosv": (1, 2048),
    "csr1000v": (2, 4096),
    "cat8000v": (2, 4096),
    "iosxrv": (2, 4096),
    "iosxrv9000": (4, 8192),
    "nxosv": (2, 4096),
    "iosvl2": (1, 2048),
    "nxosv9000": (2, 8192),
    "unmanaged_switch": (1, 512),
    "ioll2": (1, 1024),
    "server": (1, 1024),
    "desktop": (1, 1024),
    "external_connector": (1, 256),
}


def estimate_resources(blueprint: LabBlueprint) -> ResourceEstimate:
    per_node: list[PerNodeResource] = []
    total_vcpus = 0
    total_ram = 0
    for node in blueprint.nodes:
        default_vcpu, default_ram = DEFAULT_NODE_RESOURCES.get(
            node.node_definition, (1, 2048)
        )
        vcpus = node.vcpus if node.vcpus is not None else default_vcpu
        ram_mb = node.ram_mb if node.ram_mb is not None else default_ram
        per_node.append(PerNodeResource(
            node_id=node.id, node_definition=node.node_definition,
            vcpus=vcpus, ram_mb=ram_mb,
        ))
        total_vcpus += vcpus
        total_ram += ram_mb
    return ResourceEstimate(
        total_nodes=len(blueprint.nodes),
        total_links=len(blueprint.links),
        total_vcpus=total_vcpus,
        total_ram_mb=total_ram,
        per_node=per_node,
    )


def validate_blueprint(
    blueprint: LabBlueprint,
    snapshot: Optional[ControllerSnapshot] = None,
    interfaces_verified: bool = False,
) -> ValidationResult:
    """Validate a blueprint without any CML writes or network calls."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    allowed_defs: Optional[set[str]] = None
    if snapshot and snapshot.node_definitions is not None:
        allowed_defs = {d.strip().lower() for d in snapshot.node_definitions if d.strip()}
    allowed_images: Optional[set[str]] = None
    if snapshot and snapshot.image_definitions is not None:
        allowed_images = {d.strip().lower() for d in snapshot.image_definitions if d.strip()}

    for node in blueprint.nodes:
        node_def = node.node_definition
        if allowed_defs is not None:
            if node_def not in allowed_defs:
                errors.append(ValidationIssue(
                    code="unknown_node_definition",
                    message=(
                        f"node_definition '{node_def}' is not in the controller snapshot"
                    ),
                    node_id=node.id,
                ))
        elif node_def not in KNOWN_NODE_DEFINITIONS:
            errors.append(ValidationIssue(
                code="unknown_node_definition",
                message=f"node_definition '{node_def}' is not a known CML 2.9 definition",
                node_id=node.id,
            ))
        if node.image_definition and allowed_images is not None:
            if node.image_definition.strip().lower() not in allowed_images:
                errors.append(ValidationIssue(
                    code="unknown_image_definition",
                    message=(
                        f"image_definition '{node.image_definition}' is not in the "
                        "controller snapshot"
                    ),
                    node_id=node.id,
                ))

    # Interface-role sanity warnings (never errors: the device is the authority).
    for node in blueprint.nodes:
        is_switch = node.node_definition in ("iosvl2", "nxosv9000", "ioll2", "unmanaged_switch")
        is_host = node.node_definition in ("server", "desktop")
        if is_host and len(blueprint.nodes) > 1:
            used = [
                link for link in blueprint.links
                if link.node_a == node.id or link.node_b == node.id
            ]
            if len(used) > 4:
                warnings.append(ValidationIssue(
                    code="host_link_count",
                    message=f"host '{node.id}' has {len(used)} links; hosts usually need one",
                    node_id=node.id,
                ))
        _ = is_switch  # reserved for future role-specific warnings

    # Isolated-node warnings.
    linked = set()
    for link in blueprint.links:
        linked.add(link.node_a)
        linked.add(link.node_b)
    for node in blueprint.nodes:
        if len(blueprint.nodes) > 1 and node.id not in linked:
            warnings.append(ValidationIssue(
                code="isolated_node",
                message=f"node '{node.id}' has no links",
                node_id=node.id,
            ))

    estimate = estimate_resources(blueprint)

    if len(blueprint.nodes) > 20:
        warnings.append(ValidationIssue(
            code="large_lab",
            message=f"{len(blueprint.nodes)} nodes may exceed typical reservation capacity",
        ))

    if snapshot is not None:
        if snapshot.cpu_total is not None:
            used = snapshot.cpu_used or 0
            available = snapshot.cpu_total - used
            if estimate.total_vcpus > available:
                errors.append(ValidationIssue(
                    code="insufficient_cpu",
                    message=(
                        f"plan needs {estimate.total_vcpus} vCPUs but only "
                        f"{available} are available"
                    ),
                ))
            elif available > 0 and estimate.total_vcpus / available >= 0.8:
                warnings.append(ValidationIssue(
                    code="cpu_pressure",
                    message=(
                        f"plan uses {estimate.total_vcpus}/{available} available vCPUs (>=80%)"
                    ),
                ))
        if snapshot.ram_total_mb is not None:
            used_mb = snapshot.ram_used_mb or 0
            available_mb = snapshot.ram_total_mb - used_mb
            if estimate.total_ram_mb > available_mb:
                errors.append(ValidationIssue(
                    code="insufficient_ram",
                    message=(
                        f"plan needs {estimate.total_ram_mb} MB RAM but only "
                        f"{available_mb} MB are available"
                    ),
                ))
            elif available_mb > 0 and estimate.total_ram_mb / available_mb >= 0.8:
                warnings.append(ValidationIssue(
                    code="ram_pressure",
                    message=(
                        f"plan uses {estimate.total_ram_mb}/{available_mb} MB available RAM (>=80%)"
                    ),
                ))

    if blueprint.links and not interfaces_verified:
        warnings.append(ValidationIssue(
            code="interfaces_unverified",
            message=(
                "link interface labels are illustrative and unverified: "
                "per-node-definition interface capabilities cannot be "
                "discovered from the controller. Actual labels are assigned "
                "by the controller at deploy time."
            ),
        ))

    return ValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        resource_estimate=estimate,
    )
