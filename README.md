# CLIQ — Networking CLI Intelligence IDE & Unified AI Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![React 18](https://img.shields.io/badge/react-18.x-61dafb.svg)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests: 1175 passed](https://img.shields.io/badge/tests-1175%20passed-success.svg)](tests/)

CLIQ is an offline-first, deterministic command-line intelligence engine, multi-device terminal platform, and retrieval-grounded AI Copilot engineered specifically for network engineers and operators. It unites a modern, VS Code-inspired engineering IDE with sub-millisecond offline keystroke completions, interactive Cisco Modeling Labs (CML) simulation management, and an authoritative AI Copilot powered by the Meta Model API and official Cisco technical documentation.

The M1 release extends Copilot from advisory suggestions into **approval-controlled autonomous operation**: read-only multi-device command execution with full output evidence, natural-language CML lab creation and deployment, multi-device configuration planning with explicit per-plan approval, post-change baselines, and historical evidence retention across conversation turns.

CLIQ operates on the principle of **strict advisory safety**: the network device remains the authoritative source of truth. Keystrokes in the interactive hot path are evaluated deterministically offline without cloud latency, while complex diagnostic questions, protocol explanations, and configuration blueprints are synthesized with official documentation citations and validated against the target device platform before insertion. Autonomous execution never bypasses approval: read-only diagnostics run automatically within the authorized lab scope, and every configuration-changing operation requires explicit, single-use, expiring approval bound to the exact plan, lab, and device.

---

## Table of Contents

1. [Project Overview & Objectives](#1-project-overview--objectives)
2. [Current Features](#2-current-features)
   - [Interactive Multi-Device Terminal](#interactive-multi-device-terminal)
   - [Cisco Modeling Labs (CML) Integration](#cisco-modeling-labs-cml-integration)
   - [Lab Discovery & Topology Mapping](#lab-discovery--topology-mapping)
   - [Autonomous Device Command Execution (M1.2)](#autonomous-device-command-execution-m12)
   - [Lab Creation & Deployment (M1.3)](#lab-creation--deployment-m13)
   - [Configuration Planning, Approval & Baselines (M1.4)](#configuration-planning-approval--baselines-m14)
   - [Historical Execution Evidence Retention](#historical-execution-evidence-retention)
   - [WebSocket Terminal Lifecycle & Pager Handling](#websocket-terminal-lifecycle--pager-handling)
   - [Terminal Focus & Split-Pane Layout Stability](#terminal-focus--split-pane-layout-stability)
   - [Unified AI Copilot](#unified-ai-copilot)
   - [Meta Model API & Gemini Provider Support](#meta-model-api--gemini-provider-support)
   - [Retrieval-Augmented Generation (RAG) Architecture](#retrieval-augmented-generation-rag-architecture)
   - [Device-Aware Targeting & Platform Validation](#device-aware-targeting--platform-validation)
   - [Citation Relevance & Verification](#citation-relevance--verification)
   - [Safe Command Insertion & Placeholder Guards](#safe-command-insertion--placeholder-guards)
3. [Architecture Overview](#3-architecture-overview)
4. [Installation & Windows Setup](#4-installation--windows-setup)
5. [Dependencies](#5-dependencies)
6. [Environment Configuration (`.env.example`)](#6-environment-configuration-envexample)
7. [AI Provider Configuration (Meta & Gemini)](#7-ai-provider-configuration-meta--gemini)
8. [Knowledge Base Setup & Provisioning](#8-knowledge-base-setup--provisioning)
9. [Testing & Verification Results](#9-testing--verification-results)
10. [Known Limitations & Roadmap](#10-known-limitations--roadmap)
11. [License](#11-license)

---

## 1. Project Overview & Objectives

Traditional network operating systems (Cisco IOS-XE, Arista EOS, Juniper Junos) feature deep, multi-tiered command hierarchies where minor syntax oversights cause immediate outages or misconfigurations. Traditional web-based terminal emulators exacerbate these risks with disconnected sessions, truncated outputs, and unverified AI suggestions.

CLIQ resolves this through a three-layer architecture:
1. **Interactive Multiplexed Terminal**: High-performance, concurrent console tabs connecting to real hardware or CML simulations with resilient WebSockets, PTY synchronization, and clean pagination handling.
2. **Deterministic Offline Keystroke Engine**: Sub-millisecond abbreviation expansion, mode-aware validation (`exec`, `enable`, `config`, `config-if`), and inline ghost-text completions executing entirely on-premise without external network dependencies.
3. **Retrieval-Augmented AI Copilot**: A side-by-side diagnostic intelligence assistant grounded in local vector databases of official Cisco documentation. Suggestions are validated for platform compatibility, protect against invented parameters, and provide safe, operator-controlled execution — extended in M1 with autonomous read-only execution, lab lifecycle management, and approval-gated configuration.

---

## 2. Current Features

### Interactive Multi-Device Terminal
- **Concurrent Tabbed Workspaces**: Manage multiple live router and switch consoles simultaneously without cross-talk or reattachment stalls.
- **Visual Explorer Navigation**: Discovered lab devices appear in a hierarchical left sidebar with real-time operational status dots, active mode badges, and instant search filtering.
- **Status Bar Telemetry**: Live metrics reporting latency, CLI mode, active host, and connected terminal count.

### Cisco Modeling Labs (CML) Integration
- **Authenticated Controller Sessions**: Connects to CML REST APIs (`/api/v0/`) using temporary session tokens; one user's token can never retrieve another user's controller data.
- **Automated Simulation Discovery**: Discovers deployed topologies, node states, images, and resource availability.
- **Dynamic Topology Mapping**: Renders interactive ReactFlow topology maps with verified interface endpoint identifiers (`Gi0/0 ↔ Gi0/1`).
- **Console Multiplexing**: Connects directly to device console endpoints via CML's raw console proxy or telnet breakout ports.

### Lab Discovery & Topology Mapping
- **Copilot-Driven Discovery**: Natural-language requests (`Discover my CML environment`, `Inspect the active lab`) resolve the authenticated controller and retrieve node definitions, images, resources, existing labs, and live topology — including node UUIDs, interface IDs/names, and link endpoints from the topology snapshot.
- **Title-to-UUID Resolution**: Named labs resolve to CML lab UUIDs from live inventory; discovery turns never create configuration plans.
- **Deployment Validation**: Lab plans are validated against live controller data (definitions, resources, lab collisions); unverified interface mappings block deployment readiness, and offline tickets are rejected at confirmation.

### Autonomous Device Command Execution (M1.2)
- **Read-Only Auto-Execution**: Explicit inspection requests execute `show` diagnostics directly on device consoles through the existing runtime + lock pathway — no parallel terminal implementation — capturing actual stdout, stderr, exit status, and latency per command.
- **Multi-Device Orchestration**: One request fans out to every explicitly selected device (e.g., two switches plus four Linux endpoints); each result keeps its lab, device, command, and output association, unreachable devices are reported honestly without hiding healthy results, and dispatch failures surface as explicit `dispatch_failed` rejections instead of silent inspections.
- **Platform-Aware Validation**: Cisco IOS commands are rejected for Linux/BusyBox endpoints and vice versa; invalid input is rejected before reaching any device runtime.
- **Pager-Safe Sequencing**: Autonomous execution drains `--More--` pagination to completion (bounded, with explicit truncation markers), so sequential commands can never consume another command's output.
- **Safety Envelope**: Device/lab ownership validation, execution timeouts, disconnected-session handling, per-command audit records, and mandatory explicit approval for anything configuration-changing.

### Lab Creation & Deployment (M1.3)
- **Natural-Language Lab Planning**: Conversational Lab Creation wizard turns topology requirements into validated deployment plans grounded in live controller discovery.
- **Ticketed Deployment**: Prepare → review → approve → execute workflow with ownership, expiry, idempotency, and audit; live-controller validation is enforced server-side at confirmation.

### Configuration Planning, Approval & Baselines (M1.4)
- **Multi-Device Plans**: Structured per-device configuration plans (VLANs, access/trunk ports, endpoint addressing) with dependency order and expected connectivity; negation-aware parsing honors exclusions (`R1/R2 excluded`, `No VLAN 2`); routers are rejected as Layer 2 switchport targets.
- **Approval Gates**: Single-use, expiring approvals bound to exact plan/lab/device/commands; any plan modification invalidates prior approval; invalid plans cannot reach approval.
- **Healthy Baselines**: Post-configuration evidence collection (`show vlan brief`, trunk/status/MAC/spanning-tree, endpoint `ifconfig`/`route`/`arp`, connectivity checks); `HEALTHY` only when every required check passes with actual outputs — never inferred.

### Historical Execution Evidence Retention
- **Persisted Results**: Every autonomous execution result (device, lab, command, stdout/stderr, exit status, error, timestamp) is stored in conversation metadata and survives turns and restarts.
- **Assessment Recall**: Later turns in the same conversation automatically receive earlier outputs as clearly labeled historical evidence (`RETAINED … EARLIER TURNS, NOT LIVE`), scoped to requested devices/lab where possible; fresh execution always takes precedence, and cross-conversation leakage is impossible by construction.

### WebSocket Terminal Lifecycle & Pager Handling
- **State-Synchronized WebSockets**: Monitored connections with automatic reconnection recovery, intentional closure tracking, and generation counters to avoid phantom reconnects.
- **Smart Pager Interceptor**: Seamlessly detects and navigates `--More--` device pagination:
  - `Space`: Advances one full page.
  - `Enter`: Advances one single line.
  - `q` / `Tab`: Exits pager cleanly and restores active command prompt.
- **Echo & Duplicate Prompt Suppression**: Tracks input-echo cycles to prevent duplicate prompts (e.g. `R1# \n R1#`).

### Terminal Focus & Split-Pane Layout Stability
- **Deterministic Focus Tracking**: Strict synchronization between xterm.js DOM focus events, terminal header indicators (`R1 (Focused)`), and Copilot targeting.
- **Stable Pane Positions**: Split-pane layouts remain locked in place when selecting or focusing devices, eliminating pane-shuffling bugs.

### Unified AI Copilot
- **Collapsible Drawer Interface**: Side-by-side AI assistant panel with drag-to-resize width control, markdown rendering, and integrated context tools.
- **Token-by-Token SSE Streaming**: Real-time response streaming with immediate cancellation controls (`Stop` button), execution cards, configuration plan cards, and approval controls.
- **Network Context Attachment**: Attach live device interfaces, running configs, routing tables, and terminal history directly into prompt context via `@` context chips.
- **Conversation Lifecycle**: Fresh chat per lab by default with preserved, explicitly selectable history; backend-enforced per-user conversation ownership.

### Meta Model API & Gemini Provider Support
- **Multi-Provider Abstraction**: Extensible `AIProvider` base class supporting pluggable LLM backends.
- **Meta Model API**: Native streaming adapter for Meta models (including the `muse-spark` family mapping) with automatic fallback upon provider rate limits.
- **Google Gemini Provider**: Built-in support for Gemini 1.5 Pro and Flash models.
- **Offline Mock Provider**: Full-featured local mock provider (`CLIQ_AI_MOCK=1`) for offline development and testing without consuming API credits.

### Retrieval-Augmented Generation (RAG) Architecture
- **Local Embedding Vector Index**: Local SentenceTransformers (`sentence-transformers/all-MiniLM-L6-v2`) generating 384-dimensional vector embeddings stored in SQLite (`cliq_vec.sqlite`). The semantic stack is an optional install extra; without it CLIQ degrades gracefully (see §8).
- **Hybrid Exact + Semantic Retrieval**: Exact command schema lookups combined with vector similarity search against indexed technical documentation chunks (`cliq_kb.sqlite`).
- **Offline Air-Gapped Operation**: Pre-computed vector indexes run entirely on-premise without external search engine or cloud vector database dependencies.

### Device-Aware Targeting & Platform Validation
- **Device Role & Platform Gating**: Validates command syntax against device role (`router` vs `switch`), platform (`cisco_ios`, `cisco_ios_xe`, `junos`), and active CLI mode before suggesting actions.
- **Router vs. Switchport Protection**: Explicitly blocks Layer 2 switchport commands (`switchport mode access`, `switchport access vlan`) from being recommended or inserted on Layer 3 router interfaces.
- **Disconnected Device Protection**: If a question references a disconnected or stopped device (e.g., `SW1`), the command block displays a clear disconnected warning badge (`SW1 is disconnected`) and **never silently falls back to active or focused devices like R1**.

### Citation Relevance & Verification
- **Official Cisco Documentation Links**: Grounded responses include authoritative reference citations to official Cisco documentation files (e.g. `layer_2_3_commands.html`, `interface_and_hardware_commands.html`).
- **Citation Relevance Filtering**: Rejects irrelevant security / 802.1X / IBNS (`access-session`) documentation when answering standard VLAN access-port configuration queries.

### Safe Command Insertion & Placeholder Guards
- **Single & Line Insertion**: Insert individual commands directly into the active, focused terminal prompt.
- **Sequential Execution Queue**: Execute multi-line configuration blocks sequentially, monitoring device prompt return and error markers before issuing subsequent lines.
- **Placeholder Safety Guard**: Automatically detects unresolved parameter placeholders (`<interface-id>`, `<vlan-id>`, `<ip-address>`), disables the Insert button, and displays an advisory warning: `⚠️ Placeholders required: Supply <...> values before inserting`.
- **Pre-Insertion Target Verification**: Validates device connection status immediately prior to dispatching commands to prevent accidental injection into closed or disconnected terminals.

---

## 3. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               CLIQ Web Frontend (React 18 + Vite)                      │
│  ┌───────────────────────┐  ┌───────────────────────────────┐  ┌─────────────────────┐ │
│  │   Explorer Sidebar    │  │     Workspace Panes           │  │   CLIQ Copilot      │ │
│  │  - Device Inventory   │  │  - xterm.js Consoles          │  │  - SSE Chat Stream  │ │
│  │  - Status Indicators  │  │  - Focus Synchronization      │  │  - Context Chips    │ │
│  │  - Lab Select & Views │  │  - ReactFlow Topology Map     │  │  - Exec & Plan Cards│ │
│  │                       │  │  - Lab Creation Wizard        │  │  - Approval Controls│ │
│  └───────────────────────┘  └───────────────────────────────┘  └─────────────────────┘ │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ HTTP / WebSocket (Port 8000)
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              CLIQ FastAPI Async Backend                                │
│                                                                                        │
│  ┌──────────────────────────────┐ ┌───────────────────────────┐ ┌────────────────────┐ │
│  │   Terminal & CML Services    │ │   AI Copilot Orchestrator │ │  Knowledge Engine  │ │
│  │  - WebSocket Session Handler │ │  - Conversation Store     │ │  - CommandRetriever│ │
│  │  - CML REST Proxy & Token Auth│ │  - Autonomous Execution   │ │  - SQLite Vector DB│ │
│  │  - Interactive Pager & Telnet│ │  - Config Planner/Approval│ │  - Text Chunks     │ │
│  │  - Lab Discovery & Topology  │ │  - Evidence Retention     │ │                    │ │
│  │  - Lab Planner/Deploy/Val.   │ │  - Meta/Gemini Adapters   │ │                    │ │
│  └──────────────────────────────┘ └───────────────────────────┘ └────────────────────┘ │
│                                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │                 Deterministic Keystroke Intelligence Engine                      │  │
│  │  - 196 Compiled IOS-XE Commands  - Mode Hierarchy Gates   - Sub-ms Typing Loop   │  │
│  └──────────────────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                        ┌───────────────────┴───────────────────┐
                        ▼                                       ▼
             [ Cisco Modeling Labs ]                 [ Local Vector Indexes ]
             - REST Discovery (/api/v0)              - SQLite Vector Store (384-dim)
             - Console Proxy Breakout                - SentenceTransformers all-MiniLM
             - Lab Lifecycle & Topology Snapshot       (optional extra)
```

Backend entry point: `cliq.web.server:app` (`src/cliq/web/server.py`). Key AI modules live under `src/cliq/ai/` (`orchestrator.py`, `device_exec.py`, `config_plan.py`, `config_exec.py`, `baseline.py`, `lab_discovery.py`, `network_context.py`, `conversations.py`); lab lifecycle under `src/cliq/labs/` (`planner.py`, `routes.py`, `validator.py`, `deploy.py`, `discovery.py`).

---

## 4. Installation & Windows Setup

### Prerequisites
- **Python**: Version 3.11 or higher (3.11.7 verified)
- **Node.js**: Version 18.x or higher
- **PowerShell**: Version 5.1 or PowerShell 7+
- **Git**

### Step-by-Step Installation

1. **Clone the repository**:
   ```powershell
   git clone https://github.com/Sathwik0623/CLIQ.git
   cd CLIQ
   ```

2. **Set up the Python Virtual Environment** (an editable install puts `src/` on the path):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   pip install -e .
   ```

   Optional extras (see `pyproject.toml`):
   ```powershell
   pip install -e '.[dev]'    # pytest, ruff, coverage tooling
   pip install -e '.[cml]'    # virl2_client for CML SDK paths
   pip install -e '.[ingest-semantic]'  # sentence-transformers neural embeddings
   ```

3. **Install Frontend Dependencies & Build Bundle**:
   ```powershell
   cd frontend
   npm install
   npm run build
   cd ..
   ```
   For development with hot reload instead: `npm run dev` inside `frontend/` (or run `scripts/run_frontend.ps1`).

4. **Initialize Deterministic Command Database**:
   ```powershell
   python -m cliq.db.build_db
   ```

5. **Configure Environment File**:
   ```powershell
   Copy-Item .env.example .env
   ```
   Then edit `.env` (API keys, CML host). `.env` is git-ignored — never commit it.

6. **Launch CLIQ Production Server**:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn cliq.web.server:app --host 127.0.0.1 --port 8000
   ```
   (or run `scripts/run_web_backend.ps1` for a dev server with auto-reload).
   Open your browser to `http://127.0.0.1:8000/`.

---

## 5. Dependencies

### Backend Dependencies (`requirements.txt` + `pyproject.toml`)
Core (installed via `requirements.txt`): `prompt_toolkit`, `rich`, `netmiko`, `ntc-templates`, `textfsm`, `pyyaml`, `fastapi>=0.115`, `uvicorn[standard]`, `requests`.
Optional extras (`pyproject.toml`): `dev` (`pytest`, `pytest-cov`, `ruff`, `black`, `mypy`), `ingest` (PDF/DOCX parsers), `ingest-semantic` (`sentence-transformers`, `chromadb`), `cml` (`virl2_client==2.9.0`, pinned to match the controller).
SQLite (`sqlite3`, stdlib) backs the command catalog, vector store, conversations, and session logs.

### Frontend Dependencies (`frontend/package.json`)
- `react`, `react-dom` (v18.x): Core component model.
- `xterm`, `xterm-addon-fit`: Terminal emulation.
- `reactflow` (v11.x): Interactive network topology graphing.
- Dev/test: `vite`, `typescript`, `vitest`, `jsdom`, `@testing-library/react`, `tailwindcss`.

---

## 6. Environment Configuration (`.env.example`)

Copy `.env.example` to `.env` to configure your local runtime (values below are the shipped defaults; see the template for full comments):

```ini
# Server Settings
CLIQ_HOST=0.0.0.0
CLIQ_PORT=8000
CLIQ_FRONTEND_DIR=frontend/dist
CLIQ_SESSION_DB=
CLIQ_COPILOT_DB=

# AI Copilot Configuration
META_AI_API_KEY=
META_AI_BASE_URL=https://api.meta.ai/v1
GEMINI_API_KEY=
CLIQ_AI_MOCK=

# RAG & Knowledge Retrieval Settings
CLIQ_RAG_ENABLED=1
CLIQ_KB_DB=./kb/cliq_kb.sqlite
CLIQ_VEC_DB=./kb/cliq_vec.sqlite
CLIQ_EMBEDDING_MODEL=all-MiniLM-L6-v2
CLIQ_RETRIEVAL_TOP_K=5

# NetSim Reservation Policy (Optional)
CLIQ_RESERVATION_POLICY=warn
CLIQ_NETSIM_API_KEY=

# CML Connection (Optional defaults; also enterable in the web UI)
CML_HOST=
CML_PORT=443
CML_VERIFY_SSL=false

# Logging, Diagnostics & Offline Flags
CLIQ_TRACE_BYTES=5242880
CLIQ_LOG_MAX_BYTES=10485760
CLIQ_LOG_MAX_BACKUPS=5
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

---

## 7. AI Provider Configuration (Meta & Gemini)

CLIQ integrates with frontier LLMs while protecting operator privacy:

### Meta Model API Configuration
1. Obtain an API key from Meta Developer Portal or your designated model access provider.
2. In `.env`, set:
   ```ini
   META_AI_API_KEY="your_api_key"
   META_AI_BASE_URL="https://api.meta.ai/v1"
   ```
3. The Meta adapter streams supported Meta models (including the `muse-spark` family mapping) with automatic fallback upon provider rate limits.

### Google Gemini Configuration
1. Set `GEMINI_API_KEY="your_gemini_key"` in `.env`.
2. Select Gemini (1.5 Pro / Flash) from the Copilot model selector in the UI.

### Offline Mock Testing (Zero API Credits Consumed)
To test Copilot without an active API key or internet connection:
```powershell
# Set mock flag in environment or .env
$env:CLIQ_AI_MOCK="1"
```
The mock provider emulates streaming responses for offline development and testing.

---

## 8. Knowledge Base Setup & Provisioning

> [!IMPORTANT]
> **Repository Distribution Policy**: The compiled SQLite knowledge databases (`kb/cliq_kb.sqlite`, `kb/cliq_vec.sqlite`) and raw documentation sources are **strictly git-ignored**. They are **not committed** to the public repository to avoid tracking binary blobs, customer data, and copyrighted manuals.

### How to Provision the Knowledge Base on a Fresh Clone

1. **Create the local `kb/` directory**:
   ```powershell
   New-Item -ItemType Directory -Force -Path kb
   ```

2. **Ingesting Custom Documentation**:
   To populate the knowledge base from your own technical manuals, command references, or configuration guides:
   - Place source documents into `kb/sources/`.
   - Run the ingestion tooling under `tools/` (e.g. `tools/ingest_pdfs.py`, `tools/ingest_product_cr.py`, `tools/ingest_command_refs.py`), which chunk text, extract syntax structures, and generate the SQLite knowledge and vector tables.

3. **Restoring from Pre-Built Artifacts**:
   In enterprise lab environments, pre-built `cliq_kb.sqlite` and `cliq_vec.sqlite` artifacts can be copied directly into the `kb/` folder.

### Graceful Degradation When Knowledge Base is Absent
If a fresh clone is started without provisioning `kb/cliq_kb.sqlite` or when `sentence-transformers` is unavailable:
- CLIQ logs an informational notice and falls back to the native deterministic command catalog and direct LLM generation.
- The interactive terminal, CML integration, autonomous execution, lab lifecycle, and the 196-command offline deterministic keystroke engine remain **fully operational**.
- The Copilot continues to function in direct synthesis mode, relying on the model's pre-trained network engineering knowledge while local document retrieval is inactive.

---

## 9. Testing & Verification Results

Backend tests and linting require the dev extra (`pip install -e '.[dev]'`); frontend tests need `npm install` inside `frontend/`.

```powershell
# 1. Run the full backend unit and regression suite
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q

# 2. Run a focused regression area, e.g. autonomous execution
.\.venv\Scripts\python.exe -m pytest tests/unit/test_m1_2_device_exec.py -q

# 3. Run frontend unit tests and production build
npm --prefix frontend test
npm --prefix frontend run build

# 4. Lint changed Python files
.\.venv\Scripts\python.exe -m ruff check src/cliq/ai/orchestrator.py
```

### Verified Test Results (M1 Release)
- **Full Backend Suite**: **1175 passed, 23 skipped**; 6 failures in `test_phase_v3_rag_activation.py` are pre-existing and environmental (they require the optional `ingest-semantic` extra: `sentence-transformers`), unrelated to M1 behavior.
- **M1 Regression Suites**: autonomous execution, multi-device acceptance, discovery/topology, configuration planning and negation handling, evidence retention, and conversation lifecycle suites all pass.
- **Frontend Tests**: **16 passed** (vitest); **production build** compiles cleanly (`tsc` + Vite).
- **Live Acceptance**: performed manually through the CLIQ UI against a real CML controller (mocked runtimes in automated tests; live verification is never claimed from unit results alone).

---

## 10. Known Limitations & Roadmap

### Current Limitations
- **Platform Scope**: Autonomous execution and planning target Cisco IOS/IOS-XE (`iosv`/`iosvl2`) consoles and Linux/BusyBox endpoints; other platforms remain advisory-only.
- **RAG Semantic Tests**: The 6 RAG activation tests require the optional `ingest-semantic` extra; without it they fail on embedding initialization while all other suites pass.
- **Topology Fidelity**: Per-node interface capabilities are undiscoverable pre-deploy; deploy-time assignment remains the source of truth and unverified mappings block deployment readiness.
- **Single-Host Scope**: Designed for single-tenant engineering sessions; multi-tenant RBAC is planned for future milestones.
- **Manual Live Verification**: Automated tests use mocked runtimes; live controller behavior is validated manually through the UI.

### Roadmap
For full architecture specifications and roadmaps for upcoming milestones, see [`docs/NEXT_DEVELOPMENT_PHASES.md`](docs/NEXT_DEVELOPMENT_PHASES.md). Lab auto-creation from natural language (formerly Phase V.4) is implemented in this release as the M1.3 Lab Creation wizard and deployment pipeline.

---

## 11. License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
