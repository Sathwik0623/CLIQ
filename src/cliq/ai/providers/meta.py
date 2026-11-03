"""
Meta Provider Adapter for CLIQ AI Copilot (Phase V).

Connects to the official Meta Model API (e.g. hackathon credits endpoint at https://api.meta.ai/v1
or configurable via META_AI_BASE_URL). Authenticates using META_AI_API_KEY / META_API_KEY / MODEL_API_KEY.
Implements SSE streaming, token usage tracking, and rate limit backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import requests

from cliq.ai.provider import (
    AIProvider,
    ChatMessage,
    ChatResponse,
    ChatRole,
    ChatUsage,
    ModelInfo,
    StreamChunk,
)

log = logging.getLogger("cliq.ai.meta")

# Default Meta API Base URL (OpenAI-compatible /v1/chat/completions)
def parse_sse_data_line(data_str: str, target_model: str) -> Optional[StreamChunk]:
    """Parse one SSE ``data:`` payload into a chunk, or ``None`` to skip it.

    Understood shapes (OpenAI-compatible variance):
    - ``[DONE]`` → finished marker.
    - ``{"error": ...}`` → error chunk (mid-stream API failures such as
      exhausted credits or model errors MUST surface, never vanish).
    - ``{"choices": [{"delta": {"content": ...}}]}`` → content chunk.
    - ``{"choices": [{"message": {"content": ...}}]}`` → content chunk
      (non-streaming JSON shape echoed inside SSE by some shims).
    Anything else (empty choices, usage-only frames, malformed JSON) → ``None``.
    """
    text = (data_str or "").strip()
    if text == "[DONE]":
        return StreamChunk(is_finished=True, finish_reason="stop")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    err = parsed.get("error")
    if err:
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or text[:200]
        else:
            detail = str(err)[:200]
        return StreamChunk(error=f"Meta API error: {detail}")
    choices = parsed.get("choices", [])
    if not choices or not isinstance(choices, list):
        return None
    first = choices[0] if isinstance(choices[0], dict) else {}
    delta = first.get("delta", {}) or {}
    content = delta.get("content", "") if isinstance(delta, dict) else ""
    if not content:
        message = first.get("message", {}) or {}
        if isinstance(message, dict):
            content = message.get("content", "") or ""
    finish_reason = first.get("finish_reason")
    usage_data = parsed.get("usage")
    usage = None
    if isinstance(usage_data, dict) and usage_data:
        usage = ChatUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )
    if not content and not finish_reason and usage is None:
        return None
    return StreamChunk(
        delta=content or "",
        is_finished=bool(finish_reason),
        finish_reason=finish_reason,
        usage=usage,
        model=parsed.get("model") or target_model,
    )


def interpret_non_stream_body(body_text: str, status_code: int, target_model: str) -> StreamChunk:
    """Interpret a non-SSE 200 body: a full JSON answer becomes one chunk,
    anything else becomes a visible error (never silent)."""
    preview = (body_text or "")[:200]
    try:
        parsed = json.loads(body_text or "")
    except (json.JSONDecodeError, ValueError):
        return StreamChunk(
            error=f"Meta API returned a non-streaming response (HTTP {status_code}): {preview}")
    if isinstance(parsed, dict) and parsed.get("error"):
        err = parsed["error"]
        detail = (err.get("message") or err.get("code") or preview) if isinstance(err, dict) else str(err)[:200]
        return StreamChunk(error=f"Meta API error: {detail}")
    chunk = parse_sse_data_line(body_text or "", target_model)
    if chunk is not None and (chunk.delta or chunk.error):
        return chunk
    return StreamChunk(
        error=f"Meta API returned an unexpected non-streaming response (HTTP {status_code}): {preview}")


DEFAULT_META_BASE_URL = "https://api.meta.ai/v1"

# Model alias map to normalize any Llama or legacy model IDs to the authoritative Meta Model API endpoints
MODEL_ALIASES = {
    "llama-3.3-70b-instruct": "muse-spark-1.3",
    "meta-llama-3.1-70b-instruct": "muse-spark-1.2",
    "llama-3.3-70b": "muse-spark-1.3",
    "llama-3.1-70b": "muse-spark-1.2",
    "llama3.3": "muse-spark-1.3",
    "llama3.1": "muse-spark-1.2",
}

# Authoritative models supported by the Meta Model API
META_MODELS = [
    ModelInfo(
        id="muse-spark-1.3",
        name="Muse Spark 1.3",
        provider="meta",
        description="Meta's flagship reasoning model for deep network diagnostic thinking and configuration analysis.",
        context_window=128000,
        supports_streaming=True,
        supports_tools=False,
        is_default=True,
        available=True,
    ),
    ModelInfo(
        id="muse-wonder-1.0",
        name="Muse Wonder 1.0",
        provider="meta",
        description="Meta high-throughput conversational model for rapid network operational queries.",
        context_window=128000,
        supports_streaming=True,
        supports_tools=False,
        is_default=False,
        available=True,
    ),
    ModelInfo(
        id="muse-spark-1.2",
        name="Muse Spark 1.2",
        provider="meta",
        description="Meta reasoning model for protocol analysis and configuration verification.",
        context_window=128000,
        supports_streaming=True,
        supports_tools=False,
        is_default=False,
        available=True,
    ),
]


class MetaProvider(AIProvider):
    """Adapter for the official Meta Model API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "muse-spark-1.3",
        mock_mode: Optional[bool] = None,
    ):
        self._api_key = api_key or self._resolve_api_key()
        self._base_url = (base_url or os.environ.get("META_AI_BASE_URL") or os.environ.get("META_API_BASE_URL") or DEFAULT_META_BASE_URL).rstrip("/")
        self._default_model = default_model
        self._discovered_model_ids: Optional[set] = None
        self._last_discovery_time: float = 0.0

        # Mock mode is active if explicitly set or if CLIQ_AI_MOCK=1 or no key in test
        if mock_mode is not None:
            self._mock_mode = mock_mode
        else:
            self._mock_mode = os.environ.get("CLIQ_AI_MOCK", "").lower() in ("1", "true", "yes")

    @staticmethod
    def _resolve_api_key() -> str:
        """Resolve API key from standard environment variables without logging it."""
        for var in ("META_AI_API_KEY", "META_API_KEY", "MODEL_API_KEY"):
            val = os.environ.get(var)
            if val and val.strip():
                return val.strip()
        return ""

    @property
    def provider_id(self) -> str:
        return "meta"

    @property
    def display_name(self) -> str:
        return "Meta Model API"

    def is_configured(self) -> bool:
        if self._mock_mode:
            return True
        if not self._api_key:
            return False
        if self._discovered_model_ids is not None and len(self._discovered_model_ids) == 0:
            return False
        return True

    def list_models(self) -> List[ModelInfo]:
        """Return genuine, verified models supported by the Meta Model API with live availability status."""
        if not self._api_key and not self._mock_mode:
            return [
                ModelInfo(
                    id=m.id, name=m.name, provider=m.provider,
                    description=m.description, context_window=m.context_window,
                    supports_streaming=m.supports_streaming, supports_tools=m.supports_tools,
                    is_default=m.is_default, available=False,
                )
                for m in META_MODELS
            ]

        # Live model discovery check against /models endpoint (cached for 60s)
        now = time.time()
        if self._api_key and (self._discovered_model_ids is None or (now - self._last_discovery_time > 60)):
            try:
                headers = {"Authorization": f"Bearer {self._api_key}", "User-Agent": "CLIQ-Copilot/1.0"}
                resp = requests.get(f"{self._base_url}/models", headers=headers, timeout=2.5)
                if resp.status_code == 200:
                    data = resp.json()
                    model_items = data.get("data", [])
                    self._discovered_model_ids = {m.get("id") for m in model_items if isinstance(m, dict)}
                    self._last_discovery_time = now
                elif resp.status_code in (401, 403):
                    self._discovered_model_ids = set()
                    self._last_discovery_time = now
            except Exception as e:
                log.debug("Live model discovery check timed out or failed: %s", e)
                if self._discovered_model_ids is None:
                    self._discovered_model_ids = {m.id for m in META_MODELS}

        result: List[ModelInfo] = []
        for m in META_MODELS:
            is_avail = True
            if self._discovered_model_ids is not None:
                is_avail = (m.id in self._discovered_model_ids)
            result.append(
                ModelInfo(
                    id=m.id, name=m.name, provider=m.provider,
                    description=m.description, context_window=m.context_window,
                    supports_streaming=m.supports_streaming, supports_tools=m.supports_tools,
                    is_default=m.is_default, available=is_avail,
                )
            )
        return result

    def get_models(self) -> List[ModelInfo]:
        return self.list_models()

    @property
    def default_model(self) -> str:
        return self._default_model

    def get_default_model(self) -> str:
        return self._default_model

    def _format_messages_for_meta(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        """Format messages for the Meta Model API chat completions endpoint."""
        formatted: List[Dict[str, Any]] = []
        for m in messages:
            role_str = m.role.value if isinstance(m.role, ChatRole) else str(m.role)
            # Meta supports developer or system, user, assistant, tool
            if role_str == "system":
                role_str = "developer"
            entry: Dict[str, Any] = {
                "role": role_str,
                "content": m.content,
            }
            if m.name:
                entry["name"] = m.name
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            formatted.append(entry)
        return formatted

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        target_model = model or self._default_model
        resolved_model = MODEL_ALIASES.get(target_model.lower(), target_model)

        # Fail closed with actionable error if API key is missing and mock mode is not opted into
        if not self._api_key and not self._mock_mode:
            yield StreamChunk(
                error="Meta API key is not configured. Please set META_AI_API_KEY in your .env file or environment, or set CLIQ_AI_MOCK=1 to enable offline mock responses."
            )
            return

        if self._mock_mode:
            async for chunk in self._mock_stream(messages, resolved_model):
                yield chunk
            return

        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": resolved_model,
            "messages": self._format_messages_for_meta(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "CLIQ-Copilot/1.0",
        }

        # Run streaming HTTP request in background thread with an async queue
        queue: asyncio.Queue[Optional[StreamChunk]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _worker():
            try:
                with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout_seconds) as resp:
                    if resp.status_code == 401:
                        queue.put_nowait(StreamChunk(error="Authentication failed: invalid Meta API key. Please verify your META_AI_API_KEY environment setting."))
                        return
                    if resp.status_code == 429:
                        queue.put_nowait(StreamChunk(error="Meta API rate limit reached. Please wait a moment before sending another request."))
                        return
                    if not resp.ok:
                        err_text = resp.text[:200]
                        queue.put_nowait(StreamChunk(error=f"Meta API error (HTTP {resp.status_code}): {err_text}"))
                        return

                    try:
                        content_type = resp.headers.get("content-type", "") or ""
                    except Exception:
                        content_type = ""
                    if "text/event-stream" not in content_type.lower():
                        # Not an SSE stream (compat shim / proxy page): parse
                        # the whole body once instead of going silent.
                        try:
                            body_text = (resp.text or "")[:4096]
                        except Exception:
                            body_text = ""
                        queue.put_nowait(interpret_non_stream_body(
                            body_text, resp.status_code, target_model))
                        return

                    saw_data_line = False
                    for raw_line in resp.iter_lines():
                        if not raw_line:
                            continue
                        try:
                            line = raw_line.decode("utf-8").strip()
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if not line.startswith("data:"):
                            continue
                        saw_data_line = True
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            queue.put_nowait(StreamChunk(is_finished=True, finish_reason="stop"))
                            break
                        chunk = parse_sse_data_line(data_str, target_model)
                        if chunk is None:
                            continue
                        queue.put_nowait(chunk)
                        if chunk.error:
                            # In-stream API errors are terminal for this
                            # response; surface immediately instead of
                            # waiting on a dead stream until timeout.
                            break
                    if not saw_data_line:
                        queue.put_nowait(StreamChunk(
                            error="Meta API returned an empty event stream "
                                  f"(HTTP {resp.status_code}): no data received."))
            except requests.exceptions.Timeout:
                queue.put_nowait(StreamChunk(error="Request timed out while waiting for Meta API response."))
            except Exception as ex:
                log.error("Meta API connection error: %s", str(ex))
                queue.put_nowait(StreamChunk(error=f"Network error contacting Meta API: {str(ex)[:100]}"))
            finally:
                queue.put_nowait(None)

        # Start worker thread
        loop.run_in_executor(None, _worker)

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> ChatResponse:
        collected: List[str] = []
        finish_reason = "stop"
        usage: Optional[ChatUsage] = None
        target_model = model or self._default_model

        async for chunk in self.chat_stream(
            messages=messages,
            model=target_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        ):
            if chunk.error:
                raise RuntimeError(chunk.error)
            if chunk.delta:
                collected.append(chunk.delta)
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            if chunk.usage:
                usage = chunk.usage

        return ChatResponse(
            content="".join(collected),
            model=target_model,
            provider=self.provider_id,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _mock_stream(self, messages: List[ChatMessage], model: str) -> AsyncGenerator[StreamChunk, None]:
        """Realistic dynamic stream for offline testing to preserve user credits.

        Answers conceptual networking questions directly and accurately without fabricating telemetry.
        When inspecting devices, analyzes the provided telemetry context; if context is absent,
        honestly reports that live device information could not be retrieved.
        """
        last_msg = messages[-1].content if messages else ""
        all_context = "\n".join([m.content for m in messages if m.content])

        # Separate context block from user query if present
        if "--- USER QUERY ---" in last_msg:
            context_block, user_query = last_msg.split("--- USER QUERY ---", 1)
            user_query = user_query.strip()
        else:
            context_block = all_context
            user_query = last_msg.strip()

        uq_lower = user_query.lower()

        # Check for RAG citations in context block
        rag_section = ""
        if "--- AUTHORITATIVE NETWORK KNOWLEDGE" in context_block:
            import re
            sources = re.findall(r"Source: ([^\n\r|]+)", context_block)
            commands = re.findall(r"Command: `([^`]+)`", context_block)
            if sources or commands:
                rag_section = "\n\n### \U0001f4da Authoritative References\n"
                for i, src in enumerate(sources):
                    src_clean = src.strip()
                    cmd = commands[i] if i < len(commands) else "Network Command Reference"
                    rag_section += f"- \U0001f4da [{cmd}]({src_clean})\n"

        # 0. Device-Aware Targeting: Switchport on Router Mismatch
        if ("switchport" in uq_lower) and any(r in uq_lower or f"[{r.upper()}" in context_block.upper() for r in ["r1", "r2", "router"]):
            target_dev = "R1" if "r1" in uq_lower or "[R1" in context_block.upper() else "the router"
            full_text = (
                f"### Device-Aware Command Targeting: Incompatible Device ({target_dev})\n\n"
                f"**Device Mismatch:** {target_dev} is a Cisco IOS-XE Router. Switchport configuration commands "
                f"(`switchport mode access`, `switchport trunk allowed vlan`, `switchport access vlan`) are Layer 2 switching commands "
                f"and are **not supported** on router interfaces.\n\n"
                f"**Technical Explanation:**\n"
                f"- Routers route IP packets directly at Layer 3 using routed interfaces and IP addresses.\n"
                f"- To configure networking on {target_dev}, assign an IP address directly to the interface using explicit parameters:\n\n"
                f"```cisco\n"
                f"interface <interface-id>\n"
                f" ip address <ip-address> <subnet-mask>\n"
                f" no shutdown\n"
                f"```\n\n"
                f"> [!NOTE]\n"
                f"> **Missing Parameters**: Please specify your desired interface name (e.g. `GigabitEthernet0/1`), IP address, and subnet mask to generate ready-to-apply configuration.\n\n"
                f"> [!WARNING]\n"
                f"> **Incompatible Target**: Switchport commands cannot be inserted into router {target_dev}. "
                f"If 802.1Q trunking is required on a router, configure subinterfaces (`interface <interface-id>.<subint-id>`) with `encapsulation dot1Q <vlan-id>`.\n"
            ) + rag_section

        # 0a. Switch Access-Port Configuration (SW1 / Switches)
        elif ("access port" in uq_lower or "switchport mode access" in uq_lower or "access-port" in uq_lower) and any(sw in uq_lower or f"[{sw.upper()}" in context_block.upper() for sw in ["sw1", "sw2", "switch"]):
            target_dev = "SW1" if "sw1" in uq_lower or "[SW1" in context_block.upper() else "SW1"
            full_text = (
                f"### Switchport Access-Port Configuration for {target_dev}\n\n"
                f"To configure an access port on switch {target_dev}, set the interface mode to access and assign the desired VLAN:\n\n"
                f"```cisco\n"
                f"interface <interface-id>\n"
                f" switchport mode access\n"
                f" switchport access vlan <vlan-id>\n"
                f"```\n\n"
                f"> [!NOTE]\n"
                f"> **Required Parameters**: Please specify the target interface name (e.g. `GigabitEthernet0/1`) and VLAN ID (e.g. `10`) for {target_dev}.\n\n"
                f"### \U0001f4da Authoritative References\n"
                f"- \U0001f4da [switchport access vlan](https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/software/release/17-16/command_reference/b_1716_9500_cr/layer_2_3_commands.html)\n"
                f"- \U0001f4da [switchport mode](https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/software/release/17-16/command_reference/b_1716_9500_cr/layer_2_3_commands.html)\n"
            )

        # 0b. Explanation of 'show ip interface brief' with Sample Output Table
        elif "explain" in uq_lower and "show ip interface brief" in uq_lower:
            full_text = (
                "### Command Syntax & Explanation: `show ip interface brief`\n\n"
                "The `show ip interface brief` command provides a summary of the status and operational configuration of all IP interfaces on a Cisco device.\n\n"
                "**Command Syntax:**\n"
                "```cisco\n"
                "show ip interface brief\n"
                "```\n\n"
                "#### Sample Terminal Output\n\n"
                "| Interface | IP-Address | OK? | Method | Status | Protocol |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| GigabitEthernet0/0 | 192.168.1.1 | YES | manual | up | up |\n"
                "| GigabitEthernet0/1 | unassigned | YES | unset | administratively down | down |\n"
                "| Loopback0 | 10.0.0.1 | YES | manual | up | up |\n\n"
                "#### Field Definitions:\n"
                "- **Interface**: Name of the physical or virtual interface (e.g., `GigabitEthernet0/0`, `Loopback0`).\n"
                "- **IP-Address**: Configured IPv4 address, or `unassigned` if no IP has been set.\n"
                "- **OK?**: `YES` indicates the IP address is valid and accepted by Cisco IOS.\n"
                "- **Method**: How the address was assigned (`manual`, `DHCP`, `NVRAM`, `unset`).\n"
                "- **Status**: Layer 1 physical link state (`up`, `down`, or `administratively down`).\n"
                "- **Protocol**: Layer 2 data link state (`up` or `down`, requiring keepalives and valid framing).\n\n"
                "### \U0001f4da Authoritative References\n"
                "- \U0001f4da [show ip interface brief](https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/software/release/17-16/command_reference/b_1716_9500_cr/interface_and_hardware_commands.html)\n"
            )

        # 0c. Vendor / Platform Incompatibility: Juniper Junos
        elif "juniper" in uq_lower or "junos" in uq_lower or "juniper" in context_block.lower():
            full_text = (
                "### Vendor / Platform Compatibility: Juniper Networks (Junos OS)\n\n"
                "**Platform Notice:** The target device is a Juniper device running Junos OS. "
                "Cisco IOS-XE commands (such as `show ip interface brief` or `switchport`) are incompatible with Junos.\n\n"
                "**Native Junos Diagnostic Commands:**\n"
                "```junos\n"
                "show interfaces terse\n"
                "show route\n"
                "```\n\n"
                "> [!NOTE]\n"
                "> Cisco command insertion is blocked for Juniper devices. Use native Junos CLI commands."
            ) + rag_section

        # 0d. OSPF and Platform Validation (Preventing Invented Values)
        elif "ospf" in uq_lower and ("syntax" in uq_lower or "how to" in uq_lower or "configure" in uq_lower or "command" in uq_lower):
            full_text = (
                "### OSPF Routing Configuration & Platform Validation\n\n"
                "**Platform Specification:** Cisco IOS-XE / IOS syntax (validated against official command reference).\n\n"
                "To configure OSPF routing on Cisco IOS-XE using explicit parameters:\n\n"
                "```cisco\n"
                "router ospf <process-id>\n"
                " router-id <router-id>\n"
                " network <network-prefix> <wildcard-mask> area <area-id>\n"
                "```\n\n"
                "> [!NOTE]\n"
                "> **Required Parameters**: Please specify your desired OSPF process ID (e.g. `1`), router ID (e.g. `1.1.1.1`), network prefix, and area ID.\n\n"
                "*Note: On Juniper Junos, OSPF is configured under `set protocols ospf area <area-id> interface <name>`.*"
            ) + rag_section

        # ── 1. Conceptual Networking Questions (e.g. Routing Table vs ARP Table) ──
        elif (
            ("routing table" in uq_lower and "arp table" in uq_lower)
            or ("arp" in uq_lower and "routing" in uq_lower and ("difference" in uq_lower or "compare" in uq_lower or "explain" in uq_lower))
        ):
            full_text = (
                "### Understanding the Difference: Routing Table vs. ARP Table\n\n"
                "In TCP/IP internetworking, the **Routing Table** and the **ARP Table** (Address Resolution Protocol cache) "
                "operate at different layers of the OSI model to ensure end-to-end packet delivery.\n\n"
                "#### 1. Layer & Operational Scope\n"
                "- **Routing Table (Layer 3 — Network Layer)**:\n"
                "  - Contains paths to destination IP networks and subnets.\n"
                "  - Determines *where* to forward a packet based on the destination IP address.\n"
                "  - Key attributes: Destination Prefix/CIDR, Metric/Administrative Distance, Next-Hop IP address, and Outgoing Interface.\n"
                "  - Populated via: Static routes, directly connected interfaces, and dynamic routing protocols (OSPF, EIGRP, BGP).\n\n"
                "- **ARP Table (Layer 2 / Layer 3 Boundary — Data Link Layer)**:\n"
                "  - Maps a Layer 3 IPv4 address to its corresponding physical Layer 2 (48-bit MAC) address on the local broadcast domain.\n"
                "  - Enables the network interface to construct the Layer 2 Ethernet frame header.\n"
                "  - Key attributes: IPv4 Address, MAC Address, Interface, Age/Timer, and Encapsulation Type (ARPA).\n"
                "  - Populated via: Broadcast ARP requests and unicast ARP replies on the local link.\n\n"
                "#### 2. Forwarding Workflow (Packet Encapsulation)\n"
                "When a router receives an IP packet:\n"
                "1. **Route Lookup**: The router consults its **Routing Table** for the longest prefix match. It identifies the outgoing interface and next-hop IP.\n"
                "2. **L2 Resolution**: The router then consults its **ARP Table** for that next-hop IP to obtain its destination MAC address.\n"
                "3. **Frame Transmission**: If found, it encapsulates the IP packet into an Ethernet frame with the destination MAC and transmits it out the interface. If not found, it triggers an ARP request.\n\n"
                "#### 3. Diagnostic Commands\n"
                "```cisco\n"
                "show ip route           ! View the Layer 3 Routing Table\n"
                "show ip arp             ! View the Layer 2/3 ARP resolution cache\n"
                "```\n"
            )
        # ── 2. Device-Specific Interface Analysis (e.g. Analyze interface status of R1) ──
        elif any(phrase in uq_lower for phrase in ["interface status", "down interfaces", "analyze interface", "show interfaces"]):
            # Check if live interface context was provided
            has_interfaces = "ip interfaces brief" in context_block.lower() or "interface" in context_block.lower() and ("up" in context_block.lower() or "down" in context_block.lower())
            
            # Identify target device label mentioned (e.g. R1, R2, SW1, SW2)
            target_dev = "the device"
            for dev_candidate in ["R1", "R2", "SW1", "SW2"]:
                if dev_candidate.lower() in uq_lower:
                    target_dev = dev_candidate
                    break

            if has_interfaces and f"Device {target_dev}" in context_block or (has_interfaces and "--- EXPLICIT NETWORK CONTEXT" in context_block):
                # Extract snippet for analysis
                full_text = (
                    f"### Interface Status Analysis for {target_dev}\n\n"
                    f"Based on the live interface telemetry provided:\n\n"
                )
                # Parse lines from context
                down_intfs = []
                up_intfs = []
                for line in context_block.splitlines():
                    l_str = line.strip()
                    if any(l_str.startswith(p) for p in ["GigabitEthernet", "FastEthernet", "Ethernet", "Loopback", "Serial", "Vlan"]):
                        parts = l_str.split()
                        intf_name = parts[0]
                        ip_addr = parts[1] if len(parts) > 1 else "unassigned"
                        status = parts[4] if len(parts) > 4 else "unknown"
                        proto = parts[5] if len(parts) > 5 else "unknown"
                        if "down" in status.lower() or "down" in proto.lower():
                            down_intfs.append(f"- **{intf_name}** ({ip_addr}): Status `{status}`, Protocol `{proto}`")
                        else:
                            up_intfs.append(f"- **{intf_name}** ({ip_addr}): Status `{status}`, Protocol `{proto}`")

                if down_intfs:
                    full_text += "**Down / Inactive Interfaces Identified:**\n" + "\n".join(down_intfs) + "\n\n"
                else:
                    full_text += "**No interfaces currently in 'down' state.** All active interfaces report `up/up`.\n\n"

                if up_intfs:
                    full_text += "**Operational Interfaces:**\n" + "\n".join(up_intfs) + "\n\n"

                full_text += (
                    "**Recommended Read-Only Verification:**\n"
                    "```cisco\n"
                    "show ip interface brief\n"
                    "show interfaces status\n"
                    "```\n"
                )
            else:
                full_text = (
                    f"I couldn't retrieve the live device information for {target_dev}. "
                    "Please check the connection or select the device context using **Add Context** (@"
                    f"{target_dev} IP Interfaces Brief).\n\n"
                    "You can inspect interface states directly on the terminal console by executing:\n"
                    "```cisco\n"
                    "show ip interface brief\n"
                    "show interfaces status\n"
                    "```\n"
                )
        # ── 3. Multi-Device Comparison (e.g. Compare R1 and R2) ──
        elif "compare" in uq_lower and any(d in uq_lower for d in ["r1", "r2", "sw1", "sw2", "devices", "interfaces"]):
            has_multi_context = context_block.count("[Device") >= 2 or ("R1" in context_block and "R2" in context_block)
            if has_multi_context:
                full_text = (
                    "### Multi-Device Configuration & Interface Comparison\n\n"
                    "Comparing active device configurations based on retrieved context:\n\n"
                    "1. **Addressing & Subnets**: Evaluated IP assignments and subnet mask alignments across inter-device links.\n"
                    "2. **Interface States**: Operational status verified across transit links.\n\n"
                    "**Recommended Checks:**\n"
                    "```cisco\n"
                    "show ip interface brief\n"
                    "show ip route\n"
                    "show cdp neighbors\n"
                    "```\n"
                )
            else:
                full_text = (
                    "Live device context was not provided for both devices. "
                    "To compare device configurations, use **Add Context** to select interfaces or configuration for both target devices.\n\n"
                    "You can also run comparison commands directly on each terminal:\n"
                    "```cisco\n"
                    "show ip interface brief\n"
                    "show running-config | section interface\n"
                    "```\n"
                )
        # ── 4. General Networking Query ──
        else:
            full_text = (
                f"### Network Engineering Response\n\n"
                f"Regarding your query: *{user_query}*\n\n"
                "**Guidance & Verification:**\n"
                "- Ensure all physical and logical link states report `Status: up` and `Protocol: up`.\n"
                "- Verify routing table convergence with `show ip route`.\n\n"
                "```cisco\n"
                "show ip interface brief\n"
                "show ip route\n"
                "```\n"
            )

        words = full_text.split(" ")
        for i, word in enumerate(words):
            yield StreamChunk(
                delta=word + (" " if i < len(words) - 1 else ""),
                is_finished=False,
                model=model,
            )
            await asyncio.sleep(0.015)

        yield StreamChunk(
            delta="",
            is_finished=True,
            finish_reason="stop",
            usage=ChatUsage(prompt_tokens=45, completion_tokens=len(words), total_tokens=45 + len(words)),
            model=model,
        )

