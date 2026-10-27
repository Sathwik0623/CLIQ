# CLIQ — Cisco CLI Intelligence

A deterministic CLI-intelligence layer for Cisco IOS. Word-aware abbreviation
expansion, mode-aware validation, contextual auto-fix, and ghost-text
completions — backed by a curated YAML command DB compiled to SQLite.

Runs fully offline against a mock device by default, and connects to real gear
— Cisco CML labs or physical hardware over an SSH terminal server — with the
same advisory-only intelligence layer driving both.

## Features

- **Ghost text** — gray inline suggestion of the most likely completion (Tab accepts)
- **Word-by-word abbreviation expansion** — `sh ip int br`, `conf t`, `copy run start`, `line con 0`
- **Context-aware expansion** — after `configure`, `t` resolves to `terminal` (not `trunk`); after `show ip`, `ro` → `route`
- **Mode-aware validation** — 9 modes: `exec`, `enable`, `config`, `config-if`, `config-subif`, `config-router`, `config-line`, `config-vlan`, `config-ext-nacl`
- **Advisories while typing and after Enter** — context-aware inline suggestions as you type, plus post-execution advisories that stay aware of the current CLI mode and intent
- **Prioritized advisory display** — surfaces the single most relevant advisory using a clear priority: destructive-action confirmation first, then mode/context guidance (including `do` prefix tips), then abbreviation expansion, then argument hints, then closest-match suggestions
- **Intelligent match protection** — closest-match suggestions preserve user intent and never swap a global command for an unrelated one; suggestions defer to higher-priority guidance when it applies
- **Full subinterface support** — `config-subif` mode recognizes interface-level commands (e.g. `ip address` / `no ip address`) as valid
- **Wrong-mode detection** — typed `sh ip int br` in config? Offered as `do sh ip int br`
- **Deterministic typo correction** — per-token Levenshtein, never auto-rewrites
- **Argument validation** — IPs, subnet masks, VLAN ranges, MTU bounds, allowed speed/duplex enums, with friendly error messages
- **Multi-step auto-fix** — wrong-mode commands trigger the right `configure terminal` → `interface <prompt>` → original command path, prompting for placeholder values
- **Destructive-action confirmation** — `reload`, `erase startup-config`, and `no …` style commands prompt for explicit confirmation, clearly naming the exact item being changed, and require a deliberate `y/n` before proceeding; CLIQ stays advisory-only and never acts on its own
- **In-session reconnect** — if the device connection drops, CLIQ preserves the session and offers a reconnect without a page reload or re-login; terminal history and mode context are restored, with automatic reconnect attempts, a manual reconnect option, and connection health monitoring to detect drops early
- **Graceful degradation** — when a session is dead or expired, CLIQ posts a clean in-terminal notice and keeps advisories running from learned history and the knowledge base until the device is back
- **Real device connectivity** — connect to Cisco CML labs or real hardware over an SSH terminal server; the same intelligence layer drives both, and `--live` falls back to a mock device cleanly
- **Interactive login handling** — console `login:` / `Username:` / `Password:` prompts are handled in-session; credentials stay in memory only and are never stored or logged
- **Device-as-truth validation** — advisory-only; CLIQ never blocks a command the device would accept, and surfaces the device's own error alongside a closest-match suggestion when something is rejected
- **Knowledge-base Q&A** — ask a natural-language question and get the closest verified Cisco IOS command(s), with tiered exact / suggestion / related / explore answers, powered by fully offline semantic retrieval
- **Scenario troubleshooting toolkits** — protocol-aware, phased command playbooks (e.g. OSPF: neighbor/adjacency → process/config → routing state → deeper diagnostics); impactful `debug` / `clear` steps are clearly flagged and inserted for review only, never auto-run; extensible by dropping a YAML file (EIGRP/BGP) with no code change
- **Command discovery / reference** — enumerate the valid commands for a protocol or scope, merged from the live device, your learned history, and the knowledge base, each source clearly labeled
- **Sticky protocol context** — CLIQ remembers the protocol you're working on and biases suggestions accordingly, without ever filtering out valid commands
- **Operational recall** — remembers recent IPs, VLANs, and last-used values to speed up `?` help and argument hints
- **Multi-device tabs + topology** — the web UI drives several device sessions at once with a rendered topology view
- **`do <cmd>` intelligence** — engine peels off `do`, re-analyzes target as `exec`/`enable`
- **Pipe completion** — `| inc`, `| sec`, `| b`, `| e` all complete to `include` / `section` / `begin` / `exclude`
- **`?` context help** — shows expected arguments, examples, related commands, last-used values
- **Bottom toolbar** — live status icon (✓ ⏳ ⚠ ✗) and rolling stats (typos prevented, mode errors caught, score, latency)
- **Demo mode** — guided tour cycling through every feature
- **`--live` flag** — falls back to mock cleanly when no device is reachable

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m cliq.db.build_db          # compile YAML -> command_db.sqlite
python -m cliq.ui.app                # run the shell (mock device)
```

## Web UI — single-port LAN hosting

The FastAPI backend can serve the built React/xterm frontend itself, so the
whole app (page + API + WebSocket) is reachable from **one process on one port**.
Anyone on the LAN needs only the link and a browser.

```powershell
# 1. Build the frontend once (outputs frontend/dist)
cd frontend
npm install
npm run build
cd ..

# 2. Start the backend bound to all interfaces
.\scripts\run_web_backend.ps1 -ListenHost 0.0.0.0 -ListenPort 8000
```

Then share `http://<your-machine-ip>:8000/` — it loads the full CLIQ UI, and the
frontend calls the API/WebSocket on the **same origin**, so it works from any
machine on the network (no `localhost` hardcoding, no "failed to fetch").

- Static build dir is `frontend/dist` by default; override with the
  `CLIQ_FRONTEND_DIR` environment variable.
- `/api/*`, `/ws`, and `/health` always take precedence; unknown client-side
  routes fall back to `index.html` (SPA refresh works).
- If the build dir is absent, the backend runs API-only (unchanged behavior).

**Local development is unaffected:** run `npm run dev` (Vite on `:5173`) as
before — a dev-server proxy forwards `/api` and `/ws` to the backend on `:8000`.

## Offline Model Cache (Required For Air-Gapped Deployments)

CLIQ semantic retrieval uses `sentence-transformers/all-MiniLM-L6-v2` in strict
offline mode. The model must be pre-cached in the image once, before running
CLIQ in disconnected environments.

One-time pre-cache step (run on a connected build machine):

```powershell
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

Copy the Hugging Face cache into your deployment image/profile:

- Windows: `%USERPROFILE%\.cache\huggingface\hub\models--sentence-transformers--all-MiniLM-L6-v2`
- Linux/macOS: `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2`

If the cache is missing, CLIQ fails loudly at startup with a clear message
instead of silently degrading to hashing embeddings.

## Repo layout

```
build_db.py                  # YAML -> SQLite compiler
command_db/                  # source-of-truth command definitions
  modes.yaml                 # CLI mode hierarchy
  arg_types.yaml             # regex/range/enum specs for argument values
  exec/  config/             # command YAML grouped by CLI mode
src/cliq/
  db/                        # SQLite + in-memory cache
  parser/                    # tokenizer, abbreviation expander, matcher
  intelligence/              # engine, typo correction, arg validator,
                             # error explainer, risk classifier, suggester
  ui/                        # prompt_toolkit shell, Rich panels, ghost text
  audit/                     # destructive-command gates, audit log
  memory/                    # operational recall (recent IPs/VLANs)
  connection/                # CML/SSH connectors (live mode)
tests/                       # unit + integration tests
```

## Numbers

- 64 commands across 9 modes
- 20 argument types (regex / range / enum)
- 6 destructive commands gated
- 1,717 knowledge-base entries for offline semantic retrieval
- Engine analysis runs in microseconds — no network or LLM in the hot path

## Architecture

```
YAML  ->  build_db  ->  SQLite  ->  CommandDB  ->  IntelligenceEngine  ->  CLIQShell
                                                                                 \
                                                                          live=True -> connection/* (CML lab / SSH terminal server)
                                                                          live=False -> mock canned outputs
```

See [docs/architecture.md](docs/architecture.md) and the ADRs under
[docs/decisions/](docs/decisions/).

## Status

| Layer | Status |
|---|---|
| YAML schema + builder | working |
| Parser (tokenizer, abbreviation, matcher) | working |
| Intelligence (engine, typo, arg validator, explainer) | working |
| UI (prompt_toolkit + Rich) | working |
| Mock device | working |
| Live CML connector | working |
| SSH terminal-server console (real hardware) | working |
| Knowledge-base semantic retrieval | working |
| Scenario troubleshooting toolkits | working |
| In-session reconnect + health monitoring | working |
| Persistent session memory | **stub** |

## License

MIT — see [LICENSE](LICENSE).
