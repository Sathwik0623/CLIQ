#!/usr/bin/env python3
"""Unit tests for command_template — value-stripping / template extraction."""

from cliq.intelligence.command_template import (
    extract_template,
    clean_prefix,
    is_template,
    redact_args,
    strip_syntax_notation,
    PH_INTERFACE,
    PH_IP,
    PH_MASK,
    PH_ARG,
)


class TestShapeStripping:
    """Operands recognised by shape alone (context-free)."""

    def test_interface_stripped(self):
        r = extract_template("show ip ospf neighbor GigabitEthernet1")
        assert r.template == "show ip ospf neighbor <interface>"
        assert r.stripped == [PH_INTERFACE]

    def test_interface_abbreviations_and_slots(self):
        for lit, expect in [
            ("interface Gi0/0", "interface <interface>"),
            ("interface Te1/0/1", "interface <interface>"),
            ("interface Loopback0", "interface <interface>"),
            ("interface Vlan10", "interface <interface>"),
            ("interface Port-channel1", "interface <interface>"),
            ("interface BDI2", "interface <interface>"),
            ("interface GigabitEthernet0/0.100", "interface <interface>"),
        ]:
            assert extract_template(lit).template == expect, lit

    def test_ping_ip_stripped(self):
        r = extract_template("ping 10.1.1.1")
        assert r.template == "ping <ip>"
        assert r.stripped == [PH_IP]

    def test_ip_and_mask_distinguished(self):
        r = extract_template("ip address 10.1.1.1 255.255.255.0")
        assert r.template == "ip address <ip> <mask>"
        assert r.stripped == [PH_IP, PH_MASK]

    def test_wildcard_mask_is_mask(self):
        r = extract_template("access-list 10 permit 10.0.0.0 0.0.0.255")
        # 10 -> <arg>, 10.0.0.0 -> <ip>? 10.0.0.0 octets contiguous -> mask.
        assert r.template.startswith("access-list <arg> permit")
        assert r.template.endswith("<mask>")

    def test_cidr_address_stripped(self):
        r = extract_template("network 10.0.0.0/24")
        assert r.template == "network <ip>"

    def test_ipv6_stripped(self):
        r = extract_template("ping 2001:db8::1")
        assert r.template == "ping <ip>"

    def test_bare_integer_is_arg(self):
        r = extract_template("router ospf 1")
        assert r.template == "router ospf <arg>"
        assert r.stripped == [PH_ARG]

    def test_integer_range_is_arg(self):
        r = extract_template("vlan 10-20")
        assert r.template == "vlan <arg>"

    def test_multiple_operands(self):
        r = extract_template("ip ospf 1 area 0")
        assert r.template == "ip ospf <arg> area <arg>"
        assert r.stripped == [PH_ARG, PH_ARG]


class TestKeywordsKeptLiteral:
    """Subcommand keywords must never be placeholdered."""

    def test_clean_command_unchanged(self):
        r = extract_template("show ip ospf neighbor")
        assert r.template == "show ip ospf neighbor"
        assert r.stripped == []
        assert r.changed is False

    def test_keyword_that_looks_wordy_kept(self):
        # 'database' / 'detail' are keywords, not operands.
        r = extract_template("show ip ospf database detail")
        assert r.template == "show ip ospf database detail"
        assert r.stripped == []

    def test_vlan_keyword_vs_vlan_interface(self):
        # 'vlan' alone (no digit) is a keyword; 'Vlan10' is an interface.
        assert extract_template("interface vlan 10").template == "interface vlan <arg>"
        assert extract_template("interface Vlan10").template == "interface <interface>"


class TestHelpContextArgStripping:
    """Free-text WORD operands are placeholdered using ?-help keyword context."""

    def test_hostname_word_becomes_arg(self):
        # Device help for 'hostname' lists only the WORD descriptor -> R1 is an arg.
        help_map = {"hostname": ["WORD"]}
        r = extract_template("hostname R1", help_lookup=help_map.get)
        assert r.template == "hostname <arg>"
        assert r.stripped == [PH_ARG]

    def test_known_keyword_kept_via_help(self):
        # 'neighbor' IS a help keyword after 'show ip ospf' -> kept literal.
        help_map = {"show ip ospf": ["neighbor", "interface", "database"]}
        r = extract_template("show ip ospf neighbor", help_lookup=help_map.get)
        assert r.template == "show ip ospf neighbor"
        assert r.stripped == []

    def test_pool_name_becomes_arg(self):
        help_map = {"ip dhcp pool": ["WORD"]}
        r = extract_template("ip dhcp pool LAN-POOL", help_lookup=help_map.get)
        assert r.template == "ip dhcp pool <arg>"

    def test_no_help_leaves_unknown_token_literal(self):
        # Without help context an unrecognised non-shape token is NOT stripped,
        # so keywords are never corrupted by guesswork.
        r = extract_template("hostname R1")
        assert r.template == "hostname R1"
        assert r.stripped == []

    def test_uppercase_descriptor_not_treated_as_keyword(self):
        # If help only offers 'WORD'/'LINE' (arg descriptors), the token is an arg.
        help_map = {"banner motd": ["LINE"]}
        r = extract_template("banner motd HELLO", help_lookup=help_map.get)
        assert r.template == "banner motd <arg>"


class TestHelpers:
    def test_clean_prefix_truncates_at_placeholder(self):
        assert clean_prefix("show ip ospf neighbor <interface>") == "show ip ospf neighbor"
        assert clean_prefix("ping <ip>") == "ping"
        assert clean_prefix("show ip ospf neighbor") == "show ip ospf neighbor"

    def test_is_template(self):
        assert is_template("ping <ip>") is True
        assert is_template("show ip ospf neighbor") is False

    def test_redact_args_returns_only_placeholders(self):
        r = extract_template("ip address 10.1.1.1 255.255.255.0")
        # redaction surfaces ONLY the typed placeholders, never the values.
        red = redact_args(r.stripped)
        assert red == [PH_IP, PH_MASK]
        assert "10.1.1.1" not in red and "255.255.255.0" not in red


class TestDedupEquivalence:
    """Different literals of the same command collapse to one template."""

    def test_two_interfaces_one_template(self):
        a = extract_template("show ip ospf neighbor GigabitEthernet1").template
        b = extract_template("show ip ospf neighbor TenGigabitEthernet1/0/1").template
        assert a == b == "show ip ospf neighbor <interface>"

    def test_two_ips_one_template(self):
        a = extract_template("ping 10.1.1.1").template
        b = extract_template("ping 8.8.8.8").template
        assert a == b == "ping <ip>"


class TestStripSyntaxNotation:
    """DITA syntax notation -> clean, runnable base command (Insert target)."""

    def test_bracket_notation_glued_to_prior_word(self):
        syntax = (
            "show processes cpu platform history[ 1min| 5min| 5sec| 60min] "
            "location switch{ switch-number| active| standby} { 0| F0| FP active| R0}"
        )
        assert strip_syntax_notation(syntax) == "show processes cpu platform history"

    def test_bracket_notation_with_space(self):
        assert strip_syntax_notation("snmp-server enable traps cpu [ threshold]") == (
            "snmp-server enable traps cpu"
        )

    def test_bare_trailing_variable_no_brackets(self):
        # "range-of-multiplier" is a variable descriptor, not a literal keyword;
        # "queue-softmax-multiplier" (earlier in the command) must be kept.
        assert strip_syntax_notation("qos queue-softmax-multiplier range-of-multiplier") == (
            "qos queue-softmax-multiplier"
        )

    def test_clean_command_unchanged(self):
        assert strip_syntax_notation("show ip ospf neighbor") == "show ip ospf neighbor"

    def test_angle_bracket_placeholder(self):
        assert strip_syntax_notation("ip route <prefix> <mask> <next-hop>") == "ip route"

    def test_leading_optional_group_skipped_gracefully(self):
        assert strip_syntax_notation("[ no] shutdown") == "shutdown"

    def test_empty_syntax(self):
        assert strip_syntax_notation("") == ""
        assert strip_syntax_notation(None) == ""  # type: ignore[arg-type]

    def test_no_notation_ever_leaks_into_base(self):
        examples = [
            "show processes cpu platform history[ 1min| 5min] location switch{ 0| F0}",
            "snmp-server enable traps cpu [ threshold]",
            "qos queue-softmax-multiplier range-of-multiplier",
            "ip route <prefix> <mask> <next-hop>",
        ]
        for syntax in examples:
            base = strip_syntax_notation(syntax)
            for marker in "[]{}|<>":
                assert marker not in base, f"{marker!r} leaked into base {base!r}"
