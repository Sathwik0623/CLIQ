# CLIQ — Next Development Phases (Roadmap & Handoff)

> [!NOTE]
> **Planning & Design Document**: The capabilities described below represent planned future workstreams following the completion and publication of CLIQ Phase V.3. They are not completed features in the current codebase.

---

## Overview

With the completion of **CLIQ Phase V.3 (Unified AI Copilot & Knowledge Retrieval Engine)**, CLIQ provides a deterministic multi-device terminal IDE integrated with Cisco Modeling Labs (CML), offline keystroke intelligence, unified AI Copilot streaming, official Cisco IOS-XE documentation grounding (RAG), and device-aware safe insertion.

The next development phases leverage this foundation to introduce autonomous lab orchestration and next-generation CLI command intelligence.

---

## Phase V.4 — Lab Copilot / Lab Auto-Creation Agent

### 1. Objective
Enable network engineers, students, and architects to translate high-level natural language requirements or customer topology specifications into fully structured, validated, and provisioned Cisco Modeling Labs (CML) simulation topologies.

### 2. Core Functional Requirements
1. **Natural-Language Lab Plan Generation**:
   - Parse unstructured user prompts (e.g., *"Build a 3-router OSPF multi-area backbone with 2 access switches and redundant links"*) or uploaded network design documents/specifications.
   - Deconstruct requirements into structured lab blueprints containing:
     - Node manifests (hostname, node definitions, compute flavors).
     - Software image definitions (e.g. `iosv`, `iol-xe`, `cat8000v`, `server`).
     - Point-to-point interface link mappings (`Gi0/0 ↔ Gi0/0`).
     - Resource estimates (vCPU, RAM, disk quotas).
2. **Controller Resource & Definition Validation**:
   - Query the CML controller API (`/api/v0/`) as the authoritative source of truth.
   - Verify that requested node definitions, image definitions, and license capabilities exist on the target CML controller before attempting deployment.
   - Cross-check total memory and CPU requirements against live controller capacity.
3. **Interactive Topology Preview**:
   - Render a dry-run visual graph preview in the ReactFlow canvas before any resources are allocated on CML.
   - Highlight resource consumption, link endpoints, and potential allocation warnings.
4. **Mandatory Explicit User Approval Gate**:
   - Lab creation requires explicit confirmation from the engineer via an interactive modal or review dialog.
   - Display a detailed blueprint summary with a confirmation barrier before issuing creation requests.
5. **Safe, Incremental Orchestration**:
   - Use standard CML REST APIs (`POST /api/v0/labs`, `POST /api/v0/labs/{id}/nodes`, `POST /api/v0/labs/{id}/links`).
   - Never autonomously configure running device configs or execute destructive commands without interactive user submission.
6. **Per-User Isolation & Credential Security**:
   - Maintain ephemeral session tokens and per-user workspace isolation.
   - Never persist CML passwords, tokens, or customer topology files across user boundaries or unencrypted disks.

---

## Phase V.5 — CLI Command Intelligence Improvements

### 1. Objective
Enhance CLIQ's real-time terminal intelligence by deepening the deterministic command engine, integrating live device discovery, and connecting the offline RAG vector index directly into keystroke suggestions and execution error explanations.

### 2. Core Functional Requirements
1. **Context-Aware Inline Ghost-Text Suggestions**:
   - Expand the inline ghost-text engine beyond static command syntax to incorporate active device operational state (discovered interface names, active VLAN IDs, configured VRFs, and BGP AS numbers).
   - Dynamic prompt awareness: adapt suggestions depending on whether the device is in Privileged Exec (`#`), Global Configuration (`(config)#`), Interface Configuration (`(config-if)#`), or Routing Process (`(config-router)#`).
2. **Command Autocomplete & Typo Correction**:
   - Token-level Levenshtein distance calculation to catch and explain common typing mistakes (e.g., `sh ip int bri` → `show ip interface brief`, `swithport` → `switchport`).
   - Abbreviation preservation: honor established network engineer shorthands (`conf t`, `wr mem`, `no shut`) while offering canonical expansions.
3. **Platform-, Hardware-, and Version-Aware Compatibility**:
   - Validate commands against target hardware PID, software family (IOS, IOS-XE, Junos, EOS), and version metadata.
   - Flag platform mismatches immediately (e.g., preventing switchport commands on pure Layer 3 routers; rejecting unsupported encryption ciphers on older image definitions).
4. **Immediate Execution Error Explanations**:
   - Intercept standard CLI syntax rejection markers (e.g., `% Invalid input detected at '^' marker`, `% Incomplete command`, `% Ambiguous command`).
   - Automatically analyze the rejected command token against the compiled syntax grammar and provide an instant, inline explanation and fix recommendation.
5. **Documentation-Backed Suggestions & Clear Status Distinctions**:
   - Ground suggestions in official vendor documentation retrieved via the existing vector knowledge base.
   - Clearly distinguish between:
     - **Documented Syntax**: Official commands verified against the local knowledge base.
     - **Suggested/Inferred Syntax**: Template-based or heuristic recommendations requiring parameter substitution.
     - **Live-Verified Commands**: Syntax verified against live telemetry or running-config context.
6. **Single Knowledge Architecture (Zero Duplication)**:
   - Reuse the existing SQLite vector index (`kb/cliq_vec.sqlite`), command registry (`kb/cliq_kb.sqlite`), and `CommandRetriever` architecture.
   - Avoid creating divergent or duplicate command storage engines.

---

## Technical Constraints & Safety Invariants

Across all subsequent phases, the following architectural constraints remain non-negotiable:
- **Device Authority**: The physical or simulated device CLI remains the ultimate authority. Keystroke interceptors never silently alter user inputs.
- **Microsecond Hot Path**: Real-time keystroke evaluation and ghost-text generation must execute strictly in sub-millisecond offline logic, independent of cloud LLM network calls.
- **Strict Advisory Boundary**: AI agents generate plans, recommendations, and structured templates; destructive actions, node deletions, and device reboots always require human confirmation.
- **Air-Gapped Viability**: The core IDE, terminal emulator, deterministic command catalog, and local vector retrieval must remain fully functional in offline, air-gapped network environments without external cloud connectivity.
