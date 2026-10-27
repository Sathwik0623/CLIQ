#!/usr/bin/env python3
"""
test_product_cr_ingest.py — Product Command-Reference (DITA HTML) ingester.

Covers the prompt's verification checklist:
  - Book TOC crawled -> command chapters fetched; intro/prose chapters skipped.
  - Commands + syntax + mode + description extracted (no nav/ads/boilerplate).
  - Chapter topic -> capability tag (Routing->routing, VLAN->switching, etc.).
  - source=product_cr + source_url + platform + os_release + topic_chapter recorded.
  - Commands surface as advisory candidates; device-accepted -> promoted to trusted.
  - Zero network calls beyond the injected fetcher (no live requests in tests).
  - Dedup with existing "kb" rows is recorded, not silently dropped.
"""

import os
import tempfile
import unittest

from cliq.ingest import product_cr_taxonomy as taxonomy
from cliq.ingest.candidate_promotion import promote_on_device_acceptance
from cliq.ingest.product_cr_parser import RawCommand, discover_toc, parse_chapter
from cliq.ingest.product_cr_ingest import ProductCrIngestor, _to_entry
from cliq.ingest.schema import CommandEntry
from cliq.ingest.storage.embeddings import LocalHashingEmbedder
from cliq.ingest.storage.pipeline import IngestionPipeline

FIXTURE_TOC_HTML = """
<html><body>
<nav id="sitenav"><a href="/ads/tracker">Sponsored</a></nav>
<header>Site header noise</header>
<ul id="bookToc">
  <li><a href="ch1.html">Using the CLI</a></li>
  <li><a href="ch2.html">IP Routing Commands</a></li>
  <li><a href="ch3.html">VLAN Commands</a></li>
</ul>
<footer>doubleclick.net tracker pixel</footer>
</body></html>
"""

# Real Cisco DITA structure: the formal Syntax lives in ``section.refsyn`` ->
# ``<p class="figgroup synblk">`` (doc-style meta-arg words, e.g. "prefix",
# "next-hop" — never concrete values). ``section.example`` holds an
# ILLUSTRATIVE, prompt-laden session (``Device(config)#...``) that must never
# be mistaken for the command's syntax (this was the reported bug).
FIXTURE_CHAPTER_HTML = """
<html><head><script src="https://www.googletagmanager.com/gtag.js"></script></head>
<body>
<header>Header nav junk: show ip bogus-command</header>
<div id="pageContentDiv">
<article class="topic reference nested1" id="ip-route">
  <h2 class="title topictitle2">ip route</h2>
  <section class="body refbody">
    <section class="section">
      <p class="p">To establish a static route, use the ip route command in global configuration mode.</p>
    </section>
    <section class="section refsyn">
      <p class="figgroup synblk">
        <span class="keyword kwd">ip route</span>
        <var>prefix</var> <var>mask</var> <var>next-hop</var>
      </p>
      <p class="figgroup synblk">
        <span class="keyword kwd">no</span>
        <span class="keyword kwd">ip route</span>
        <var>prefix</var> <var>mask</var> <var>next-hop</var>
      </p>
    </section>
    <table>
      <tr><th>Mode</th><th>Command</th></tr>
      <tr><td>Global configuration</td><td>ip route ...</td></tr>
    </table>
    <section class="example">
      <h2 class="sectiontitle">Examples</h2>
      <p class="p">The following example shows how to configure a static route:</p>
      <pre class="pre codeblock"><code>Device(config)# ip route 10.1.1.0 255.255.255.0 10.1.1.1</code></pre>
    </section>
  </section>
</article>
<article class="topic reference nested1" id="show-ip-route">
  <h2 class="title topictitle2">show ip route</h2>
  <section class="body refbody">
    <section class="section">
      <p class="p">Displays the current state of the routing table.</p>
    </section>
    <section class="section refsyn">
      <p class="figgroup synblk">
        <span class="keyword kwd">show ip route</span>
      </p>
    </section>
    <table>
      <tr><th>Mode</th><th>Command</th></tr>
      <tr><td>Privileged EXEC</td><td>show ip route</td></tr>
    </table>
    <section class="example">
      <h2 class="sectiontitle">Examples</h2>
      <p class="p">The following example shows how to display the routing table:</p>
      <pre class="pre codeblock"><code>Device# show ip route</code></pre>
    </section>
  </section>
</article>
</div>
<footer>doubleclick.net feedback widget</footer>
</body></html>
"""

FIXTURE_VLAN_CHAPTER_HTML = """
<html><body>
<div id="pageContentDiv">
<article class="topic reference nested1" id="vlan">
  <h2 class="title topictitle2">vlan</h2>
  <section class="body refbody">
    <section class="section">
      <p class="p">To add a VLAN, use the vlan command in global configuration mode.</p>
    </section>
    <section class="section refsyn">
      <p class="figgroup synblk">
        <span class="keyword kwd">vlan</span>
        <var>vlan-id</var>
      </p>
    </section>
    <table>
      <tr><th>Mode</th><th>Command</th></tr>
      <tr><td>Global configuration</td><td>vlan</td></tr>
    </table>
    <section class="example">
      <h2 class="sectiontitle">Examples</h2>
      <pre class="pre codeblock"><code>Device(config)# vlan 10</code></pre>
    </section>
  </section>
</article>
<article class="topic reference nested1" id="show-vlan-brief">
  <h2 class="title topictitle2">show vlan brief</h2>
  <section class="body refbody">
    <section class="section">
      <p class="p">Displays VLAN information in brief.</p>
    </section>
    <section class="section refsyn">
      <p class="figgroup synblk">
        <span class="keyword kwd">show vlan brief</span>
      </p>
    </section>
    <table>
      <tr><th>Mode</th><th>Command</th></tr>
      <tr><td>Privileged EXEC</td><td>show vlan brief</td></tr>
    </table>
    <section class="example">
      <h2 class="sectiontitle">Examples</h2>
      <pre class="pre codeblock"><code>Device# show vlan brief</code></pre>
    </section>
  </section>
</article>
</div>
</body></html>
"""

# Direct regression fixture for the reported bug: a multi-command config
# session (with nested device prompts) living in the Examples section must
# never be captured as the command's syntax — only the single "broadcast-
# underlay" keyword + its own doc meta-arg belongs in syntax.
FIXTURE_MULTILINE_EXAMPLE_HTML = """
<html><body>
<div id="pageContentDiv">
<article class="topic reference nested1" id="broadcast-underlay">
  <h2 class="title topictitle2">broadcast-underlay</h2>
  <section class="body refbody">
    <section class="section">
      <p class="p">To configure the underlay in a LISP network to use a multicast group, use the broadcast-underlay command.</p>
    </section>
    <section class="section refsyn">
      <p class="figgroup synblk">
        <span class="keyword kwd">broadcast-underlay</span>
        <var>multicast-ip</var>
      </p>
    </section>
    <section class="example">
      <h2 class="sectiontitle">Examples</h2>
      <pre class="pre codeblock"><code>device(config)#router lisp
device(config-router-lisp)#instance-id 3
device(config-router-lisp-inst)#service ethernet
device(config-router-lisp-inst-serv-eth)#eid-table vlan 250
device(config-router-lisp-inst-serv-eth)#broadcast-underlay 225.1.1.1</code></pre>
    </section>
  </section>
</article>
</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Taxonomy: chapter title -> capability tag
# ---------------------------------------------------------------------------
class TestChapterTaxonomy(unittest.TestCase):
    def test_prose_chapters_skipped(self):
        for title in ("Using the CLI", "Preface", "New and Changed Information"):
            cls = taxonomy.classify_chapter(title)
            self.assertFalse(cls.is_command_chapter, title)

    def test_routing_maps_to_router_l3(self):
        cls = taxonomy.classify_chapter("IP Routing Commands")
        self.assertTrue(cls.is_command_chapter)
        self.assertEqual(cls.category, "ip-routing")
        self.assertEqual(cls.capability_req, "routing")
        self.assertEqual(cls.applies_to, ["router", "l3-switch"])

    def test_vlan_maps_to_switching_gated(self):
        cls = taxonomy.classify_chapter("VLAN Commands")
        self.assertEqual(cls.category, "lan-switching")
        self.assertEqual(cls.capability_req, "switching")
        self.assertNotIn("router", cls.applies_to)

    def test_security_qos_universal(self):
        self.assertEqual(taxonomy.classify_chapter("Security Commands").applies_to, ["all"])
        self.assertEqual(taxonomy.classify_chapter("QoS Commands").applies_to, ["all"])

    def test_ip_multicast_and_mpls(self):
        self.assertEqual(taxonomy.classify_chapter("IP Multicast Commands").category, "ip-multicast")
        self.assertEqual(taxonomy.classify_chapter("MPLS Commands").category, "mpls")

    def test_interface_hardware_and_system_mgmt_universal(self):
        self.assertEqual(taxonomy.classify_chapter("Interface and Hardware Commands").applies_to, ["all"])
        self.assertEqual(taxonomy.classify_chapter("System Management Commands").applies_to, ["all"])

    def test_unrecognised_command_chapter_kept_low_confidence(self):
        cls = taxonomy.classify_chapter("Some Brand-New Chapter Commands")
        self.assertTrue(cls.is_command_chapter)
        self.assertFalse(cls.confident)


# ---------------------------------------------------------------------------
# Parser: TOC discovery + per-command DITA extraction
# ---------------------------------------------------------------------------
class TestParser(unittest.TestCase):
    def test_discover_toc_finds_only_booktoc_links(self):
        entries = discover_toc(FIXTURE_TOC_HTML)
        titles = [e.title for e in entries]
        self.assertEqual(titles, ["Using the CLI", "IP Routing Commands", "VLAN Commands"])
        self.assertNotIn("Sponsored", titles)   # nav ad link outside bookToc excluded

    def test_parse_chapter_extracts_commands_no_boilerplate(self):
        commands = parse_chapter(FIXTURE_CHAPTER_HTML)
        self.assertEqual(len(commands), 2)
        titles = [c.title for c in commands]
        self.assertEqual(titles, ["ip route", "show ip route"])

        ip_route = commands[0]
        # Syntax comes from the Syntax section (doc meta-args), not the Example.
        self.assertIn("prefix", ip_route.syntax)
        self.assertIn("next-hop", ip_route.syntax)
        self.assertNotIn("10.1.1.1", ip_route.syntax)
        self.assertIn("static route", ip_route.description)
        self.assertEqual(ip_route.mode, "config")
        # Header/footer boilerplate must never leak into extracted fields.
        self.assertNotIn("bogus-command", ip_route.syntax)
        self.assertNotIn("doubleclick", ip_route.description)
        # Example is illustrative-only, kept separate, with device prompts stripped.
        self.assertIn("10.1.1.1", ip_route.example)
        self.assertNotIn("Device(config)#", ip_route.example)
        self.assertNotIn("(config)#", ip_route.example)

        show_route = commands[1]
        self.assertEqual(show_route.syntax, "show ip route")
        self.assertEqual(show_route.mode, "enable")
        self.assertIn("routing table", show_route.description)
        self.assertNotIn("Device#", show_route.example)

    def test_parse_chapter_ignores_scripts(self):
        commands = parse_chapter(FIXTURE_CHAPTER_HTML)
        for c in commands:
            self.assertNotIn("googletagmanager", c.syntax)
            self.assertNotIn("googletagmanager", c.description)

    def test_examples_never_leak_into_syntax(self):
        for c in parse_chapter(FIXTURE_CHAPTER_HTML):
            self.assertNotIn("Device", c.syntax)
            self.assertNotIn("#", c.syntax)

    def test_multiline_config_session_example_not_stored_as_command(self):
        """Direct regression test for the reported bug: a multi-command config
        session with nested device prompts in the Examples section must never
        become the command's syntax/name."""
        commands = parse_chapter(FIXTURE_MULTILINE_EXAMPLE_HTML)
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertEqual(cmd.title, "broadcast-underlay")
        self.assertEqual(cmd.syntax, "broadcast-underlay multicast-ip")
        self.assertNotIn("router lisp", cmd.syntax)
        self.assertNotIn("instance-id", cmd.syntax)
        self.assertNotIn("device(config", cmd.syntax.lower())
        # Illustrative example kept separately, with prompts stripped.
        self.assertIn("router lisp", cmd.example)
        self.assertNotIn("device(config", cmd.example.lower())


# ---------------------------------------------------------------------------
# Orchestrator: crawl -> tag -> store (candidates), dedup, report
# ---------------------------------------------------------------------------
def _fake_fetch(pages: dict):
    def fetch(url: str) -> str:
        for key, html in pages.items():
            if url.endswith(key):
                return html
        raise AssertionError(f"unexpected fetch: {url}")
    return fetch


# ---------------------------------------------------------------------------
# Clean insertable base command vs. full DITA syntax notation (display-only)
# ---------------------------------------------------------------------------
class TestCleanBaseCommand(unittest.TestCase):
    def test_notation_stripped_from_stored_command(self):
        raw = RawCommand(
            title="show processes cpu platform history",
            syntax=(
                "show processes cpu platform history[ 1min| 5min| 5sec| 60min] "
                "location switch{ switch-number| active| standby} { 0| F0| FP active| R0}"
            ),
            description="Displays CPU usage history.",
            mode="enable",
        )
        entry = _to_entry(raw, doc_source="https://example.com/cr")
        self.assertEqual(entry.command, "show processes cpu platform history")
        for marker in "[]{}|":
            self.assertNotIn(marker, entry.command)
        # Full notation preserved verbatim for panel/reference display.
        self.assertEqual(entry.syntax_variants, [raw.syntax])

    def test_bare_trailing_variable_stripped(self):
        raw = RawCommand(
            title="qos queue-softmax-multiplier",
            syntax="qos queue-softmax-multiplier range-of-multiplier",
            description="Sets the queue soft-max multiplier.",
            mode="config",
        )
        entry = _to_entry(raw, doc_source="https://example.com/cr")
        self.assertEqual(entry.command, "qos queue-softmax-multiplier")
        self.assertEqual(entry.syntax_variants, [raw.syntax])

    def test_already_clean_command_unaffected(self):
        raw = RawCommand(
            title="show ip route", syntax="show ip route",
            description="Displays the routing table.", mode="enable",
        )
        entry = _to_entry(raw, doc_source="https://example.com/cr")
        self.assertEqual(entry.command, "show ip route")
        self.assertEqual(entry.syntax_variants, ["show ip route"])


class TestProductCrIngestor(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.pipe = IngestionPipeline(
            kb_db_path=os.path.join(d, "kb.sqlite"),
            vector_db_path=os.path.join(d, "vec.sqlite"),
            embedder=LocalHashingEmbedder(),
        )
        fetch = _fake_fetch({
            "toc.html": FIXTURE_TOC_HTML,
            "ch1.html": "<html><body><div id='pageContentDiv'>prose, no commands</div></body></html>",
            "ch2.html": FIXTURE_CHAPTER_HTML,
            "ch3.html": FIXTURE_VLAN_CHAPTER_HTML,
        })
        self.ingestor = ProductCrIngestor(pipeline=self.pipe, fetch=fetch)

    def tearDown(self):
        self.pipe.store.close()
        self.pipe.vectors.close()
        self._dir.cleanup()

    def test_crawl_book_skips_prose_and_ingests_command_chapters(self):
        report = self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
        )
        by_title = {c.title: c for c in report.chapters}
        self.assertTrue(by_title["Using the CLI"].skipped)
        self.assertFalse(by_title["IP Routing Commands"].skipped)
        self.assertFalse(by_title["VLAN Commands"].skipped)
        self.assertEqual(by_title["IP Routing Commands"].commands_extracted, 2)
        self.assertEqual(report.candidates_stored, 4)   # 2 chapters x 2 commands

    def test_stored_rows_carry_product_cr_provenance(self):
        self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
        )
        rows = self.pipe.store.query_nodes(limit=100)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["source"], "product_cr")
            self.assertEqual(row["platform"], "cat9500")
            self.assertEqual(row["os_release"], "17.16.x")
            self.assertIn(row["source_url"], (
                "https://example.example.com/ch2.html", "https://example.example.com/ch3.html",
            ))
            self.assertIn(row["topic_chapter"], ("IP Routing Commands", "VLAN Commands"))
            self.assertEqual(row["candidate_status"], "candidate")   # advisory, not yet trusted

    def test_capability_gating_by_chapter(self):
        self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
        )
        rows = self.pipe.store.query_nodes(limit=100)
        routing_rows = [r for r in rows if r["topic_chapter"] == "IP Routing Commands"]
        vlan_rows = [r for r in rows if r["topic_chapter"] == "VLAN Commands"]
        self.assertTrue(all("router" in r["applies_to"] for r in routing_rows))
        self.assertTrue(all("router" not in r["applies_to"] for r in vlan_rows))

    def test_dedup_flagged_against_existing_kb_row(self):
        # Pre-seed a breadth-only MCL/PDF-sourced row for the same command.
        existing = CommandEntry(
            command="show ip route", description="existing kb desc", platform="iosxe",
            source="kb",
        )
        self.pipe.ingest_entries([existing], "existing.pdf", "iosxe")

        report = self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
        )
        total_dedup = sum(c.dedup_matches for c in report.chapters)
        self.assertGreaterEqual(total_dedup, 1)   # "show ip route" matches the seeded kb row

    def test_dry_run_style_max_chapters_cap(self):
        report = self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
            max_chapters=1,
        )
        self.assertEqual(len(report.chapters), 1)
        self.assertEqual(report.chapters[0].title, "Using the CLI")


# ---------------------------------------------------------------------------
# Two-stage promotion: candidate -> trusted on device acceptance
# ---------------------------------------------------------------------------
class TestCandidatePromotion(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.pipe = IngestionPipeline(
            kb_db_path=os.path.join(d, "kb.sqlite"),
            vector_db_path=os.path.join(d, "vec.sqlite"),
            embedder=LocalHashingEmbedder(),
        )
        fetch = _fake_fetch({"toc.html": FIXTURE_TOC_HTML, "ch2.html": FIXTURE_CHAPTER_HTML,
                             "ch1.html": "<html><body><div id='pageContentDiv'></div></body></html>",
                             "ch3.html": FIXTURE_VLAN_CHAPTER_HTML})
        self.ingestor = ProductCrIngestor(pipeline=self.pipe, fetch=fetch)
        self.ingestor.crawl_book(
            "https://example.example.com/toc.html", platform="cat9500", os_release="17.16.x",
        )

    def tearDown(self):
        self.pipe.store.close()
        self.pipe.vectors.close()
        self._dir.cleanup()

    def test_matching_device_acceptance_promotes_candidate(self):
        candidates_before = self.pipe.store.get_candidates(platform="cat9500")
        self.assertTrue(any(c["command"] == "show ip route" for c in candidates_before))

        # "show ip route" has no arguments in its Syntax section, so its stored
        # template is the bare literal command — an exact match against the
        # same bare command typed on the device. (Doc-style meta-arg words like
        # "prefix"/"next-hop" are NOT recognised operand shapes, so promotion
        # of argument-bearing commands needs device ?-help context; see
        # candidate_promotion module docs.)
        promoted_id = promote_on_device_acceptance(
            self.pipe.store, "show ip route", "cat9500",
        )
        self.assertIsNotNone(promoted_id)

        row = self.pipe.store.get_by_id(promoted_id)
        self.assertEqual(row["candidate_status"], "trusted")

    def test_nonmatching_acceptance_leaves_candidates_untouched(self):
        promoted_id = promote_on_device_acceptance(
            self.pipe.store, "show version", "cat9500",
        )
        self.assertIsNone(promoted_id)
        counts = self.pipe.store.count_candidates()
        self.assertEqual(counts.get("trusted", 0), 0)

    def test_wrong_platform_never_promotes(self):
        promoted_id = promote_on_device_acceptance(
            self.pipe.store, "show ip route", "iosv",
        )
        self.assertIsNone(promoted_id)


if __name__ == "__main__":
    unittest.main()
