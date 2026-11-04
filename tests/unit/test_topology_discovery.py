"""
Regression tests for the CML topology discovery gap.

``CMLAuthManager.get_topology`` must parse the SDK-verified single-shot
``GET /labs/{lab_id}/topology`` contract (virl2_client ``Lab._sync_topology`` /
``import_lab``): links reference *interfaces* (``interface_a``/``interface_b``
UUIDs) — never nodes directly — and node/link/interface identity must reach
Copilot's discovery rendering instead of being discarded.

All controller I/O uses in-memory fakes. No live CML is touched.
"""

import pytest

from cliq.connection.cml_auth import CMLAuthManager

LAB_ID = "lab-ent-uuid-0001"

SW1 = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1"
SW2 = "aaaaaaaa-2222-4222-8222-aaaaaaaaaaa2"
SW1_GI01 = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbb1"
SW2_GI01 = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbb2"
LINK_1 = "cccccccc-1111-4111-8111-ccccccccccc1"


def _snapshot(**over):
    """SDK-shaped GET /labs/{id}/topology payload."""
    topo = {
        "lab": {"title": "Enterprise Two-Department Isolated Lab",
                "description": "", "notes": "", "owner": "admin"},
        "nodes": [
            {"id": SW1, "label": "SW1", "node_definition": "iosvl2",
             "state": "BOOTED", "x": -160, "y": 80},
            {"id": SW2, "label": "SW2", "node_definition": "iosvl2",
             "state": "BOOTED", "x": 160, "y": 80},
        ],
        "interfaces": [
            {"id": SW1_GI01, "node": SW1, "label": "Gi0/1",
             "type": "physical", "slot": 1},
            {"id": SW2_GI01, "node": SW2, "label": "Gi0/1",
             "type": "physical", "slot": 1},
        ],
        "links": [
            {"id": LINK_1, "interface_a": SW1_GI01,
             "interface_b": SW2_GI01, "state": "STARTED"},
        ],
    }
    topo.update(over)
    return topo


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _auth(routes, calls=None):
    """CMLAuthManager with a scripted transport (no sockets, no token files)."""
    auth = CMLAuthManager.__new__(CMLAuthManager)

    def fake_get(path):
        if calls is not None:
            calls.append(path)
        if path not in routes:
            raise RuntimeError(f"404 for {path}")
        payload = routes[path]
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)

    auth._get = fake_get
    auth._ensure_auth = lambda: None
    return auth


def _routes_for_snapshot(topo):
    return {f"/labs/{LAB_ID}/topology": topo}


# ── 1. Successful retrieval resolves links to nodes + interfaces ─────────────

def test_topology_snapshot_resolves_links():
    auth = _auth(_routes_for_snapshot(_snapshot()))
    topo = auth.get_topology(LAB_ID)

    assert [n["label"] for n in topo["nodes"]] == ["SW1", "SW2"]
    assert topo["nodes"][0]["id"] == SW1
    assert topo["nodes"][0]["type"] == "iosvl2"
    assert len(topo["links"]) == 1
    link = topo["links"][0]
    assert link["id"] == LINK_1
    # Node UUIDs derived via the interface -> node mapping (links never
    # carry node refs on the wire).
    assert link["node_a"] == SW1
    assert link["node_b"] == SW2
    assert link["node_a_intf"] == "Gi0/1"
    assert link["node_b_intf"] == "Gi0/1"
    # Interface inventory is available, not discarded.
    by_id = {i["id"]: i for i in topo["interfaces"]}
    assert by_id[SW1_GI01]["label"] == "Gi0/1"
    assert by_id[SW1_GI01]["node"] == SW1


def test_topology_nested_node_interfaces():
    """Some controllers nest interfaces under their node instead."""
    snap = _snapshot()
    del snap["interfaces"]
    snap["nodes"][0]["interfaces"] = [
        {"id": SW1_GI01, "label": "Gi0/1", "type": "physical"}]
    snap["nodes"][1]["interfaces"] = [
        {"id": SW2_GI01, "label": "Gi0/1", "type": "physical"}]
    auth = _auth(_routes_for_snapshot(snap))
    topo = auth.get_topology(LAB_ID)
    link = topo["links"][0]
    assert (link["node_a"], link["node_b"]) == (SW1, SW2)
    assert (link["node_a_intf"], link["node_b_intf"]) == ("Gi0/1", "Gi0/1")


# ── 2. Missing links ─────────────────────────────────────────────────────────

def test_topology_missing_links_key():
    snap = _snapshot()
    del snap["links"]
    auth = _auth(_routes_for_snapshot(snap))
    topo = auth.get_topology(LAB_ID)
    assert topo["links"] == []
    assert [n["label"] for n in topo["nodes"]] == ["SW1", "SW2"]


# ── 3. Missing node references ───────────────────────────────────────────────

def test_topology_unknown_interface_reference():
    """A link referencing an unknown interface keeps its id, refs stay empty."""
    snap = _snapshot(interfaces=[])
    auth = _auth(_routes_for_snapshot(snap))
    topo = auth.get_topology(LAB_ID)
    assert len(topo["links"]) == 1
    link = topo["links"][0]
    assert link["id"] == LINK_1
    assert link["node_a"] == "" and link["node_b"] == ""
    assert link["node_a_intf"] == "" and link["node_b_intf"] == ""


# ── 4. Malformed responses never raise and keep required keys ────────────────

@pytest.mark.parametrize("topo_payload", [
    None,
    "not-a-dict",
    ["not", "a", "dict"],
    {"nodes": "nope", "links": "nope"},
    {"nodes": ["SW1", 42, None], "links": ["l-1", None]},
    {"nodes": [{"label": "NO-ID"}], "links": [{"interface_a": "x"}]},
])
def test_topology_malformed_payloads(topo_payload):
    auth = _auth(_routes_for_snapshot(topo_payload))
    topo = auth.get_topology(LAB_ID)  # must not raise
    assert isinstance(topo["nodes"], list)
    assert isinstance(topo["links"], list)
    for n in topo["nodes"]:
        assert "id" in n  # UI renderer indexes n["id"]
    for link in topo["links"]:
        assert "id" in link


# ── 5. Snapshot endpoint down: legacy per-item fallback, still no raise ──────

def test_topology_falls_back_to_per_item_enumeration():
    calls = []
    auth = _auth({
        f"/labs/{LAB_ID}/nodes": [SW1],
        f"/labs/{LAB_ID}/nodes/{SW1}": {
            "label": "SW1", "node_definition": "iosvl2",
            "state": "BOOTED", "x": 0, "y": 0},
        f"/labs/{LAB_ID}/links": [],
    }, calls=calls)
    topo = auth.get_topology(LAB_ID)
    assert f"/labs/{LAB_ID}/topology" in calls  # snapshot tried first
    assert [n["label"] for n in topo["nodes"]] == ["SW1"]
    assert topo["links"] == []


def test_topology_total_failure_returns_empty():
    auth = _auth({})
    topo = auth.get_topology(LAB_ID)  # must not raise
    assert topo["nodes"] == [] and topo["links"] == []


# ── 6. Copilot rendering keeps links, node IDs, interface names ──────────────

class _FakeCMLAuth:
    """Minimal controller double for discover_environment."""

    def __init__(self, topo):
        self._topo = topo
        self.calls = []

    def get_system_information(self):
        return {"version": "2.7.2", "ready": True}

    def get_labs(self):
        from types import SimpleNamespace
        self.calls.append("labs")
        return [SimpleNamespace(lab_id=LAB_ID,
                                title="Enterprise Two-Department Isolated Lab",
                                state="STARTED", owner="admin")]

    def get_topology(self, lab_id):
        self.calls.append(f"topology:{lab_id}")
        return dict(self._topo)

    def _get(self, path):
        self.calls.append(f"GET {path}")
        routes = {
            "/node_definitions": [{"id": "iosvl2"}],
            "/image_definitions": [{"id": "iosvl2-15.2"}],
            "/system_stats": {"cpu_total": 16, "cpu_used": 4,
                              "ram_total_mb": 32768, "ram_used_mb": 8192},
            "/system_health": {"ready": True},
            "/system_information": {"version": "2.7.2"},
        }
        if path not in routes:
            raise RuntimeError(f"404 for {path}")
        return _Resp(routes[path])


def _session(monkeypatch, topo):
    from types import SimpleNamespace
    from cliq.web import server
    monkeypatch.setitem(server.SESSIONS, "tok-T", SimpleNamespace(
        host="cml", auth=_FakeCMLAuth(topo), connection_type="cml"))


def test_discovery_renders_links_node_ids_and_interfaces(monkeypatch):
    from cliq.ai import lab_discovery

    auth = _auth(_routes_for_snapshot(_snapshot()))
    _session(monkeypatch, auth.get_topology(LAB_ID))
    res = lab_discovery.discover_environment("tok-T", lab_id=LAB_ID)
    assert res.ok
    prompt = lab_discovery.format_discovery_for_prompt(res)
    # Authoritative link endpoints with interface names.
    assert "SW1:Gi0/1" in prompt
    assert "SW2:Gi0/1" in prompt
    # Node and interface identity (not just labels).
    assert SW1 in prompt
    assert SW1_GI01 in prompt
    summary = lab_discovery.summarize_for_metadata(res)
    assert summary["topology_node_count"] == 2
    assert summary["topology_link_count"] == 1


# ── 7. Unavailable resources stay honestly unavailable ───────────────────────

def test_resources_unavailable_reported_honestly(monkeypatch):
    from types import SimpleNamespace
    from cliq.ai import lab_discovery
    from cliq.web import server

    class _NoStatsAuth(_FakeCMLAuth):
        def _get(self, path):
            if path == "/system_stats":
                raise RuntimeError("404 for /system_stats")
            return super()._get(path)

    monkeypatch.setitem(server.SESSIONS, "tok-T", SimpleNamespace(
        host="cml", auth=_NoStatsAuth({"nodes": [], "links": []}),
        connection_type="cml"))
    res = lab_discovery.discover_environment("tok-T")
    assert "resources" in res.unavailable
    assert res.resources is None
    prompt = lab_discovery.format_discovery_for_prompt(res)
    assert "Unavailable" in prompt and "resources" in prompt
