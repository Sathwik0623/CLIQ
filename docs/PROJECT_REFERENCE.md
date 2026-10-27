# CLIQ — Project Reference (as of 2026-07-06)

> Single source of truth for AI-session context and project management. Compiled
> from source, repo-memory notes, and the phase decision docs. Line/count facts
> verified against the current tree (592 backend tests collected).

---

## 1. What CLIQ Is (one paragraph)

**CLIQ (Contextual Live Intelligence Quotient)** is a deterministic, offline-first
CLI-intelligence layer for Cisco network engineers. It sits between an engineer and
a live device (CML lab console over SSH, or a real device reached through a lab
terminal server over SSH) and provides VS Code–style ghost-text completion, mode-
and capability-aware validation, faithful "?" help passthrough, "Did you mean?"
corrections, safety gating for destructive commands, and an advisory "Ask CLIQ"
stack (KB enrichment + troubleshooting toolkits) — all **advisory-only** (CLIQ never
auto-runs a command; only the engineer's Enter submits). The hot path is pure Python
with no LLM and no network calls; the device is always the source of truth. It ships
as (a) a prompt-toolkit terminal client and (b) a FastAPI + WebSocket web backend
with a React/xterm.js frontend. Knowledge comes from a curated YAML command DB and
capability-tagged Cisco command-reference PDFs ingested into local SQLite.

---

## 2. Architecture (structure, entry points, request lifecycles)

### 2.1 Module / directory structure

```
src/cliq/
  __init__.py          # loads .env at import (load_env_file) BEFORE any submodule
  __main__.py          # `cliq` console script → cliq.ui.app:main
  config.py            # dependency-free .env loader (load_env_file/_parse_line)
  audit/               # audit_log.py (~/.cliq/audit.log), safety_gates.py
  connection/          # transport + device + CML/NetSim layer (see §4)
    transport.py           # DeviceTransport Protocol (runtime_checkable)
    live_device.py         # CMLConsoleDevice (SSH console proxy, netmiko-free read loop)
    ssh_console_device.py  # SSHConsoleDevice (real HW via terminal server, netmiko)
    session_manager.py     # lazy device connect, resumes mode from session_log
    session_health.py      # transport-agnostic death detection / reattach
    cml_client.py          # virl2_client 2.9.0 wrapper (labs/topology/version/events)
    cml_auth.py            # CMLAuthManager (login, token cache, reservation policy)
    cml_discovery.py       # lab/topology discovery helpers
    cml_events.py          # opt-in EventListener (CLIQ_CML_EVENTS=1)
    netsim_client.py       # READ-ONLY NetSim reservation status (cml.example.com)
    lab_connect.py         # connect orchestration
    device_connector.py    # connector abstractions
    output_cleaner.py      # console output cleanup helpers
  db/                  # build_db.py, models.py, query.py (CommandDB, in-memory)
  ingest/              # doc/PDF ingestion → capability-tagged KB (see §3.4)
    doc_parser.py, schema.py, readers.py, text_repair.py
    taxonomy.py, tree.py, pdf_ingest.py
    storage/{sqlite_store,vector_store,embeddings,pipeline}.py
  intelligence/        # hot-path + advisory reasoning (see §5)
    engine.py, suggester.py, arg_validator.py, typo_correction.py,
    error_explainer.py, did_you_mean.py, autofix.py, command_template.py,
    help_engine.py, risk_classifier.py, learned_commands.py, troubleshooting.py
  memory/              # SQLite-backed state (see §3.1)
    store.py (session_log), session_memory.py, operational_memory.py,
    command_cache.py, capture.py, redaction.py
  parser/              # tokenizer / expander / matcher (hot path primitives)
  retrieval/           # schema.py, intent_classifier.py, ranker.py, query.py, assistant.py
  ui/                  # terminal client: app.py, ghost_text.py, keybindings.py,
                       #   layout.py, panels.py, device_sidebar.py, stats_dashboard.py,
                       #   mock_device.py
  utils/               # observability.py, latency.py, credentials.py (EMPTY stub),
                       #   logging_setup.py (EMPTY stub)
  validation/          # schema.py, blocklist.py, platform_rules.py, validator.py,
                       #   hybrid_validator.py
  web/                 # server.py (FastAPI + WebSocket, ~2900 lines, the hub)

frontend/              # Vite + React 18 + TypeScript + Tailwind + xterm.js + reactflow
  src/App.tsx, main.tsx, index.css
  src/components/       # LoginStep, MainTerminalUi, etc.
  src/lib/             # api.ts, useTerminalSessions.ts
  src/types/api.ts     # WS + REST message contracts

command_db/            # YAML source of truth for the curated command DB
  arg_types.yaml, modes.yaml, parser_config.yaml, recipes.yaml, schema_version.yaml
  config/, exec/, snippets/, troubleshooting/*.yaml (one file per protocol)

kb/                    # ingested KB: sources/ios-xe/<category>/*.pdf, cliq_kb.sqlite,
                       #   cliq_vec.sqlite, ingest_report.json
tests/{unit,integration,fixtures}/
tools/                 # dev/ingest/validation CLIs (ingest_pdfs, verify_kb, etc.)
scripts/               # build.ps1/sh, run_web_backend.ps1, run_frontend.ps1, run_demo.ps1
docs/                  # architecture.md, command_db_schema.md, decisions/, this file
```

### 2.2 Entry points

| Surface | Command | Wiring |
|---|---|---|
| Terminal client | `python -m cliq.ui.app` (or `cliq`) | `__main__.py` → `cliq.ui.app:main`; `--live` swaps mock device for a real console backend |
| Web backend | `scripts/run_web_backend.ps1 [-ListenHost -ListenPort]` | `uvicorn cliq.web.server:app --reload`; default `127.0.0.1:8000` |
| Frontend (dev) | `scripts/run_frontend.ps1` / `cd frontend; npm run dev` | Vite dev server (React/xterm) |
| Frontend (build) | `cd frontend; npm run build` | `tsc -b && vite build` |
| DB build | `python -m cliq.db.build_db` | Builds `command_db.sqlite` from `command_db/*.yaml` |
| PDF ingest | `python tools/ingest_pdfs.py --dir kb\sources\ios-xe --refresh` | Capability-tagged KB into `kb/cliq_kb.sqlite` + `cliq_vec.sqlite` |
| Demo | `scripts/run_demo.ps1` | Guided tour |

Backend imports at module load: `.env` is loaded first (`src/cliq/__init__.py`), then
`server.py` reads DB paths and runs `_startup()` (loads command DB, ghost index,
learned store, retrieval assistant, logs reservation posture).

### 2.3 Request lifecycles

**(a) Login (`POST /api/auth`, CML)**
1. `normalize_cml_host(raw)` strips scheme/path → bare `host[:port]`; empty → HTTP 400.
2. `CMLAuthManager.login()` **fails closed**: with creds → always a live `_authenticate`
   (no cached-token fallback); on failure invalidates cached token and re-raises.
3. On success, `get_labs()`; then `_reservation_decision(auth)` detects reservation
   state via NetSim (`check_reservation_detail`) and applies `resolve_reservation_access`.
4. Failure branches → non-2xx `HTTPException` via `_reject(...)` with a distinct
   `errorKind`: `dns_failure` / `bad_credentials` / `reservation_expired` /
   `missing_api_key`/`key_invalid` / `backend_unreachable` / `invalid_host` / `auth_error`.
5. Success returns normalized `host`, optional `warning` (unverifiable warn-mode),
   and `unverified` flag. Obs `log_event("auth","login",...)`. Frontend `App.tsx`
   adopts normalized host; catch stays on login (no advance).

**(b) Device connect (WS `connect_device`, or `POST /api/ssh/connect` first)**
1. WS `/ws?token&host`; frontend `onopen` sends `connect_device` for each device.
2. `connect_device` computes `connection_type`/`log_key`/`prior`; if already in
   `runtimes` → returns. Sends `device_connecting` (latency mask).
3. **Console re-attach gate**: for CML re-attach with `prior` and `session.auth`,
   `_reservation_decision` must be `allowed` (NetSim authority) or the runtime is
   never registered and an `error_panel` is returned.
4. Constructs `CMLConsoleDevice` (SSH console proxy) or `SSHConsoleDevice`; calls
   `connect()`/`reattach()` in a thread. `InteractiveAuthRequired` → parks creds,
   sends `device_auth_required` (NX-OS `login:`/`Password:`).
5. `_finalize_connection` seeds `DeviceRuntime` (mode, node_type, capability),
   registers `_DEVICE_RUNTIMES[(token, device_id)]`, acquires `_CONSOLE_LOCKS`,
   sends `device_status connected`. Ghost index rehydrated from persistent cache.

**(c) Keystroke suggestion (WS `suggest`, per keystroke, deterministic)**
1. Frontend debounces ~90ms, bumps a `seq`, sends `{suggest, partial, deviceId, seq}`.
2. Backend `engine.analyze(partial, mode)` (Tier-1: tokenize → expand → match → typo
   → arg-validate) + `_ghost_complete(partial, device_id, mode, node_type, bias)`
   (device `?`-help tokens → KB/PDF/learned ghost index, next-token suffix only).
3. `_build_live_hint` returns at most one hint (do-hint / capability / typo suppressed
   mid-type). Emits `ghost_text` and/or `live_hint` echoing `seq`; frontend discards
   any message whose `seq != latest` (afterEnter hints exempt).

**(d) "Ask CLIQ" advisory (WS `ask`, or `POST /api/intel/ask`)**
1. REST path requires `require_session` (Bearer token vs `SESSIONS`, else 401).
2. Resolution hierarchy (see §5.2): device-owned runtime → persistent cache → KB
   enrichment (`retrieval/assistant.py`) → troubleshooting toolkit / discovery.
3. `_build_toolkit_advisory` + `_build_discovery_reference` run independent of the KB
   assistant; capability-gated by `runtime.capability`, rejected commands dropped.
4. Returns `intel_answer` (+ `toolkit`, `reference`, `active_context`); reconciled so
   the device always wins over KB (`_reconcile_qa_result`).

---

## 3. Data & State (DBs, in-memory, KB tree)

### 3.1 SQLite databases

| DB / file | Location (default) | Env override | Stores |
|---|---|---|---|
| Command DB | `command_db.sqlite` (built from YAML) | — (build target) | Curated commands/modes/arg-types (FTS5); loaded in-memory by `db/query.py` |
| KB (enrichment) | `kb/cliq_kb.sqlite` | `CLIQ_KB_DB` | PDF-ingested capability-tagged command nodes (~3300+ rows) |
| KB vectors | `kb/cliq_vec.sqlite` | `CLIQ_VEC_DB` | Semantic vectors for retrieval (LocalHashing or all-MiniLM-L6-v2) |
| Learned commands | `.cliq/learned_commands.sqlite` | `CLIQ_LEARNED_DB` | Device-accepted commands / accepted-fix learning |
| Persistent command cache | `.cliq/memory/command_cache.db` | (dir arg) | Two-tier device cmd + `?`-help cache, TTL 30d, source always `device` |
| Session log | `./session_log.sqlite` | — | Operational memory: per-session history, mode resume |
| Operational memory | `.cliq/memory/operational_memories.db` (+`_vectors.db`) | — | Past resolutions (Phase 6 recall) |
| CML token cache | `~/.cliq/token_cache.json` | — | Cached CML auth tokens (revalidated live before reuse) |
| CML host registry | `~/.cliq/cml_hosts.json` | — | Saved host config (NEVER passwords) |

Log files (JSON lines) live under `~/.cliq/`: `audit.log`, `cml.log`,
`cml_connection.log`, `cml_events.log`, `terminal.log`, `command.log`, `usage.log`,
`netsim_api.log`, `ingest.log`, `auth.log` (see §5 observability). Size-capped
rotation via `CLIQ_LOG_MAX_BYTES` / `CLIQ_LOG_MAX_BACKUPS`.

### 3.2 In-memory state (web/server.py) and lifecycle

| Name | Type | Lifecycle |
|---|---|---|
| `SESSIONS` | `dict[str, AuthSession]` | Created on `/api/auth` or `/api/ssh/connect`; SSH tokens popped on WS close |
| `DEVICES_BY_TOKEN` | `dict[token, dict[device_id, meta]]` | Per-token device metadata; SSH synthetic device added at connect |
| `_DEVICE_RUNTIMES` | `dict[(token, device_id), DeviceRuntime]` | **Keyed by (token, device_id)** for tenant isolation; registered at connect, popped on WS close / death / reattach-refuse |
| `_CONSOLE_LOCKS` | `dict[console_path, token]` | Exclusive console attach; acquired on connect, released on close/fail |
| `_ghost_index` | `_GhostIndex \| None` | Loaded at `_startup`; KB(tier0)/PDF(tier1)/learned(tier2), bisect prefix |
| `_help_cache` | `dict[str, _HelpContext]` | Learn-once `?`-help tokens keyed `device\x00mode\x00head`; session-persistent |
| `_DISCOVERY_WALK_CACHE` | `dict[str, list[str]]` | Per device+proto+PID `?`-walk cache for discovery |
| `_ghost_hydrated` | `set[str]` | Tracks which devices seeded ghost from persistent cache |

`DeviceRuntime` fields include: `device`, `mode`, `node_type`, `capability`
(frozenset of routing/switching), `rejected` (syntax-rejected cmds), `active_context`
(sticky protocol), `dead`/`dead_reason`/`empty_read_strikes`, per-device `lock`.

### 3.3 Capability gating (router / switch / l3-switch), end to end

- **Source (KB):** `ingest/taxonomy.py` maps PDF category → `capability_req` +
  `applies_to`. lan-switching → `[l2-switch, l3-switch]` (never a pure router);
  routing/services/mpls/wan → `[router, l3-switch]`; debug/mgmt/security/qos/system →
  `[all]`. Stored on each KB row; `tree.lookup` gates by `applies_to`
  (contains class OR `all` OR empty=legacy-universal).
- **Device (runtime):** `validation/platform_rules.py` `platform_capabilities(node_type)`
  → subset of `{routing, switching}` (`iosvl2`→switching, `iosxr/iosv`→routing,
  `iosxe/cat8000v/csr`→both, unknown→both/permissive). `capability_from_pid` /
  `node_type_from_pid` derive authoritative capability from `show version` PID for SSH.
- **Reconciliation:** device is truth. `_refine_capability` only **unions** command
  capability on accepted output (never removes). Advisory/toolkit/discovery suppress a
  capability only when `runtime.capability` is non-empty AND the requirement isn't in
  it (empty = permissive). CLIQ never hard-blocks a command the device would accept;
  truly invalid commands surface via the device's own `% Invalid input` (faithful
  passthrough) optionally plus a 💡 did-you-mean.

### 3.4 KB tree structure

Ingested via `tools/ingest_pdfs.py` → `pdf_ingest.PdfTreeIngestor`: classify (folder =
authoritative category) → parse → `tag_entries` (protocol per command via
`taxonomy.protocol_for_command`) → embed → upsert. `commands` table is additively
migrated with: `category, protocol, capability_req, applies_to(JSON), command_type
(default PI), source_pdf, source(default kb), os_family`. KB is **enrichment only** —
every node is `command_type=PI`, `source=kb`; the live device is always truth. Real
ingest to date: 5 IP-routing PDFs (ospf/bgp/eigrp/isis/multicast) + LAN-switching
(vlan/stp/etherchannel/vtp/…); bidirectional gate proven via `tools/verify_kb.py`
(router/ospf>0 & l2/ospf=0; l2/vlan>0 & router/vlan=0).

---

## 4. Transport & Device Layer

### 4.1 `DeviceTransport` protocol (`connection/transport.py`)

`runtime_checkable` structural Protocol (no inheritance). Surface both backends
conform to: `connect / reattach / complete_login / disconnect`, `execute`,
`execute_paged`, `pager_send_key`, `context_help_start / context_help_more`,
`_check_connection`; props `is_connected / current_prompt / awaiting_auth`; attr
`node_type`.

- **`CMLConsoleDevice`** (`live_device.py`): drives a CML node console over an SSH
  proxy with a **hand-rolled continuous read loop** (`_read_page` / `_read_until`),
  not netmiko `send_command`. Mode is parsed from the prompt string
  (`mode_from_prompt`). Streams cleaned deltas via `on_chunk`. Handles `--More--`
  paging, `?`-help capture (`_extract_context_help`), interactive auth
  (`InteractiveAuthRequired`).
- **`SSHConsoleDevice`** (`ssh_console_device.py`): real HW (e.g. ISR4331) via a lab
  terminal server. Connects as `terminal_server` + `redispatch` (avoids the
  `terminal width 511` auto-prep timeout on console lines), `_wake_console`, enable,
  `terminal length/width 0`. `node_type`/capability derived from PID
  (`show version`, fallback `running-config | include license udi`).

### 4.2 Echo-synchronized read + `^@` stripping (final state)

The SSH read path went through 13 phases; the **final** design (Phase 11–13) mirrors
CML exactly: `_stream_command(command, on_chunk)` writes the command **once**, then
loops `read_channel()`, NUL-strips at source (`chunk.replace("\x00","")`), and emits a
**monotonic prefix-guarded** clean delta (`_clean_stream = clean(_normalize_console_
output(...))`) so each byte is forwarded exactly once (stale-buffer replay is
structurally impossible). It breaks the instant `_PROMPT_RE` matches the last line — no
echo-gate, no grace, no stale-prompt machinery (those were removed as they blocked
real-HW echoes that don't match verbatim). `_normalize_console_output` resolves bare-`\r`
repaints (keeps text after the last `\r`) and strips one echo line (with/without a
space after the prompt). Defense-in-depth `^@`/`^C` display-boundary strip lives in
`server.py` (`_sanitize_console_display` at the stream/output chokepoints) and the
frontend (`stripDisplayCtrl` + a `term.write` wrapper). The frontend Phase-9 busy-gate
queue+replay was **removed** (Phase 13): keystrokes now pass straight through
(PuTTY-style), device echo is truth. CML path unchanged throughout.

### 4.3 Context-help (`?`), ghost-text, and did-you-mean reconciliation

- **`?` capture:** mid-line `?` sends `device_help`; device output parsed by
  `_extract_context_help` / `parse_help_options`. Faithful error passthrough
  (Phase 7b): `%`-lines are shown verbatim when there are no real options.
- **Ghost-text:** merged index resolver `_ghost_complete` — Tier-1 device `?`-help
  tokens (`_help_complete`, in-mem `_help_cache` + durable cache) win, then Tiers 2–4
  `_GhostIndex.best_completion` (KB/PDF/learned, next-token suffix only). Frontend
  `ghostFromHelp` is a parallel Tier-1 that wins on the client.
- **Did-you-mean:** corrections fire **after Enter** on device rejection
  (`analyze_device_error` in `did_you_mean.py`), never mid-type (typo hints are
  suppressed on-type). A rejected command is authoritative (`runtime.rejected`), so the
  advisory/RAG **defers to the device** (`_explain_command(device_rejected=True)` →
  `wrong_device`, no re-run). The engineer's Enter is the only thing that runs a fix.

---

## 5. Intelligence / Advisory Stack (endpoints, hierarchy, toolkits)

### 5.1 `/api/intel/*` endpoints (post-hardening)

All require `Authorization: Bearer <token>` via `require_session` (else **401**,
fail-closed). All are `POST` except `/health`.

| Endpoint | Input | Output | Notes |
|---|---|---|---|
| `/api/intel/ask` | question, device_platform/mode (+ owned runtime) | `intel_answer` items, `toolkit`, `reference`, `active_context` | Uses `_owned_runtime(token, device_id)`; emits `authz` obs event |
| `/api/intel/explain` | command, device context | verdict + suggestion | Device-rejected → `wrong_device`, defers to device |
| `/api/intel/validate` | command, mode, platform | `SafetyVerdict` | Offline validator (blocklist + mode); no device contact |
| `/api/intel/recall` | query | past resolutions | Operational memory (Phase 6) |
| `/api/intel/resolve` | query/context | resolution suggestion | `final_rank_score` similarity |
| `/api/auth` | host, username, password | token, host(normalized), warning?, unverified? | Fail-closed; reservation-gated |
| `/api/devices` | token | lab devices (+capability) | Host compared via `normalize_cml_host` |
| `/api/ssh/connect` | host, console_user, console_port, password | token + synthetic device | Password never echoed/logged |
| `/health` | — | `{status: ok}` | Liveness |
| `/ws?token&host` | WebSocket | terminal + suggest/ask/help stream | Main interactive channel |

Isolation fix: `_DEVICE_RUNTIMES` is keyed `(token, device_id)`; `_owned_runtime`
returns another session's runtime as `None`; the SSH handle is `ssh-{uuid4[:8]}`
(no bearer-token bytes). Frontend uses the WS (not these REST endpoints) for intel.

### 5.2 Live Device > Cache > KB > Recipe hierarchy (in code)

1. **Live device** (`DeviceRuntime` — `?`-help, accepted commands, `rejected` set) is
   always truth; `_refine_capability`/`_reconcile_qa_result` let it override KB.
2. **Persistent cache** (`memory/command_cache.py`) — device-sourced commands + `?`-help
   options, TTL 30d, PI keyed by device_type, PD keyed by platform; rehydrates ghost on
   connect. KB is never written here.
3. **KB enrichment** (`retrieval/assistant.py` over `retrieval/query.py`
   `CommandRetriever`) — tiered `exact|suggestion|related|fallback`; capability/platform
   filtered; enrichment only.
4. **Recipe / troubleshooting toolkit** (`intelligence/troubleshooting.py`) — bottom of
   the hierarchy, scenario-based advisory.

### 5.3 Troubleshooting toolkits (YAML) & intent detection

- **Data:** `command_db/troubleshooting/*.yaml`, one file per protocol (ospf.yaml
  today). Dropping `eigrp.yaml`/`bgp.yaml` adds a toolkit with **no code change**
  (loader globs `*.yaml`). Schema: `protocol, title, required_capability,
  intent{keywords, phrases, command_prefixes, protocol_aliases, triggers}, discovery,
  phases[{name, commands[{command, description, impactful}]}]`.
- **Load:** `load_toolkits(dir, refresh)` cached by dir; `CLIQ_TROUBLESHOOTING_DIR`
  overrides (default `./command_db/troubleshooting`).
- **Intent:** `detect_toolkit_intent(text, *, fuzzy=False)` is conservative — phrase
  substring OR `command_prefix` startswith OR (protocol alias AND a troubleshoot/working
  signal); optional difflib fuzzy ≥0.82. Bare "ospf" / "show version" / "configure vlan"
  do NOT fire. `detect_discovery_intent` separately drives command discovery ("<proto>
  commands" → toolkit; enumeration/scope word → discovery, Device>Learned>KB dedupe).
- **Server wiring:** `_build_toolkit_advisory` + `_build_discovery_reference`, gated by
  `runtime.capability`, drop `runtime.rejected`, sticky `active_context` (idle 900s),
  wired into both WS `ask` and REST `/api/intel/ask`.

---

## 6. Complete Environment Variables

| Variable | Controls | Default |
|---|---|---|
| `CLIQ_NETSIM_API_KEY` | Personal NetSim API key for reservation verification (rotates ~30d) | unset |
| `CLIQ_RESERVATION_POLICY` | `enforce` (refuse unverifiable) / `warn` (allow + banner) | `warn` |
| `CLIQ_ALLOW_UNVERIFIED_RESERVATION` | Legacy alias → `warn`; truthy (1/true/yes/on) | unset |
| `CLIQ_KB_DB` | KB SQLite path | `./kb/cliq_kb.sqlite` |
| `CLIQ_VEC_DB` | KB vector SQLite path | `./kb/cliq_vec.sqlite` |
| `CLIQ_LEARNED_DB` | Learned-commands SQLite path | `.cliq/learned_commands.sqlite` |
| `CLIQ_RECIPES` | Recipes YAML path | `./command_db/recipes.yaml` |
| `CLIQ_TROUBLESHOOTING_DIR` | Toolkit YAML dir | `./command_db/troubleshooting` |
| `CLIQ_CML_EVENTS` | Opt-in CML EventListener | `0` (off) |
| `CLIQ_TRACE_BYTES` | Verbose byte-dump DEBUG logs (live_device/ssh/server) | off |
| `CLIQ_LOG_MAX_BYTES` | Per-log rotation size cap | `2097152` (2 MiB) |
| `CLIQ_LOG_MAX_BACKUPS` | Rotation backups (.1/.2/.3) | `3` |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `HF_HUB_DISABLE_XET` | Set by `ingest/storage/embeddings.py` for offline HF | set to `1` internally |

Loading: `src/cliq/__init__.py` calls `load_env_file()` (dependency-free) at import;
a real shell env var **wins** over `.env` unless `override=True`. `~/.cliq/` is the
home for token cache, host registry, and logs.

---

## 7. Config Files

| File | Configures |
|---|---|
| `command_db/schema_version.yaml` | Command-DB schema version |
| `command_db/modes.yaml` | CLI modes (exec/enable/config/config-if/…) |
| `command_db/arg_types.yaml` | Argument type validators (IP, mask, interface, …) |
| `command_db/parser_config.yaml` | Parser: command verbs, `mode_context_labels`, etc. |
| `command_db/recipes.yaml` | Guided-config command recipes/snippets |
| `command_db/config/*.yaml`, `exec/*.yaml`, `snippets/*.yaml` | Curated command definitions per category |
| `command_db/troubleshooting/*.yaml` | Scenario toolkits (one per protocol) |
| `.env` / `.env.example` | Per-developer secrets/policy (git-ignored) |
| `pyproject.toml` | Package metadata, deps + extras (`dev`, `ingest`, `ingest-semantic`, `cml`), `cliq` script, pytest markers |
| `requirements.txt` | Pinned runtime deps |
| `frontend/package.json` + `tsconfig*.json` + `vite.config.ts` + `tailwind.config.*` | Frontend build |
| `kb/ingest_report.json`, `kb/sources/` | KB ingestion inventory + source PDFs |

---

## 8. Test Landscape (structure, count, conventions)

- **Count:** 592 tests collected across `tests/unit` + `tests/integration/test_latency.py`
  (verified 2026-07-06). Frontend gate = `tsc --noEmit` + `npm run build` (no JS test runner).
- **Run:** `$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m pytest tests/unit tests/integration/test_latency.py -q`
- **Unit files (`tests/unit/`, 30):** parser (`test_tokenizer`, `test_matcher`,
  `test_abbreviation`, `test_arg_validator`, `test_command_template`, `test_mode_tracker`),
  intelligence (`test_typo_correction`, `test_error_explainer`, `test_did_you_mean`,
  `test_risk_classifier`, `test_troubleshooting_toolkit`), retrieval (`test_retrieval_core`),
  ingest (`test_doc_parser`, `test_text_repair`, `test_ingest_storage`, `test_pdf_tree`),
  db (`test_db_query`), validation (`test_validation`), observability (`test_observability`),
  config (`test_config_env`), auth/security (`test_auth_hardening`, `test_cml_auth_diagnosis`,
  `test_console_reattach_gate`, `test_intel_authz`), device (`test_ssh_console`,
  `test_ssh_connect_endpoint`, `test_session_health`, `test_help_error_passthrough`),
  QA (`test_qa_device_reconcile`), and the omnibus `test_regression_bugs.py` (all phased bug fixes).
- **Integration (`tests/integration/`):** `test_latency.py` (streaming/latency, offline),
  `test_mock_session.py`, `test_cml_live.py` (opt-in `live` marker, needs real CML).
- **Fakes/mocks conventions:** offline everywhere. `_FakeSession` (scriptable CML
  post/get), `_EndpointAuth`/`_FakeAuth`/`FakeCMLDevice`/`_FakeAuth(state)` for auth &
  reservation, `FakeSSHDevice`/`_RecordingFakeSSH` and a fake netmiko channel for SSH,
  `_FakeTransport` for health probing. Auth tests use an autouse `_isolate_env` fixture
  that clears `CLIQ_NETSIM_API_KEY` / `CLIQ_RESERVATION_POLICY` /
  `CLIQ_ALLOW_UNVERIFIED_RESERVATION` and points `obs.CLIQ_HOME` / `_TOKEN_CACHE` at
  tmp. `_DEVICE_RUNTIMES` asserts must use the `(token, device_id)` tuple key. No
  pytest-asyncio: async fns are driven via `asyncio.run(...)` in sync tests.

---

## 9. Current Status (DONE / IN-PROGRESS / PLANNED)

**Phase:** Post–security-hardening cycle on a working web + terminal product (auth,
reservation policy, intel authz, and device isolation all landed). Roughly the
architecture roadmap's Phase 2–5 range, with a security/hardening overlay.

**DONE**
- Curated command DB + parser hot path (tokenize/expand/match/typo/arg-validate).
- Ghost text, pipe completion, mode- & capability-aware validation.
- CML integration (virl2_client 2.9.0): labs/topology/version/events/auth.
- NetSim read-only reservation verification + reservation policy (warn/enforce).
- CML console + real-HW SSH console transports (echo-sync, `^@` handling final).
- Faithful `?`-help passthrough, did-you-mean, config-mode `do`-hint, interactive auth.
- Validation/safety layer (blocklist + mode; capability now advisory-soft).
- Retrieval layer + KB PDF ingestion (routing + LAN-switching, capability-gated).
- Troubleshooting toolkit v2 (intent, sticky context, discovery) — OSPF shipped.
- Latency masking + unified observability + usage analytics seed.
- Session health monitor (mid-session death detection / degrade).
- Security: auth fail-closed chain, console re-attach gate, `/api/intel/*` authz,
  `_DEVICE_RUNTIMES` isolation, `.env` loader.

**IN-PROGRESS / PARTIAL**
- KB coverage: only routing + LAN-switching PDFs ingested; more categories pending.
- Toolkits: only `ospf.yaml` (schema supports drop-in eigrp/bgp with no code).
- Semantic embeddings: default hashing; all-MiniLM upgrade optional/behind extra.

**PLANNED (roadmap, not built)**
- LLM layer (Ollama offline first) for Tier-2 async explanations (never inline).
- RAG over broader Cisco docs (ChromaDB), auto-learning promotion pipeline.
- Multi-user web platform bits: PostgreSQL analytics, Redis session state, Grafana TL
  dashboard, Docker/Kubernetes deploy.

---

## 10. Open Bugs, TODOs & Deferred Decisions

- **NetSim multi-user key scope (deferred):** the NetSim API key is personal and
  30-day-rotating; hosting for multiple users needs a key-scope decision (see §15).
- **#5 token lifecycle:** CML token cache revalidated live before reuse, but there is
  no proactive refresh/expiry sweep — deferred.
- **#2 / #6 rate limiting:** no request rate limiting on `/api/auth` or `/api/intel/*`
  yet (relevant once hosted/multi-user).
- **KB cross-PDF collision:** a command in >1 PDF keeps only the last doc's protocol tag
  (one row per command+platform). Affects generic commands only.
- **Parser noise per-doc:** routing PDFs format differently than the tuned 9600 CR PDF,
  so a few example fragments can leak as "commands"; rejection heuristics could be
  tuned per-doc (not done).
- **SSH enable at connect:** may log "enable failed (continuing…)" on routers without an
  enable secret; benign, user enables via `execute("enable")`. Left as-is.
- **`show ip route` KB gap:** live `cliq_kb.sqlite` has no literal `show ip route` row,
  so that acceptance sample returns nearest suggestions unless the KB is enriched.
- **Green fakes ≠ real HW:** SSH fake tests can't reproduce terminal-server byte timing;
  real-device verification remains the acceptance criterion.

---

## 11. Empty Stubs / Incomplete Modules

- `src/cliq/utils/credentials.py` — **EMPTY** (intended OS-keyring credential storage).
- `src/cliq/utils/logging_setup.py` — **EMPTY** (logging is currently done ad-hoc via
  `utils/observability.py` + stdlib `logging.basicConfig`).
- Otherwise no empty modules found in `src/cliq/**` (verified by directory scan). Some
  roadmap folders from the original architecture plan (`llm/`, `deploy/`, `web_ui/`)
  do not yet exist — the LLM layer and deploy manifests are unbuilt.

---

## 12. Known Limitations / Constraints

- **Single-session consoles:** a CML/SSH console is exclusive; `_CONSOLE_LOCKS` refuses
  a second attach ("🔒 in use by another session"). No shared console.
- **SQLite concurrency:** all local stores are SQLite (WAL where writes are hot); fine
  single-user/single-process, but shared hosting needs a PersistentVolume or a real DB.
- **Personal-scoped NetSim keys:** verification is per-developer; without a key the
  default `warn` policy allows logins behind an explicit "UNVERIFIED ACCESS" banner
  (expired labs cannot be detected). `enforce` requires a key.
- **No LLM / offline by design:** the hot path is deterministic; there is no natural-
  language command generation yet (planned Tier-2 async).
- **Deterministic mode from prompt:** CLI mode is parsed from the prompt string, not a
  tracked stack; unusual prompts could mis-parse.
- **SSH has no reconnect story:** SSH tokens + password are dropped on WS close (no
  re-attach); CML has token-based reconnect.
- **Embeddings quality:** default LocalHashingEmbedder yields low semantic scores; the
  neural upgrade is optional and adds heavy deps.

---

## 13. Security Ledger (fixes + 6-question status)

### 13.1 Fixes applied this cycle (before → after)

1. **Auth 200-on-failure → fail-closed 401.** Before: `/api/auth` returned 200 even on
   bad creds and `login()` swallowed errors to `False`, allowing stale-token bypass.
   After: `login()` always does a live authenticate when creds are given, invalidates
   the cached token on failure, and re-raises; `/api/auth` raises a non-2xx
   `HTTPException` with a distinct `errorKind`.
2. **Host-with-scheme (`host='https'`).** Before: pasted URLs produced a double-prefixed
   host. After: `normalize_cml_host` strips scheme/path/query; empty → 400; frontend
   adopts the normalized host.
3. **Console re-attach auth bypass.** Before: an expired lab was still operable through
   CLIQ's own console SSH re-attach (a second door not gated by the reservation). After:
   `connect_device` gates re-attach on `_reservation_decision`/`check_reservation_state`
   (NetSim authority, fail-closed); refusal never registers the runtime.
4. **Reservation authority = NetSim, not CML controller.** Before: the controller stays
   "up" during the post-expiry grace window → false "active". After: NetSim portal is the
   authority; states `active|expired|unverifiable` with cause (`no_key`/`key_invalid`/
   `netsim_unreachable`); policy `warn` (default) vs `enforce`; verified-expired always
   refused; 30-day key-rotation classified distinctly + startup posture log.
5. **`/api/intel/*` had no auth + global `_DEVICE_RUNTIMES` leak.** Before: any client
   could read another session's runtime by guessing `device_id`; the SSH handle leaked 8
   bytes of the bearer token. After: `require_session` (Bearer → 401 fail-closed) on all
   intel endpoints; `_DEVICE_RUNTIMES` re-keyed `(token, device_id)`; `_owned_runtime`
   returns cross-session as `None`; SSH handle is `ssh-{uuid4[:8]}` (unlinked).
6. **Secret handling.** `.env` loader (never logged), NetSim key in-memory only and
   redacted, `redact()` scrubs key=value/Authorization/Bearer/Token/hex blobs, and the
   interactive-auth device-write log redacts passwords (`bytes=<redacted len=N>`).

### 13.2 6-question checklist status

1. **AuthN on every endpoint?** ✅ `/api/intel/*` + WS gated; `/api/auth` fail-closed;
   `/health` intentionally open.
2. **Tenant isolation of runtime state?** ✅ `_DEVICE_RUNTIMES` keyed `(token, device_id)`;
   cross-session reads return `None`.
3. **Reservation / lab-access enforcement?** ✅ NetSim-authoritative gate at `/api/auth`
   and console re-attach; expired always refused; policy configurable.
4. **Secret exposure (keys/tokens/passwords)?** ✅ redaction + in-memory-only key +
   `.env` git-ignored + SSH handle no longer leaks token bytes.
5. **Token lifecycle (rotation/expiry)?** ⚠️ **Partial** — live revalidation before
   reuse, NetSim key rotation classified; no proactive token refresh/expiry sweep.
6. **Rate limiting / abuse (DoS)?** ❌ **Open** — no rate limiting on auth or intel yet.

### 13.3 Outstanding security items (priority)

1. **High:** rate limiting on `/api/auth` (+ intel) before any shared hosting.
2. **Medium:** proactive token lifecycle (refresh/expiry sweep, revoke on logout at scale).
3. **Medium:** NetSim multi-user key scope decision (see §15) — today a personal key.
4. **Low:** move secrets to a vault (Conjur) for hosted deployment; keyring for local
   (`credentials.py` stub).

---

## 14. Hosting Prerequisites (RunOn / OpenShift)

**Required before hosting:**
1. **NetSim key + `CLIQ_RESERVATION_POLICY=enforce`** — set a valid `CLIQ_NETSIM_API_KEY`
   (and resolve multi-user scope, §15); enforce so expired/unverifiable labs are refused.
2. **PersistentVolume for shared SQLite DBs** — `kb/cliq_kb.sqlite`, `kb/cliq_vec.sqlite`,
   `.cliq/learned_commands.sqlite`, `.cliq/memory/*.db`, `session_log.sqlite`, and
   `~/.cliq/` logs must live on a PV (or migrate hot writes to Postgres/Redis per the
   architecture plan). SQLite concurrency limits apply if multiple pods share it.
3. **Secrets via Conjur (or platform secrets)** — inject `CLIQ_NETSIM_API_KEY` and any
   CML creds as secrets, not `.env`/image layers.
4. **Health checks** — wire `GET /health` as the liveness/readiness probe.
5. **Rate limiting** — add before multi-user exposure (§13.3).
6. **CORS / TLS / reverse proxy** — front `uvicorn` with the platform ingress (TLS
   termination); restrict origins; run without `--reload`.
7. **Log rotation sizing** — set `CLIQ_LOG_MAX_BYTES` / `CLIQ_LOG_MAX_BACKUPS` for the PV.
8. **Frontend build served statically** (`vite build` output) behind the ingress.

**Dev → hosted config deltas:**
- `--reload` dev server → production `uvicorn`/gunicorn workers, no reload.
- Bind `127.0.0.1` → `0.0.0.0` behind ingress (already parameterized in the script).
- `warn` → `enforce` reservation policy.
- Local `.env` → injected secrets.
- Local `~/.cliq` + CWD-relative SQLite → PV-backed paths via the `CLIQ_*_DB` env vars.
- Personal NetSim key → scoped/service key (§15).

---

## 15. Deferred: NetSim Multi-User Key Scope (Option A/B/C decision)

**Problem:** `CLIQ_NETSIM_API_KEY` is a **personal**, 30-day-rotating key tied to one
engineer's NetSim account. A single hosted CLIQ serving many engineers can't use one
person's key to verify everyone's reservations, and can't store each user's key safely
without a design decision. This gates §14 hosting with `enforce`.

**Options (decision pending):**
- **Option A — Per-user key passthrough.** Each engineer supplies their own NetSim key
  (entered per session, held in memory only). *Pro:* honest per-user authority, no shared
  secret. *Con:* UX friction, key rotation burden per user, must never persist server-side.
- **Option B — Single service/team key.** One scoped service account key verifies all
  reservations. *Pro:* zero user friction. *Con:* needs a NetSim service account with
  visibility into others' reservations (may not exist / policy), single rotation point,
  broad blast radius if leaked.
- **Option C — Hybrid (service key for verification, per-user for ownership).** Service
  key answers "is this lab active?"; user identity/ownership handled by CLIQ auth. *Pro:*
  balances friction and authority. *Con:* most implementation work; still needs a service
  key.

**Current default:** none chosen → runs `warn` (unverified access banner) without a key.
For hosted `enforce`, pick A, B, or C first.
