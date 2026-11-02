# CLIQ — Networking Device CLI Intelligence & Terminal IDE

CLIQ is a deterministic, offline-first CLI intelligence engine and multi-device terminal platform engineered for network operators and engineers. It combines a modern, enterprise-grade IDE with real-time command guidance: word-aware abbreviation expansion, mode-aware validation, contextual auto-fix, argument boundary checks, and inline ghost-text completions.

CLIQ connects seamlessly to physical network hardware over SSH console servers, simulated labs via Cisco Modeling Labs (CML), or fully offline mock devices with zero external cloud dependencies in the keystroke hot path.

---

## Table of Contents

1. [Project Overview & Objectives](#1-project-overview--objectives)
2. [Key Features](#2-key-features)
3. [Modern IDE Architecture & UI Overview](#3-modern-ide-architecture--ui-overview)
4. [Multi-Device Terminal Architecture](#4-multi-device-terminal-architecture)
5. [Supported Connection Methods](#5-supported-connection-methods)
6. [Cisco Modeling Labs (CML) Integration](#6-cisco-modeling-labs-cml-integration)
7. [Direct SSH & Console Server Connectivity](#7-direct-ssh--console-server-connectivity)
8. [Command Intelligence & Ghost-Text Engine](#8-command-intelligence--ghost-text-engine)
9. [Interactive Pager & Help-Capture Pipeline](#9-interactive-pager--help-capture-pipeline)
10. [Authentication & Privilege Escalation Handling](#10-authentication--privilege-escalation-handling)
11. [Installation & Prerequisites](#11-installation--prerequisites)
12. [Startup & Deployment Workflows](#12-startup--deployment-workflows)
13. [Configuration & Environment Variables](#13-configuration--environment-variables)
14. [Testing & Quality Assurance](#14-testing--quality-assurance)
15. [Repository Structure & Codebase Layout](#15-repository-structure--codebase-layout)
16. [Security & Safety Architecture](#16-security--safety-architecture)
17. [Current Limitations & Roadmap](#17-current-limitations--roadmap)

---

## 1. Project Overview & Objectives

Traditional network operating system CLIs require memorizing hundreds of command hierarchies, syntax nuances, and syntax differences across vendors and execution modes. Mistakes in production environments cause catastrophic downtime, syntax rejections, and accidental disruptions.

CLIQ addresses this by introducing a deterministic intelligence layer positioned between the network engineer and the device console:

- **Universal Multi-Vendor Vision**: While seeded with comprehensive Cisco IOS grammar, CLIQ is designed as an extensible, multi-NOS platform capable of indexing syntax catalogs for Arista EOS, Juniper Junos, SONiC, and Linux network appliances.
- **Microsecond Latency**: Keystroke evaluation executes strictly in sub-millisecond offline logic, avoiding latency spikes and cloud LLM dependencies in the interactive typing loop.
- **Advisory-Only Safety Model**: The device remains the authoritative source of truth. CLIQ never intercepts or modifies keystrokes autonomously; it provides non-blocking advisories, confirmations for destructive actions (`reload`, `erase startup-config`), and actionable typo fixes.
- **Enterprise Engineering IDE**: Replaces disconnected terminal emulators with a centralized workspace featuring an Explorer sidebar, multi-tab console strip, integrated lab topology mapping, command palette, and real-time session health observability.

---

## 2. Key Features

- **Inline Ghost-Text Completion**: Displays the highest-confidence completion inline in muted gray text; pressing `Tab` completes the command or argument token.
- **Multi-Token Abbreviation Expansion**: Instant expansion of established network engineer shorthands (`sh ip int br`, `conf t`, `copy run start`, `line con 0`).
- **Context & Mode-Aware Validation**: Tracks device CLI modes (`exec`, `enable`, `config`, `config-if`, `config-subif`, `config-router`, etc.) to prevent executing commands in the wrong context (e.g., offering `do show ...` when typed inside global configuration).
- **Subinterface & Virtual Interface Awareness**: Recognizes IP addressing, encapsulation, and interface parameters on subinterfaces, loopbacks, and port-channels.
- **Destructive-Command Confirmation Gates**: Flags high-risk operations (`reload`, `write erase`, `no router ospf`) with mandatory confirmation dialogs before execution.
- **Deterministic Argument Verification**: Validates IPv4/IPv6 syntax, subnet bitmasks, VLAN IDs (1–4094), MTU constraints, and duplex/speed settings against regex and range rules.
- **Multi-Device Tabbed Workspaces**: Maintain multiple active console connections concurrently without cross-session interference or reattachment drops.
- **Real-Time Interactive Pager Handling**: Seamlessly navigates `--More--` device pagination with single-key controls (`Space` for page, `Enter` for line, `Tab`/`q` to quit).
- **Interactive Topology Visualization**: Renders live CML network topologies with verified interface endpoint identifiers (`Gi0/0 ↔ Gi0/1`) and real-time link states.
- **Offline Semantic Search & Troubleshooting Playbooks**: Offline vector-indexed command reference with phased playbooks for complex protocols (OSPF, BGP, STP, VRF).

---

## 3. Modern IDE Architecture & UI Overview

CLIQ features a unified, dark-theme engineering IDE inspired by modern development environments:

```
+------------------------------------------------------------------------------------+
|  CLIQ  [Lab: Production-DC]                     [Cmd Palette: Ctrl+P]  (o) Status  |
+-------------------+----------------------------------------------------------------+
| EXPLORER          | [ R1: Console x ] [ R2: Console x ] [ Topology ]               |
| v LAB TOPOLOGY    +----------------------------------------------------------------+
|   > Routers       | R1# show ip interface brief                                    |
|     * R1 (Online) | Interface          IP-Address      OK? Method Status  Protocol |
|     * R2 (Online) | GigabitEthernet0/0 10.0.1.1        YES NVRAM  up      up       |
|   > Switches      | GigabitEthernet0/1 unassigned      YES NVRAM  up      up       |
|     * SW1 (Online)|                                                                |
|     * SW2 (Online)| R1# configure terminal                                         |
|                   | Enter configuration commands, one per line.  End with CNTL/Z.  |
| v SAVED SESSIONS  | R1(config)# interface GigabitEthernet0/1                       |
|   - Core-GW-01    | R1(config-if)# ip address 10.0.2.1 255.255.255.0               |
|   - Spine-02      |                                                                |
+-------------------+----------------------------------------------------------------+
| [✓ Ready]  Mode: config-if  | Latency: 1.2ms  | Typos Blocked: 4  | Lab: CML-Sim   |
+------------------------------------------------------------------------------------+
```

### Key UI Elements
- **Top Application Bar**: Displays lab identification, active connection state, global search/command palette launcher (`Ctrl+P` / `Cmd+P`), and quick access to global preferences.
- **Left Explorer Sidebar**: Hierarchical navigation tree showing discovered lab devices, interface inventory, connection health indicators, and bookmarked sessions.
- **Central Workspace Tabs**: Chrome/VS Code-style tab bar hosting independent terminal consoles alongside the interactive topology canvas. Tabs feature active accent highlighting, individual close buttons, and background persistence.
- **Bottom Status Bar**: Surfaces real-time telemetry including current CLI execution mode, WebSocket latency, session engine health, and diagnostic indicators.
- **Global Command Palette**: Quick navigation across lab devices, recent commands, troubleshooting playbooks, and terminal preferences.

---

## 4. Multi-Device Terminal Architecture

CLIQ’s terminal subsystem delivers concurrent, non-blocking sessions across multiple network elements:

```
┌─────────────────────────────────────────────────────────────┐
│                      Web Browser (UI)                       │
│  ┌──────────────────────────────┐  ┌──────────────────────┐ │
│  │     R1 Terminal Session      │  │  R2 Terminal Session │ │
│  │ (xterm.js + Local State)     │  │ (xterm.js + State)   │ │
│  └──────────────┬───────────────┘  └──────────┬───────────┘ │
└─────────────────┼─────────────────────────────┼─────────────┘
                  │ WebSocket                   │ WebSocket
                  ▼                             ▼
┌─────────────────────────────────────────────────────────────┐
│                 FastAPI Async Server (:8000)                │
│  ┌──────────────────────────────┐  ┌──────────────────────┐ │
│  │      R1 Session Handler      │  │  R2 Session Handler  │ │
│  │   - Prompt Deduplication     │  │  - Stage Callbacks   │ │
│  │   - Pager Interceptor        │  │  - Prompt Cache      │ │
│  └──────────────┬───────────────┘  └──────────┬───────────┘ │
└─────────────────┼─────────────────────────────┼─────────────┘
                  ▼                             ▼
       [ CML Breakout / Telnet ]       [ SSH Terminal Server ]
```

### Key Technical Mechanisms
- **Session Isolation**: Each terminal tab maintains dedicated xterm.js instances, independent prompt-cycle caches, and local input buffers. Opening or switching tabs never causes reattachment or output drops on existing devices.
- **Prompt Deduplication Cycle Engine**: Prevents duplicate prompts (e.g., `SW1# \n SW1#`) caused by race conditions between device echoes, PTY signals, and synthetic prompt restoration. The engine tracks cycle states (`input_sent` → `device_echo` → `prompt_rendered`) to suppress redundant echoes.
- **Visible Connection Stages**: During multi-second network or breakout attachments, CLIQ renders live in-terminal status indicators (`Connecting to device...`, `Authenticating console...`, `Waking session...`) that automatically clear upon receiving the initial device prompt.

---

## 5. Supported Connection Methods

CLIQ adapts to three primary deployment scenarios:

| Mode | Target Environment | Latency | Requirements |
|---|---|---|---|
| **Mock Device** | Local testing & development | < 1 ms | Fully offline; zero configuration required |
| **Cisco CML Labs** | Virtualized topologies (IOL, IOSv, Cat8000v) | 2–8 ms | CML Controller REST API & console breakout |
| **Direct SSH / Hardware** | Physical lab gear, console servers, terminal servers | 10–50 ms | Reachable SSH host, port, and credentials |

---

## 6. Cisco Modeling Labs (CML) Integration

CLIQ integrates natively with Cisco Modeling Labs:

- **Automated Lab Discovery**: Authenticates using temporary bearer tokens against the CML API (`/api/v0/authenticate`), retrieves running topologies, and enumerates all node definitions.
- **Dynamic Topology Mapping**: Visualizes active link interconnections with verified interface designations (e.g. `Gi0/0 ↔ Gi0/1`), completely filtering internal node UUIDs and opaque hashes.
- **Console Multiplexing**: Connects directly to device console endpoints via CML's raw console proxy or telnet breakout ports, supporting concurrent active terminals across all lab nodes.

---

## 7. Direct SSH & Console Server Connectivity

For physical hardware racks, CLIQ provides production-ready SSH integration:

- **Netmiko & Paramiko Engine**: Implements robust device drivers capable of managing console servers (Opengear, Cisco 2911 HWIC-16A, Avocent) as well as direct management interfaces.
- **Terminal Shell Normalization**: Auto-negotiates VT100 terminal dimensions (`80x24` base with dynamic resize synchronization) to ensure predictable pagination and cursor positioning.
- **Graceful Teardown**: Ensures clean session disconnection on tab closure without leaving orphan VTY processes on physical equipment.

---

## 8. Command Intelligence & Ghost-Text Engine

The heart of CLIQ is a high-speed deterministic intelligence engine:

```
[Keystroke: "conf t"]
        │
        ▼
[Tokenizer & Abbrev Expander]  ──► Matches "configure terminal"
        │
        ▼
[Mode Hierarchy Gate]          ──► Verifies command valid in 'enable' mode
        │
        ▼
[Argument Validator]           ──► Checks argument boundaries & types
        │
        ▼
[Inline Ghost Text Engine]     ──► Surfaces inline completion in terminal
```

- **Compiled SQLite Registry**: Source YAML command schemas are compiled into an optimized SQLite binary database (`command_db.sqlite`) loaded into memory at startup.
- **Token Levenshtein Correction**: Detects typographical errors (e.g. `interafce` → `interface`) deterministically without ambiguous fuzzy guesswork.
- **Contextual Pipe Handling**: Recognizes execution filters (`| include`, `| section`, `| begin`, `| exclude`) and provides completion parameters based on active syntax.

---

## 9. Interactive Pager & Help-Capture Pipeline

Network devices page long outputs using `--More--` prompts that traditionally break web terminal emulators. CLIQ handles this transparently:

- **Interactive Paging**: Detects paging boundaries and captures single-keystroke inputs directly:
  - `Space`: Advances one full screen page.
  - `Enter`: Advances a single line.
  - `Tab` or `q`: Exits pagination cleanly and returns to the active prompt.
- **Inline `?` Help Capture**: When entering `?`, CLIQ captures the context-sensitive help buffer, cleans control sequences, and presents options cleanly without polluting your terminal command buffer.

---

## 10. Authentication & Privilege Escalation Handling

CLIQ supports interactive authentication flows while safeguarding sensitive credentials:

- **Ephemeral In-Memory Handling**: Usernames, passwords, and enable secrets reside strictly in transient memory for the duration of the authentication handshake. Credentials are **never** persisted to disk, written to SQLite, or emitted to log streams.
- **Interactive Login Detectors**: Detects standard console challenges (`login:`, `Username:`, `Password:`, `Enter PIN:`) and bridges input securely.
- **Automatic Privilege Escalation**: Recognizes `enable` commands, detects password challenges, and updates the active parser state as soon as privilege escalation (`#` prompt) is confirmed.

---

## 11. Installation & Prerequisites

### Prerequisites
- **Python**: Version 3.10 or higher
- **Node.js**: Version 18.x or higher
- **npm**: Version 9.x or higher
- **Operating System**: Windows, Linux, or macOS

### Step-by-Step Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Sathwik0623/CLIQ.git
   cd CLIQ
   ```

2. **Configure Python Virtual Environment**:
   ```powershell
   # Windows PowerShell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
   ```bash
   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Build the Command Intelligence Database**:
   ```bash
   python -m cliq.db.build_db
   ```

4. **Install Frontend Dependencies**:
   ```bash
   cd frontend
   npm install
   cd ..
   ```

---

## 12. Startup & Deployment Workflows

### Option A: Docker & Docker Compose (Containerized Production — Recommended)

CLIQ provides a production-grade multi-stage Dockerfile that builds the React frontend and packages the FastAPI backend into a secure, non-root, single-port container:

```powershell
# 1. Prepare environment configuration
Copy-Item .env.example .env

# 2. Build and launch container in background
docker compose build
docker compose up -d
```

- Access the IDE at `http://localhost:8000/`.
- Verify container health: `curl http://localhost:8000/health` (or check `docker compose ps`).
- View streaming logs: `docker compose logs -f cliq`.
- Restart container: `docker compose restart cliq`.
- Shut down: `docker compose down`.
- Data persistence: Runtime learned commands, operational memory, and audit traces are preserved across restarts via the `cliq_data` volume.
- See the comprehensive [docs/DOCKER.md](docs/DOCKER.md) for full networking, volume management, and troubleshooting details.

### Option B: Single-Port Native Production Mode

FastAPI serves both the built React/xterm.js frontend and the WebSocket terminal backend on a single port. Ideal for lab sharing and air-gapped hosting without Docker:

```powershell
# 1. Build frontend bundle
cd frontend
npm run build
cd ..

# 2. Launch unified server
.\scripts\run_web_backend.ps1 -ListenHost 0.0.0.0 -ListenPort 8000
```

Access the IDE at `http://<machine-ip>:8000/`.

### Option C: Local Development Workflow

Run backend and frontend independently with hot reloading enabled:

```powershell
# Terminal 1: Backend
.\.venv\Scripts\Activate.ps1
uvicorn cliq.web.server:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2: Frontend (Vite Dev Server)
cd frontend
npm run dev
```

Vite proxies `/api` and `/ws` requests directly to `:8000`. Access the dev server at `http://localhost:5173/`.

### Option D: Standalone Terminal CLI

Run the prompt_toolkit-based terminal shell directly in your console:

```powershell
python -m cliq.ui.app
```

---

## 13. Configuration & Environment Variables

Create a local `.env` file in the project root to configure lab connectivity. **Never commit this file to version control.**

```ini
# Server Settings
CLIQ_HOST=0.0.0.0
CLIQ_PORT=8000
CLIQ_FRONTEND_DIR=frontend/dist

# Cisco Modeling Labs (CML) Configuration
CML_HOST=10.10.x.x
CML_PORT=443
CML_USERNAME=cml_admin
CML_PASSWORD=example_secure_password
CML_LAB_ID=cml-lab-uuid-example
CML_VERIFY_SSL=false

# Offline Semantic Cache (for Air-Gapped Networks)
# Models are cached at ~/.cache/huggingface/hub or %USERPROFILE%\.cache\huggingface\hub
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

---

## 14. Testing & Quality Assurance

CLIQ maintains a comprehensive test suite covering parser grammar, intelligence rules, state engines, and multi-device connection handlers.

```powershell
# Run full unit & regression test suite
.\.venv\Scripts\python.exe -m pytest tests/unit -v

# Run Phase U multi-device terminal regression tests
.\.venv\Scripts\python.exe -m pytest tests/unit/test_phase_u* -v

# Run frontend type-check & production build
npm --prefix frontend run build
```

---

## 15. Repository Structure & Codebase Layout

```
CLIQ/
├── Dockerfile                   # Multi-stage production container definition
├── docker-compose.yml           # Service orchestration and volume configuration
├── .dockerignore                # Container build context exclusions
├── .env.example                 # Configuration template with security guidelines
├── command_db/                  # Source-of-truth command syntax definitions (YAML)
│   ├── modes.yaml               # CLI execution mode hierarchy
│   ├── arg_types.yaml           # Argument validators (regex, ranges, enums)
│   ├── exec/                    # User exec and privileged exec commands
│   └── config/                  # Configuration mode commands (if, router, line)
├── docs/                        # Technical documentation
│   └── DOCKER.md                # Detailed Docker architecture and networking guide
├── frontend/                    # Modern React / Vite / Tailwind engineering IDE
│   ├── src/
│   │   ├── components/          # IDE layout, Explorer, Tabs, StatusBar, Terminal
│   │   ├── lib/                 # Terminal session management & WebSocket hooks
│   │   └── types/               # TypeScript interfaces & API message schemas
│   └── package.json
├── src/cliq/
│   ├── connection/              # Device connectors (CML REST, SSH, Live Device)
│   ├── db/                      # SQLite command catalog & schema compilers
│   ├── intelligence/            # Deterministic engine, typo corrector, validators
│   ├── parser/                  # Lexer, tokenizer, abbreviation resolution
│   ├── ui/                      # Standalone CLI interface (prompt_toolkit / Rich)
│   └── web/                     # FastAPI backend & WebSocket terminal multiplexer
├── tests/                       # Unit tests and regression suites
│   └── unit/
├── scripts/                     # Operational launch and build automation scripts
├── requirements.txt             # Python dependencies
└── README.md                    # System documentation
```

---

## 16. Security & Safety Architecture

- **Strictly Advisory**: CLIQ functions as an advisor, not an autonomous agent. Commands are only dispatched to devices when explicitly submitted by the user.
- **Zero Cloud Leakage**: Keystroke parsing, ghost completions, and semantic retrieval execute entirely on-premise without communicating with third-party LLM providers.
- **Credential Hygiene**: Authentication tokens and secrets remain volatile in process memory. `.gitignore` rules prevent accidental commits of local tokens, credential dumps, and diagnostic bundles.
- **Air-Gapped Deployment**: All vector models and databases can be pre-cached to support classified or disconnected network environments.

---

## 17. Current Limitations & Roadmap

### Current Focus
- Comprehensive Cisco IOS/IOS-XE grammar coverage.
- CML 2.x and direct SSH console server integration.
- Single-instance multiplexed terminal sessions.

### Upcoming Milestones
- **Multi-Vendor Syntax Catalogs**: Expanding command databases to cover Arista EOS, Juniper Junos, and Enterprise SONiC.
- **Cross-Session Network Memory**: Contextual recollection of recently queried interfaces, IP subnets, and routing peers across sessions.
- **Topology Configuration Sync**: Two-way synchronization between visual topology links and underlying interface configurations.
- **Automated Golden-Config Auditing**: Real-time compliance scoring against corporate configuration standards as commands are typed.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
