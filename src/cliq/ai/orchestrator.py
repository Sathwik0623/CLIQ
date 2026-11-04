"""
CLIQ AI Copilot Orchestration Engine (Phase V).

Assembles conversation histories, selected network context chips, and uploaded
attachments into a coherent, structured prompt for network engineering analysis.
Streams model responses token-by-token and persists message turn histories.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from cliq.ai.conversations import ConversationStore, StoredMessage
from cliq.ai.network_context import get_network_context_service
from cliq.ai.provider import ChatMessage, ChatRole, StreamChunk
from cliq.ai.registry import get_provider
from cliq.ai import device_exec as _device_exec
from cliq.ai import config_plan as _config_plan
from cliq.ai import lab_discovery as _lab_discovery

log = logging.getLogger("cliq.ai.orchestrator")

SYSTEM_PROMPT = """You are CLIQ Copilot, an enterprise-grade AI networking assistant built into the CLIQ Network Engineering IDE.

Cisco IOS Command Syntax Precision & Platform Rules (MANDATORY):
1. Canonical Cisco IOS / IOS-XE Command Syntax:
   - For inspecting an interface running-configuration: ALWAYS use `show run interface <interface>` (singular, e.g. `show run interface Ethernet0/3`). NEVER output the invalid plural form `show run interfaces <interface>`.
   - Prefer standard canonical Cisco IOS/IOS-XE command forms.
2. Platform Compatibility & Device-Aware Command Targeting:
   - Device Validation: Before recommending or formatting commands, validate the vendor, platform, OS, device role, and CLI mode against the target device.
   - Routers (e.g. R1, R2, role: router): NEVER recommend or output Cisco switchport configuration commands (`switchport mode access`, `switchport trunk allowed vlan`, `switchport access vlan`, `switchport`) for a router. If asked how to configure a switchport on a router, explicitly explain the mismatch: explain that switchport commands are Layer 2 switching commands invalid on router interfaces, and provide Layer 3 routed interface or subinterface configuration instead. DO NOT offer switchport insertion for routers.
   - Switches (e.g. SW1, SW2, role: switch): Switchport commands, VLAN commands, and spanning-tree commands are valid and supported.
   - Non-Cisco Vendors (e.g. Juniper Junos, Linux endpoints): NEVER output Cisco IOS-XE commands for Juniper or Linux devices. For Juniper, output native Junos syntax (e.g. `show interfaces terse`, `set interfaces ...`) in a ```junos block. Explain the vendor mismatch if asked to run Cisco commands on Juniper.
   - Incompatibility Handling: If the target device is incompatible with the requested command, clearly explain the mismatch and do not offer insertion. NEVER silently redirect commands to another device.
   - Interface Troubleshooting: Treat interface names (e.g. `GigabitEthernet0/1`, `eth0`) as illustrative generic examples UNLESS discovered from actual live telemetry in the context. Prefer actual interface names (e.g. `GigabitEthernet0/0`) whenever live device context is available. Clearly distinguish documentation-based suggestions from live-verified commands.
   - OSPF & Platform Validation: Validate command compatibility against platform metadata. Distinguish exact verified syntax from documentation-only suggestions. Avoid presenting Cisco IOS-XE commands as universal networking commands; always qualify the platform.
3. Multi-Command Sequential Code Blocks & Platform Separation:
   - When the user asks for a troubleshooting sequence, diagnostic checks, all commands together, or a workflow for sequential execution, generate ONE coherent fenced code block containing all related commands with each command on its own separate line.
   - DO NOT fragment diagnostic sequences into separate single-line blocks with prose between them. Explanations and technical rationale must go outside the code block (before or after it).
   - Platform Separation is MANDATORY:
     * Cisco IOS / IOS-XE commands MUST be in a ```cisco code block.
     * Linux host / PC / Server commands MUST be in a ```bash code block.
     * NEVER mix Cisco and Linux commands in the same code block.
     * When targeting multiple device types or '@All Devices', generate separate platform-specific blocks clearly grouped and labeled by target platform or device role (e.g. "For Cisco Routers/Switches:", "For Linux Endpoints / PCs:").
   - Clearly separate read-only diagnostic checks from configuration-changing suggestions.
4. Truthfulness & Verification:
   - Clearly label uncertain commands with "(Requires verification on device)".
   - NEVER claim a command was tested or executed on a device unless its observed live output is present in the context.
   - Do not recommend remediation or configuration changes unless observed evidence directly supports it.
   - Never repeat the same command under both 'Recommended Checks' and 'Suggested Commands'.

Core Responsibilities and Constraints:
- Provide rigorous, deterministic, expert-level network engineering guidance for Cisco IOS/IOS-XE, Arista EOS, Juniper Junos, and general routing/switching architectures.
- Read-only diagnostic commands may be executed automatically through your device tool (see Autonomous Execution below). You cannot and must never attempt to apply configuration changes automatically.
- Emphasize safety, verification, and non-disruptive diagnostics before suggesting configuration changes.
- Output high-clarity network CLI commands in syntax-highlighted markdown code blocks with clear language tags (e.g. ```cisco).

Target Device Ambiguity & Clarification:
- When a user query is ambiguous about which device is targeted (e.g., 'what is the interface status?', 'why is the route down?') and no device context or active device is selected, DO NOT guess or assume a device. Instead, clarify which device the operator wishes to examine, or offer to inspect the active device or all connected devices.
- For general conceptual questions (e.g. 'What is the difference between an ARP table and a routing table?'), explain the protocol concepts directly without inventing device issues or requiring device context.
- Never claim a device is connected or in a particular state unless supported by the observed context.

Evidence and Factuality Standards (Strict):
1. Distinguish Evidence vs Inferences vs Assumptions:
   - Explicitly separate **Observed Evidence** (directly quoted or visible in provided telemetry outputs), **Inferred Possibilities** (logical deductions), and **Unverified Assumptions** (unknowns requiring operator verification).
   - Never claim an unverified state as a fact.
2. Technical Consistency Across Subsystems:
   - Maintain absolute consistency between Layer 1/2 interface status and Layer 3 routing state.
   - For example: when an interface is `administratively down` or `down/down` in 'show ip interface brief', its connected subnet (C) will NOT be installed in 'show ip route'. Explain that the connected route is absent BECAUSE the interface is down. Never contradict yourself by asserting that a connected route is present while its interface is down.
3. Zero Telemetry Fabrication:
   - NEVER fabricate telemetry, command outputs, MAC addresses, or IP addresses.
   - If live telemetry is missing or not provided for a device, state clearly: 'Live telemetry for [Device] is not available in the current context. Run show ip interface brief / show ip route to inspect.'
4. Conceptual Questions:
   - If the user asks a conceptual networking question (e.g. 'Explain the difference between a routing table and an ARP table'), provide an authoritative, technical explanation of protocol mechanisms without inventing device outages, fictitious OSPF errors, or imaginary device problems.

Troubleshooting Response Structure (when diagnosing incidents or routing/switching issues):
1. **Understanding of the Issue**: Concise summary of the symptom, topology, and user question.
2. **Observed Evidence**: Direct analysis of the provided command outputs (citing device, interface, IP, route, state).
3. **Likely Root Cause**: Identified technical root cause based strictly on observed evidence.
4. **Recommended Checks**: Specific read-only verification commands to confirm or rule out edge cases.
5. **Suggested Commands**: Exact syntax required to resolve the issue safely.
6. **Expected Results**: Output that confirms resolution.
7. **Next Steps**: Follow-up verifications.

Authoritative Documentation & Grounded Knowledge (RAG):
- When official documentation is provided under '--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---':
  1. Treat retrieved documentation as the primary authoritative source of truth for command syntax, keyword arguments, and protocol mechanisms.
  2. Ground your explanations in the retrieved documentation. NEVER fabricate unsupported command syntax.
  3. Clearly distinguish retrieved documentation (what the platform supports) from live device state (what the connected device is currently experiencing).
  4. Always cite documentation sources at the end of your technical guidance under an '### Authoritative References' section (e.g. citing command name, documentation file, and official URL when available).

5. Prevent Invented Configuration Values (MANDATORY):
   - NEVER invent IP addresses (e.g. 192.168.10.1), subnet masks (e.g. 255.255.255.0), VLAN IDs, interface IDs, or routing parameters unless explicitly supplied by the operator or observed in live telemetry.
   - Use explicit placeholders when values are not known (e.g. `<ip-address>`, `<subnet-mask>`, `<vlan-id>`, `<interface-id>`).
   - Ask the operator for missing configuration details. Clearly separate examples from actionable configuration.
   - Do NOT present configuration as ready to apply without required information.
6. Separation of Command Blocks and Output Examples (MANDATORY):
   - Render executable command syntax as ```cisco or ```bash code blocks.
   - Render sample terminal output, logs, routing tables, and interface lists as plain text or Markdown tables.
   - NEVER format sample output rows (e.g. output from 'show ip interface brief') as executable CLI command blocks.
7. Citation Relevance:
   - Retrieve sources supporting the exact recommended command. Verify platform, OS version, and CLI mode.
   - For standard VLAN access ports (`switchport mode access`), cite Layer 2/3 switching documentation (`layer_2_3_commands.html`), NEVER 802.1X access-session documentation.
   - Reject irrelevant citations and never claim a citation is authoritative for a command it does not document.

Autonomous Device Command Execution (TOOL):
- When the engineer asks you to execute, run, retrieve, or inspect commands yourself (e.g. 'Inspect SW1 and retrieve show vlan brief ... Execute these commands yourself'), the IDE runs each read-only command on the live device console BEFORE you answer.
- Actual outputs appear under '--- COPILOT AUTONOMOUS EXECUTION RESULTS ---'. Treat them as Observed Evidence: quote and summarize ONLY what they contain. NEVER fabricate telemetry.
- Configuration-changing commands are NEVER auto-executed. They arrive as 'approval_required' with a ticket id: explain exactly what would change, name the device, and tell the engineer to review and Approve in the execution card. Never invent approval.
- Commands that fail platform validation arrive as 'not executed' with a reason: explain the mismatch briefly (e.g. Cisco syntax on a Linux endpoint) and never invent output for them.
- When no execution results are present, answer advisory as usual with insertable ```cisco / ```bash blocks.
Configuration Planning (M1.4 PLAN ONLY):
- When the engineer asks to configure the network (VLANs, access/trunk ports, endpoint addresses), the IDE builds a structured multi-device plan BEFORE you answer; it appears under '--- CONFIGURATION PLAN ---' with per-device commands and open questions.
- Present the plan, call out every open question, and direct the engineer to review and Approve in the plan card. You NEVER prepare, approve, or execute configuration yourself — approval and execution are explicit operator actions in the UI.

Tone: Professional, direct, authoritative, and concise. Avoid conversational filler."""


class CopilotOrchestrator:
    """Orchestrates AI chat completions with dynamic context assembly and persistence."""

    def __init__(
        self,
        store: Optional[ConversationStore] = None,
        retriever: Optional[Any] = None,
    ):
        self.store = store or ConversationStore()
        self.context_service = get_network_context_service()
        self._retriever = retriever

    def set_retriever(self, retriever: Any) -> None:
        """Inject or update the active CommandRetriever."""
        self._retriever = retriever

    def get_retriever(self) -> Optional[Any]:
        """Get the injected retriever or lazily obtain the shared singleton."""
        if self._retriever is not None:
            return self._retriever
        try:
            from cliq.retrieval.query import get_shared_retriever
            self._retriever = get_shared_retriever()
        except Exception as exc:
            log.warning("Could not obtain shared retriever: %s", exc)
        return self._retriever

    @staticmethod
    def _is_rag_enabled() -> bool:
        val = os.environ.get("CLIQ_RAG_ENABLED", "1").lower().strip()
        return val in ("1", "true", "yes", "on")

    @staticmethod
    def should_retrieve_for_query(text: str) -> bool:
        """Determine whether input requires official networking documentation retrieval."""
        cleaned = text.strip().lower()
        if not cleaned:
            return False

        # Exclude pure greetings, pleasantries, and meta questions about Copilot
        greeting_patterns = [
            r"^(?:hi|hello|hey|howdy|hola|greetings)(?:[!\s,.]|$)",
            r"^(?:good\s+(?:morning|afternoon|evening|day))(?:[!\s,.]|$)",
            r"^(?:thanks|thank\s+you|thx|cheers)(?:[!\s,.]|$)",
            r"^(?:bye|goodbye|see\s+ya|farewell)(?:[!\s,.]|$)",
            r"^(?:who\s+are\s+you|what\s+can\s+you\s+do|what\s+is\s+your\s+name)(?:[?\s,.]|$)",
        ]
        for pat in greeting_patterns:
            if re.match(pat, cleaned):
                words = cleaned.split()
                if len(words) <= 5:
                    return False

        # CLI command verbs or common Cisco keywords
        cisco_keywords = {
            "show", "ip", "interface", "interfaces", "route", "routing", "ospf", "bgp",
            "eigrp", "vlan", "trunk", "access", "switchport", "port", "ping", "traceroute",
            "acl", "access-list", "nat", "dhcp", "dns", "arp", "mac", "stp", "spanning-tree",
            "lldp", "cdp", "vrf", "mpls", "neighbor", "status", "brief", "running-config",
            "config", "configure", "troubleshoot", "down", "err-disabled", "packet", "mtu"
        }
        tokens = set(re.findall(r"[a-z0-9\-]+", cleaned))
        if tokens & cisco_keywords:
            return True

        # Natural language question indicators with more than 3 words
        if any(w in cleaned for w in ["what", "how", "explain", "why", "which", "command", "difference"]):
            return True

        return False

    def _retrieve_knowledge(
        self,
        user_text: str,
        detected_platform: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Any]:
        if not self._is_rag_enabled():
            return []
        if not self.should_retrieve_for_query(user_text):
            return []
        retriever = self.get_retriever()
        if not retriever:
            return []
        try:
            from cliq.retrieval.schema import RetrievalQuery
            top_k_env = int(os.environ.get("CLIQ_RETRIEVAL_TOP_K", str(top_k)))
            q = RetrievalQuery(
                input_text=user_text,
                mode="auto",
                platform=detected_platform,
                top_k=top_k_env,
                min_confidence=0.40,
            )
            result = retriever.query(q)
            return result.candidates
        except Exception as exc:
            log.warning("Copilot RAG retrieval error (falling back to model weights): %s", exc)
            return []

    def _format_retrieved_knowledge(self, candidates: List[Any]) -> str:
        if not candidates:
            return ""
        try:
            from cliq.retrieval.query import validate_citation
        except ImportError:
            validate_citation = None

        valid_candidates = []
        for c in candidates:
            if validate_citation:
                ok, _ = validate_citation(c)
                if not ok:
                    continue
            valid_candidates.append(c)

        if not valid_candidates:
            return ""

        lines = [
            "--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---",
            "The following authoritative documentation was retrieved from the official CLIQ knowledge base for this query:",
        ]
        for i, c in enumerate(valid_candidates, 1):
            cmd = getattr(c, "command", "")
            desc = getattr(c, "description", "").strip() or "Official command syntax and operational usage."
            doc_src = getattr(c, "doc_source", "") or "Cisco IOS-XE Command Reference"
            platform = getattr(c, "platform", "iosxe").upper()
            mode = getattr(c, "cmd_mode", "exec")
            match_type = getattr(c, "match_type", "semantic").title()
            conf = getattr(c, "confidence", 0.0)
            url = getattr(c, "source_url", "")

            lines.append(f"[{i}] Command: `{cmd}`")
            lines.append(f"    Description: {desc}")
            lines.append(f"    Platform: Cisco {platform} (Mode: {mode}) | Source: {doc_src}")
            if url:
                lines.append(f"    URL: {url}")
            lines.append(f"    Match Type: {match_type} (Confidence: {conf:.2f})")
        return "\n".join(lines)

    def _assemble_context_text(
        self,
        session_token: Optional[str],
        context_chips: List[Dict[str, Any]],
        attachments: List[Dict[str, Any]],
        user_text: str = "",
    ) -> str:
        """Format user-selected context chips, auto-retrieved device state, and file attachments."""
        blocks: List[str] = []
        processed_devices = set()

        # 1. Process selected network context chips
        if context_chips:
            blocks.append("--- EXPLICIT NETWORK CONTEXT (USER-SELECTED) ---")
            for chip in context_chips:
                chip_type = chip.get("type", "device")
                device_id = chip.get("deviceId") or chip.get("device_id")
                if not device_id:
                    raw_id = str(chip.get("id") or "")
                    device_id = raw_id.split("_", 1)[1] if "_" in raw_id else raw_id

                label = chip.get("label", device_id)
                clean_label = label.lstrip("@").split()[0] if label else str(device_id)
                processed_devices.add(clean_label.upper())
                processed_devices.add(str(device_id).upper())

                if (chip_type in ("all_devices", "all") or chip.get("id") == "all_devices" or "all devices" in label.lower()):
                    all_devs = self.context_service.list_devices(session_token)
                    for dev in all_devs:
                        d_id = dev.get("id") or ""
                        d_lbl = dev.get("label") or d_id
                        processed_devices.add(d_lbl.upper())
                        processed_devices.add(str(d_id).upper())
                        info = self.context_service.get_device_info(session_token or "", d_id)
                        intfs = self.context_service.get_interfaces(session_token, d_id)
                        node_type = info.get("node_type") or ""
                        platform_desc = _device_exec.describe_platform(node_type)
                        blocks.append(f"[Device Summary: {d_lbl} (ID: {d_id}) | Platform: {platform_desc} (Type: {node_type}) | Status: {info.get('status')} | Mode: {info.get('mode')}]\n[IP Interfaces Brief]:\n{intfs}")
                elif chip_type in ("device", "active_device") and device_id:
                    info = self.context_service.get_device_info(session_token or "", device_id)
                    intfs = self.context_service.get_interfaces(session_token, device_id)
                    node_type = info.get("node_type") or ""
                    platform_desc = _device_exec.describe_platform(node_type)
                    blocks.append(f"[Device Summary: {clean_label} (ID: {device_id}) | Platform: {platform_desc} (Type: {node_type}) | Status: {info.get('status')} | Mode: {info.get('mode')}]\n[IP Interfaces Brief]:\n{intfs}")
                elif chip_type == "interfaces" and device_id:
                    intfs = self.context_service.get_interfaces(session_token, device_id)
                    blocks.append(f"[Device {clean_label} - IP Interfaces Brief]:\n{intfs}")
                elif chip_type == "routes" and device_id:
                    routes = self.context_service.get_routing_table(session_token, device_id)
                    blocks.append(f"[Device {clean_label} - Routing Table]:\n{routes}")
                elif chip_type == "config" and device_id:
                    cfg = self.context_service.get_running_config(session_token, device_id)
                    cfg_snippet = "\n".join(cfg.splitlines()[:120])
                    blocks.append(f"[Device {clean_label} - Running Configuration Snippet (Sanitized)]:\n{cfg_snippet}")
                elif chip_type == "neighbors" and device_id:
                    nbrs = self.context_service.get_neighbors(session_token, device_id)
                    blocks.append(f"[Device {clean_label} - Discovery Neighbors]:\n{nbrs}")
                elif chip_type == "terminal_history" and device_id:
                    hist = self.context_service.get_recent_commands(session_token, device_id)
                    blocks.append(f"[Device {clean_label} - Recent Terminal Commands]:\n" + "\n".join(hist))

        # 2. Auto-retrieve context for mentioned devices if not explicitly chipped.
        # Candidates come from the session's live inventory (never a hardcoded
        # device list), matched against labels/ids named in the turn.
        if user_text:
            try:
                inventory = self.context_service.list_devices(session_token) or []
            except Exception:
                inventory = []
            mentioned = []
            for dev in inventory:
                d_id = str(dev.get("id") or "")
                d_label = str(dev.get("label") or "") or d_id
                if not d_id:
                    continue
                if (d_label.upper() in processed_devices
                        or d_id.upper() in processed_devices):
                    continue
                for key in (d_label, d_id):
                    if key and re.search(rf"\b{re.escape(key)}\b",
                                         user_text, re.IGNORECASE):
                        mentioned.append((d_label, d_id))
                        break

            if mentioned:
                auto_blocks = []
                for d_label, d_id in mentioned:
                    intfs = self.context_service.get_interfaces(session_token, d_id)
                    if not intfs.startswith("[Device "):
                        auto_blocks.append(f"[Device {d_label} - IP Interfaces Brief]:\n{intfs}")
                    else:
                        auto_blocks.append(f"[Device {d_label}: live telemetry not currently available or device not connected]")
                if auto_blocks:
                    blocks.append("--- AUTO-RETRIEVED LIVE DEVICE CONTEXT ---")
                    blocks.extend(auto_blocks)

        # 3. Process attached files
        if attachments:
            blocks.append("--- ATTACHED CONFIGURATION & LOG FILES ---")
            for att in attachments:
                fname = att.get("filename", "file.txt")
                content = att.get("text_content", "")
                if content:
                    snippet = "\n".join(content.splitlines()[:200])
                    blocks.append(f"[File: {fname}]:\n{snippet}")

        # 4. Retrieve authoritative documentation from knowledge base (RAG)
        if user_text:
            candidates = self._retrieve_knowledge(user_text)
            if candidates:
                rag_block = self._format_retrieved_knowledge(candidates)
                if rag_block:
                    blocks.append(rag_block)

        return "\n\n".join(blocks)

    @staticmethod
    def _is_availability_error(err_msg: str) -> bool:
        """Determine if an error represents a model availability failure (e.g. 404, 503) suitable for fallback."""
        if not err_msg:
            return False
        lower = err_msg.lower()
        # Never fallback on auth errors or rate limits
        if any(k in lower for k in ["401", "403", "unauthorized", "forbidden", "invalid meta api key", "authentication failed", "rate limit", "429"]):
            return False
        # Fallback on availability / not found errors
        if any(k in lower for k in ["404", "model_not_found", "503", "service unavailable", "model not found", "does not exist", "unsupported model"]):
            return True
        return False

    # Explicit configuration-planning verbs. Deliberately verb forms only:
    # "configuration block" / "configuration plan" nouns must not trigger a
    # build, and "regenerate" is excluded via lookbehind.
    _CONFIG_VERB_RE = re.compile(
        r"\b(configure[sd]?|configuring|reconfigur\w*|provision\w*|deploy(?:ment|ing|s)?\b|"
        r"setup|set\s+up|create\s+vlan|"
        r"assign(\s+\w+){0,3}\s+(vlan|ip|address)|"
        r"new\s+configuration)",
        re.IGNORECASE,
    )
    # Weak nouns: config signals only without read-only framing ("show ...
    # trunk" is discovery, not a build order).
    _CONFIG_NOUN_RE = re.compile(
        r"\bswitchport\b|\btrunk\b|access\s+ports?",
        re.IGNORECASE,
    )
    _GENERATE_PLAN_RE = re.compile(
        r"(?<![\w])generate\b.{0,30}\bplan\b",
        re.IGNORECASE,
    )
    _VERB_NEGATION_RE = re.compile(
        r"\b(?:do\s+not|don't|does\s+not|doesn't|never|not|no|without|stop)\b"
        r"[\w\s,]{0,24}$",
        re.IGNORECASE,
    )
    _READONLY_FRAMING_RE = re.compile(
        r"\bshow\b|read-only|read only|discover\w*|inspect\w*|retriev\w*|"
        r"reconcil\w*|\bstatus\b|\bdisplay\b|\breport\b|do\s+not\s+configur|"
        r"no\s+configuration",
        re.IGNORECASE,
    )
    _VALIDATE_VERB_RE = re.compile(
        r"validat\w*|inspect\w*|review\w*|verif\w*|\bchecks?\b|confirm\w*|"
        r"reject\w*|do\s+not\s+regenerat|don't\s+regenerat|without\s+regenerat|"
        r"current\s+draft|this\s+plan|the\s+plan|existing\s+plan|continue\s+.*review",
        re.IGNORECASE,
    )
    _PLAN_WORD_RE = re.compile(r"\bplan\b|\bdraft\b|\bcfg-[0-9a-f]{10}\b", re.IGNORECASE)
    # Retained execution evidence bounds (historical tool_results).
    MAX_RETAINED_EXEC_RESULTS = 10
    MAX_RETAINED_EXEC_CHARS = 12000
    MAX_RETAINED_ENTRY_CHARS = 3000
    _PLAN_ID_RE = re.compile(r"\bcfg-[0-9a-f]{10}\b")

    @classmethod
    def _unnegated_search(cls, pattern, text):
        """True when pattern matches outside a negation span."""
        for m in pattern.finditer(text or ""):
            if not cls._VERB_NEGATION_RE.search(text[:m.start()]):
                return True
        return False

    def _has_config_verb(self, text):
        return (self._unnegated_search(self._CONFIG_VERB_RE, text)
                or self._unnegated_search(self._GENERATE_PLAN_RE, text))

    _LAB_INTENT_RE = re.compile(
        r"\b(create|build|deploy|provision|generate|plan|design|setup|set\s+up)\b"
        r".{0,40}\b(labs?|topology|environment)\b"
        r"|\bdiscover\w*\b.{0,30}\b(controller|cml|environment|labs?|topology)\b"
        r"|\b(lab|environment)\s+discovery\b"
        r"|\bwhich\s+(device\s+)?images?\s+are\s+available\b"
        r"|\bwhat\s+(node\s+)?definitions?\s+are\s+available\b"
        r"|\blist\s+(my\s+|the\s+|all\s+)?labs\b"
        r"|\blab\s+creation\b",
        re.IGNORECASE,
    )
    _LAB_ID_RE = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )

    # Read-only inspection of the ACTIVE lab (device discovery inside a lab).
    # Distinct from _LAB_INTENT_RE (Lab Creation): these turns must also
    # suppress configuration planning entirely.
    _LAB_INSPECT_RE = re.compile(
        r"\binspect\w*\b.{0,40}\b(active|current|this|the|named)\s+"
        r"(lab|topology|devices?)\b"
        r"|\bdiscover\w*\b.{0,30}\b(this|the|active|current|named)\s+"
        r"(lab|topology)\b"
        r"|\bexamin\w*\b.{0,40}\btopology\b"
        r"|\bread-only\b.{0,30}\bdevice\s+discovery\b"
        r"|\binspect\w*\b.{0,30}\bdevices?\b.{0,20}\bin\b",
        re.IGNORECASE,
    )

    @classmethod
    def _should_discover_lab(cls, user_text) -> bool:
        """True when the turn asks for Lab Creation or controller discovery."""
        text = user_text or ""
        return bool(cls._LAB_INTENT_RE.search(text)
                    or cls._LAB_INSPECT_RE.search(text))

    @classmethod
    def _lab_id_hint(cls, user_text):
        """Lab UUID mentioned in the turn, for scoped topology fetch."""
        m = cls._LAB_ID_RE.search(user_text or "")
        return m.group(0) if m else None

    @staticmethod
    def _prior_discovery_summary(conv):
        """Most recent retained discovery summary in this conversation."""
        for m in reversed(getattr(conv, "messages", None) or []):
            md = getattr(m, "metadata", None) or {}
            if isinstance(md, dict) and md.get("lab_discovery"):
                return md["lab_discovery"]
        return None

    @staticmethod
    def _prior_exec_results(conv, user_text="", lab_id=None):
        """Retained execution results from earlier turns, newest first.

        Mirrors _prior_discovery_summary: same conversation only
        (conv.messages is conversation-scoped by the store). Each entry
        preserves device, lab, command, output, status and error fields plus
        the originating message timestamp when available. Device/lab filters
        narrow only when they match something, otherwise everything is kept.
        """
        try:
            messages = list(getattr(conv, "messages", None) or [])
        except Exception:
            return []
        collected = []
        cap = CopilotOrchestrator.MAX_RETAINED_EXEC_RESULTS
        for m in reversed(messages):
            md = getattr(m, "metadata", None) or {}
            if not isinstance(md, dict):
                continue
            results = md.get("tool_results")
            if not isinstance(results, list):
                continue
            ts = getattr(m, "created_at", None)
            recorded = None
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                try:
                    recorded = datetime.fromtimestamp(
                        ts, tz=timezone.utc).isoformat(timespec="seconds")
                except Exception:
                    recorded = None
            for r in results:
                if not isinstance(r, dict):
                    continue
                if not (r.get("command") or r.get("device_id")
                        or r.get("device_label")):
                    continue
                entry = {
                    "device_id": str(r.get("device_id") or ""),
                    "device_label": str(r.get("device_label") or ""),
                    "lab_id": str(r.get("lab_id") or ""),
                    "platform": str(r.get("platform") or ""),
                    "command": str(r.get("command") or ""),
                    "send_command": str(r.get("send_command") or ""),
                    "stdout": str(r.get("stdout") or ""),
                    "stderr": str(r.get("stderr") or ""),
                    "exit_status": r.get("exit_status", 0),
                    "success": bool(r.get("success", False)),
                    "error": str(r.get("error") or ""),
                    "error_code": str(r.get("error_code") or ""),
                }
                if recorded:
                    entry["recorded_at"] = recorded
                collected.append(entry)
                if len(collected) >= cap:
                    break
            if len(collected) >= cap:
                break
        text = user_text or ""
        if text:
            wanted = set()
            for e in collected:
                for key in (e["device_label"], e["device_id"]):
                    if key and re.search(rf"\b{re.escape(key)}\b",
                                         text, re.IGNORECASE):
                        wanted.add(key)
            if wanted:
                pairs = {(e["device_label"], e["device_id"]) for e in collected
                         if e["device_label"] in wanted or e["device_id"] in wanted}
                collected = [e for e in collected
                             if (e["device_label"], e["device_id"]) in pairs]
        if lab_id:
            narrowed = [e for e in collected if e["lab_id"] == lab_id]
            if narrowed:
                collected = narrowed
        return collected

    @staticmethod
    def _format_retained_exec_results(entries) -> str:
        """Render retained results as bounded, clearly historical context."""
        lines = [
            "--- RETAINED DEVICE EXECUTION RESULTS (EARLIER TURNS, NOT LIVE) ---",
            "These outputs were collected by earlier autonomous executions in "
            "this conversation. They are historical evidence, not fresh "
            "telemetry; prefer live results above when both exist.",
        ]
        budget = CopilotOrchestrator.MAX_RETAINED_EXEC_CHARS
        per_entry = CopilotOrchestrator.MAX_RETAINED_ENTRY_CHARS
        ordered = list(reversed(entries or []))
        used, shown, omitted = 0, 0, 0
        for i, e in enumerate(ordered):
            label = e.get("device_label") or e.get("device_id") or "?"
            head = ("[%s | %s | success=%s exit=%s"
                    % (label, e.get("command") or "?",
                       e.get("success", False), e.get("exit_status", 0)))
            if e.get("error_code"):
                head += " " + str(e["error_code"])
            if e.get("recorded_at"):
                head += " | recorded " + str(e["recorded_at"])
            head += "]"
            body = e.get("stdout") or ""
            if len(body) > per_entry:
                body = body[:per_entry] + "\n...[retained output truncated]"
            if e.get("stderr"):
                err = str(e["stderr"])
                if len(err) > 1000:
                    err = err[:1000] + "\n...[truncated]"
                body += "\n[stderr]\n" + err
            if e.get("error"):
                body += "\n[execution note: " + str(e["error"]) + "]"
            chunk = "\n" + head + "\n" + body
            if used + len(chunk) > budget and shown:
                omitted = len(ordered) - shown
                break
            lines.append(chunk)
            used += len(chunk)
            shown += 1
        if omitted:
            lines.append("\n...and %d more retained result(s) omitted "
                         "(context budget)." % omitted)
        return "\n".join(lines)

    def _classify_config_request(self, session_token, user_text):
        """Classify a turn as plan build / plan validation / no planning.

        Returns (mode, plan_id): "build" creates via build_plan; "validate"
        resolves an explicit (or latest owned) plan without building; "none"
        plans nothing. Read-only discovery never builds.
        """
        text = user_text or ""
        explicit = self._PLAN_ID_RE.search(text)
        if explicit and self._VALIDATE_VERB_RE.search(text):
            return ("validate", explicit.group(0))
        if (self._VALIDATE_VERB_RE.search(text)
                and self._PLAN_WORD_RE.search(text)
                and not self._has_config_verb(text)):
            return ("validate", None)
        try:
            devices = self.context_service.list_devices(session_token)
        except Exception:
            devices = []
        import re as _re
        mentioned = False
        for dev in devices or []:
            for key in (str(dev.get("label") or ""), str(dev.get("id") or "")):
                if key and _re.search(rf"\b{_re.escape(key)}\b", text, _re.IGNORECASE):
                    mentioned = True
                    break
            if mentioned:
                break
        has_vlan_evidence = bool(
            _re.search(r"\bvlan\s+\d+", text, _re.IGNORECASE)
            and (_re.search(r"\b(?:Gi|GigabitEthernet|Fa|FastEthernet|Eth|Ethernet|eth)\s*\d",
                            text, _re.IGNORECASE)
                 or _re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)))
        # Read-only framing ("show ...", "read-only", "discover", "do not
        # configure") takes precedence over incidental config verbs such as
        # "deployed topology".
        if (self._has_config_verb(text) and (mentioned or has_vlan_evidence)
                and not self._READONLY_FRAMING_RE.search(text)):
            return ("build", None)
        weak_signal = (self._unnegated_search(self._CONFIG_NOUN_RE, text)
                       or has_vlan_evidence)
        if (weak_signal and (mentioned or has_vlan_evidence)
                and not self._READONLY_FRAMING_RE.search(text)):
            return ("build", None)
        return ("none", None)

    @staticmethod
    def _format_config_plan(plan) -> str:
        """Format a configuration plan as model context (review, never execute)."""
        lines = [
            "--- CONFIGURATION PLAN (AWAITING OPERATOR REVIEW) ---",
            f"Plan: {plan.title} (id: {plan.plan_id}, version: {plan.version})",
            plan.summary,
        ]
        for dp in plan.devices:
            if dp.steps:
                lines.append(f"\n[{dp.device_label} ({dp.role}/{dp.node_type}): {len(dp.steps)} commands]")
                for s in dp.steps[:40]:
                    lines.append(f"  - {s.command}  # {s.purpose}")
        open_q = [q for q in plan.questions if q.blocking and not q.resolved]
        if open_q:
            lines.append("\n[Open questions — ask the operator:]")
            for q in open_q:
                lines.append(f"  - {q.message}")
        if plan.assumptions:
            lines.append("\n[Assumptions to confirm:]")
            for a in plan.assumptions[:10]:
                lines.append(f"  - {a}")
        lines.append(
            "\nEndpoint commands use only capability-verified applets (ifconfig/route on BusyBox "
            "unless the plan states otherwise). Do not suggest sudo or ip commands: they are "
            "unverified on these endpoints.")
        lines.append("\nDirect the engineer to review the plan card and Approve explicitly. Never execute.")
        return "\n".join(lines)

    def _plan_autonomous(
        self,
        session_token: Optional[str],
        user_text: str,
    ):
        """Build a single-device autonomous execution plan, or None."""
        try:
            devices = self.context_service.list_devices(session_token)
        except Exception:
            return None
        if not devices:
            return None
        lab_lookup: Dict[str, str] = {}
        platform_lookup: Dict[str, str] = {}
        try:
            from cliq.web import server
            inv = server.DEVICES_BY_TOKEN.get(session_token or "") or {}
            for dev_id, meta in inv.items():
                if meta.get("lab_id"):
                    lab_lookup[dev_id] = meta["lab_id"]
                plat = _device_exec.classify_platform(
                    str(meta.get("type") or ""),
                    str(meta.get("ssh_device_type") or ""))
                if plat:
                    platform_lookup[dev_id] = plat
        except Exception:
            pass
        try:
            plan = _device_exec.plan_autonomous_execution(
                user_text, devices, lab_lookup, platform_lookup)
        except Exception as exc:
            log.warning("Autonomous plan parse error: %s", exc)
            return None
        if plan is None or not plan.lab_id:
            return None
        return plan

    def _plan_autonomous_multi(
        self,
        session_token: Optional[str],
        user_text: str,
    ):
        """Build one autonomous execution plan per mentioned device.

        Each plan reuses the single-device validation path via
        ``plan_multi_execution``; returns ([plans], skip_reason).
        """
        try:
            devices = self.context_service.list_devices(session_token)
        except Exception:
            return [], ""
        if not devices:
            return [], ""
        lab_lookup: Dict[str, str] = {}
        platform_lookup: Dict[str, str] = {}
        try:
            from cliq.web import server
            inv = server.DEVICES_BY_TOKEN.get(session_token or "") or {}
            for dev_id, meta in inv.items():
                if meta.get("lab_id"):
                    lab_lookup[dev_id] = meta["lab_id"]
                plat = _device_exec.classify_platform(
                    str(meta.get("type") or ""),
                    str(meta.get("ssh_device_type") or ""))
                if plat:
                    platform_lookup[dev_id] = plat
        except Exception:
            pass
        try:
            plans = _device_exec.plan_multi_execution(
                user_text, devices, lab_lookup, platform_lookup)
        except Exception as exc:
            log.warning("Autonomous multi-plan parse error: %s", exc)
            return [], ""
        kept = [p for p in plans if p.lab_id]
        if kept:
            return kept, ""
        # Nothing dispatchable: explain instead of passing silently.
        try:
            reason = _device_exec.dispatch_skip_reason(
                user_text, devices, lab_lookup)
        except Exception:
            reason = ""
        return [], reason

    @staticmethod
    def _format_tool_results(plan, outputs: List[Dict[str, Any]]) -> str:
        """Format live execution outputs as model context (Observed Evidence)."""
        lines = [
            "--- COPILOT AUTONOMOUS EXECUTION RESULTS (LIVE DEVICE OUTPUT) ---",
            f"Target: {plan.device_label} (lab: {plan.lab_id}). These commands were "
            "executed on the live console just now. Summarize ONLY these actual outputs.",
        ]
        for out in outputs:
            lines.append(
                f"\n[Command: {out.get('command')} | success={out.get('success')} "
                f"exit={out.get('exit_status')}]"
            )
            if out.get("error_code") == "approval_required":
                lines.append(
                    f"HELD FOR APPROVAL (ticket {out.get('ticket', {}).get('ticket_id')}): "
                    f"{out.get('error')}"
                )
            elif out.get("stdout"):
                stdout = out["stdout"]
                if len(stdout) > 6000:
                    stdout = stdout[:6000] + "\n...[truncated]"
                lines.append(stdout)
            if out.get("error") and out.get("error_code") not in ("", "approval_required"):
                lines.append(f"[execution note: {out['error']}]")
        for rej in getattr(plan, "rejected", []) or []:
            cmd = rej.get("command") if isinstance(rej, dict) else getattr(rej, "command", "")
            code = rej.get("error_code") if isinstance(rej, dict) else getattr(rej, "error_code", "")
            msg = rej.get("message") if isinstance(rej, dict) else getattr(rej, "message", "")
            lines.append(f"\n[Not executed: {cmd} | reason={code}]\n{msg}")
        return "\n".join(lines)

    @staticmethod
    def _format_tool_results_multi(plans, outputs: List[Dict[str, Any]]) -> str:
        """Format live execution outputs across devices as model context."""
        plans = [p for p in (plans or [])]
        if len(plans) == 1:
            return CopilotOrchestrator._format_tool_results(plans[0], outputs)
        lines = [
            "--- COPILOT AUTONOMOUS EXECUTION RESULTS (LIVE DEVICE OUTPUT) ---",
            "Targets: " + "; ".join(
                f"{p.device_label} (lab: {p.lab_id})" for p in plans)
            + ". These commands were executed on the live consoles just "
            "now. Summarize ONLY these actual outputs.",
        ]
        for out in outputs:
            where = out.get("device_label") or out.get("device_id") or ""
            lines.append(
                f"\n[Device: {where} | Command: {out.get('command')} | "
                f"success={out.get('success')} exit={out.get('exit_status')}]"
            )
            if out.get("error_code") == "approval_required":
                lines.append(
                    f"HELD FOR APPROVAL (ticket {out.get('ticket', {}).get('ticket_id')}): "
                    f"{out.get('error')}"
                )
            elif out.get("stdout"):
                stdout = out["stdout"]
                if len(stdout) > 6000:
                    stdout = stdout[:6000] + "\n...[truncated]"
                lines.append(stdout)
            if out.get("error") and out.get("error_code") not in ("", "approval_required"):
                lines.append(f"[execution note: {out['error']}]")
        for plan in plans:
            for rej in getattr(plan, "rejected", []) or []:
                cmd = rej.command if hasattr(rej, "command") else rej.get("command")
                code = rej.error_code if hasattr(rej, "error_code") else rej.get("error_code")
                msg = rej.message if hasattr(rej, "message") else rej.get("message")
                lines.append(
                    f"\n[Device: {plan.device_label} | Not executed: {cmd} | "
                    f"reason={code}]\n{msg}")
        return "\n".join(lines)

    async def handle_user_message_stream(
        self,
        conversation_id: str,
        user_text: str,
        session_token: Optional[str] = None,
        model: Optional[str] = None,
        provider_id: str = "meta",
        context_chips: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Record user message, assemble context, query provider, stream chunks, and persist reply."""
        chips = context_chips or []
        atts = attachments or []

        # 1. Persist user message with metadata
        user_meta = {
            "context_chips": chips,
            "attachment_ids": [a.get("id") for a in atts if a.get("id")],
        }
        self.store.add_message(
            conversation_id=conversation_id,
            role="user",
            content=user_text,
            metadata=user_meta,
        )

        # 2. Retrieve conversation history for context replay
        conv = self.store.get_conversation(conversation_id, session_token)
        if not conv:
            yield {"type": "error", "message": f"Conversation {conversation_id} not found."}
            return

        # 2b. Autonomous device execution (M1.2): run requested read-only
        # diagnostics through the existing console pathway and inject the
        # ACTUAL outputs into the model context below.
        tool_results_block = ""
        executed_commands: List[Dict[str, Any]] = []
        rejected_commands: List[Dict[str, Any]] = []
        tool_outputs: List[Dict[str, Any]] = []
        autonomous_plans, dispatch_skip = self._plan_autonomous_multi(
            session_token, user_text)
        for autonomous_plan in autonomous_plans:
            for plan_rej in autonomous_plan.rejected:
                rej_entry = {
                    "command": plan_rej.command,
                    "error_code": plan_rej.error_code,
                    "message": plan_rej.message,
                }
                rejected_commands.append({
                    "device_id": autonomous_plan.device_id,
                    "device_label": autonomous_plan.device_label,
                    **rej_entry,
                })
                yield {
                    "type": "tool_rejected",
                    "tool": "execute_device_command",
                    "lab_id": autonomous_plan.lab_id,
                    "device_id": autonomous_plan.device_id,
                    "device_label": autonomous_plan.device_label,
                    **rej_entry,
                }
            for plan_cmd in autonomous_plan.commands:
                yield {
                    "type": "tool_call",
                    "tool": "execute_device_command",
                    "lab_id": autonomous_plan.lab_id,
                    "device_id": autonomous_plan.device_id,
                    "device_label": autonomous_plan.device_label,
                    "command": plan_cmd,
                }
                try:
                    plan_result = await _device_exec.execute_device_command(
                        session_token or "",
                        autonomous_plan.lab_id,
                        autonomous_plan.device_id,
                        plan_cmd,
                    )
                except Exception as plan_exc:
                    log.error("Autonomous exec failure: %s", plan_exc)
                    plan_result = _device_exec.DeviceExecResult(
                        success=False, device_id=autonomous_plan.device_id,
                        device_label=autonomous_plan.device_label,
                        lab_id=autonomous_plan.lab_id, command=plan_cmd,
                        error=f"Execution failed: {str(plan_exc)[:120]}",
                        error_code="execution_error", exit_status=2)
                plan_payload = plan_result.to_dict()
                if len(plan_payload.get("stdout") or "") > 8000:
                    plan_payload["stdout"] = plan_payload["stdout"][:8000] + "\n...[truncated for display]"
                    plan_payload["truncated"] = True
                yield {"type": "tool_result", "tool": "execute_device_command",
                       "result": plan_payload}
                if plan_result.approval_required and plan_result.ticket:
                    yield {"type": "approval_required",
                           "lab_id": plan_result.lab_id,
                           "device_id": plan_result.device_id,
                           "device_label": plan_result.device_label,
                           "command": plan_result.command,
                           "ticket": {k: v for k, v in plan_result.ticket.items()
                                      if k != "session_token"}}
                tool_outputs.append(plan_payload)
                executed_commands.append({
                    "device_id": plan_result.device_id,
                    "device_label": plan_result.device_label,
                    "command": plan_result.command,
                    "success": plan_result.success,
                    "error_code": plan_result.error_code,
                })
        if tool_outputs or any(getattr(p, "rejected", None) for p in autonomous_plans):
            tool_results_block = self._format_tool_results_multi(
                autonomous_plans, tool_outputs)
        elif dispatch_skip:
            # Explicit dispatch failure: devices + commands were named but
            # nothing ran. Never let auto-retrieved context pass as results.
            tool_results_block = (
                "--- COPILOT AUTONOMOUS EXECUTION ---\n"
                "No commands were dispatched to devices although this turn "
                f"named devices and commands ({dispatch_skip}). Do not present "
                "auto-retrieved context as executed results; tell the engineer "
                "what is missing.")
            rejected_commands.append({
                "device_id": "", "device_label": "",
                "command": "", "error_code": "dispatch_failed",
                "message": dispatch_skip,
            })
            yield {
                "type": "tool_rejected",
                "tool": "execute_device_command",
                "lab_id": "", "device_id": "", "device_label": "",
                "command": "",
                "error_code": "dispatch_failed",
                "message": f"Not dispatched: {dispatch_skip}",
            }

        # 3. Assemble messages for LLM
        llm_messages: List[ChatMessage] = [
            ChatMessage(role=ChatRole.DEVELOPER, content=SYSTEM_PROMPT)
        ]

        # Add prior conversation turns (sliding window of last 8 messages)
        history_slice = conv.messages[-9:-1] if len(conv.messages) > 1 else []
        for m in history_slice:
            llm_messages.append(ChatMessage(role=m.role, content=m.content))

        # Build dynamic context block
        context_block = self._assemble_context_text(session_token, chips, atts, user_text=user_text)
        augmented_prompt = user_text
        # 2b. Lab discovery (M1.3 integration): read-only controller
        # discovery for Lab Creation / discovery intent. Never builds,
        # approves, or executes anything.
        discovery_block = ""
        discovery_summary = None
        did_discover = False
        try:
            if self._should_discover_lab(user_text):
                did_discover = True
                disc = await asyncio.to_thread(
                    _lab_discovery.discover_environment,
                    session_token or "", self._lab_id_hint(user_text),
                    user_text)
                discovery_summary = _lab_discovery.summarize_for_metadata(disc)
                yield {
                    "type": "lab_discovery",
                    "ok": disc.ok,
                    "partial": disc.partial,
                    "error_code": disc.error_code,
                    "summary": discovery_summary,
                    "message": disc.message,
                }
                discovery_block = _lab_discovery.format_discovery_for_prompt(disc)
            else:
                prior = self._prior_discovery_summary(conv)
                if prior:
                    discovery_block = _lab_discovery.format_prior_summary(prior)
        except Exception as disc_exc:
            log.warning("Lab discovery skipped: %s", disc_exc)

        # 2c. Configuration planning (M1.4): build a reviewable plan for
        # config intent, or resolve an existing plan for validation-only
        # requests (never rebuild on validate/inspect). Planning runs
        # read-only evidence only; it never prepares, approves, or executes.
        config_plan_block = ""
        config_plan_id = None
        try:
            plan_mode, plan_ref = self._classify_config_request(session_token, user_text)
            if did_discover and not (plan_mode == "validate" and plan_ref):
                # Discovery turns never plan: no build, no "latest plan"
                # fallback event, no requirements merge. An explicit plan-ID
                # validation on the same turn still resolves.
                plan_mode, plan_ref = "none", None
            if plan_mode == "validate":
                cfg_plan = None
                if plan_ref:
                    candidate = _config_plan.get_plan(plan_ref)
                    if (candidate is not None
                            and candidate.owner_token == (session_token or "")):
                        cfg_plan = candidate
                else:
                    cfg_plan = _config_plan.latest_plan_for_session(session_token or "")
                if cfg_plan is not None:
                    config_plan_id = cfg_plan.plan_id
                    yield {"type": "config_plan", "plan": cfg_plan.to_dict()}
                    config_plan_block = (
                        self._format_config_plan(cfg_plan)
                        + "\n\n(Existing plan retrieved for validation — nothing regenerated.)")
                else:
                    config_plan_block = (
                        "--- CONFIGURATION PLAN ---\n"
                        "No existing configuration plan was found for this session. "
                        "Describe the required VLANs, ports, and addresses to create one.")
            elif plan_mode == "build":
                cfg_plan, cfg_err = await _config_plan.build_plan(
                    session_token or "", user_text, conversation_id=conversation_id)
                if cfg_plan is not None and not cfg_err:
                    config_plan_id = cfg_plan.plan_id
                    yield {"type": "config_plan", "plan": cfg_plan.to_dict()}
                    config_plan_block = self._format_config_plan(cfg_plan)
        except Exception as cfg_exc:
            log.warning("Config planning skipped: %s", cfg_exc)

        # 2d. Historical execution evidence (retention): when this turn ran
        # nothing new, re-inject persisted tool_results from this same
        # conversation so assessments can cite earlier outputs.
        retained_block = ""
        if not tool_outputs and not any(getattr(p, "rejected", None) for p in autonomous_plans):
            try:
                retained = self._prior_exec_results(
                    conv, user_text=user_text,
                    lab_id=self._lab_id_hint(user_text))
                if retained:
                    retained_block = self._format_retained_exec_results(retained)
            except Exception as ret_exc:
                log.warning("Retained exec evidence skipped: %s", ret_exc)

        context_parts = [b for b in (tool_results_block, retained_block, config_plan_block, discovery_block, context_block) if b]
        if context_parts:
            joined_context = "\n\n".join(context_parts)
            augmented_prompt = f"{joined_context}\n\n--- USER QUERY ---\n{user_text}"

        llm_messages.append(ChatMessage(role=ChatRole.USER, content=augmented_prompt))

        # 4. Invoke provider streaming
        provider = get_provider(provider_id)
        chosen_model = model or provider.get_default_model()
        full_reply: List[str] = []
        token_usage = None
        fallback_from = None

        try:
            has_started = False
            async for chunk in provider.chat_stream(
                messages=llm_messages,
                model=chosen_model,
            ):
                if chunk.error:
                    if not has_started and self._is_availability_error(chunk.error):
                        avail_models = [
                            m.id for m in provider.list_models()
                            if getattr(m, "available", True) and m.id != chosen_model
                        ]
                        fallback_model = (
                            avail_models[0]
                            if avail_models
                            else ("muse-spark-1.3" if chosen_model != "muse-spark-1.3" else None)
                        )
                        if fallback_model and fallback_model != chosen_model:
                            log.info("Model %s unavailable (%s). Falling back to %s", chosen_model, chunk.error, fallback_model)
                            fallback_notice = f"> ℹ️ *Model `{chosen_model}` was unavailable. Switched to `{fallback_model}`.*\n\n"
                            full_reply.append(fallback_notice)
                            yield {
                                "type": "chunk",
                                "delta": fallback_notice,
                                "model": fallback_model,
                            }
                            fallback_from = chosen_model
                            chosen_model = fallback_model
                            async for fb_chunk in provider.chat_stream(messages=llm_messages, model=chosen_model):
                                if fb_chunk.error:
                                    yield {"type": "error", "message": fb_chunk.error}
                                    return
                                if fb_chunk.delta:
                                    full_reply.append(fb_chunk.delta)
                                    yield {
                                        "type": "chunk",
                                        "delta": fb_chunk.delta,
                                        "model": chosen_model,
                                    }
                                if fb_chunk.usage:
                                    token_usage = fb_chunk.usage
                            break

                    yield {"type": "error", "message": chunk.error}
                    return

                if chunk.delta:
                    has_started = True
                    full_reply.append(chunk.delta)
                    yield {
                        "type": "chunk",
                        "delta": chunk.delta,
                        "model": chosen_model,
                    }

                if chunk.usage:
                    token_usage = chunk.usage

            # 5. Persist assistant reply in conversation store
            reply_text = "".join(full_reply)
            if not reply_text.strip():
                # Zero content with zero errors (e.g. the provider returned
                # an empty completion or an unparsable shape): never persist
                # an empty assistant message or emit a silent done. The
                # failed turn keeps only the user message, so retry works.
                yield {
                    "type": "error",
                    "message": ("The model returned an empty response (no content was generated). "
                                "This can happen when the provider rejects the request mid-stream "
                                "(for example exhausted API credits or an unavailable model). "
                                "Check the backend logs and provider status, then retry."),
                }
                return
            asst_meta = {
                "model": chosen_model,
                "provider": provider_id,
                "fallback_from": fallback_from,
                "executed_commands": executed_commands,
                "rejected_commands": rejected_commands,
                "tool_results": tool_outputs,
                "config_plan_id": config_plan_id,
                "lab_discovery": discovery_summary,
                "token_usage": {
                    "prompt_tokens": token_usage.prompt_tokens if token_usage else 0,
                    "completion_tokens": token_usage.completion_tokens if token_usage else 0,
                    "total_tokens": token_usage.total_tokens if token_usage else 0,
                } if token_usage else None,
            }
            stored_asst = self.store.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=reply_text,
                metadata=asst_meta,
            )

            # Auto-title conversation on first turn if still default
            if conv.title in ("New Conversation", "New Chat") and len(conv.messages) <= 2:
                snippet = user_text.strip().split("\n")[0][:36]
                auto_title = snippet or "Network Analysis"
                self.store.rename_conversation(conversation_id, auto_title, session_token)
                yield {"type": "rename", "title": auto_title}

            yield {
                "type": "done",
                "message_id": stored_asst.id,
                "model": chosen_model,
                "usage": asst_meta["token_usage"],
            }
        except Exception as ex:
            log.error("Orchestrator error during chat streaming: %s", ex)
            yield {"type": "error", "message": f"Copilot execution failure: {str(ex)[:120]}"}


# Global orchestrator instance
_copilot_orchestrator = CopilotOrchestrator()

def get_copilot_orchestrator() -> CopilotOrchestrator:
    return _copilot_orchestrator

