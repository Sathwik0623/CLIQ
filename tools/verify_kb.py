#!/usr/bin/env python3
"""
verify_kb.py — Track A: capability-gating verification over the REAL KB.

Runs the bidirectional gating proof against ``./kb/cliq_kb.sqlite`` using the
read-only :class:`KnowledgeTree`. The KB is enrichment only; this tool just
asserts that capability gating prunes branches correctly per device class:

    ROUTING  gated OFF a pure L2 switch (proven when routing PDFs ingested)
    SWITCHING gated OFF a pure router   (proven once the lan-switching PDF lands)

USAGE
    python tools/verify_kb.py
    python tools/verify_kb.py --kb-db ./kb/cliq_kb.sqlite

Exit code 0 = all assertions held; 1 = at least one gate failed (or the
lan-switching branch is not yet ingested, which is reported explicitly).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest.tree import KnowledgeTree  # noqa: E402

# (capability_class, protocol_hint, comparator, label)
#   comparator: "zero" expects 0 nodes; "some" expects > 0 nodes.
_GATE_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    # --- mirror gate: switching gated OFF a pure router -------------------
    ("router",    "vlan",          "zero", "VLAN gated OFF pure router"),
    ("l2-switch", "vlan",          "some", "VLAN available on L2 switch"),
    ("l3-switch", "vlan",          "some", "VLAN available on L3 switch"),
    ("router",    "spanning-tree", "zero", "STP gated OFF router"),
    ("l2-switch", "spanning-tree", "some", "STP available on L2 switch"),
    # --- routing untouched (no regression) -------------------------------
    ("router",    "ospf",          "some", "Routing still works on router"),
    ("l3-switch", "ospf",          "some", "Routing works on L3 switch"),
    ("l2-switch", "ospf",          "zero", "Routing gated OFF L2 switch"),
)


def _verify(kb_db: str) -> int:
    tree = KnowledgeTree(kb_db)
    protocols = tree.protocols()
    has_switching = any(p in protocols for p in ("vlan", "spanning-tree"))

    print(f"KB: {kb_db}")
    print(f"protocols ingested: {len(protocols)} "
          f"(switching present: {'yes' if has_switching else 'NO — ingest lan-switching PDF'})")
    print(f"  {'capability':<11} {'protocol':<15} {'count':>6}  result")
    print("  " + "-" * 60)

    failures = 0
    for cap, proto, comparator, label in _GATE_CHECKS:
        n = len(tree.lookup(cap, protocol_hint=proto))
        ok = (comparator == "zero" and n == 0) or (comparator == "some" and n > 0)
        # If switching isn't ingested yet, the "some" switching checks can't pass
        # — flag them as PENDING rather than a hard failure of the gate logic.
        pending = (not has_switching and comparator == "some"
                   and proto in ("vlan", "spanning-tree"))
        if pending:
            status = "PENDING (lan-switching not ingested)"
        elif ok:
            status = "ok"
        else:
            status = "FAIL"
            failures += 1
        print(f"  {cap:<11} {proto:<15} {n:>6}  {status}  — {label}")

    tree.close()
    print()
    if failures:
        print(f"RESULT: {failures} gate check(s) FAILED.")
        return 1
    if not has_switching:
        print("RESULT: routing gates hold; lan-switching gate PENDING ingestion.")
        return 1
    print("RESULT: bidirectional gating proven (routing↔switching). ✅")
    return 0


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_kb = os.path.join(os.path.dirname(__file__), "..", "kb", "cliq_kb.sqlite")
    ap.add_argument("--kb-db", default=os.path.normpath(default_kb),
                    help="SQLite KB path (default: ./kb/cliq_kb.sqlite)")
    args = ap.parse_args()
    if not os.path.isfile(args.kb_db):
        print(f"!! KB not found: {args.kb_db}")
        return 2
    return _verify(args.kb_db)


if __name__ == "__main__":
    raise SystemExit(main())
