#!/usr/bin/env python3
"""
test_ingest_storage.py — Tests for the CLIQ storage & ingestion layer (Phase 1B/1C).

Covers the success criteria:
  - entries land in BOTH SQLite and the Vector DB, reconciled and traceable
  - duplicates are resolved and LOGGED (never silently dropped)
  - re-ingestion is idempotent (no duplicate rows)
  - cross-platform duplicate commands are preserved
  - empty-description entries are stored but flagged (low confidence)
  - a document with zero entries is reported, not crashed
  - partial failure (one bad doc) does not abort a multi-doc run
  - validation report reconciles + produces a quality score
  - exact SQLite lookup stays fast after ingestion
"""

import os
import tempfile
import time
import unittest

from cliq.ingest.schema import CommandEntry, CONFIDENCE_LOW
from cliq.ingest.storage import IngestionPipeline, DedupPolicy
from cliq.ingest.storage.embeddings import LocalHashingEmbedder, get_embedder


def _entry(command, desc="A useful command description that is long enough.",
           platform="iosxe", mode="exec", doc="ref.txt", conf="ok", flags=None):
    return CommandEntry(
        command=command, description=desc, syntax_variants=[],
        mode=mode, platform=platform, doc_source=doc,
        confidence=conf, flags=flags or [],
    )


class _PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = os.path.join(self.tmp, "kb.sqlite")
        self.vec = os.path.join(self.tmp, "vec.sqlite")

    def make_pipe(self, policy=DedupPolicy.KEEP_FIRST):
        return IngestionPipeline(
            dedup_policy=policy, kb_db_path=self.kb, vector_db_path=self.vec,
            embedder=LocalHashingEmbedder(),
        )


class TestDualStorage(_PipelineTestBase):
    def test_lands_in_both_stores_and_reconciles(self):
        pipe = self.make_pipe()
        entries = [_entry("show ip route"), _entry("show version")]
        pipe.ingest_entries(entries, "ref.txt", "iosxe")
        report = pipe.build_report([])
        self.assertEqual(report.sqlite_total, 2)
        self.assertEqual(report.vector_total, 2)
        self.assertTrue(report.reconciled, report.reconcile_detail)
        self.assertEqual(report.missing_vector_total, 0)
        pipe.close()

    def test_vector_traceable_to_sqlite(self):
        pipe = self.make_pipe()
        pipe.ingest_entries([_entry("show ip route")], "ref.txt", "iosxe")
        hits = pipe.semantic_search("display the routing table", top_k=1)
        self.assertEqual(len(hits), 1)
        sqlite_id = hits[0].metadata["sqlite_id"]
        row = pipe.store.get_by_id(sqlite_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["command"], "show ip route")
        self.assertEqual(row["vector_id"], hits[0].vector_id)
        pipe.close()


class TestDeduplication(_PipelineTestBase):
    def test_duplicates_resolved_and_logged_not_dropped(self):
        pipe = self.make_pipe(DedupPolicy.KEEP_FIRST)
        dup = _entry("show ip route", desc="first copy, the longer description here.")
        dup2 = _entry("show ip route", desc="second", flags=["duplicate"])
        result = pipe.ingest_entries([dup, dup2], "ref.txt", "iosxe")
        # Only ONE row stored for the (command, platform) pair...
        self.assertEqual(pipe.store.count(), 1)
        # ...but the duplicate was RESOLVED and LOGGED, not silently dropped.
        self.assertEqual(result.duplicates_resolved, 1)
        logged = pipe.store._conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE decision='duplicate_resolved'"
        ).fetchone()[0]
        self.assertEqual(logged, 1)
        pipe.close()

    def test_keep_most_complete_policy(self):
        pipe = self.make_pipe(DedupPolicy.KEEP_MOST_COMPLETE)
        short = _entry("show ip route", desc="short one.")
        longer = _entry("show ip route",
                        desc="A much longer and more complete description of the command.")
        pipe.ingest_entries([short, longer], "ref.txt", "iosxe")
        row = pipe.store.get_by_command_platform("show ip route", "iosxe")
        self.assertIn("more complete", row["description"])
        pipe.close()

    def test_cross_platform_duplicates_preserved(self):
        pipe = self.make_pipe()
        pipe.ingest_entries(
            [_entry("show ip route", platform="iosxe"),
             _entry("show ip route", platform="iosxr")],
            "ref.txt", "iosxe",
        )
        self.assertEqual(pipe.store.count(), 2)  # both platforms kept
        by_plat = pipe.store.count_by_platform()
        self.assertEqual(by_plat.get("iosxe"), 1)
        self.assertEqual(by_plat.get("iosxr"), 1)
        pipe.close()


class TestIdempotency(_PipelineTestBase):
    def test_reingest_same_entries_no_duplicates(self):
        pipe = self.make_pipe()
        entries = [_entry("show ip route"), _entry("show version")]
        pipe.ingest_entries(entries, "ref.txt", "iosxe")
        # Re-ingest identical content — must NOT create new rows.
        result2 = pipe.ingest_entries(entries, "ref.txt", "iosxe")
        self.assertTrue(result2.skipped_idempotent)
        self.assertEqual(pipe.store.count(), 2)
        self.assertEqual(pipe.vectors.count(), 2)
        pipe.close()

    def test_changed_entry_updates_in_place(self):
        pipe = self.make_pipe()
        pipe.ingest_entries([_entry("show ip route", desc="original description here.")],
                            "ref.txt", "iosxe")
        # Same command, new description -> update, not insert.
        r = pipe.ingest_entries([_entry("show ip route", desc="updated description text now.")],
                                "ref.txt", "iosxe", force=True)
        self.assertEqual(pipe.store.count(), 1)
        self.assertEqual(r.updated, 1)
        row = pipe.store.get_by_command_platform("show ip route", "iosxe")
        self.assertIn("updated", row["description"])
        pipe.close()

    def test_refresh_document_clean_reingest(self):
        pipe = self.make_pipe()
        pipe.ingest_entries([_entry("show ip route"), _entry("show version")],
                            "ref.txt", "iosxe")
        # Refresh with fewer entries: stale rows for this doc are removed.
        pipe.ingest_entries([_entry("show ip route")], "ref.txt", "iosxe", refresh=True)
        self.assertEqual(pipe.store.count(), 1)
        self.assertEqual(pipe.vectors.count(), 1)
        pipe.close()


class TestEdgeCases(_PipelineTestBase):
    def test_empty_description_stored_but_flagged(self):
        pipe = self.make_pipe()
        e = _entry("ip routing", desc="", conf=CONFIDENCE_LOW, flags=["empty_description"])
        pipe.ingest_entries([e], "ref.txt", "iosxe")
        self.assertEqual(pipe.store.count(), 1)              # stored
        self.assertEqual(pipe.store.count_empty_description(), 1)
        self.assertEqual(pipe.store.count_low_confidence(), 1)  # flagged
        pipe.close()

    def test_zero_entry_document_reported_not_crashed(self):
        pipe = self.make_pipe()
        result = pipe.ingest_entries([], "empty.txt", "iosxe")
        self.assertEqual(result.entries_seen, 0)
        self.assertTrue(result.warnings)
        self.assertEqual(pipe.store.count(), 0)
        pipe.close()

    def test_partial_failure_isolated(self):
        pipe = self.make_pipe()
        # Two real docs; a third path does not exist. The bad one must not
        # abort the run, and the good entries must still be stored.
        good_txt = os.path.join(self.tmp, "good.txt")
        with open(good_txt, "w", encoding="utf-8") as fh:
            fh.write(
                "show ip route\n"
                "To display the current state of the routing table, use the "
                "show ip route command in privileged EXEC mode.\n"
            )
        report = pipe.ingest_documents([
            (good_txt, "iosxe"),
            (os.path.join(self.tmp, "missing.pdf"), "iosxr"),
        ])
        self.assertGreaterEqual(report.sqlite_total, 1)
        # The missing doc is reported as a warning/zero-entry, run still completes.
        self.assertEqual(len(report.per_doc), 2)
        pipe.close()


class TestValidationReport(_PipelineTestBase):
    def test_report_and_quality_score(self):
        pipe = self.make_pipe()
        pipe.ingest_entries(
            [_entry("show ip route"), _entry("show version"),
             _entry("ip routing", desc="", conf=CONFIDENCE_LOW, flags=["empty_description"])],
            "ref.txt", "iosxe",
        )
        report = pipe.build_report([])
        self.assertEqual(report.sqlite_total, 3)
        self.assertTrue(report.reconciled)
        self.assertEqual(report.low_confidence_total, 1)
        self.assertTrue(report.high_value_present["show ip route"])
        self.assertIn("show running-config", report.high_value_present)
        self.assertGreater(report.quality_score, 0)
        self.assertLessEqual(report.quality_score, 100)
        # spot-check renders both stores
        sc = pipe.spot_check(limit=3)
        self.assertIn("SQLite sample", sc)
        self.assertIn("Vector DB sample", sc)
        self.assertIn("High-value commands", sc)
        pipe.close()

    def test_missing_high_value_command_is_backfilled(self):
        pipe = self.make_pipe()
        pipe.ingest_entries([_entry("show version")], "ref.txt", "iosxe", ensure_high_value=True)
        row = pipe.store.get_by_command_platform("show ip route", "iosxe")
        self.assertIsNotNone(row)
        self.assertEqual(row["doc_source"], "curated_show_commands.yaml")
        report = pipe.build_report([])
        self.assertTrue(report.high_value_present["show ip route"])
        pipe.close()


class TestLatency(_PipelineTestBase):
    def test_exact_lookup_fast_after_ingestion(self):
        pipe = self.make_pipe()
        entries = [_entry(f"show item {i}", platform="iosxe") for i in range(500)]
        pipe.ingest_entries(entries, "big.txt", "iosxe")
        self.assertEqual(pipe.store.count(), 500)
        t0 = time.perf_counter()
        row = pipe.store.get_by_command_platform("show item 321", "iosxe")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertIsNotNone(row)
        self.assertLess(elapsed_ms, 50.0, f"lookup took {elapsed_ms:.1f}ms")
        pipe.close()


class TestEmbedderSwappable(unittest.TestCase):
    def test_local_hashing_embedder_is_deterministic(self):
        emb = get_embedder("local-hashing")
        v1 = emb.embed("show ip route")
        v2 = emb.embed("show ip route")
        self.assertEqual(v1, v2)                 # deterministic
        self.assertEqual(len(v1), emb.dim)
        self.assertEqual(getattr(emb, "name", ""), "local-hashing")

    def test_unknown_embedder_raises(self):
        with self.assertRaises(ValueError):
            get_embedder("does-not-exist")


if __name__ == "__main__":
    unittest.main()
