#!/usr/bin/env python3
"""
test_doc_parser.py — Unit tests for the CLIQ command-reference parser (Phase 1A).

Validates the success criteria on a representative TXT command reference:
  - commands are correctly separated from their descriptions
  - page headers/footers/page-numbers/TOC noise are stripped
  - platform and doc_source are accurately tagged
  - modes are detected (and default to exec)
  - duplicates are flagged (not dropped)
  - low-confidence (empty/short description) entries are flagged
  - unreadable/unsupported files degrade gracefully (no crash)
"""

import os
import unittest

from cliq.ingest import parse_document, format_preview
from cliq.ingest.doc_parser import load_parser_config, normalize_pages

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "sample_command_ref.txt"
)


class TestCommandReferenceParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries, cls.report = parse_document(_FIXTURE, platform="iosxe")
        # Keep the FIRST occurrence of each command (the later duplicate is flagged).
        cls.by_cmd = {}
        for e in cls.entries:
            cls.by_cmd.setdefault(e.command, e)

    def test_extracts_expected_commands(self):
        for cmd in ("show ip route", "show ip interface brief",
                    "clear ip route", "ip routing"):
            self.assertIn(cmd, self.by_cmd, f"missing command: {cmd}")

    def test_command_separated_from_description(self):
        entry = self.by_cmd["show ip route"]
        self.assertTrue(entry.description.startswith("To display the current state"))
        # The description must NOT swallow the command token list verbatim, nor
        # the section labels.
        self.assertNotIn("Syntax Description", entry.description)
        self.assertNotIn("Command Modes", entry.description)

    def test_syntax_variants_captured(self):
        entry = self.by_cmd["show ip route"]
        self.assertTrue(
            any("[vrf vrf-name]" in v for v in entry.syntax_variants),
            f"variants={entry.syntax_variants}",
        )

    def test_mode_detection(self):
        self.assertEqual(self.by_cmd["show ip route"].mode, "enable")
        self.assertEqual(self.by_cmd["ip routing"].mode, "config")

    def test_platform_and_source_tagged(self):
        entry = self.by_cmd["show ip route"]
        self.assertEqual(entry.platform, "iosxe")
        self.assertEqual(entry.doc_source, "sample_command_ref.txt")

    def test_noise_stripped(self):
        # No entry should be created from TOC/page-number/header lines.
        self.assertNotIn("Table of Contents", self.by_cmd)
        self.assertGreater(self.report.lines_skipped_noise, 0)
        for entry in self.entries:
            self.assertNotIn("....", entry.description)
            self.assertNotRegex(entry.command, r"^\d+$")

    def test_duplicate_flagged_not_dropped(self):
        dupes = [e for e in self.entries if "duplicate" in e.flags]
        self.assertEqual(len(dupes), 1, "the repeated 'show ip route' should be flagged once")
        self.assertEqual(self.report.duplicates_flagged, 1)
        # Both copies still present — duplicates are flagged, never silently dropped.
        self.assertEqual(sum(1 for e in self.entries if e.command == "show ip route"), 2)

    def test_low_confidence_flagged(self):
        # 'ip routing' has no description block -> empty_description / low confidence.
        entry = self.by_cmd["ip routing"]
        self.assertEqual(entry.confidence, "low")
        self.assertIn("empty_description", entry.flags)
        self.assertGreaterEqual(self.report.low_confidence, 1)

    def test_report_counts(self):
        self.assertEqual(self.report.entries_extracted, len(self.entries))
        self.assertEqual(self.report.platform, "iosxe")
        self.assertGreater(self.report.lines_total, 0)

    def test_preview_renders(self):
        text = format_preview(self.entries, limit=5)
        self.assertIn("show ip route", text)
        self.assertIn("and", text)  # "... and N more"


class TestGracefulDegradation(unittest.TestCase):
    def test_missing_file_does_not_crash(self):
        entries, report = parse_document("does_not_exist.txt", platform="iosxe")
        self.assertEqual(entries, [])
        self.assertTrue(report.warnings)

    def test_unsupported_format_does_not_crash(self):
        entries, report = parse_document(__file__.replace(".py", ".xyz"), platform="iosxe")
        self.assertEqual(entries, [])
        self.assertTrue(report.warnings)


class TestNormalization(unittest.TestCase):
    def test_running_header_detection(self):
        cfg = load_parser_config()
        pages = [
            "ACME Reference\nshow foo\nbody one\n1",
            "ACME Reference\nshow bar\nbody two\n2",
            "ACME Reference\nshow baz\nbody three\n3",
        ]
        clean, total, skipped, toc_skipped = normalize_pages(pages, cfg)
        self.assertNotIn("ACME Reference", clean)   # running header removed
        self.assertNotIn("1", clean)                # page numbers removed
        self.assertGreater(skipped, 0)
        self.assertEqual(total, sum(len(p.splitlines()) for p in pages))


class TestRealPdfHardening(unittest.TestCase):
    """Phase 1A.1 — reject TOC / page-number / structural-heading noise that
    real Cisco command-reference PDFs leak into the parser."""

    def _parse_text(self, text: str):
        """Parse an in-memory document by writing it to a temp .txt file."""
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(text)
            path = fh.name
        try:
            return parse_document(path, platform="iosxe")
        finally:
            os.remove(path)

    def test_toc_region_is_skipped(self):
        text = (
            "Catalyst 9600 Command Reference\n\n"
            "Table of Contents\n"
            "prefix-list 56\n"
            "service 60\n"
            "reload 200\n"
            "interface 243\n"
            "Interface and Hardware Components 221P ART IV\n"
            "MPLS Commands 951CHAPTER 9\n"
            "QoS 1221P ART X\n\n"
            "Chapter 1\n\n"
            "show ip route\n"
            "To display the current state of the routing table, use the show ip "
            "route command in privileged EXEC mode.\n"
            "show ip route [vrf vrf-name]\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, report = self._parse_text(text)
        cmds = {e.command for e in entries}
        # The only genuine command in the body must be the one that survives.
        self.assertIn("show ip route", cmds)
        # No TOC page-number or structural-heading residue should appear.
        for bad in ("prefix-list 56", "reload 200", "interface 243"):
            self.assertNotIn(bad, cmds)
        self.assertFalse(any("PART" in c or "CHAPTER" in c for c in cmds))
        self.assertGreater(report.toc_lines_skipped, 0)

    def test_trailing_page_number_stripped(self):
        # A TOC-free fragment where a heading still carries a trailing page
        # number should keep the command but drop the number.
        text = (
            "reload 200\n"
            "To reload the operating system, use the reload command in "
            "privileged EXEC mode.\n"
            "reload [warm]\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, _ = self._parse_text(text)
        cmds = {e.command for e in entries}
        self.assertIn("reload", cmds)
        self.assertNotIn("reload 200", cmds)

    def test_command_argument_number_preserved(self):
        # A number that is a genuine command argument (followed by more tokens)
        # must NOT be stripped as a page number.
        from cliq.ingest.doc_parser import CiscoReferenceLayout

        self.assertEqual(
            CiscoReferenceLayout._strip_page_number("access-list 10 permit ip any any"),
            "access-list 10 permit ip any any",
        )
        self.assertEqual(
            CiscoReferenceLayout._strip_page_number("prefix-list .... 56"),
            "prefix-list",
        )
        self.assertEqual(
            CiscoReferenceLayout._strip_page_number("reload 200"),
            "reload",
        )

    def test_structural_headings_rejected(self):
        text = (
            "Interface and Hardware Components 221P ART IV\n"
            "MPLS Commands 951CHAPTER 9\n"
            "show version\n"
            "To display version information, use the show version command in "
            "privileged EXEC mode.\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, report = self._parse_text(text)
        cmds = {e.command for e in entries}
        self.assertIn("show version", cmds)
        self.assertFalse(any("PART" in c or "CHAPTER" in c for c in cmds))

    def test_evidence_required_for_acceptance(self):
        # A bare verb-led line with no syntax, no description and no section is
        # rejected (counted), not stored.
        text = (
            "show foobar\n\n"
            "show running-config\n"
            "To display the running configuration, use the show running-config "
            "command in privileged EXEC mode.\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, report = self._parse_text(text)
        cmds = {e.command for e in entries}
        self.assertIn("show running-config", cmds)
        self.assertNotIn("show foobar", cmds)
        self.assertGreaterEqual(report.candidates_rejected, 1)

    def test_suspicious_command_flagged(self):
        from cliq.ingest.doc_parser import _is_suspicious_command

        self.assertTrue(_is_suspicious_command("reload 200"))
        self.assertTrue(_is_suspicious_command("MPLS Commands 951CHAPTER 9"))
        self.assertTrue(_is_suspicious_command("Interface and Hardware Components PART IV"))
        self.assertFalse(_is_suspicious_command("show ip route"))
        self.assertFalse(_is_suspicious_command("access-list 10 permit ip any any"))

    def test_capitalised_mode_labels_rejected(self):
        # Capitalised "command headings" (VLAN, Interface, Line, IPv6 ...) are
        # mode-table labels / show-output residue, not real CLI commands.
        text = (
            "VLAN\n"
            "configuration Use this mode to configure VLAN parameters.\n"
            "Interface\n"
            "configuration Use this mode to configure interface parameters.\n"
            "show running-config\n"
            "To display the running configuration, use the show running-config "
            "command in privileged EXEC mode.\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, _ = self._parse_text(text)
        cmds = {e.command for e in entries}
        self.assertIn("show running-config", cmds)
        self.assertNotIn("VLAN", cmds)
        self.assertNotIn("Interface", cmds)

    def test_bare_mode_label_with_mode_description_rejected(self):
        # A lowercase bare mode-context label whose description describes the
        # MODE ("Use this mode to ...") is a section label, not a command.
        from cliq.ingest.doc_parser import CiscoReferenceLayout, load_parser_config, _Builder

        cfg = load_parser_config()
        b = _Builder(command="interface", platform="iosxe", doc_source="d",
                     source_line=1, config=cfg)
        b.add_description("configuration Use this mode to configure the interface parameters.")
        self.assertEqual(b.rejection_reason(), "section_label")

    def test_real_interface_command_kept(self):
        # A genuine 'interface' command page (argument-bearing syntax + command
        # description) must be KEPT despite 'interface' being a mode label.
        from cliq.ingest.doc_parser import load_parser_config, _Builder

        cfg = load_parser_config()
        b = _Builder(command="interface", platform="iosxe", doc_source="d",
                     source_line=1, config=cfg)
        b.add_description("To configure an interface, use the interface command in "
                          "global configuration mode.")
        b.add_syntax("interface type number")
        self.assertIsNone(b.rejection_reason())

    def test_descriptive_note_rejected(self):
        from cliq.ingest.doc_parser import load_parser_config, _Builder

        cfg = load_parser_config()
        b = _Builder(command="IPv6 RLOC minimum mask length: /0", platform="iosxe",
                     doc_source="d", source_line=1, config=cfg)
        b.add_description("Map-cache size/limit: 0/32768 and other counters.")
        self.assertEqual(b.rejection_reason(), "descriptive_note")

    def test_no_form_and_short_commands_kept(self):
        from cliq.ingest.doc_parser import load_parser_config, _Builder

        cfg = load_parser_config()
        # 'no ' negation form with empty description must be kept.
        b1 = _Builder(command="no ip pim", platform="iosxe", doc_source="d",
                      source_line=1, config=cfg)
        b1.add_syntax("no ip pim [vrf vrf-name] rp-address rp-address")
        self.assertIsNone(b1.rejection_reason())
        # A valid short command with a real description must be kept.
        b2 = _Builder(command="reload", platform="iosxe", doc_source="d",
                      source_line=1, config=cfg)
        b2.add_description("To reload the operating system, use the reload command.")
        self.assertIsNone(b2.rejection_reason())

    def test_command_heading_repaired_before_storage(self):
        text = (
            "show ipo s pf neighbor\n"
            "To display OSPF neighbor information, use the show ipo s pf neighbor command.\n"
            "show ipo s pf neighbor [detail]\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, _ = self._parse_text(text)
        cmds = {e.command for e in entries}
        self.assertIn("show ip ospf neighbor", cmds)

    def test_reject_reasons_reported(self):
        text = (
            "VLAN\n"
            "configuration Use this mode to configure VLAN parameters.\n"
            "show version\n"
            "To display version information, use the show version command in "
            "privileged EXEC mode.\n"
            "Command Modes\n"
            "Privileged EXEC (#)\n"
        )
        entries, report = self._parse_text(text)
        self.assertIn("show version", {e.command for e in entries})
        # The breakdown fields exist and the total equals their sum.
        total = (
            report.rejected_no_evidence
            + report.rejected_section_label
            + report.rejected_descriptive_note
            + report.rejected_empty_heading
        )
        self.assertEqual(report.candidates_rejected, total)


if __name__ == "__main__":
    unittest.main()
