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
from typing import Any, AsyncGenerator, Dict, List, Optional

from cliq.ai.conversations import ConversationStore, StoredMessage
from cliq.ai.network_context import get_network_context_service
from cliq.ai.provider import ChatMessage, ChatRole, StreamChunk
from cliq.ai.registry import get_provider

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
- All device interaction is strictly read-only and advisory. You cannot and must never attempt to apply configuration changes automatically.
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
                        node_type = info.get("node_type") or ("switch" if "SW" in str(d_lbl).upper() else "router")
                        platform_desc = "Cisco IOS Switch (L2/L3)" if node_type == "switch" else "Cisco IOS Router"
                        blocks.append(f"[Device Summary: {d_lbl} (ID: {d_id}) | Platform: {platform_desc} (Type: {node_type}) | Status: {info.get('status')} | Mode: {info.get('mode')}]\n[IP Interfaces Brief]:\n{intfs}")
                elif chip_type in ("device", "active_device") and device_id:
                    info = self.context_service.get_device_info(session_token or "", device_id)
                    intfs = self.context_service.get_interfaces(session_token, device_id)
                    node_type = info.get("node_type") or ("switch" if "SW" in str(device_id).upper() or "SW" in clean_label.upper() else "router")
                    platform_desc = "Cisco IOS Switch (L2/L3)" if node_type == "switch" else "Cisco IOS Router"
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

        # 2. Auto-retrieve context for mentioned devices if not explicitly chipped
        if user_text:
            mentioned = []
            for dev_name in ["R1", "R2", "SW1", "SW2"]:
                pattern = rf"\b{dev_name}\b"
                if re.search(pattern, user_text, re.IGNORECASE) and dev_name.upper() not in processed_devices:
                    mentioned.append(dev_name)

            if mentioned:
                auto_blocks = []
                for dev_name in mentioned:
                    intfs = self.context_service.get_interfaces(session_token, dev_name)
                    if not intfs.startswith("[Device "):
                        auto_blocks.append(f"[Device {dev_name} - IP Interfaces Brief]:\n{intfs}")
                    else:
                        auto_blocks.append(f"[Device {dev_name}: live telemetry not currently available or device not connected]")
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
        if context_block:
            augmented_prompt = f"{context_block}\n\n--- USER QUERY ---\n{user_text}"

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
            asst_meta = {
                "model": chosen_model,
                "provider": provider_id,
                "fallback_from": fallback_from,
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

