#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_phase6_intel.py — Phase 6 preview/validation script.

Demonstrates the full inline intelligence integration:
  1. Command typed → inline suggestion + validation advisory
  2. Engineer asks a question → tiered answer
  3. Known past issue → memory recall surfaces the resolution
  4. Blocklisted command → critical advisory warning (never executes)
  5. CLIQ never executes anything on the device.

Run from repo root:
    PYTHONPATH=src .venv\\Scripts\\python.exe tools\\test_phase6_intel.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Force offline so the embedder never phones home.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

# ── ANSI helpers ─────────────────────────────────────────────────────────────
CYAN   = "\x1b[36m"
GREEN  = "\x1b[32m"
YELLOW = "\x1b[33m"
RED    = "\x1b[31m"
VIOLET = "\x1b[35m"
BOLD   = "\x1b[1m"
DIM    = "\x1b[2m"
RST    = "\x1b[0m"


def header(text: str) -> None:
    width = 70
    print(f"\n{BOLD}{CYAN}{'─' * width}{RST}")
    print(f"{BOLD}{CYAN}  {text}{RST}")
    print(f"{BOLD}{CYAN}{'─' * width}{RST}\n")


def cliq_tag(severity: str = "info") -> str:
    colors = {"info": CYAN, "warning": YELLOW, "critical": RED}
    c = colors.get(severity, CYAN)
    return f"{c}{BOLD}⚡ CLIQ [{severity.upper()}]:{RST}{c}"


def device_line(text: str) -> str:
    """Simulate a device prompt line (green, like a real terminal)."""
    return f"{GREEN}{text}{RST}"


# ── 1. Load the advisory stack ───────────────────────────────────────────────

def load_stack(kb_db: str = "./kb/cliq_kb.sqlite", vec_db: str = "./kb/cliq_vec.sqlite"):
    """Load retrieval assistant + validator + memory store."""
    from cliq.ingest.storage.sqlite_store import CommandStore
    from cliq.ingest.storage.vector_store import SQLiteVectorStore
    from cliq.retrieval.assistant import IntelligentCommandAssistant
    from cliq.retrieval.query import CommandRetriever
    from cliq.validation.validator import SafetyValidator
    from cliq.memory.operational_memory import OperationalMemoryStore

    if not Path(kb_db).exists() or not Path(vec_db).exists():
        print(f"{YELLOW}⚠  KB files not found ({kb_db} / {vec_db}).{RST}")
        print(f"{DIM}   Run build_db.py and re_embed_kb.py first.{RST}\n")
        return None, None, None

    sqlite_store = CommandStore(kb_db)
    vector_store = SQLiteVectorStore(vec_db)
    retriever = CommandRetriever(sqlite_store, vector_store)
    assistant = IntelligentCommandAssistant(retriever, enable_memory=False)
    validator = SafetyValidator()
    memory_store = OperationalMemoryStore()
    return assistant, validator, memory_store


# ── 2. Scenario helpers ──────────────────────────────────────────────────────

def demo_suggest_and_validate(assistant, validator):
    """Scenario: engineer types a partial/mistyped command."""
    header("Scenario 1 — Inline Suggestion + Validation (as engineer types)")

    cases = [
        ("show ip rout", "iosxe", "exec"),
        ("shwo ip route", "iosxe", "exec"),
        ("reload", "iosxe", "exec"),
        ("debug ip ospf events", "iosxe", "exec"),
    ]

    for cmd, platform, mode in cases:
        print(f"  {device_line('R1>')} {cmd}")

        # Validation advisory (SafetyValidator)
        from cliq.validation.schema import ValidationContext
        ctx = ValidationContext(command=cmd, device_platform=platform, current_mode=mode)
        verdict = validator.validate(ctx)
        severity = verdict.severity.value
        if not verdict.valid:
            print(f"  {cliq_tag(severity)} {verdict.reason}")
            if verdict.suggested_action:
                print(f"  {DIM}  → {verdict.suggested_action}{RST}")
        else:
            print(f"  {cliq_tag('info')} Command looks valid ({verdict.verdict_type.value}){RST}")

        # Suggestion advisory (IntelligentCommandAssistant)
        if assistant is not None:
            from cliq.retrieval.schema import RetrievalQuery
            q = RetrievalQuery(input_text=cmd, mode="auto", platform=platform, top_k=3, min_confidence=0.3)
            try:
                resp = assistant.respond(q)
                top = resp.results[0] if resp.results else None
                if top and top.command.lower() != cmd.lower():
                    print(f"  {cliq_tag('info')} Did you mean: {BOLD}{top.command}{RST}{CYAN}?{RST}")
                elif top:
                    print(f"  {cliq_tag('info')} Exact match: {BOLD}{top.command}{RST}")
                elif resp.message:
                    print(f"  {cliq_tag('info')} {resp.message}")
            except Exception as exc:
                print(f"  {DIM}(suggestion unavailable: {exc}){RST}")

        print()


def demo_ask_anything(assistant):
    """Scenario: engineer asks natural-language questions in the terminal."""
    header("Scenario 2 — Ask Anything in the Terminal (Q&A mode)")

    questions = [
        "how do i check ospf neighbors",
        "what commands for multicast",
        "show me routing table commands",
        "verify bgp neighbor status",
    ]

    if assistant is None:
        print(f"  {YELLOW}⚠  Knowledge base not loaded; skipping Q&A demo.{RST}\n")
        return

    from cliq.retrieval.schema import RetrievalQuery
    for question in questions:
        print(f"  {VIOLET}{BOLD}? {question}{RST}")
        q = RetrievalQuery(input_text=question, mode="auto", top_k=4, min_confidence=0.3)
        try:
            resp = assistant.respond(q)
            tier_color = {"exact": GREEN, "suggestion": YELLOW, "related": CYAN, "fallback": DIM}.get(
                resp.response_tier, DIM
            )
            print(f"  {BOLD}⚡ CLIQ [{resp.response_tier}]:{RST} {resp.message}")
            if resp.results:
                for r in resp.results[:3]:
                    print(f"    {GREEN}• {r.command}{RST}  {DIM}{r.description[:60] or ''}{RST}")
            elif resp.grouped_results:
                for g in resp.grouped_results[:2]:
                    print(f"    {CYAN}{g.label}:{RST}")
                    for r in g.results[:3]:
                        print(f"      {GREEN}• {r.command}{RST}  {DIM}{r.description[:50] or ''}{RST}")
        except Exception as exc:
            print(f"  {DIM}(error: {exc}){RST}")
        print()


def demo_memory_recall(memory_store):
    """Scenario: seed memory with a past resolution, then recall it."""
    header("Scenario 3 — Memory-Augmented Recall (past resolution surfaces inline)")

    # Seed a known past issue.
    memory_id = memory_store.store_memory(
        issue_text="OSPF neighbors stuck in EXSTART state after link flap",
        commands=["show ip ospf neighbor", "debug ip ospf adj", "clear ip ospf process"],
        platform="iosxe",
        device_name="R1",
        outcome="resolved",
        outcome_notes="Cleared OSPF process; adjacency re-formed within 30 s.",
        scope="cross_device",
    )
    print(f"  {DIM}(seeded memory: {memory_id}){RST}\n")

    # Now simulate engineer typing a related query and recall firing.
    query = "ospf neighbors not coming up"
    print(f"  {device_line('R1>')} {query}")
    print(f"  {cliq_tag('info')} Recalling past resolutions…\n")

    resolutions = memory_store.recall_similar(query, top_k=3, min_similarity=0.3)
    if resolutions:
        for pr in resolutions:
            sim_pct = round(pr.final_rank_score * 100)
            mr = pr.memory_record
            sim_pct = round(pr.final_rank_score * 100)
            print(f"  {VIOLET}{BOLD}[MEMORY] Past Resolution ({sim_pct}% similar):{RST}")
            print(f"  {BOLD}Issue:{RST} {mr.issue_text}")
            if mr.commands:
                cmds = "  ".join(mr.commands[:3])
                print(f"  {BOLD}Commands:{RST} {GREEN}{cmds}{RST}")
            if mr.outcome_notes:
                print(f"  {BOLD}Outcome:{RST} {DIM}{mr.outcome_notes}{RST}")
            print(f"  {DIM}Platform: {mr.platform}  Device: {mr.device_name or 'no device'}  Status: {mr.outcome}{RST}")
            print()
    else:
        print(f"  {DIM}No similar past resolutions found.{RST}\n")

    # Mark the query as resolved.
    new_id = memory_store.store_memory(
        issue_text=query,
        commands=["show ip ospf neighbor", "clear ip ospf process"],
        platform="iosxe",
        device_name="R2",
        outcome="resolved",
        outcome_notes="Same EXSTART fix worked.",
        scope="cross_device",
    )
    print(f"  {GREEN}✓ Engineer marked issue resolved (memory_id={new_id}){RST}\n")


def demo_blocklist_advisory(validator):
    """Scenario: blocklisted command → clear advisory warning, never blocked."""
    header("Scenario 4 — Blocklisted Command Advisory (critical warning, advisory only)")

    blocklisted = ["reload", "write erase", "erase startup-config", "format flash:"]

    from cliq.validation.schema import ValidationContext
    for cmd in blocklisted:
        ctx = ValidationContext(command=cmd, device_platform="iosxe", current_mode="enable")
        verdict = validator.validate(ctx)
        severity = verdict.severity.value
        icon = "[CRIT]" if severity == "critical" else "[WARN]"
        print(f"  {device_line('R1#')} {cmd}")
        print(f"  {icon} {cliq_tag(severity)} {verdict.reason}")
        if verdict.suggested_action:
            print(f"     {DIM}Advisory: {verdict.suggested_action}{RST}")
        print(f"     {DIM}(CLIQ does NOT block the engineer — they remain in full control){RST}\n")


def demo_separation_principle():
    """Print the advisory-only principle clearly."""
    header("Phase 6 Core Principle — Advisory Only")
    lines = [
        "CLIQ never executes commands on the device.",
        "The engineer types and runs every command themselves.",
        "CLIQ provides: suggestions, validation warnings, Q&A answers, memory recalls.",
        "All output is visually distinct from device output (⚡ CLIQ prefix, color-coded).",
        "Blocklisted commands show a CRITICAL advisory — but the engineer can still proceed.",
    ]
    for line in lines:
        print(f"  {GREEN}✓{RST} {line}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Force UTF-8 on Windows so ANSI + Unicode glyphs print correctly.
    import sys, io
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(f"\n{BOLD}{CYAN}Phase 6 -- Terminal Intelligence Integration Preview{RST}")
    print(f"{DIM}Advisory-only: CLIQ never executes on the device.{RST}\n")

    assistant, validator, memory_store = load_stack()

    demo_separation_principle()

    if validator:
        demo_suggest_and_validate(assistant, validator)
        demo_blocklist_advisory(validator)

    demo_ask_anything(assistant)

    if memory_store:
        demo_memory_recall(memory_store)

    print(f"{BOLD}{GREEN}Phase 6 preview complete.{RST}\n")
    if assistant is None:
        print(f"{YELLOW}Note:{RST} Build the KB to enable full Q&A + suggestion demos:")
        print(f"  {DIM}PYTHONPATH=src .venv\\Scripts\\python.exe build_db.py{RST}")
        print(f"  {DIM}PYTHONPATH=src .venv\\Scripts\\python.exe tools\\re_embed_kb.py{RST}\n")


if __name__ == "__main__":
    main()
