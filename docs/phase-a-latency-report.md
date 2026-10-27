# Phase A — Latency & Observability Report

> Advisory-only, offline-first. All instrumentation is **additive** (measurement
> only) — no behavior change to terminal, ghost text, pager, recipes, 💡, the
> read-only NetSim layer, or CML connection logic.

## 1. Unified structured-logging schema (foundation)

A single helper — [src/cliq/utils/observability.py](../src/cliq/utils/observability.py)
— now backs **every** layer. Each record is one JSON line:

```json
{ "ts": "<UTC ISO>", "layer": "netsim|cml|terminal|command|usage|event",
  "op": "connect|run_command|get_projects|...", "stage": "auth|first_byte|complete|...",
  "outcome": "success|error|timeout|...", "status_code": null,
  "elapsed_ms": 12.3, "detail": "<redacted>" }
```

Per-layer files under `~/.cliq/` (size-capped, rotating `.1`/`.2`/`.3`):

| Layer | File | Emitted by |
|---|---|---|

| `cml` | `cml.log` | `cml_client` REST connect stages + **live console attach/reattach** stages, `list_labs`, `get_topology`, `auth_diagnosis` |
| `terminal` | `terminal.log` | **live per-command latency** (`live_device.execute` / `execute_paged`), `session_manager.connect` |
| `command` | `command.log` | reserved / legacy command-layer events |
| `usage` | `usage.log` | web server usage events (Phase H seed) |
| `event` | `cml_events.log` | optional `EventListener` |

**Secret safety:** `redact()` scrubs `key=value`, `Authorization: Token …`,
`Bearer …`, and 16+ char hex blobs from every `detail` and every extra kwarg;
secret-named keys (`api_key`, `password`, `token`, …) are replaced wholesale.
Logging is fully non-fatal (a write/serialization failure can never break a
connection or command). This extends the Phase 0.5 guarantee to all layers.

Rotation cap defaults: `CLIQ_LOG_MAX_BYTES=2 MiB`, `CLIQ_LOG_MAX_BACKUPS=3`.

## 2. CML cold-start breakdown (top priority)

`CMLClient.connect()` now emits a `StageTimer` breakdown (op `connect_stages`)
so the 5–10 s cold-start is attributable:

| Stage | What it measures | Typical cost |
|---|---|---|
| `reservation_check` | local expiry guard (cheap) | ~0 ms |
| `auth` | `ClientLibrary(...)` = host reachable + auth + initial controller handshake | **dominant** on cold start |
| `version_check` | `check_controller_version()` (cached after construction) | small |
| `complete` | total connect wall-time | sum |

**How to read it:** tail `~/.cliq/cml.log` and filter `op == "connect_stages"`.
The `auth` stage carries the bulk of the elapsed time on a cold connect because
the `virl2_client` constructor bundles TCP/TLS to the (possibly just-spun-up)
host, login, and the first version probe. A large `auth` with a small
`version_check` → cost is **host/reservation spin-up + auth**, not node boot. A
warm reconnect shows `auth` collapsing toward ~1 s — matching the observed
~5–10 s cold → ~1 s warm.

Node-boot vs. host-spin-up is further separated by the readiness signals
(`is_system_ready()`, `node.is_booted()`, `wait_until_lab_converged()` — the
latter logs `converge ok/fail` with `elapsed_ms`), so booting is measured via
real readiness rather than fixed sleeps.

### 2a. Live console attach breakdown (the real ~10 s cold-start)

The ~10 s a user actually feels is the **console-session open/attach** over the
CML terminal-server SSH proxy — *not* the virl2 REST auth above. This path lives
in `CMLConsoleDevice.connect()` / `reattach()` ([src/cliq/connection/live_device.py](../src/cliq/connection/live_device.py))
and now emits a `StageTimer` breakdown to `cml.log` (op `console_attach` /
`console_reattach`):

| Stage | What it measures |
|---|---|
| `auth` | SSH auth to the CML gateway + interactive shell setup |
| `console_open` | `open /<lab>/<node>/0` → CML proxy attaches the device console |
| `probe_prompt` | wake loop: blank-line nudges until a real IOS prompt appears (boot / "Press RETURN") |
| `prompt_ready` | normalize (enable / `terminal width 0`) → console ready |

**How to read it:** tail `~/.cliq/cml.log` and filter `op == "console_attach"`.
A large `probe_prompt` → the node is still booting; a large `console_open` →
proxy/terminal-server latency; a large `auth` → SSH/gateway round-trip. The
verb-free `detail` carries only the console path (`/lab/node/0`), never the
password.

**Recommended optimization (data-driven):**
- If `auth` dominates → cache/reuse the authenticated `ClientLibrary` across
  operations (avoid reconnecting), and warm the reservation before first use.
- If `converge`/node-boot dominates → drive UI off readiness events (§4) instead
  of blocking, and lazy-connect device consoles (§5) so boot overlaps usage.

## 3. Terminal & command latency

The **live** terminal path the WebSocket UI drives is `execute_paged()` (not
`execute()`), so it is the one instrumented for the real per-command latency.
Both paths emit request→first_byte→complete (op `run_command`, layer
`terminal` → `terminal.log`):

- `CMLConsoleDevice.execute_paged` (live web path) and `execute` reset a
  first-byte stamp, send, and read; `_read_page` / `_read_until` record the
  first received byte. Two records per command: `first_byte` (request→first
  byte) and `complete` (request→prompt/page). A `paged` flag marks commands
  that stopped at a `--More--` pager.
- `LiveDevice.execute` (Netmiko) logs `complete` (the library hides first-byte).
- Only the **command verb** is logged (never the full line) so credential-bearing
  commands (`username … password …`) never leak.
- The web `usage.log` `command_run` / `recipe_insert` events now also carry the
  measured `elapsed_ms`.

This gives a per-command baseline for the "slight latency" felt in the terminal:
a large `first_byte` points at device/console round-trip; a large gap between
`first_byte` and `complete` points at output volume / paging.

## 4. Real-time CML state via `EventListener` (feature-flagged)

[src/cliq/connection/cml_events.py](../src/cliq/connection/cml_events.py) adds an
optional `CMLEventMonitor` that subscribes to the controller event stream and
pushes normalized node/link/lab state changes to a callback — replacing polling.

- **Opt-in:** `CLIQ_CML_EVENTS=1` (`observability.event_listener_enabled()`).
  Default OFF → CLIQ keeps its validated polling / `sync_*_if_outdated` path.
- **Additive & safe:** never touches the terminal/console path; only updates an
  in-memory state cache. Disabling it (unset flag, or `.stop()`) is instant and
  clean — protecting the terminal path for A/B comparison.
- **Defensive:** the `virl2_client` event API has shifted across 2.x; the wrapper
  discovers entry points at runtime and degrades to a logged no-op
  (`outcome="unavailable"`) if absent.
- Each event logs handling latency (`layer="event"`, receipt→callback) so
  real-time-vs-polling can be compared directly.

### 4a. How to trigger & verify a real event

Events only appear when the controller actually emits one — i.e. a **lab/node
state change**, not merely opening a console. To produce and confirm one live:

1. Start CLIQ with the flag set: `$env:CLIQ_CML_EVENTS = "1"` before launching
   the backend.
2. Connect to the lab as usual.
3. In the **CML web UI** (or via `virl2_client`), **start or stop a node**
   (e.g. wipe/start `iosv-0`, or stop then start it). State transitions
   (`BOOTED` ↔ `STOPPED` / `DEFINED_ON_CORE`) are what the controller streams.
4. Verify a record is written:
   ```pwsh
   Get-Content ~/.cliq/cml_events.log -Tail 20
   ```
   A successful subscription logs `op="subscribe"` then one record per state
   change with `layer="event"` and an `elapsed_ms` (receipt→callback). If the
   installed `virl2_client` lacks the event API, you'll instead see a single
   `outcome="unavailable"` line and CLIQ silently stays on polling.

Note: link up/down and lab start/stop also emit events; a console command (e.g.
`show version`) does **not** — that is terminal traffic, not a controller event.

## 5. Large-topology performance

- **Lazy device sessions:** `SessionManager.add()` registers without connecting;
  `connect()` opens the console on first use (now timed, op `session_connect`).
  No eager fan-out connect.
- **Efficient refresh:** `get_topology` uses `sync_states_if_outdated()`
  (incremental) — no full reload — and times `sync_state`/`nodes`/`links`.
- **Load-test harness:** [tools/loadtest_topology.py](../tools/loadtest_topology.py)
  injects N fake nodes/links (no network) and times extraction.

Measured (this machine, best-of-3):

| nodes | links | best_ms | ms/node |
|---:|---:|---:|---:|
| 10 | 9 | 2.43 | 0.243 |
| 50 | 49 | 3.10 | 0.062 |
| 100 | 99 | 3.55 | 0.036 |
| 250 | 249 | 4.46 | 0.018 |
| 500 | 499 | 5.84 | 0.012 |
| 1000 | 999 | 12.86 | 0.013 |

Per-node time **falls** as the lab grows (fixed overhead amortizes) →
**linear, no lag growth**. The harness asserts the largest/smallest per-node
ratio ≤ 3× and exits non-zero otherwise (CI-friendly).

## 6. Usage analytics (Phase H seed)

Lightweight **local-only** events (`layer="usage"`, no external sync), emitted
from the web server:

| Event (`op`) | Where | Payload (redacted) |
|---|---|---|
| `session_start` | `POST /api/auth` | lab count only |
| `session_end` | WS disconnect | device count |
| `lab_opened` | `POST /api/devices` | lab id, device count |
| `command_run` | typed command exec | **verb only** |
| `recipe_insert` | Insert-button / config block (`raw=True`) | **verb only** |
| `ghost_accept` | optional `ghost_accept` WS message | **verb only** |

No full command text is logged; `redact()` is a backstop on every field. This
seeds the Phase H admin dashboard without building it now.

## 7. Friendly "reservation expired" on CML 401 (NetSim cross-check)

When a reservation expires, the CML host returns a bare
`401 Client Error: Unauthorized for url: https://<host>/api/v0/labs`. CLIQ now
translates that into a plain-language, actionable message by cross-checking the
read-only NetSim reservation API:

- `diagnose_cml_auth_failure(host)` ([src/cliq/connection/cml_client.py](../src/cliq/connection/cml_client.py))
  is called whenever `POST /api/auth` hits a 401/403 (fresh login rejected, or a
  cached token whose reservation has since expired surfacing on `GET /labs`).
- It calls `NetSimClient.find_by_cml_host(host)` (read-only, `X-API-Key`) and
  decides:
  | NetSim says | Message kind | What the user sees |
  |---|---|---|
  | reservation expired / in-grace / unavailable / **absent** | `expired` | *"Reservation for `<host>` has expired. Open the NetSim portal (cml.example.com) to extend or create a reservation. CLIQ is advisory-only…"* |
  | reservation **still active** | `auth_failed` | *"Authentication failed for `<host>` — credentials may be incorrect or the host is unavailable… NOT an expiry."* |
  | NetSim unreachable / no API key | `unverified` | honest *"…could not verify the reservation…"* — never guesses expiry |
- The friendly text + an `errorKind` is returned to the frontend instead of the
  raw stack trace. The NetSim API key (from `CLIQ_NETSIM_API_KEY`) is **never
  logged**; only the decision (`expired` / `auth_failed` / `unverified`) is
  written to `cml.log` as `op="auth_diagnosis"`.

**Config:** set `CLIQ_NETSIM_API_KEY` to enable the cross-check. Without it, CLIQ
still shows a clear (if unverified) message and points to the portal.

## Acceptance summary

| Acceptance test | Status |
|---|---|
| Every layer writes the unified schema; logs rotate; no secrets/keys | ✅ unit + integration tests |
| CML cold-start per-stage breakdown (REST + **live console attach**) | ✅ `connect_stages` + `console_attach`/`console_reattach` (auth/console_open/probe_prompt/prompt_ready) |
| Each command logs request→first_byte→complete to `terminal.log` (live path) | ✅ `terminal` layer via `execute_paged` |
| `usage.log` `command_run` carries non-null `elapsed_ms` | ✅ |
| `terminal.log` is created and rotates like other layers | ✅ same size-capped rotation |
| `EventListener` updates state + logs latency; disabling is clean; live trigger documented | ✅ feature-flagged, defaults off (§4a) |
| Large-topology fetch is lazy/efficient; no lag growth | ✅ load test (linear) |
| Usage events emitted locally with content redacted | ✅ 6 event types, verb-only |
| CML 401 → friendly "reservation expired" / distinct "auth failed" / honest "unverified" | ✅ NetSim cross-check (§7) |
| No regression (terminal, ghost, pager, recipes, 💡, NetSim) | ✅ unit tests pass |

## How to inspect

```pwsh
Get-Content ~/.cliq/cml.log -Tail 20         # REST connect + live console attach stages
Get-Content ~/.cliq/terminal.log -Tail 20    # per-command latency (live path)
Get-Content ~/.cliq/usage.log -Tail 20       # usage events (with elapsed_ms)
Get-Content ~/.cliq/cml_events.log -Tail 20  # real-time events (opt-in)
$env:CLIQ_CML_EVENTS = "1"                    # enable real-time events (opt-in)
$env:CLIQ_NETSIM_API_KEY = "<key>"           # enable friendly 401 cross-check
python tools/loadtest_topology.py            # large-topology load test
```
