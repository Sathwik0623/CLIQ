#!/usr/bin/env python3
"""
Phase A latency/observability integration tests.

Validates that the instrumented layers emit the unified schema:
  * ``CMLClient.get_topology`` produces per-stage records (sync_state → nodes →
    links → complete).
  * The command-execution path emits request→first_byte→complete latency.
  * No secret/credential ever reaches a log file.

These run fully offline (no virl2_client, no SSH, no network) by injecting fake
client/lab/channel objects.
"""

from __future__ import annotations

import json

import pytest

from cliq.utils import observability as obs
from cliq.connection import cml_client as cmlmod
from cliq.connection.cml_client import CMLClient, CMLHost


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    # get_topology's _get_lab calls _import_virl2 only to obtain exception types;
    # stub it so the offline test does not need the optional dependency.
    monkeypatch.setattr(cmlmod, "_import_virl2", lambda: (object, object))
    yield


def _read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── fake virl2 lab/client ──────────────────────────────────────────────────
class _Node:
    def __init__(self, i):
        self.id, self.label = f"n{i}", f"node-{i}"
        self.node_definition = "iosv"
        self.state, self.x, self.y = "BOOTED", i, i

    def is_booted(self):
        return True


class _If:
    def __init__(self, lbl):
        self.label = lbl


class _Link:
    def __init__(self, i, a, b):
        self.id, self.node_a, self.node_b = f"l{i}", a, b
        self.interface_a, self.interface_b = _If("G0/0"), _If("G0/1")
        self.state = "UP"


class _Lab:
    def __init__(self, n):
        self._nodes = [_Node(i) for i in range(n)]
        self._links = [_Link(i, self._nodes[i], self._nodes[i + 1]) for i in range(n - 1)]

    def sync_states_if_outdated(self):
        pass

    def nodes(self):
        return self._nodes

    def links(self):
        return self._links


class _Client:
    def __init__(self, lab):
        self._lab = lab

    def get_local_lab(self, _):
        return self._lab


def test_get_topology_emits_stage_breakdown(tmp_path):
    client = CMLClient(CMLHost(url="https://x.invalid", username="U"), password="")
    client._client = _Client(_Lab(5))

    topo = client.get_topology("lab1")
    assert len(topo.nodes) == 5 and len(topo.links) == 4

    records = _read(tmp_path / "cml.log")
    stages = [r["stage"] for r in records if r["op"] == "get_topology"]
    for expected in ("sync_state", "nodes", "links", "complete"):
        assert expected in stages
    # Every stage carries a measured elapsed_ms.
    for r in records:
        if r["op"] == "get_topology":
            assert r["elapsed_ms"] is not None


def test_command_latency_request_first_byte_complete(tmp_path):
    # Exercise the unified command-latency helper directly via a tiny stand-in
    # that mirrors CMLConsoleDevice._log_command_latency semantics.
    import time

    t_request = time.monotonic()
    first_byte = t_request + 0.01
    t_complete = t_request + 0.05
    obs.log_event("command", "run_command", "first_byte", "success",
                  elapsed_ms=(first_byte - t_request) * 1000, detail="show")
    obs.log_event("command", "run_command", "complete", "success",
                  elapsed_ms=(t_complete - t_request) * 1000, detail="show", out_chars=42)

    records = _read(tmp_path / "command.log")
    stages = [r["stage"] for r in records]
    assert "first_byte" in stages and "complete" in stages
    # Only the verb is logged, never a full command line.
    assert all(r["detail"] == "show" for r in records)


def test_no_secret_in_any_layer_log(tmp_path):
    obs.log_event("cml", "connect", "auth", "success",
                  detail="connecting password=hunter2 key=DEADBEEFDEADBEEF1234")
    obs.log_event("netsim", "auth", "auth", "success", api_key="SECRETKEY123456")
    for name in ("cml.log", "netsim_api.log"):
        raw = (tmp_path / name).read_text(encoding="utf-8")
        assert "hunter2" not in raw
        assert "SECRETKEY123456" not in raw
        assert "DEADBEEFDEADBEEF1234" not in raw


# ── live command path (execute_paged → terminal.log) ────────────────────────
class _FakeChan:
    """Minimal paramiko-channel stand-in for the live execute_paged path."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def send_ready(self):
        return True

    def gettimeout(self):
        return 0.2

    def settimeout(self, _t):
        pass

    def send(self, _data):
        pass

    def recv(self, _n):
        import socket
        if self._chunks:
            return self._chunks.pop(0)
        raise socket.timeout()


def test_execute_paged_writes_terminal_log(tmp_path):
    """The LIVE web path (execute_paged) must emit request→first_byte→complete
    latency to terminal.log — not only the rarely-used execute() helper."""
    from cliq.connection.live_device import CMLConsoleDevice

    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._current_prompt = "R1#"
    dev._chan = _FakeChan([b"show version\r\nCisco IOS XE\r\nR1#"])

    page, more = dev.execute_paged("show version")
    assert more is False

    records = _read(tmp_path / "terminal.log")
    stages = [r["stage"] for r in records if r["op"] == "run_command"]
    assert "first_byte" in stages and "complete" in stages
    # Verb only — never the full command line — and a measured elapsed_ms.
    for r in records:
        if r["op"] == "run_command":
            assert r["detail"] == "show"
            assert r["elapsed_ms"] is not None


def test_execute_paged_streams_cleaned_deltas(tmp_path):
    """Phase 3 latency masking: execute_paged must push cleaned output deltas to
    the on_chunk callback as bytes arrive, and the streamed text must equal the
    final cleaned page (no ANSI, no echoed command, no trailing prompt)."""
    from cliq.connection.live_device import CMLConsoleDevice

    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._current_prompt = "R1#"
    dev._chan = _FakeChan(
        [b"show version\r\n", b"Cisco IOS XE\r\n", b"uptime 5 min\r\n", b"R1#"]
    )

    deltas: list[str] = []
    page, more = dev.execute_paged("show version", deltas.append)

    assert more is False
    # Something streamed, and the concatenation equals the final cleaned page.
    assert deltas
    assert "".join(deltas) == page
    # The echoed command and the trailing prompt never leak into the stream.
    streamed = "".join(deltas)
    assert "show version" not in streamed
    assert "R1#" not in streamed
    assert "Cisco IOS XE" in streamed


def test_execute_paged_without_callback_is_unchanged(tmp_path):
    """Omitting on_chunk must behave exactly as before (no streaming side-effects)."""
    from cliq.connection.live_device import CMLConsoleDevice

    dev = CMLConsoleDevice.__new__(CMLConsoleDevice)
    dev._connected = True
    dev._current_prompt = "R1#"
    dev._chan = _FakeChan([b"show version\r\nCisco IOS XE\r\nR1#"])

    page, more = dev.execute_paged("show version")
    assert more is False
    assert "Cisco IOS XE" in page
    # The full line must never leak.
    raw = (tmp_path / "terminal.log").read_text(encoding="utf-8")
    assert "show version" not in raw

