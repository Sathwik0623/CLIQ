#!/usr/bin/env python3
"""
loadtest_topology.py — Phase A #5 large-topology load-test harness.

Confirms that ``CMLClient.get_topology`` scales linearly (no lag growth) as a
lab grows from a handful of nodes to several hundred. It builds in-memory fake
``virl2_client`` lab/node/link objects exposing exactly the attributes the real
extraction path reads, injects them into a ``CMLClient`` (no network), and times
the fetch for a range of node counts.

It also exercises the unified observability per-stage timing (sync_state →
nodes → links → complete) so you can inspect ``~/.cliq/cml.log`` afterwards.

Usage::

    python tools/loadtest_topology.py
    python tools/loadtest_topology.py --sizes 10 50 100 250 500 --repeat 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make ``src`` importable when run from the repo root.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cliq.connection import cml_client as _cml  # noqa: E402
from cliq.connection.cml_client import CMLClient, CMLHost  # noqa: E402

# The fake client provides get_local_lab/join_existing_lab directly, so the real
# virl2_client import is unnecessary for this offline harness. Stub it so the
# load test runs without the optional CML dependency installed.
_cml._import_virl2 = lambda: (object, object)  # type: ignore[assignment]


class _FakeNode:
    def __init__(self, idx: int):
        self.id = f"n{idx}"
        self.label = f"node-{idx}"
        self.node_definition = "iosv" if idx % 2 == 0 else "iosvl2"
        self.state = "BOOTED"
        self.x = idx * 10
        self.y = idx * 5

    def is_booted(self) -> bool:
        return True


class _FakeInterface:
    def __init__(self, label: str):
        self.label = label


class _FakeLink:
    def __init__(self, idx: int, a: _FakeNode, b: _FakeNode):
        self.id = f"l{idx}"
        self.node_a = a
        self.node_b = b
        self.interface_a = _FakeInterface("G0/0")
        self.interface_b = _FakeInterface("G0/1")
        self.state = "UP"


class _FakeLab:
    """Mimics the subset of the virl2_client Lab API get_topology() uses."""

    def __init__(self, n_nodes: int):
        self._nodes = [_FakeNode(i) for i in range(n_nodes)]
        # Chain topology: node i ↔ node i+1 (n-1 links).
        self._links = [
            _FakeLink(i, self._nodes[i], self._nodes[i + 1])
            for i in range(max(0, n_nodes - 1))
        ]
        self.sync_calls = 0

    def sync_states_if_outdated(self) -> None:
        self.sync_calls += 1  # incremental refresh — O(1) here

    def nodes(self):
        return self._nodes

    def links(self):
        return self._links


class _FakeClientLib:
    def __init__(self, lab: _FakeLab):
        self._lab = lab

    def get_local_lab(self, _lab_id: str):
        return self._lab

    def join_existing_lab(self, _lab_id: str):
        return self._lab


def _make_client(n_nodes: int) -> CMLClient:
    host = CMLHost(url="https://loadtest.invalid", username="LOADTEST")
    client = CMLClient(host, password="")
    client._client = _FakeClientLib(_FakeLab(n_nodes))  # inject fake (no network)
    return client


def run(sizes: list[int], repeat: int) -> int:
    print(f"{'nodes':>7} {'links':>7} {'best_ms':>9} {'ms/node':>9}")
    print("-" * 36)
    per_node_samples: list[float] = []
    for n in sizes:
        best = float("inf")
        links = max(0, n - 1)
        for _ in range(repeat):
            client = _make_client(n)
            t0 = time.perf_counter()
            topo = client.get_topology("loadtest")
            dt = (time.perf_counter() - t0) * 1000.0
            best = min(best, dt)
            assert len(topo.nodes) == n
            assert len(topo.links) == links
        per_node = best / n if n else 0.0
        per_node_samples.append(per_node)
        print(f"{n:>7} {links:>7} {best:>9.2f} {per_node:>9.4f}")

    # Lag-growth check: per-node time at the largest size should not be wildly
    # higher than at the smallest (allows 3x headroom for fixed overhead).
    if len(per_node_samples) >= 2:
        small, large = per_node_samples[0], per_node_samples[-1]
        ratio = (large / small) if small else 1.0
        verdict = "OK (linear, no lag growth)" if ratio <= 3.0 else "WARN (super-linear!)"
        print("-" * 36)
        print(f"per-node ratio largest/smallest = {ratio:.2f} -> {verdict}")
        return 0 if ratio <= 3.0 else 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CLIQ large-topology load test")
    ap.add_argument("--sizes", type=int, nargs="+",
                    default=[10, 50, 100, 250, 500])
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    return run(args.sizes, args.repeat)


if __name__ == "__main__":
    raise SystemExit(main())
