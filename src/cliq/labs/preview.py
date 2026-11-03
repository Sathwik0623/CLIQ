"""
ReactFlow-compatible topology preview (M1).

Converts a :class:`~cliq.labs.models.LabBlueprint` into the same
``{"nodes": [...], "links": [...]}`` shape the frontend already renders with
ReactFlow (see ``frontend/src/types/api.ts`` ``TopologyNode``/``TopologyLink``
and ``LabDeviceStep.tsx``). Pure function: no I/O, no CML access.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from cliq.labs.models import LabBlueprint
from cliq.labs.validator import estimate_resources

_PLANNED_STATE = "PLANNED"
_GRID_DX = 220
_GRID_DY = 160


def _layout_positions(blueprint: LabBlueprint) -> dict[str, dict[str, int]]:
    """Use explicit coordinates when present, else a deterministic grid."""
    positions: dict[str, dict[str, int]] = {}
    if any(n.x != 0 or n.y != 0 for n in blueprint.nodes):
        for node in blueprint.nodes:
            positions[node.id] = {"x": node.x, "y": node.y}
        return positions
    count = len(blueprint.nodes)
    cols = max(1, math.ceil(math.sqrt(count)))
    for index, node in enumerate(blueprint.nodes):
        row, col = divmod(index, cols)
        positions[node.id] = {"x": col * _GRID_DX, "y": row * _GRID_DY}
    return positions


def blueprint_to_reactflow(blueprint: LabBlueprint) -> dict[str, Any]:
    """Build ReactFlow-compatible preview data plus a summary block."""
    positions = _layout_positions(blueprint)
    estimate = estimate_resources(blueprint)

    nodes = [
        {
            "id": node.id,
            "position": positions[node.id],
            "data": {
                "label": node.label,
                "type": node.node_definition,
                "node_state": _PLANNED_STATE,
                "cli_connected": False,
            },
        }
        for node in blueprint.nodes
    ]
    links = [
        {
            "id": link.id,
            "label": f"{link.interface_a} ↔ {link.interface_b}",
            "source": link.node_a,
            "target": link.node_b,
            "sourceHandle": link.interface_a,
            "targetHandle": link.interface_b,
        }
        for link in blueprint.links
    ]
    definition_counts = dict(Counter(n.node_definition for n in blueprint.nodes))
    return {
        "title": blueprint.title,
        "version": blueprint.version,
        "nodes": nodes,
        "links": links,
        "summary": {
            "total_nodes": len(nodes),
            "total_links": len(links),
            "node_definitions": definition_counts,
            "total_vcpus": estimate.total_vcpus,
            "total_ram_mb": estimate.total_ram_mb,
        },
        "notes": blueprint.notes,
    }
