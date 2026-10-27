#!/usr/bin/env python3
"""
test_pdf_tree.py — Track A verification: PDF → tree-structured, capability-tagged KB.

Covers the spec's verification checklist:
  - PDF filenames classify to the correct category/protocol branch.
  - Capability gating: lan-switching → [l2-switch, l3-switch] (NOT router);
    routing commands → [router, l3-switch].
  - Out-of-scope docs (voice/wireless) are skipped.
  - The KB schema migrates additively (existing untagged rows stay queryable).
  - tree.lookup() gates by capability class, scopes by protocol, and matches
    command paths — with NO cross-protocol bleed.
  - Ingested nodes are tagged command_type=PI / source=kb (enrichment only).
"""

import os
import tempfile
import unittest
from unittest import mock

from cliq.ingest.schema import CommandEntry
from cliq.ingest.storage import IngestionPipeline
from cliq.ingest.storage.embeddings import LocalHashingEmbedder
from cliq.ingest.storage.sqlite_store import CommandStore
from cliq.ingest import taxonomy
from cliq.ingest.tree import KnowledgeTree
from cliq.ingest.pdf_ingest import PdfTreeIngestor, tag_entries


# ---------------------------------------------------------------------------
# Part 2/4 — taxonomy: branch mapping + capability gating
# ---------------------------------------------------------------------------
class TestTaxonomy(unittest.TestCase):
    def test_routing_protocols_map_to_router_l3(self):
        for fn, proto in [("ospf_cmd_ref.pdf", "ospf"),
                          ("bgp_command_reference.pdf", "bgp"),
                          ("eigrp_cr.pdf", "eigrp")]:
            cls = taxonomy.classify_pdf(fn)
            self.assertFalse(cls.skip, fn)
            self.assertEqual(cls.category, "ip-routing", fn)
            self.assertEqual(cls.protocol, proto, fn)
            self.assertEqual(cls.capability_req, "routing", fn)
            self.assertEqual(cls.applies_to, ["router", "l3-switch"], fn)
            self.assertEqual(cls.command_type, "PI", fn)
            self.assertEqual(cls.source, "kb", fn)

    def test_lan_switching_is_gated_to_switches(self):
        for fn in ("vlan_cmd_ref.pdf", "spanning-tree_cr.pdf", "lan-switching.pdf"):
            cls = taxonomy.classify_pdf(fn)
            self.assertEqual(cls.category, "lan-switching", fn)
            self.assertEqual(cls.capability_req, "switching", fn)
            self.assertEqual(cls.applies_to, ["l2-switch", "l3-switch"], fn)
            # The critical rule: never on a pure router.
            self.assertNotIn("router", cls.applies_to, fn)
            self.assertTrue(taxonomy.is_gated_to_switching(cls.applies_to), fn)

    def test_universal_categories_apply_to_all(self):
        for fn, cat in [("debug_a_through_z.pdf", "debug"),
                        ("snmp_cmd_ref.pdf", "network-mgmt"),
                        ("qos_command_reference.pdf", "qos"),
                        ("crypto_security_cr.pdf", "security")]:
            cls = taxonomy.classify_pdf(fn)
            self.assertEqual(cls.category, cat, fn)
            self.assertEqual(cls.applies_to, ["all"], fn)

    def test_services_and_mpls_and_wan(self):
        self.assertEqual(taxonomy.classify_pdf("hsrp_fhrp_cr.pdf").protocol, "fhrp")
        self.assertEqual(taxonomy.classify_pdf("dhcp_cmd_ref.pdf").protocol, "ip-addressing")
        self.assertEqual(taxonomy.classify_pdf("mpls_cmd_ref.pdf").category, "mpls")
        self.assertEqual(taxonomy.classify_pdf("atm_broadband_cr.pdf").category, "wan")


# ---------------------------------------------------------------------------
# Per-command protocol derivation (multi-family lan-switching branch)
# ---------------------------------------------------------------------------
class TestProtocolDerivation(unittest.TestCase):
    def test_vlan_family(self):
        self.assertEqual(taxonomy.derive_command_protocol("show vlan brief", "vlan"), "vlan")
        self.assertEqual(taxonomy.derive_command_protocol("vlan 10", "vlan"), "vlan")

    def test_spanning_tree_family(self):
        self.assertEqual(
            taxonomy.derive_command_protocol("spanning-tree mode rapid-pvst", "vlan"),
            "spanning-tree")
        self.assertEqual(
            taxonomy.derive_command_protocol("show spanning-tree vlan 1", "vlan"),
            "spanning-tree")

    def test_switchport_before_trunk_and_vlan(self):
        # 'switchport trunk allowed vlan' contains trunk AND vlan, but the
        # primary family is switchport (most specific wins by rule order).
        self.assertEqual(
            taxonomy.derive_command_protocol("switchport trunk allowed vlan 10", "vlan"),
            "switchport")
        self.assertEqual(
            taxonomy.derive_command_protocol("switchport mode trunk", "vlan"),
            "switchport")

    def test_etherchannel_family(self):
        for cmd in ("channel-group 1 mode active", "show etherchannel summary",
                    "port-channel load-balance src-mac"):
            self.assertEqual(
                taxonomy.derive_command_protocol(cmd, "vlan"), "etherchannel", cmd)

    def test_fallback_when_no_family_matches(self):
        self.assertEqual(taxonomy.derive_command_protocol("show version", "vlan"), "vlan")
        self.assertEqual(taxonomy.derive_command_protocol("", "vlan"), "vlan")

    def test_protocol_for_command_only_derives_for_multi_family(self):
        # Single-topic routing branch keeps the PDF-level protocol untouched
        # (no regression: an OSPF command is never re-bucketed).
        self.assertEqual(
            taxonomy.protocol_for_command("ip-routing", "show ip ospf neighbor", "ospf"),
            "ospf")
        # The lan-switching branch derives per command.
        self.assertEqual(
            taxonomy.protocol_for_command("lan-switching", "spanning-tree mode mst", "vlan"),
            "spanning-tree")

    def test_out_of_scope_skipped(self):
        for fn in ("cube_voice_cr.pdf", "wireless_mobility_cmd.pdf", "wlan_cr.pdf"):
            cls = taxonomy.classify_pdf(fn)
            self.assertTrue(cls.skip, fn)

    def test_unrecognised_is_skipped_not_misfiled(self):
        cls = taxonomy.classify_pdf("b_xe_17_random.pdf")
        self.assertTrue(cls.skip)
        self.assertFalse(cls.confident)

    def test_explicit_override_forces_branch(self):
        cls = taxonomy.classify_pdf("b_xe_17_random.pdf",
                                    category="ip-routing", protocol="ospf")
        self.assertEqual(cls.category, "ip-routing")
        self.assertEqual(cls.protocol, "ospf")
        self.assertEqual(cls.applies_to, ["router", "l3-switch"])

    def test_priority_order_routing_first(self):
        self.assertLess(
            taxonomy.classify_pdf("ospf_cr.pdf").priority,
            taxonomy.classify_pdf("atm_cr.pdf").priority,
        )


# ---------------------------------------------------------------------------
# Part 1/5 — schema migration is additive; existing rows survive
# ---------------------------------------------------------------------------
class TestSchemaMigration(unittest.TestCase):
    def test_legacy_db_gains_tree_columns_without_data_loss(self):
        import sqlite3, time
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "legacy.sqlite")
            # Build a pre-Track-A "commands" table (no tree columns).
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL, description TEXT DEFAULT '',
                    syntax_variants TEXT DEFAULT '[]', mode TEXT DEFAULT 'exec',
                    platform TEXT NOT NULL, doc_source TEXT DEFAULT '',
                    confidence TEXT DEFAULT 'ok', flags TEXT DEFAULT '[]',
                    content_hash TEXT NOT NULL, vector_id TEXT DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(command, platform)
                );
                """
            )
            conn.execute(
                "INSERT INTO commands (command, platform, content_hash, created_at, updated_at) "
                "VALUES ('show version', 'iosxe', 'h', ?, ?)", (time.time(), time.time()))
            conn.commit()
            conn.close()

            # Opening through CommandStore must migrate, not destroy.
            store = CommandStore(path)
            cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(commands)")}
            for c in ("category", "protocol", "capability_req", "applies_to",
                      "command_type", "source_pdf", "source", "os_family"):
                self.assertIn(c, cols)
            self.assertEqual(store.count(), 1)  # legacy row preserved
            # Legacy untagged row is universal — returned for ANY capability class.
            rows = store.query_nodes(capability_class="router", command_path="show version")
            self.assertEqual(len(rows), 1)
            store.close()


# ---------------------------------------------------------------------------
# Part 5/Tree — capability-gated lookup over ingested nodes
# ---------------------------------------------------------------------------
def _entry(command, desc="A sufficiently long command description for testing."):
    return CommandEntry(command=command, description=desc, platform="iosxe")


class TestTreeLookup(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.pipe = IngestionPipeline(
            kb_db_path=os.path.join(d, "kb.sqlite"),
            vector_db_path=os.path.join(d, "vec.sqlite"),
            embedder=LocalHashingEmbedder(),
        )
        # OSPF routing branch.
        ospf = [_entry("show ip ospf neighbor"), _entry("show ip ospf database")]
        tag_entries(ospf, taxonomy.classify_pdf("ospf_cr.pdf"), "ospf_cr.pdf")
        self.pipe.ingest_entries(ospf, "ospf_cr.pdf", "iosxe")
        # BGP routing branch (cross-protocol bleed guard).
        bgp = [_entry("show ip bgp summary")]
        tag_entries(bgp, taxonomy.classify_pdf("bgp_cr.pdf"), "bgp_cr.pdf")
        self.pipe.ingest_entries(bgp, "bgp_cr.pdf", "iosxe")
        # VLAN lan-switching branch (GATED).
        vlan = [_entry("show vlan brief")]
        tag_entries(vlan, taxonomy.classify_pdf("vlan_cr.pdf"), "vlan_cr.pdf")
        self.pipe.ingest_entries(vlan, "vlan_cr.pdf", "iosxe")
        # Multi-family LAN-switching PDF (one doc, several families) — exercises
        # per-command protocol derivation: every command lands under its OWN
        # protocol sub-branch (vlan / spanning-tree / trunk / etherchannel /
        # switchport) even though the whole doc shares one classification.
        lan = [
            _entry("show vlan id 10"),
            _entry("spanning-tree mode rapid-pvst"),
            _entry("show spanning-tree vlan 1"),
            _entry("switchport mode trunk"),
            _entry("switchport trunk allowed vlan 10"),
            _entry("channel-group 1 mode active"),
            _entry("show etherchannel summary"),
        ]
        tag_entries(lan, taxonomy.classify_pdf("lan_switching_cmd_ref.pdf"),
                    "lan_switching_cmd_ref.pdf")
        self.pipe.ingest_entries(lan, "lan_switching_cmd_ref.pdf", "iosxe")
        self.tree = KnowledgeTree(store=self.pipe.store)

    def tearDown(self):
        self.pipe.store.close()
        self.pipe.vectors.close()
        self._dir.cleanup()

    def test_ospf_lookup_returns_correct_tags(self):
        nodes = self.tree.lookup("router", protocol_hint="ospf")
        self.assertTrue(nodes)
        cmds = {n.command_path for n in nodes}
        self.assertIn("show ip ospf neighbor", cmds)
        for n in nodes:
            self.assertEqual(n.protocol, "ospf")
            self.assertEqual(n.applies_to, ["router", "l3-switch"])
            self.assertEqual(n.command_type, "PI")
            self.assertEqual(n.source, "kb")

    def test_routing_command_applies_to_router_l3(self):
        nodes = self.tree.lookup("router", command_path="show ip ospf neighbor")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].applies_to, ["router", "l3-switch"])

    def test_lan_switching_not_surfaced_on_router(self):
        # A pure router must NOT see the VLAN command.
        cmds = {n.command_path for n in self.tree.lookup("router")}
        self.assertNotIn("show vlan brief", cmds)
        # But an l2-switch must.
        l2 = {n.command_path for n in self.tree.lookup("l2-switch")}
        self.assertIn("show vlan brief", l2)
        # And l3-switch (shared class) too.
        l3 = {n.command_path for n in self.tree.lookup("l3-switch")}
        self.assertIn("show vlan brief", l3)

    def test_protocol_scope_has_no_cross_bleed(self):
        ospf_cmds = {n.command_path for n in self.tree.lookup(protocol_hint="ospf")}
        self.assertIn("show ip ospf neighbor", ospf_cmds)
        self.assertNotIn("show ip bgp summary", ospf_cmds)
        self.assertNotIn("show vlan brief", ospf_cmds)

    def test_command_path_prefix_match(self):
        nodes = self.tree.lookup("router", command_path="show ip ospf", prefix=True)
        cmds = {n.command_path for n in nodes}
        self.assertIn("show ip ospf neighbor", cmds)
        self.assertIn("show ip ospf database", cmds)

    def test_protocol_counts(self):
        protos = self.tree.protocols()
        self.assertEqual(protos.get("ospf"), 2)
        self.assertEqual(protos.get("bgp"), 1)
        # Per-command protocol derivation splits the multi-family LAN doc into
        # distinct sub-branches (plus the standalone vlan_cr.pdf's one vlan node).
        self.assertEqual(protos.get("vlan"), 2)            # show vlan brief + show vlan id 10
        self.assertEqual(protos.get("spanning-tree"), 2)   # mode + show spanning-tree
        self.assertEqual(protos.get("switchport"), 2)      # mode trunk + trunk allowed vlan
        self.assertEqual(protos.get("etherchannel"), 2)    # channel-group + show etherchannel

    # -- Mirror-gate proof (Track A) --------------------------------------
    # Routing gated-off-L2 is proven elsewhere; this proves the MIRROR:
    # switching gated-off-router, with routing untouched on the router.
    def test_mirror_gate_vlan_gated_off_router(self):
        self.assertEqual(len(self.tree.lookup("router", protocol_hint="vlan")), 0)
        self.assertGreater(len(self.tree.lookup("l2-switch", protocol_hint="vlan")), 0)
        self.assertGreater(len(self.tree.lookup("l3-switch", protocol_hint="vlan")), 0)

    def test_mirror_gate_spanning_tree_gated_off_router(self):
        self.assertEqual(len(self.tree.lookup("router", protocol_hint="spanning-tree")), 0)
        # STP commands DO exist (proving the 0 above is from GATING, not absence).
        self.assertGreater(len(self.tree.lookup("l2-switch", protocol_hint="spanning-tree")), 0)
        self.assertGreater(len(self.tree.lookup("l3-switch", protocol_hint="spanning-tree")), 0)

    def test_mirror_gate_switching_families_gated_off_router(self):
        for proto in ("switchport", "etherchannel", "trunk"):
            router = self.tree.lookup("router", protocol_hint=proto)
            self.assertEqual(len(router), 0, f"{proto} must be gated off a pure router")

    def test_routing_still_works_on_router_no_regression(self):
        # The mirror gate must NOT affect routing: OSPF stays available on a router.
        self.assertGreater(len(self.tree.lookup("router", protocol_hint="ospf")), 0)
        self.assertGreater(len(self.tree.lookup("l3-switch", protocol_hint="ospf")), 0)
        # And routing stays gated OFF a pure L2 switch (the earlier direction).
        self.assertEqual(len(self.tree.lookup("l2-switch", protocol_hint="ospf")), 0)

    def test_per_command_protocol_derivation_in_tree(self):
        # Each multi-family command resolved to its own protocol sub-branch.
        stp = {n.command_path for n in self.tree.lookup("l2-switch", protocol_hint="spanning-tree")}
        self.assertIn("spanning-tree mode rapid-pvst", stp)
        self.assertIn("show spanning-tree vlan 1", stp)
        sw = {n.command_path for n in self.tree.lookup("l2-switch", protocol_hint="switchport")}
        self.assertIn("switchport mode trunk", sw)
        ec = {n.command_path for n in self.tree.lookup("l2-switch", protocol_hint="etherchannel")}
        self.assertIn("show etherchannel summary", ec)


# ---------------------------------------------------------------------------
# Part 3/6 — orchestrator: classify → parse → tag → store, with reporting
# ---------------------------------------------------------------------------
class TestPdfIngestor(unittest.TestCase):
    def test_ingest_pdf_tags_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            ingestor = PdfTreeIngestor(
                kb_db_path=os.path.join(d, "kb.sqlite"),
                vector_db_path=os.path.join(d, "vec.sqlite"),
                pipeline=IngestionPipeline(
                    kb_db_path=os.path.join(d, "kb.sqlite"),
                    vector_db_path=os.path.join(d, "vec.sqlite"),
                    embedder=LocalHashingEmbedder(),
                ),
            )
            fake_entries = [_entry("show ip ospf neighbor"),
                            _entry("clear ip ospf process")]
            fake_report = mock.Mock(warnings=[])
            with mock.patch("cliq.ingest.pdf_ingest.parse_document",
                            return_value=(fake_entries, fake_report)):
                rep = ingestor.ingest_pdf("somewhere/ospf_cmd_ref.pdf")

            self.assertFalse(rep.skipped)
            self.assertEqual(rep.category, "ip-routing")
            self.assertEqual(rep.protocol, "ospf")
            self.assertEqual(rep.commands_extracted, 2)
            self.assertEqual(rep.nodes_stored, 2)
            self.assertEqual(rep.embedded_count, 2)

            tree = ingestor.tree()
            nodes = tree.lookup("router", protocol_hint="ospf")
            self.assertEqual({n.command_path for n in nodes},
                             {"show ip ospf neighbor", "clear ip ospf process"})
            ingestor.close()

    def test_out_of_scope_pdf_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            ingestor = PdfTreeIngestor(
                kb_db_path=os.path.join(d, "kb.sqlite"),
                vector_db_path=os.path.join(d, "vec.sqlite"),
                pipeline=IngestionPipeline(
                    kb_db_path=os.path.join(d, "kb.sqlite"),
                    vector_db_path=os.path.join(d, "vec.sqlite"),
                    embedder=LocalHashingEmbedder(),
                ),
            )
            rep = ingestor.ingest_pdf("refs/cube_voice_cr.pdf")
            self.assertTrue(rep.skipped)
            self.assertEqual(rep.nodes_stored, 0)
            ingestor.close()


if __name__ == "__main__":
    unittest.main()
