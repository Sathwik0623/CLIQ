#!/usr/bin/env python3
"""
validate_phase_351.py — Offline validation harness for Phase 3.5.1/3.5.2.

Uses the same production retrieval + embedder path and verifies:
- Exact-tier behavior for key commands
- Typo behavior
- Description cleanliness
- Store reconciliation
"""

from __future__ import annotations

import os
import sqlite3
import sys

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from cliq.ingest.storage.sqlite_store import CommandStore
from cliq.ingest.storage.vector_store import SQLiteVectorStore
from cliq.retrieval.assistant import IntelligentCommandAssistant
from cliq.retrieval.query import CommandRetriever
from cliq.retrieval.schema import RetrievalQuery


def _assert_exact(assistant: IntelligentCommandAssistant, query: str, expected: str) -> tuple[bool, str]:
    response = assistant.respond(RetrievalQuery(input_text=query))
    ok = bool(
        response.response_tier == "exact"
        and response.results
        and response.results[0].command == expected
    )
    line = f"{query} -> tier={response.response_tier}, top={response.results[0].command if response.results else 'none'}"
    return ok, line


def _assert_typo(assistant: IntelligentCommandAssistant) -> tuple[bool, str]:
    response = assistant.respond(RetrievalQuery(input_text="shwo ip rote"))
    top = response.results[0].command if response.results else "none"
    ok = response.response_tier in {"suggestion", "exact"} and top == "show ip route"
    return ok, f"shwo ip rote -> tier={response.response_tier}, top={top}"


def _assert_clean_text(assistant: IntelligentCommandAssistant) -> tuple[bool, str]:
    response = assistant.respond(RetrievalQuery(input_text="how do i check ospf neighbors"))
    if not response.results:
        return False, "how do i check ospf neighbors -> no results"
    desc = response.results[0].description
    dirty = ["ipo s pf", "IOSXEG i br altar"]
    bad = [fragment for fragment in dirty if fragment in desc]
    return (not bad), (
        "how do i check ospf neighbors -> clean description"
        if not bad
        else f"how do i check ospf neighbors -> dirty fragments found: {bad}"
    )


def _assert_reconciled() -> tuple[bool, str]:
    kb = sqlite3.connect("./kb/cliq_kb.sqlite")
    vec = sqlite3.connect("./kb/cliq_vec.sqlite")
    try:
        kb_count = kb.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        vec_count = vec.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    finally:
        kb.close()
        vec.close()
    ok = kb_count == vec_count
    return ok, f"reconciliation -> sqlite={kb_count}, vectors={vec_count}"


def main() -> int:
    sqlite_store = CommandStore("./kb/cliq_kb.sqlite")
    vector_store = SQLiteVectorStore("./kb/cliq_vec.sqlite")
    try:
        assistant = IntelligentCommandAssistant(CommandRetriever(sqlite_store, vector_store))

        checks = [
            _assert_exact(assistant, "show ip route", "show ip route"),
            _assert_exact(assistant, "show ip interface brief", "show ip interface brief"),
            _assert_exact(assistant, "show running-config", "show running-config"),
            _assert_exact(assistant, "show version", "show version"),
            _assert_typo(assistant),
            _assert_clean_text(assistant),
            _assert_reconciled(),
        ]

        failed = False
        for ok, line in checks:
            print(f"[{'OK' if ok else 'FAIL'}] {line}")
            failed = failed or (not ok)

        if failed:
            print("\nValidation failed.")
            return 1

        print("\nValidation passed.")
        return 0
    finally:
        sqlite_store.close()
        vector_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
