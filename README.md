# CLIQ — Networking CLI Intelligence IDE & Unified AI Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![React 18](https://img.shields.io/badge/react-18.x-61dafb.svg)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests: 855 passed](https://img.shields.io/badge/tests-855%20passed-success.svg)](tests/)

CLIQ is an offline-first, deterministic command-line intelligence engine, multi-device terminal platform, and retrieval-grounded AI Copilot engineered specifically for network engineers and operators. It unites a modern, VS Code-inspired engineering IDE with sub-millisecond offline keystroke completions, interactive Cisco Modeling Labs (CML) simulation management, and an authoritative AI Copilot powered by the Meta Model API and official Cisco IOS-XE technical documentation.

CLIQ operates on the principle of **strict advisory safety**: the network device remains the authoritative source of truth. Keystrokes in the interactive hot path are evaluated deterministically offline without cloud latency, while complex diagnostic questions, protocol explanations, and configuration blueprints are synthesized with official documentation citations and validated against the target device platform before insertion.

---

## Table of Contents

1. [Project Overview & Objectives](#1-project-overview--objectives)
2. [Current Features](#2-current-features)
   - [Interactive Multi-Device Terminal](#interactive-multi-device-terminal)
   - [Cisco Modeling Labs (CML) Integration](#cisco-modeling-labs-cml-integration)
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
3. **Retrieval-Augmented AI Copilot**: A side-by-side diagnostic intelligence assistant grounded in local vector databases of official Cisco documentation. Suggestions are validated for platform compatibility, protect against invented parameters, and provide safe, operator-controlled execution.

---

## 2. Current Features

### Interactive Multi-Device Terminal
- **Concurrent Tabbed Workspaces**: Manage multiple live router and switch consoles simultaneously without cross-talk or reattachment stalls.
- **Visual Explorer Navigation**: Discovered lab devices appear in a hierarchical left sidebar with real-time operational status dots, active mode badges, and instant search filtering.
- **Status Bar Telemetry**: Live metrics reporting latency, CLI mode, active host, and connected terminal count.

### Cisco Modeling Labs (CML) Integration
- **Automated Simulation Discovery**: Connects to CML REST APIs (`/api/v0/`) using temporary session tokens to discover deployed topologies and node states.
- **Dynamic Topology Mapping**: Renders interactive ReactFlow topology maps with verified interface endpoint identifiers (`Gi0/0 ↔ Gi0/1`), completely filtering raw internal UUIDs.
- **Console Multiplexing**: Connects directly to device console endpoints via CML's raw console proxy or telnet breakout ports.

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
- **Token-by-Token SSE Streaming**: Real-time response streaming with immediate cancellation controls (`Stop` button).
- **Network Context Attachment**: Attach live device interfaces, running configs, routing tables, and terminal history directly into prompt context via `@` context chips.

### Meta Model API & Gemini Provider Support
- **Multi-Provider Abstraction**: Extensible `AIProvider` base class supporting pluggable LLM backends.
- **Meta Model API**: Native streaming adapter supporting Meta Llama models (`muse-spark-1.3`, `muse-wonder-1.0`, `muse-spark-1.2`) with automatic fallback upon provider rate limits.
- **Google Gemini Provider**: Built-in support for Gemini 1.5 Pro and Flash models.
- **Offline Mock Provider**: Full-featured local mock provider (`CLIQ_AI_MOCK=1`) for offline development and testing without consuming API credits.

### Retrieval-Augmented Generation (RAG) Architecture
- **Local Embedding Vector Index**: Uses local SentenceTransformers (`sentence-transformers/all-MiniLM-L6-v2`) generating 384-dimensional vector embeddings stored in SQLite (`cliq_vec.sqlite`).
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
│  │  - Device Inventory   │  │  - xterm.js Consoles (R1, R2) │  │  - SSE Chat Stream  │ │
│  │  - Status Indicators  │  │  - Focus Synchronization      │  │  - Context Chips    │ │
│  │  - Lab Select & Views │  │  - ReactFlow Topology Map     │  │  - Safe Insertion   │ │
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
│  │  - CML REST Proxy & Token Auth│ │  - MetaProvider Adapter   │ │  - SQLite Vector DB│ │
│  │  - Interactive Pager & Telnet│ │  - GeminiProvider Adapter │ │  - Text Chunks     │ │
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
```

---

## 4. Installation & Windows Setup

### Prerequisites
- **Python**: Version 3.10 or higher
- **Node.js**: Version 18.x or higher
- **PowerShell**: Version 5.1 or PowerShell 7+
- **Git**

### Step-by-Step Installation

1. **Clone the repository**:
   ```powershell
   git clone https://github.com/Sathwik0623/CLIQ.git
   cd CLIQ
   ```

2. **Set up the Python Virtual Environment**:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Install Frontend Dependencies & Build Bundle**:
   ```powershell
   cd frontend
   npm install
   npm run build
   cd ..
   ```

4. **Initialize Deterministic Command Database**:
   ```powershell
   python -m cliq.db.build_db
   ```

5. **Configure Environment File**:
   ```powershell
   Copy-Item .env.example .env
   ```

6. **Launch CLIQ Production Server**:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn cliq.web.server:app --host 127.0.0.1 --port 8000
   ```
   Open your browser to `http://127.0.0.1:8000/`.

---

## 5. Dependencies

### Backend Dependencies (`requirements.txt`)
- `fastapi`, `uvicorn[standard]`, `pydantic>=2.0`: Modern asynchronous API routing and SSE streaming.
- `websockets`: Bi-directional terminal multiplexing.
- `sentence-transformers`, `torch`: Offline semantic embedding generation.
- `sqlite3` / `pysqlite3`: Local vector storage and relational command metadata.
- `netmiko`, `paramiko`: Direct SSH and terminal-server console handling.
- `requests`, `httpx`: Synchronous and asynchronous HTTP communication.
- `pytest`, `pytest-asyncio`, `playwright`: Automated unit, integration, and browser testing.

### Frontend Dependencies (`frontend/package.json`)
- `react`, `react-dom` (v18.x): Core component model.
- `@xterm/xterm`, `@xterm/addon-fit`, `@xterm/addon-web-links`: Terminal emulation.
- `reactflow` (@xyflow/react): Interactive network topology graphing.
- `lucide-react`: UI iconography.
- `tailwindcss`, `vite`: Fast build tooling and modular styling.

---

## 6. Environment Configuration (`.env.example`)

Copy `.env.example` to `.env` to configure your local runtime:

```ini
# Server Settings
CLIQ_HOST=0.0.0.0
CLIQ_PORT=8000
CLIQ_FRONTEND_DIR=frontend/dist

# AI Copilot Configuration
META_AI_API_KEY=your_meta_api_key_here
META_AI_BASE_URL=https://api.meta.ai/v1
GEMINI_API_KEY=
CLIQ_AI_MOCK=

# RAG & Knowledge Retrieval Settings
CLIQ_RAG_ENABLED=1
CLIQ_KB_DB=./kb/cliq_kb.sqlite
CLIQ_VEC_DB=./kb/cliq_vec.sqlite
CLIQ_EMBEDDING_MODEL=all-MiniLM-L6-v2
CLIQ_RETRIEVAL_TOP_K=5

# CML Sandbox Connection (Optional default)
CML_HOST=10.10.20.161
CML_PORT=443
CML_VERIFY_SSL=false
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
3. Supported Meta models include `muse-spark-1.3`, `muse-wonder-1.0`, and `muse-spark-1.2`.
4. If rate-limited or temporarily unavailable, CLIQ automatically cascades to available models.

### Google Gemini Configuration
1. Set `GEMINI_API_KEY="your_gemini_key"` in `.env`.
2. Select Gemini from the Copilot model selector in the UI.

### Offline Mock Testing (Zero API Credits Consumed)
To test Copilot without an active API key or internet connection:
```powershell
# Set mock flag in environment or .env
$env:CLIQ_AI_MOCK="1"
```
The mock provider emulates streaming responses, validates device targeting, handles parameter placeholders, and cites authoritative documentation.

---

## 8. Knowledge Base Setup & Provisioning

> [!IMPORTANT]
> **Repository Distribution Policy**: The compiled SQLite knowledge databases (`kb/cliq_kb.sqlite`, `kb/cliq_vec.sqlite`) and raw documentation sources (`kb/sources/`) contain thousands of compiled records and are **strictly git-ignored**. They are **not committed** to the public repository to avoid tracking binary blobs, customer data, and copyrighted manuals.

### How to Provision the Knowledge Base on a Fresh Clone

1. **Create the local `kb/` directory**:
   ```powershell
   New-Item -ItemType Directory -Force -Path kb
   ```

2. **Ingesting Custom Documentation**:
   To populate the knowledge base from your own technical manuals, command references, or configuration guides (PDF, HTML, or Markdown format):
   - Place source documents into `kb/sources/`.
   - Run the ingestion and vectorization pipeline:
     ```powershell
     python -m cliq.ingest.run_ingest --source-dir kb/sources/ --output-kb kb/cliq_kb.sqlite --output-vec kb/cliq_vec.sqlite
     ```
   This chunks text, extracts syntax structures, computes 384-dimensional dense vectors using `all-MiniLM-L6-v2`, and generates the SQLite vector tables.

3. **Restoring from Pre-Built Artifacts**:
   In enterprise lab environments, pre-built `cliq_kb.sqlite` and `cliq_vec.sqlite` artifacts can be copied directly into the `kb/` folder.

### Graceful Degradation When Knowledge Base is Absent
If a fresh clone is started without provisioning `kb/cliq_kb.sqlite` or when `sentence-transformers` is unavailable:
- CLIQ logs an informational notice: `[INFO] Knowledge base not found. Falling back to native deterministic command catalog and direct LLM generation.`
- The interactive terminal, CML integration, and the 196-command offline deterministic keystroke engine remain **100% operational**.
- The Copilot continues to function in direct synthesis mode, relying on the model's pre-trained network engineering knowledge while notifying the operator that local document retrieval is inactive.

---

## 9. Testing & Verification Results

CLIQ maintains an exhaustive automated testing suite spanning backend logic, parser grammar, RAG retrieval accuracy, and live browser terminal interactions.

```powershell
# 1. Run full unit and regression test suite
.\.venv\Scripts\python.exe -m pytest tests/unit/ -v

# 2. Run dedicated Phase V.3 Critical Acceptance Fixes suite
.\.venv\Scripts\python.exe -m pytest tests/unit/test_phase_v3_critical_fixes.py -v

# 3. Run Phase V.3 RAG Activation test suite
.\.venv\Scripts\python.exe -m pytest tests/unit/test_phase_v3_rag_activation.py -v

# 4. Run frontend type-check & production build
npm --prefix frontend run build
```

### Verified Test Results (Phase V.3 Milestone)
- **Full Backend Suite**: **855 passed, 0 failed, 23 skipped** in 152.99s.
- **Critical Acceptance Fixes**: **11 passed in 6.21s** (0 failed).
- **RAG Activation Suite**: **23 passed in 13.11s** (0 failed).
- **Frontend Production Build**: Built cleanly with Vite in 8.81s (0 TypeScript errors).
- **Chromium Live Browser Acceptance**: Verified via Playwright automation:
  - Preserved disconnected target `SW1` badge; prevented fallback to `R1`.
  - Verified placeholder warnings and disabled insertion for incomplete commands.
  - Verified sample terminal outputs render in clean tables without command action buttons.
  - Verified citation relevance targeting `layer_2_3_commands.html`.

---

## 10. Known Limitations & Roadmap

### Current Limitations
- **Syntax Focus**: Currently optimized for Cisco IOS and IOS-XE command grammar.
- **RAG Vector Scope**: Semantic retrieval operates on pre-ingested Cisco reference documentation.
- **Single-Host Scope**: Designed for single-tenant engineering sessions; multi-tenant RBAC is planned for future milestones.

### Planned Future Workstreams
For full architecture specifications and roadmaps for upcoming milestones, see [`docs/NEXT_DEVELOPMENT_PHASES.md`](docs/NEXT_DEVELOPMENT_PHASES.md):
- **Phase V.4 — Lab Copilot / Lab Auto-Creation Agent**: Autonomous translation of natural language topology requirements into validated CML simulation blueprints with human confirmation gates.
- **Phase V.5 — CLI Command Intelligence Improvements**: Live operational-state ghost text, dynamic typo correction, platform-aware compatibility checking, and instant error explanations.

---

## 11. License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
