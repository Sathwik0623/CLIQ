#!/usr/bin/env python3
"""Unit tests for cliq.ingest.text_repair.desegment_text.

These tests exercise the pure word-boundary reconstruction logic directly, with
no PDF dependency. Tests that require the optional ``wordninja`` segmenter skip
gracefully when it is not installed.
"""

import pytest

from cliq.ingest import text_repair
from cliq.ingest.text_repair import desegment_text, repair_command_text

_HAS_NINJA = text_repair._load_wordninja() is not None
requires_ninja = pytest.mark.skipif(
    not _HAS_NINJA, reason="wordninja not installed"
)


class TestPreservation:
    """Clean / already-spaced text and punctuation must survive untouched."""

    def test_empty_and_none(self):
        assert desegment_text("") == ""
        assert desegment_text(None) == ""

    def test_already_spaced_text_unchanged(self):
        text = "Use this mode to configure the terminal line."
        assert desegment_text(text) == text

    def test_short_words_not_split(self):
        # No run reaches the glue threshold, so nothing changes.
        text = "show ip route default"
        assert desegment_text(text) == text

    def test_punctuation_and_identifiers_preserved(self):
        # Hyphenated identifiers and brackets must not be mangled.
        text = "no broadcast-underlay multicast-ip"
        assert desegment_text(text) == text

    def test_command_phrase_repairs(self):
        assert repair_command_text("show ipo s pf neighbor") == "show ip ospf neighbor"
        assert repair_command_text("show ip m route") == "show ip mroute"

    def test_boilerplate_ocr_repairs(self):
        text = "This command was introduced.Cisco IOSXEG i br altar16.11.1"
        out = desegment_text(text)
        assert "Cisco IOS XE Gibraltar 16.11.1" in out


@requires_ninja
class TestReconstruction:
    """With the segmenter present, glued runs are rebuilt into spaced words."""

    def test_glued_sentence_is_respaced(self):
        out = desegment_text("IPaddressofthemulticastgroup")
        assert out == "IP address of the multicast group"

    def test_compound_repair_keeps_multicast_whole(self):
        # 'multicast' must not degrade to 'multi cast'.
        out = desegment_text("Specifiesthemulticastaddress")
        assert "multi cast" not in out
        assert "multicast" in out

    def test_acronym_preserved(self):
        # All-uppercase acronyms must survive ('VRF', not 'V RF').
        out = desegment_text("ThedeviceisnotconfiguredasETRbydefault.")
        assert out == "The device is not configured as ETR by default."

    def test_acronym_glued_to_word_is_split(self):
        # 'VRFtable' -> 'VRF table' via the acronym-word boundary.
        assert desegment_text("VRFtable") == "VRF table"

    def test_camelcase_boundary_split(self):
        out = desegment_text("ProtocolIndependentMulticast")
        assert out == "Protocol Independent Multicast"

    def test_short_lowercase_domain_token_preserved(self):
        # Trailing argument keyword 'vrf' must not fragment to 'v rf'.
        out = desegment_text("forwarding(VRF)instance.vrf")
        assert "v rf" not in out
        assert out.endswith(".vrf")

    def test_punctuation_preserved_through_split(self):
        out = desegment_text("Theabbreviationfor(VPN)isused.")
        assert "(VPN)" in out
        assert out.endswith(".")

    def test_clean_text_with_long_real_word_unchanged(self):
        # 'configuration' is a single long English word; it must stay whole.
        text = "Enters configuration mode."
        assert desegment_text(text) == text


@requires_ninja
class TestAcronymProtection:
    """Networking acronyms must stay atomic so command_path tokens are clean."""

    def test_eigrp_kept_whole(self):
        # The core bug: 'eigrp' must not degrade to 'ei grp'.
        assert repair_command_text("show eigrp plugins") == "show eigrp plugins"
        assert repair_command_text("eigrp") == "eigrp"

    def test_eigrp_glued_in_command_run(self):
        # A concatenated command run is re-spaced with the acronym preserved.
        assert "ei grp" not in repair_command_text("showeigrpplugins")

    def test_netflow_kept_whole(self):
        assert "net flow" not in repair_command_text("show netflow cache")
        assert "netflow" in repair_command_text("show netflow cache")

    def test_ipsla_kept_whole(self):
        assert "ip sla" not in repair_command_text("ipsla")
        assert repair_command_text("ipsla") == "ipsla"

    def test_acronym_in_prose_cluster(self):
        # Embedded acronym inside a glued lowercase cluster survives segmentation.
        out = desegment_text("configuretheeigrpprocess")
        assert "eigrp" in out
        assert "ei grp" not in out

    def test_english_word_containing_acronym_substring_unaffected(self):
        # 'crisis' contains 'isis'; 'combination' contains 'nat'. wordninja keeps
        # these whole, so the acronym guard must never fragment them.
        assert desegment_text("crisis") == "crisis"
        assert desegment_text("combination") == "combination"
        assert desegment_text("Thenetworkconfiguration") == "The network configuration"


@requires_ninja
class TestSwitchingAcronymProtection:
    """LAN-switching families must stay atomic in the lan-switching PDF."""

    def test_switchport_kept_whole(self):
        assert "switch port" not in desegment_text("configuretheswitchportmode")
        assert "switchport" in desegment_text("configuretheswitchportmode")

    def test_etherchannel_kept_whole(self):
        assert "ether channel" not in desegment_text("configuretheetherchannelgroup")
        assert "etherchannel" in desegment_text("configuretheetherchannelgroup")

    def test_dot1q_kept_whole(self):
        assert "dot 1 q" not in desegment_text("encapsulationdot1qvlan")
        assert "dot1q" in desegment_text("encapsulationdot1qvlan")

    def test_vlan_and_stp_kept_whole(self):
        assert repair_command_text("show vlan brief") == "show vlan brief"
        assert "span ning" not in repair_command_text("spanning-tree mode")

    def test_protected_switching_acronyms_registered(self):
        from cliq.ingest.text_repair import _PROTECTED_ACRONYMS
        for tok in ("vlan", "stp", "switchport", "etherchannel", "dot1q",
                    "vtp", "mstp", "pvst", "rstp", "lacp", "pagp"):
            assert tok in _PROTECTED_ACRONYMS, tok

    def test_stp_single_word_keywords_kept_whole(self):
        # IOS spanning-tree keywords are single atomic CLI tokens; the generic
        # model splits them ("bpduguard" -> "bpd u guard") without protection.
        assert repair_command_text("spanning-tree bpduguard") == "spanning-tree bpduguard"
        assert repair_command_text("spanning-tree bpdufilter") == "spanning-tree bpdufilter"
        assert repair_command_text("spanning-tree portfast default") == "spanning-tree portfast default"
        assert repair_command_text("spanning-tree uplinkfast") == "spanning-tree uplinkfast"
        assert repair_command_text("spanning-tree backbonefast") == "spanning-tree backbonefast"
        assert repair_command_text("spanning-tree loopguard default") == "spanning-tree loopguard default"

    def test_protected_stp_keywords_registered(self):
        from cliq.ingest.text_repair import _PROTECTED_ACRONYMS
        for tok in ("bpduguard", "bpdufilter", "portfast", "uplinkfast",
                    "backbonefast", "loopguard", "spantree"):
            assert tok in _PROTECTED_ACRONYMS, tok


@requires_ninja
class TestReadableSpacing:
    """Description prose gets readable spacing; command lines do not."""

    def test_paren_acronym_respaced_in_description(self):
        out = desegment_text("First(OSPF)neighbor", readable=True)
        assert out == "First (OSPF) neighbor"

    def test_command_lines_keep_parens_glued(self):
        # repair_command_text never requests readable spacing.
        out = repair_command_text("show(OSPF)neighbor")
        assert "(OSPF)" in out


def test_count_protected_acronyms():
    from cliq.ingest.text_repair import count_protected_acronyms

    assert count_protected_acronyms("show eigrp plugins") == 1
    assert count_protected_acronyms("show ip ospf bgp") == 2
    assert count_protected_acronyms("show version") == 0
    assert count_protected_acronyms("") == 0
    assert count_protected_acronyms(None) == 0
