export type Lab = {
  id: string;
  title: string;
  state?: string;
  expires_at?: string | null;
  expiry_at?: string | null;
  expiresOn?: string | null;
};

export type Device = {
  id: string;
  label: string;
  type?: string;
  mode?: string;
  status?: string;
  cli_connected?: boolean;
  node_state?: 'BOOTED' | 'STARTED' | 'STOPPED' | 'STARTING' | string;
};

export type TopologyNodeData = {
  label: string;
  type?: string;
  node_state?: string;
  cli_connected?: boolean;
};

export type TopologyNode = {
  id: string;
  position: { x: number; y: number };
  data: TopologyNodeData;
};

export type TopologyLink = {
  id: string;
  label?: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
};

export type AuthResponse = {
  success: boolean;
  token: string;
  host?: string;
  labs: Lab[];
  error?: string;
  errorKind?: string;
  /** Non-blocking warning surfaced when the login proceeded without NetSim
   *  reservation verification (unverifiable + 'warn' policy). */
  warning?: string;
  /** True ONLY for an unverified session (reservation state could not be
   *  confirmed active or expired). Drives the persistent session indicator.
   *  Never true for a NetSim-verified login. */
  unverified?: boolean;
};

export type DevicesResponse = {
  devices: Device[];
  topology: {
    nodes: TopologyNode[];
    links: TopologyLink[];
  };
};

/** Response from POST /api/ssh/connect — one real device over an SSH console. */
export type SshConnectResponse = {
  success: boolean;
  token: string;
  host: string;
  connection_type: string;
  device: Device;
  error?: string;
};

export type LiveHint = {
  kind: 'typo' | 'wrong_mode' | 'missing_arg' | 'wrong_device' | 'destructive';
  message: string;
  fix?: string | null;
  fixDeviceId?: string | null;
  fixDeviceLabel?: string | null;
  // Tier 1 destructive-action confirmation: `danger` flags a red y/n prompt and
  // `ansiMessage` carries the backend-rendered ANSI-red line (bold red).
  danger?: boolean;
  ansiMessage?: string | null;
};

// ── Phase 6: Intelligence types ─────────────────────────────────────────────

export type IntelResult = {
  command: string;
  description: string;
  platform: string;
  mode: string;
  confidence: number;
  source?: string;
  match_type?: string;
  is_candidate?: boolean;
  /** Full DITA syntax notation (reference/display only — never insert this). */
  syntax_variants?: string[];
};

export type IntelGroup = {
  label: string;
  results: IntelResult[];
};

/** One ordered step in a setup sequence (config first, then verify). */
export type IntelSequenceStep = IntelResult & {
  kind: 'config' | 'verify';
};

/** One diagnostic command in a scenario-based troubleshooting toolkit. */
export type IntelToolkitCommand = {
  /** Command rendered for the current CLI mode (e.g. `do show ...` in config). */
  command: string;
  /** The bare command before any `do` prefix (used for copy/insert de-dup). */
  base_command: string;
  description: string;
  /** True for debug/impactful commands — flagged in the UI, never auto-run. */
  impactful: boolean;
  mode: string;
};

/** One diagnostic phase (ordered group of commands) in a toolkit. */
export type IntelToolkitPhase = {
  name: string;
  commands: IntelToolkitCommand[];
};

/** A curated, capability/mode-gated preliminary troubleshooting toolkit. */
export type IntelToolkit = {
  protocol: string;
  title: string;
  required_capability: string;
  /** Non-empty when the toolkit contains impactful (debug) commands. */
  safety_note: string;
  phases: IntelToolkitPhase[];
};

/** One command in a discovery reference, with source attribution (Feature 3). */
export type IntelReferenceCommand = {
  /** Command rendered for the current CLI mode. */
  command: string;
  base_command: string;
  description: string;
  /** True for debug/clear (impactful) commands — flagged with a ⚠ chip. */
  impactful: boolean;
  /** Highest-authority source: 'device' > 'learned' > 'kb'. */
  source: 'device' | 'learned' | 'kb';
  /** True only when device-confirmed; false = "unverified on this platform". */
  verified: boolean;
  group: string;
  mode: string;
};

/** A group (Show/Config/Clear/Debug) within a discovery reference. */
export type IntelReferenceGroup = {
  name: string;
  commands: IntelReferenceCommand[];
};

/** Live-validated command reference merged from Device + Learned + KB (Feature 3). */
export type IntelReference = {
  protocol: string;
  scope: string;
  title: string;
  groups: IntelReferenceGroup[];
  /** Which sources contributed (subset of device/learned/kb). */
  sources_present: string[];
  /** Non-empty when the reference contains impactful (debug/clear) commands. */
  safety_note: string;
  /** Non-empty when some commands are unverified on this platform / device offline. */
  unverified_note: string;
  device_available: boolean;
};

/** Sticky protocol context the engineer is actively working on (Feature 2). */
export type ActiveContext = {
  protocol: string;
  source: 'advisory' | 'cli';
  ts: number;
  unrelated?: number;
};

/** Plain-language explanation + safety verdict for a single command. */
export type IntelExplain = {
  command: string;
  description: string;
  source?: string;
  verdict: string;
  severity: 'info' | 'warning' | 'critical';
  reason: string;
  suggestion?: string | null;
};

export type PastResolution = {
  memory_id: string;
  issue_text: string;
  commands: string[];
  platform: string;
  device_name?: string | null;
  outcome: string;
  outcome_notes?: string | null;
  similarity: number;
};

/** A Q&A answer item surfaced in the intelligence panel. */
export type IntelAnswerItem = {
  kind: 'answer';
  id: string;
  question: string;
  tier: string;
  message: string;
  results: IntelResult[];
  groups: IntelGroup[];
  sequence?: IntelSequenceStep[];
  toolkit?: IntelToolkit | null;
  reference?: IntelReference | null;
};

/** An explain item (Fix/Explain affordance) surfaced in the panel. */
export type IntelExplainItem = {
  kind: 'explain';
  id: string;
} & IntelExplain;

/** A memory recall item surfaced in the intelligence panel. */
export type IntelMemoryItem = {
  kind: 'memory';
  id: string;
  trigger: 'ask' | 'recall';
  resolutions: PastResolution[];
};

/** A validation verdict item surfaced in the intelligence panel. */
export type IntelValidationItem = {
  kind: 'validation';
  id: string;
  command: string;
  verdict: string;
  severity: 'info' | 'warning' | 'critical';
  reason: string;
  suggested_action?: string | null;
};

export type IntelPanelItem = IntelAnswerItem | IntelMemoryItem | IntelValidationItem | IntelExplainItem;

// ── WebSocket protocol ───────────────────────────────────────────────────────

export type WsInbound =
  | { type: 'device_output'; device_id: string; output: string; prompt?: string; paging?: boolean }
  | { type: 'device_status'; device_id: string; mode?: string; latency?: number; status?: string; prompt?: string }
  // Session Health Monitor: the device session died mid-use (reservation
  // reclaimed / socket gone / re-auth demanded / persistent empty reads). Clean
  // notice over the EXISTING channel — surfaced in the same terminal.
  | { type: 'session_expired'; device_id: string; reason: string; prompt_last_seen?: string; message?: string }
  // Phase 3 latency masking: console attach in progress (~10s) / live output stream
  | { type: 'device_connecting'; device_id: string; label?: string }
  | { type: 'device_stream'; device_id: string; chunk: string }
  | { type: 'ghost_text'; device_id: string; text: string; seq?: number }
  // Native --More-- pager (Space=page, Enter=line, q=quit)
  | { type: 'pager_active'; device_id: string; prompt?: string; latency?: number }
  | { type: 'pager_done'; device_id: string; prompt?: string; mode?: string }
  | { type: 'live_hint'; device_id: string; hint: LiveHint | null; inputKey?: string; afterEnter?: boolean; command?: string; seq?: number }
  | { type: 'history_restore'; device_id: string; history: string[] }
  | { type: 'clear_warnings'; device_id: string; inputKey?: string; seq?: number }
  | { type: 'error_panel'; device_id: string; errorType: 'typo' | 'wrong_mode' | 'missing_arg' | 'wrong_device'; what: string; how: string; fixCommand?: string; inputKey?: string; quiet?: boolean }
  // Phase 6
  | { type: 'intel_answer'; device_id: string; question: string; tier: string; message: string; results: IntelResult[]; groups: IntelGroup[]; sequence?: IntelSequenceStep[]; toolkit?: IntelToolkit | null; reference?: IntelReference | null }
  | { type: 'active_context'; device_id: string; context: ActiveContext | null }
  | { type: 'intel_memory'; device_id: string; trigger: 'ask' | 'recall'; resolutions: PastResolution[] }
  | { type: 'intel_resolve_ack'; device_id: string; memory_id: string; issue_text: string }
  // Phase 7
  | { type: 'intel_explain'; device_id: string; command: string; description: string; source?: string; verdict: string; severity: 'info' | 'warning' | 'critical'; reason: string; suggestion?: string | null }
  | { type: 'device_help'; device_id: string; partial: string; output: string; more?: boolean; prompt?: string; isError?: boolean }
  // Destructive-action confirmation gate: the server HELD a destructive command
  // ('no ...', shutdown, clear, erase, reload, ...) pending an explicit y/n.
  // `ansiMessage` is the ANSI-red confirmation line naming the exact object.
  | { type: 'confirm_required'; device_id: string; command: string; message: string; ansiMessage?: string | null; reprompt?: boolean }
  // Gate resolved with 'n': the held command was discarded and NEVER sent.
  | { type: 'confirm_resolved'; device_id: string; outcome: 'discarded'; command: string; prompt?: string }
  // Bug 7: console requires interactive login — prompt the operator for creds.
  | { type: 'device_auth_required'; device_id: string; label?: string; stage?: 'username' | 'password'; message?: string; retry?: boolean }
  // In-session reconnect heartbeat: server echo of a client ping so a silently
  // half-dead browser socket can be detected before the operator types into it.
  | { type: 'pong'; ts?: number };

export type WsOutbound =
  | { type: 'connect_device'; device_id: string }
  | { type: 'command'; device_id: string; command: string; raw?: boolean }
  | { type: 'run_fix'; device_id: string; command: string }
  // Operator's answer to a held destructive command ('y' forwards it to the
  // device, 'n' discards it, anything else re-prompts). The answer is a single
  // keystroke; the gate decision is authoritative on the server.
  | { type: 'confirm_response'; device_id: string; answer: string }
  | { type: 'suggest'; device_id: string; partial: string; seq?: number }
  | { type: 'pager_key'; device_id: string; key: 'space' | 'enter' | 'q' }
  // Phase 6
  | { type: 'ask'; device_id: string; question: string }
  | { type: 'recall_memory'; device_id: string; query: string }
  | { type: 'mark_resolved'; device_id: string; issue_text: string; commands: string[]; platform: string; device_name?: string; outcome_notes?: string }
  // Feature 2 — sticky protocol context chip (set/change/clear)
  | { type: 'set_context'; device_id: string; protocol: string }
  | { type: 'clear_context'; device_id: string }
  // Phase 7
  | { type: 'explain'; device_id: string; command: string; platform?: string; mode?: string }
  | { type: 'device_help'; device_id: string; partial: string }
  // Bug 4: client-side render-order telemetry (forwarded to observability).
  | { type: 'client_event'; device_id: string; layer: string; op: string; outcome: string; detail?: string }
  // Bug 7: masked credentials for a parked interactive console login.
  | { type: 'auth_credentials'; device_id: string; username: string; password: string }
  // In-session reconnect heartbeat: periodic client liveness ping (echoed as pong).
  | { type: 'ping'; ts?: number };

