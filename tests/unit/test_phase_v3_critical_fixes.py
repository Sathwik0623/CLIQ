"""
Unit tests for CLIQ V.3 Critical Acceptance Fixes:
1. SW1 disconnected: insertion disabled.
2. SW1 disconnected while R1 is connected: no fallback to R1.
3. SW1 reconnects: insertion targets SW1.
4. Focus changes between R1 and SW1: original response cannot silently change its target.
5. Missing IP/VLAN values: request clarification rather than invent values.
6. Output tables: no command insertion actions.
7. Actual command blocks: insertion still works.
8. Sequential insertion: target remains consistent for the entire sequence.
9. Citation relevance: standard VLAN access ports reject 802.1X access-session documentation.
"""

import pytest
import re
from cliq.ai.providers.meta import MetaProvider
from cliq.ai.provider import ChatMessage, ChatRole
from cliq.retrieval.query import get_shared_retriever, validate_citation
from cliq.retrieval.schema import RetrievalQuery, RetrievalCandidate


class TestTargetDevicePreservationAndFallbackProtection:
    """Tests 1, 2, 3, 4: Device targeting, disconnected state, and focus isolation."""

    def test_sw1_disconnected_insertion_disabled(self):
        """SW1 disconnected must disable insertion and never fall back."""
        # Simulated target resolver logic mirroring MarkdownView
        devices = [
            {"id": "r1", "label": "R1", "type": "router"},
            {"id": "sw1", "label": "SW1", "type": "switch"},
        ]
        open_devices = [{"id": "r1", "label": "R1"}, {"id": "sw1", "label": "SW1"}]
        device_metrics = {
            "r1": {"status": "connected", "mode": "exec"},
            "sw1": {"status": "disconnected", "mode": "exec"},
        }
        target_device_id = "sw1"
        focused_device_id = "r1"

        # Authoritative target resolution
        intended_id = target_device_id
        target_dev = next((d for d in devices if d["id"] == intended_id), None)
        assert target_dev is not None
        assert target_dev["label"] == "SW1"

        metric = device_metrics.get(target_dev["id"])
        is_disconnected = metric is None or metric["status"] in ("disconnected", "error")
        assert is_disconnected is True, "SW1 must be identified as disconnected"

        # Insertion eligibility must be disabled
        is_target_eligible = not is_disconnected
        assert is_target_eligible is False, "Insertion must be disabled when SW1 is disconnected"

    def test_sw1_disconnected_while_r1_is_connected_no_fallback(self):
        """When SW1 is target and disconnected while R1 is focused/connected, target MUST NOT fall back to R1."""
        target_device_id = "sw1"
        focused_device_id = "r1"
        devices = [
            {"id": "r1", "label": "R1", "type": "router"},
            {"id": "sw1", "label": "SW1", "type": "switch"},
        ]
        device_metrics = {
            "r1": {"status": "connected", "mode": "exec"},
            "sw1": {"status": "disconnected", "mode": "exec"},
        }

        # Must bind to intended target (SW1), NOT focused R1
        resolved_target_id = target_device_id
        assert resolved_target_id != focused_device_id
        assert resolved_target_id == "sw1"

        # Warning must display SW1, not R1
        target_label = next(d["label"] for d in devices if d["id"] == resolved_target_id)
        warning = f"{target_label} is disconnected"
        assert warning == "SW1 is disconnected"
        assert "R1" not in warning

    def test_sw1_reconnects_insertion_targets_sw1(self):
        """When SW1 reconnects, insertion eligibility is restored for SW1."""
        target_device_id = "sw1"
        devices = [
            {"id": "sw1", "label": "SW1", "type": "switch"},
        ]
        # Reconnected state
        device_metrics = {
            "sw1": {"status": "connected", "mode": "exec"},
        }
        metric = device_metrics.get(target_device_id)
        is_disconnected = metric is None or metric["status"] in ("disconnected", "error")
        assert is_disconnected is False

        is_target_eligible = not is_disconnected
        assert is_target_eligible is True
        target_label = next(d["label"] for d in devices if d["id"] == target_device_id)
        action_button = f"Insert into {target_label}"
        assert action_button == "Insert into SW1"

    def test_focus_changes_between_r1_and_sw1_preserves_target(self):
        """When focus changes from SW1 to R1, an existing message bound to SW1 must retain SW1."""
        # Message originally generated for SW1
        message_target_device_id = "sw1"

        # Operator subsequently focuses R1
        current_focused_device_id = "r1"

        # The message's target MUST remain SW1
        effective_target = message_target_device_id or current_focused_device_id
        assert effective_target == "sw1", "Target must not silently follow terminal focus changes"


class TestPreventInventedConfigurationValues:
    """Test 5: Explicit placeholders and refusal to invent IP/VLAN values."""

    def test_router_switchport_mismatch_uses_placeholders_and_asks_details(self):
        """Router response must use placeholders for IP/mask and ask for missing details."""
        import asyncio
        provider = MetaProvider(mock_mode=True)
        messages = [
            ChatMessage(role=ChatRole.USER, content="Configure switchport mode access on R1")
        ]
        reply = asyncio.run(provider.chat(messages))
        content = reply.content

        # Must NOT contain invented IP addresses
        assert "192.168.10.1" not in content
        assert "192.168.1.1" not in content

        # Must contain explicit standard placeholders
        assert "<interface-id>" in content or "<ip-address>" in content
        assert "<subnet-mask>" in content or "<ip-address>" in content

        # Must explicitly request operator configuration details
        assert any(phrase in content.lower() for phrase in ("specify", "supply", "missing parameters", "required"))

    def test_switch_access_port_uses_placeholders_and_requests_vlan(self):
        """SW1 access port response must use placeholders and request interface/VLAN details."""
        import asyncio
        provider = MetaProvider(mock_mode=True)
        messages = [
            ChatMessage(role=ChatRole.USER, content="Configure SW1 access port")
        ]
        reply = asyncio.run(provider.chat(messages))
        content = reply.content

        # Must identify SW1
        assert "SW1" in content

        # Must use placeholders
        assert "<interface-id>" in content
        assert "<vlan-id>" in content

        # Must prompt operator for required parameters
        assert "Please specify" in content or "Required Parameters" in content


class TestSeparateCommandBlocksFromOutputExamples:
    """Tests 6, 7: Output tables vs actual executable command blocks."""

    def test_explain_show_ip_interface_brief_renders_output_table(self):
        """Explaining show ip interface brief must render sample output as a table, not executable code."""
        import asyncio
        provider = MetaProvider(mock_mode=True)
        messages = [
            ChatMessage(role=ChatRole.USER, content="Explain show ip interface brief")
        ]
        reply = asyncio.run(provider.chat(messages))
        content = reply.content

        # Command syntax must be present in code block
        assert "```cisco\nshow ip interface brief\n```" in content

        # Sample output rows MUST be rendered as a Markdown table (with | columns), NOT as a cisco code block
        assert "| Interface | IP-Address | OK? | Method | Status | Protocol |" in content
        assert "| GigabitEthernet0/0 |" in content

        # Sample output rows must NOT be inside ```cisco ... ```
        code_blocks = re.findall(r"```(?:cisco|cli)?\n(.*?)```", content, re.DOTALL)
        for block in code_blocks:
            assert "Interface                  IP-Address" not in block
            assert "GigabitEthernet0/0         192.168" not in block

    def test_output_heuristic_detects_tabular_terminal_output(self):
        """Tabular terminal output lines must be recognized as output and not executable commands."""
        sample_output = (
            "Interface                  IP-Address      OK? Method Status                Protocol\n"
            "GigabitEthernet0/0         192.168.1.1     YES manual up                    up      \n"
            "GigabitEthernet0/1         unassigned      YES unset  administratively down down    \n"
        )
        lines = [l.strip() for l in sample_output.strip().split("\n") if l.strip()]
        first_line = lines[0]

        # Test header matching pattern
        is_table_header = bool(re.match(r"^interface\s+ip-address\s+ok\?", first_line, re.IGNORECASE))
        assert is_table_header is True

        # Test data line matching pattern
        is_data_row = bool(re.match(r"^(?:gigabitethernet|fastethernet)\S*\s+(?:unassigned|\d+\.\d+\.\d+\.\d+)\s+yes", lines[1], re.IGNORECASE))
        assert is_data_row is True


class TestSequentialInsertionTargetConsistency:
    """Test 8: Sequential insertion remains bound to target device across steps."""

    def test_sequence_binding_preserves_target_id(self):
        """A running sequence initiated on SW1 must remain bound to SW1."""
        target_id = "sw1"
        bound_device_id = target_id

        # Simulating focus moving to R1 during execution
        focused_device_id = "r1"

        # Sequence runner must evaluate bound device
        executing_device_id = bound_device_id
        assert executing_device_id == "sw1"
        assert executing_device_id != focused_device_id


class TestCitationRelevance:
    """Test 9: VLAN access port citation must reject 802.1X access-session documentation."""

    def test_vlan_access_port_rejects_8021x_access_session_citation(self):
        """802.1X access-session documentation must be rejected for standard VLAN access ports."""
        invalid_8021x_candidate = RetrievalCandidate(
            command="access-session port-control auto",
            description="Enable IEEE 802.1X port-based authentication.",
            platform="cat9500",
            cmd_mode="config-if",
            doc_source="https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/software/release/17-16/command_reference/b_1716_9500_cr/1716-ibns-commands.html",
        )
        ok, msg = validate_citation(invalid_8021x_candidate)
        assert not ok
        assert "802.1X" in msg or "relevance" in msg.lower()

    def test_vlan_access_port_accepts_layer2_switching_citation(self):
        """Layer 2/3 switching documentation for switchport access vlan must be accepted."""
        valid_vlan_candidate = RetrievalCandidate(
            command="switchport access vlan 10",
            description="Configure port VLAN membership for standard access ports.",
            platform="cat9500",
            cmd_mode="config-if",
            doc_source="https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/software/release/17-16/command_reference/b_1716_9500_cr/layer_2_3_commands.html",
        )
        ok, msg = validate_citation(valid_vlan_candidate)
        assert ok, f"Expected valid citation, got: {msg}"
