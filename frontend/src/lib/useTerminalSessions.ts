import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Terminal } from 'xterm';
import { FitAddon } from 'xterm-addon-fit';
import 'xterm/css/xterm.css';
import type { Device, IntelPanelItem, LiveHint, WsInbound, WsOutbound } from '../types/api';

type DeviceMetrics = {
  mode: string;
  status: string;
  latency: number;
  typosCaught: number;
  commandsRun: number;
  ghostText: string;
};

type DeviceError = {
  type: 'typo' | 'wrong_mode' | 'missing_arg' | 'wrong_device';
  what: string;
  how: string;
  fixCommand?: string;
  inputKey?: string;
};

type ConfirmState = {
  fix: string;
  deviceId?: string | null;
  switchTo?: boolean;
};

type Session = {
  term: Terminal;
  fit: FitAddon;
  inputBuffer: string;
  history: string[];
  historyIdx: number;
  ghostLen: number;
  hintShown: boolean;
  confirm: ConfirmState | null;
  // Destructive-action confirmation gate. Non-null while a destructive command
  // ('no ...', shutdown, clear, erase, reload, ...) is HELD awaiting the
  // operator's y/n. While set, the NEXT keystroke is captured as the answer
  // (never echoed into the buffer) and routed to the server as confirm_response;
  // the command is forwarded to the device only on 'y'.
  awaitingConfirm: { command: string } | null;
  cursor: number;        // caret index within inputBuffer (0..length)
  promptLen: number;     // visible width of the current prompt + trailing space
  askMode: boolean;      // Phase 6: true while typing a '?' question
  paging: boolean;       // true while a native --More-- pager is active
  pendingBlock: string[]; // remaining config-block lines to review one-by-one
  rawInput: boolean;     // Bug 4: current line is inserted plain text — bypass
                         // command-recognition/ask; run as-is on the engineer's Enter
  helpTokens: string[];  // Tier 1: continuation tokens captured from device '?'-help
  helpHead: string;      // normalized (lower) command head those tokens continue
  connecting: boolean;   // Phase 3: true while the SSH console attach is in flight
  streaming: boolean;    // Phase 3: true while command output is streaming in live
  lastEnterAt: number;   // Bug 2: ts of the last Enter, to coalesce a CR/LF pair
  lastSubmitCmd: string; // Bug 2: last command sent, for the duplicate-submit guard
  lastSubmitAt: number;  // Bug 2: ts of the last submit, for the duplicate-submit guard
  helpPending: boolean;  // Bug 4: true between a mid-line '?' and the device's
                         // help reply — gates prompt redraws so the dangling
                         // C8k# can never render before the help body.
  helpPendingTimer: number | null; // Bug 4: safety net — auto-clears
                         // helpPending if the device_help reply never arrives
                         // so output can never be permanently frozen.
  everConnected: boolean; // Bug 9: true once the device has reached a
                         // 'connected' status at least once. The ⏳ Connecting
                         // banner is shown ONLY on the first attach; silent idle
                         // re-attaches (keepalive recovery) never inject it.
  authActive: boolean;   // Bug 10: AUTH_PASSTHROUGH — true while the device is
                         // parked at an interactive login/password prompt. All
                         // command validation, suggestions, ghost text and
                         // 'did you mean?' are disabled and terminal keystrokes
                         // are swallowed; credentials flow ONLY through the
                         // secure masked dialog (Bug 7), never the command line.
  authSecret: boolean;   // Bug 10: true when the active auth stage is a
                         // password — input must never be echoed or logged.
  busy: boolean;         // Inert completion marker only. The busy-gate is
                         // NEUTRALIZED: input is NEVER queued, gated, or
                         // replayed — keystrokes pass straight through (raw
                         // terminal) and the device echo is the source of truth.
  queuedInput: string[]; // Vestigial (always empty). Nothing is ever enqueued;
                         // kept only so residual call sites compile. No replay.
  busyTimer: number | null; // Vestigial: no safety timer is armed anymore.
  feedInput: ((data: string) => void) | null; // Vestigial: the key handler is
                         // registered directly on term.onData; never replayed.
  frozen: boolean;       // In-session reconnect: true once the device/browser
                         // socket has dropped. While frozen the terminal FREEZES
                         // input — keystrokes are neither echoed nor sent into a
                         // dead socket (fixes "prompt accepts commands but nothing
                         // happens"). Cleared on a successful reconnect (device
                         // reaches 'connected' again under the SAME session), which
                         // re-arms input without a page reload or re-login.
  disconnected?: boolean;
  intentionalExit?: boolean;
  disconnectedNoticeShown?: boolean;
  sessionGen?: string;
  lastSeq?: number;
};

// In-session reconnect banner state surfaced to the terminal UI. Sticky at the
// TOP of the terminal while a dropped session is being (auto/manually) resumed.
export type ReconnectInfo = {
  phase: 'retrying' | 'failed' | 'reconnected';
  attempt: number;
  maxAttempts: number;
  message: string;
};

const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
const DIM = '\x1b[2m';
const RESET = '\x1b[0m';

// ── In-session reconnect tuning ────────────────────────────────────────────
// Auto-reconnect backoff schedule (ms) — one entry per retry. Length defines
// the number of automatic attempts before falling back to a manual [Reconnect].
const RECONNECT_BACKOFFS_MS = [2000, 4000, 8000];
const RECONNECT_MAX_ATTEMPTS = RECONNECT_BACKOFFS_MS.length; // 3
// How long to wait for a reconnect attempt to report 'connected' before the
// attempt is considered failed and the next backoff step is scheduled. Sized
// above the ~10s SSH console attach so a slow-but-successful attach is not
// prematurely abandoned.
const RECONNECT_WATCHDOG_MS = 15000;
// Heartbeat: how often the client pings the server, and how long it waits for a
// pong before declaring the browser socket silently dead (→ freeze + reconnect).
const HEARTBEAT_INTERVAL_MS = 25000;
const HEARTBEAT_TIMEOUT_MS = 25000;

// ── Destructive-command detection (mirrors advisory_router.py) ─────────────
// Configurable/extensible list — add a verb here and to the backend
// DESTRUCTIVE_HEADS/PHRASES to broaden coverage. This is a LOCAL, deterministic
// hot-path check used ONLY to arm the keystroke gate synchronously on Enter (so
// the y/n answer is never echoed into the buffer during the round-trip). The
// server independently detects and HOLDS the command — it is the authority for
// whether the command ever reaches the device.
const DESTRUCTIVE_HEADS = new Set(['shutdown', 'clear', 'erase', 'reload', 'delete', 'format']);
const DESTRUCTIVE_PHRASES: string[][] = [['write', 'erase'], ['no', 'shutdown']];
function isDestructiveCommand(command: string): boolean {
  const toks = (command || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (toks.length === 0) return false;
  if (toks[0] === 'no') return true; // any negation removes config
  if (DESTRUCTIVE_HEADS.has(toks[0])) return true;
  for (const phrase of DESTRUCTIVE_PHRASES) {
    if (phrase.every((w, i) => toks[i] === w)) return true;
  }
  return false;
}

// ── Build the inline hint line (drawn one row below the input, demo-style) ──
function formatHintLine(hint: LiveHint): string {
  const YELLOW = '\x1b[33m';
  const RED = '\x1b[31m';
  const BOLD = '\x1b[1m';
  const NB = '\x1b[22m'; // bold off
  const DIM2 = '\x1b[2m';
  const RST = '\x1b[0m';
  // Phase 7 req 3: VS Code-style 💡 lightbulb inline at the error location, with
  // a Ctrl+. affordance to open the Fix/Explain overlay.
  const action = `${DIM2}  ·  Ctrl+. for Fix/Explain${RST}`;
  // Tier 1 destructive-action confirmation — render in bold terminal RED. Prefer
  // the backend-rendered ANSI line verbatim (it already names the exact object);
  // fall back to a locally-reddened message so the y/n prompt is never plain.
  if (hint.kind === 'destructive' || hint.danger) {
    const line = hint.ansiMessage
      ? hint.ansiMessage
      : `${BOLD}${RED}⚠ ${hint.message}${RST}`;
    return `${line}${action}`;
  }
  if (hint.kind === 'typo' && hint.fix) {
    return `${YELLOW}💡 Did you mean: ${BOLD}${hint.fix}${NB}?${RST}${action}`;
  }
  if (hint.kind === 'wrong_device') {
    return `${RED}💡 ${hint.message}${RST}${action}`;
  }
  if (hint.kind === 'wrong_mode') {
    return `${YELLOW}💡 ${hint.message}${RST}${action}`;
  }
  return `${YELLOW}💡 ${hint.message}${RST}${action}`;
}

// ── Priority 1: Tier 1 ghost text from device '?'-help tokens ──────────────
// The LIVE device's own context help is authoritative for what it supports
// (the KB/PDF is a different IOS train). When the engineer presses '?', we
// capture the continuation tokens; their next keystrokes' LAST token is then
// completed from those tokens BEFORE the KB/RAG/learned index.
//
// Every stage is logged ([GHOST] (a) parse, (b) last-token, (c) match,
// (d) suffix, (e) render) so the exact break is visible in the console.
function parseHelpTokens(helpText: string): string[] {
  const tokens: string[] = [];
  const seen = new Set<string>();
  for (const rawLine of (helpText || '').split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    // IOS help is 'keyword<2+ spaces>description' (sometimes several keyword
    // columns). A column with no internal whitespace is a keyword; a column
    // with whitespace is prose and is skipped. '<cr>', 'A.B.C.D' etc. skipped.
    for (const col of line.split(/\s{2,}/)) {
      const tok = col.trim();
      if (!tok || /\s/.test(tok)) continue;
      if (!/^[A-Za-z][\w-]*$/.test(tok)) continue;
      const low = tok.toLowerCase();
      if (!seen.has(low)) {
        seen.add(low);
        tokens.push(tok);
      }
    }
  }
  // eslint-disable-next-line no-console
  console.debug('[GHOST] (a) parsed %d help token(s): %o', tokens.length, tokens);
  return tokens;
}

function ghostFromHelp(tokens: string[], head: string, partial: string): string {
  if (!tokens.length || !partial) return '';
  const words = partial.split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  const trailingSpace = /\s$/.test(partial);
  const headWords = trailingSpace ? words : words.slice(0, -1);
  const last = trailingSpace ? '' : words[words.length - 1].toLowerCase();
  const cachedHead = head.split(/\s+/).filter(Boolean);
  // eslint-disable-next-line no-console
  console.debug('[GHOST] (b) last-token=%o head=%o cachedHead=%o', last, headWords, cachedHead);
  if (headWords.length !== cachedHead.length) {
    // eslint-disable-next-line no-console
    console.debug('[GHOST] (c) head length mismatch -> no help match');
    return '';
  }
  // Abbreviation-tolerant: each typed head word is a prefix of the cached
  // canonical word OR vice-versa ('sh' <-> 'show').
  for (let i = 0; i < headWords.length; i++) {
    const tw = headWords[i].toLowerCase();
    const cw = cachedHead[i].toLowerCase();
    if (!(cw.startsWith(tw) || tw.startsWith(cw))) {
      // eslint-disable-next-line no-console
      console.debug('[GHOST] (c) head mismatch at %d: %o vs %o', i, tw, cw);
      return '';
    }
  }
  const cands = tokens.filter(
    (t) => t.toLowerCase().startsWith(last) && t.toLowerCase() !== last,
  );
  // eslint-disable-next-line no-console
  console.debug('[GHOST] (c) candidates=%o', cands);
  if (!cands.length) return '';
  // Single best: shortest (most specific), tie-broken alphabetically.
  cands.sort((a, b) => a.length - b.length || a.toLowerCase().localeCompare(b.toLowerCase()));
  const suffix = cands[0].slice(last.length);
  // eslint-disable-next-line no-console
  console.debug('[GHOST] (d) best=%o suffix=%o', cands[0], suffix);
  return suffix;
}

// Control-char artifacts that must never render on xterm.js: NUL (^@, \x00),
// ETX (^C, \x03) and the other non-printing C0 controls a laggy terminal server
// can spray. \t \n \r are legitimate layout and are preserved.
// eslint-disable-next-line no-control-regex
const DISPLAY_CTRL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

function stripDisplayCtrl(text: string): string {
  return text ? text.replace(DISPLAY_CTRL_RE, '') : text;
}

function normalizeDeviceOutput(output: string, prompt: string): string {
  // Absolute last display boundary (defense-in-depth): strip control-char
  // artifacts (^@/NUL, ^C/ETX, other C0) so none can ever render on xterm.js,
  // even if a byte slipped past the backend guard. \t \n \r kept for layout.
  let text = stripDisplayCtrl(output ?? '');
  const cleanPrompt = prompt.trim();

  if (cleanPrompt) {
    // Some devices echo the prompt at the end of output; strip repeated tails.
    while (text.trimEnd().endsWith(cleanPrompt)) {
      text = text.trimEnd().slice(0, -cleanPrompt.length);
    }
  }

  // Normalize all line endings, then emit CRLF for xterm rendering consistency.
  const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  return normalized
    .split('\n')
    .map((line) => line.replace(/\s+$/g, ''))
    .join('\n')
    .trimEnd();
}

export function useTerminalSessions(
  host: string,
  token: string,
  devices: Device[],
  onRequestSwitch?: (deviceId: string) => void,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const socketGenRef = useRef<number>(0);
  const outboundQueueRef = useRef<WsOutbound[]>([]);
  const sessionsRef = useRef<Map<string, Session>>(new Map());
  const mountRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const ghostRef = useRef<Record<string, string>>({});
  const promptRef = useRef<Record<string, string>>({});
  const hintRef = useRef<Record<string, LiveHint | null>>({});
  const switchRef = useRef<typeof onRequestSwitch>(onRequestSwitch);
  switchRef.current = onRequestSwitch;
  const suggestTimerRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // Bug 4a — per-device keystroke sequence for in-flight suggestion cancel.
  // `latest` is bumped on EVERY keystroke; the debounced send carries that seq.
  // ghost_text/live_hint responses whose seq != the latest are stale (a newer
  // keystroke has since fired) and are DISCARDED so old ghosts never clobber
  // the current line.
  const suggestSeqRef = useRef<Record<string, number>>({});
  const [metrics, setMetrics] = useState<Record<string, DeviceMetrics>>({});
  const [errors, setErrors] = useState<Record<string, DeviceError[]>>({});
  const [liveHints, setLiveHints] = useState<Record<string, LiveHint | null>>({});
  // Phase 6: per-device intelligence panel items (Q&A answers, memory recalls)
  const [intelItems, setIntelItems] = useState<Record<string, IntelPanelItem[]>>({});
  // Feature 2: per-device sticky protocol context (server-authoritative mirror).
  const [activeContext, setActiveContext] = useState<
    Record<string, import('../types/api').ActiveContext | null>
  >({});
  // Bug 7: per-device interactive-login prompt. When set, the UI shows a secure
  // masked credential dialog for that device. Credentials are sent over the WS
  // and never retained in this state after submit.
  const [authPrompt, setAuthPrompt] = useState<
    { deviceId: string; label: string; message: string; retry: boolean; stage?: string; stageType?: string } | null
  >(null);
  // In-session reconnect — per-device sticky banner shown at the TOP of the
  // terminal while a dropped session is being resumed. null = healthy/hidden.
  const [reconnectBanners, setReconnectBanners] = useState<
    Record<string, ReconnectInfo | null>
  >({});
  // In-session reconnect — per-device auto-reconnect controller (timers +
  // attempt counter). Refs (not state) so the retry loop reads live values
  // without stale-closure hazards.
  const reconnectRef = useRef<
    Record<string, { attempt: number; timer: number | null; watchdog: number | null; active: boolean }>
  >({});
  // In-session reconnect — bumping this re-runs the WS effect, tearing down the
  // dead socket and opening a fresh one under the SAME token/host (no re-login).
  const [wsEpoch, setWsEpoch] = useState(0);
  // Phase 7 req 3: inline lightbulb overlay (VS Code-style Fix/Explain at cursor).
  const lightbulbRef = useRef<Record<string, { command: string; fix?: string } | null>>({});
  const [lightbulbOverlay, setLightbulbOverlay] = useState<
    { deviceId: string; command: string; fix?: string; top: number; left: number } | null
  >(null);
  // Phase 7 req 3 — the always-visible, clickable 💡 gutter marker anchored
  // inline to the terminal cursor/prompt line on .xterm-screen. Distinct from
  // lightbulbOverlay (the Fix/Explain popup the marker opens).
  const [lightbulbAnchor, setLightbulbAnchor] = useState<
    { deviceId: string; top: number; left: number; height: number } | null
  >(null);
  // Which device terminal is currently focused/active — the 💡 marker is only
  // rendered for this one so it never floats over a hidden tab.
  const focusedDeviceRef = useRef<string>('');

  const deviceMap = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices]);

  function clearGhost(session: Session) {
    if (session.ghostLen > 0) {
      session.term.write('\x1b[K'); // erase from cursor to end of line
      session.ghostLen = 0;
    }
  }

  // ── Inline hint drawn one row BELOW the input line (matches the CLI demo).
  // Uses DECSC/DECRC (ESC 7 / ESC 8) to save the cursor at the end of the input,
  // drop to the line below, paint the hint, then return to the input line so the
  // user keeps typing seamlessly.
  function clearHintBelow(session: Session) {
    if (!session.hintShown) return;
    session.term.write('\x1b7\r\n\x1b[2K\x1b8');
    session.hintShown = false;
  }

  function drawHintBelow(session: Session, text: string) {
    if (session.hintShown) {
      session.term.write('\x1b7\r\n\x1b[2K\x1b8');
      session.hintShown = false;
    }
    if (!text) return;
    session.term.write(`\x1b7\r\n\x1b[2K${text}\x1b8`);
    session.hintShown = true;
  }

  function showGhost(session: Session, ghost: string) {
    clearGhost(session);
    if (ghost) {
      session.term.write(`${DIM}${ghost}${RESET}`);
      session.term.write(`\x1b[${ghost.length}D`); // move cursor back
      session.ghostLen = ghost.length;
    }
  }

  // Drop any executed-command warnings shown for this device. Returns the
  // same state reference when already empty so React skips a re-render — this
  // is called on every keystroke for instant "stale warning" clearing.
  function clearDeviceErrors(deviceId: string) {
    setErrors((current) => {
      if (!current[deviceId] || current[deviceId].length === 0) return current;
      return { ...current, [deviceId]: [] };
    });
  }

  // Phase 7 req 3 — inline lightbulb helpers.
  function clearLightbulb(deviceId: string) {
    if (lightbulbRef.current[deviceId]) lightbulbRef.current[deviceId] = null;
    setLightbulbOverlay((cur) => (cur && cur.deviceId === deviceId ? null : cur));
    setLightbulbAnchor((cur) => (cur && cur.deviceId === deviceId ? null : cur));
  }

  // Compute the viewport (fixed-position) coordinates of the terminal cursor by
  // anchoring to the .xterm-screen element — NOT the right panel. Shared by the
  // always-visible 💡 marker and the Fix/Explain overlay so both sit inline at
  // the prompt line (near R1#).
  function cursorViewportCoords(
    session: Session,
  ): {
    rowTop: number; rowLeft: number; cursorLeft: number; cellH: number; panelLeft: number; mount: string;
  } | null {
    const term = session.term;
    const screenEl = term.element?.querySelector('.xterm-screen') as HTMLElement | null;
    if (!screenEl) return null;
    const rect = screenEl.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const cellW = rect.width / term.cols;
    const cellH = rect.height / term.rows;
    const cy = term.buffer.active.cursorY; // viewport-relative row
    const cx = term.buffer.active.cursorX;
    const rowTop = rect.top + cy * cellH;
    // rowLeft = column 0 of the active row (start of the device prompt).
    const rowLeft = rect.left;
    // panelLeft = the dark terminal container's left edge. Change 3: the 💡 must
    // be clamped to this so it can NEVER bleed onto the white topology panel.
    const panelRect = (term.element as HTMLElement | undefined)?.getBoundingClientRect();
    const panelLeft = panelRect ? panelRect.left : rect.left;
    // cursorLeft = one cell right of the caret (used by the Fix/Explain popup).
    const cursorLeft = rect.left + Math.min(cx + 1, Math.max(0, term.cols - 2)) * cellW;
    return { rowTop, rowLeft, cursorLeft, cellH, panelLeft, mount: screenEl.className || '(.xterm-screen)' };
  }

  // Position (or hide) the always-visible 💡 marker. Per the spec it is pinned
  // to the START of the active prompt row — immediately BEFORE the device prompt
  // (e.g. "💡C8k#"), like VS Code's gutter lightbulb — NOT at the cursor column,
  // so it never lands mid-text however much the engineer has typed.
  function updateLightbulbAnchor(deviceId: string) {
    const armed = lightbulbRef.current[deviceId];
    const focused = focusedDeviceRef.current === deviceId;
    if (!armed || !focused) {
      setLightbulbAnchor((cur) => (cur && cur.deviceId === deviceId ? null : cur));
      return;
    }
    const session = sessionsRef.current.get(deviceId);
    if (!session) return;
    const c = cursorViewportCoords(session);
    if (!c) {
      // eslint-disable-next-line no-console
      console.debug('[LIGHTBULB] anchor skipped — no .xterm-screen coords for %s', deviceId);
      return;
    }
    // Sit the marker in a small gutter just left of column 0 so its right edge
    // butts up against the prompt's first character — but Change 3: clamp it to
    // the dark terminal panel's left edge so it never bleeds onto the white
    // topology panel (legibility + correct placement on any layout).
    const markerW = Math.max(c.cellH, 14);
    const left = Math.max(c.panelLeft, c.rowLeft - markerW);
    // eslint-disable-next-line no-console
    console.debug(
      '[LIGHTBULB] anchor device=%s rowTop=%.1f left=%.1f (rowLeft=%.1f) cellH=%.1f mount=%o',
      deviceId, c.rowTop, left, c.rowLeft, c.cellH, c.mount,
    );
    setLightbulbAnchor({ deviceId, top: c.rowTop, left, height: c.cellH });
  }

  // Open the Fix/Explain overlay just below the prompt line (Ctrl+. / click).
  function openLightbulbOverlay(deviceId: string) {
    const lb = lightbulbRef.current[deviceId];
    if (!lb) return;
    const session = sessionsRef.current.get(deviceId);
    if (!session) return;
    const c = cursorViewportCoords(session);
    const top = c ? c.rowTop + c.cellH : 140;
    const left = c ? c.cursorLeft : 140;
    // eslint-disable-next-line no-console
    console.debug('[LIGHTBULB] overlay open device=%s top=%.1f left=%.1f', deviceId, top, left);
    setLightbulbOverlay({ deviceId, command: lb.command, fix: lb.fix, top, left });
  }

  function scheduleSuggest(deviceId: string, partial: string) {
    const existing = suggestTimerRef.current[deviceId];
    if (existing) clearTimeout(existing);
    // Bump the sequence on every keystroke so any suggestion response still in
    // flight for a PRIOR keystroke is recognised as stale on arrival and
    // discarded (Bug 4a — cancel in-flight requests).
    const seq = (suggestSeqRef.current[deviceId] ?? 0) + 1;
    suggestSeqRef.current[deviceId] = seq;
    if (!partial.trim()) {
      ghostRef.current[deviceId] = '';
      hintRef.current[deviceId] = null;
      const s = sessionsRef.current.get(deviceId);
      if (s) {
        clearGhost(s);
        clearHintBelow(s);
      }
      clearLightbulb(deviceId);
      setLiveHints((current) => ({ ...current, [deviceId]: null }));
      setMetrics((current) => ({
        ...current,
        [deviceId]: { ...(current[deviceId] ?? defaultMetrics()), ghostText: '' },
      }));
      return;
    }
    suggestTimerRef.current[deviceId] = setTimeout(() => {
      // ~90ms debounce keeps typing snappy while collapsing bursts into one
      // request; seq lets the response be matched to the current keystroke.
      send({ type: 'suggest', device_id: deviceId, partial, seq });
    }, 90);
  }

  function ensureSession(deviceId: string): Session {
    const cached = sessionsRef.current.get(deviceId);
    if (cached) return cached;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      // Use a widely-available monospace. This MUST match the actually-rendered
      // font (no CSS override on .xterm-rows) or xterm's measured cell geometry
      // diverges from the painted glyphs and clips the first column.
      fontFamily: "'Consolas', 'Courier New', ui-monospace, SFMono-Regular, monospace",
      theme: {
        background: '#000000',
        foreground: '#00e676',
        cursor: '#00e676',
        selectionBackground: '#1a4d1a',
        black: '#000000',
        brightBlack: '#1a1a1a',
        red: '#ff5252',
        brightRed: '#ff6e6e',
        green: '#00e676',
        brightGreen: '#69ff47',
        yellow: '#ffff00',
        brightYellow: '#ffff8d',
        blue: '#40c4ff',
        brightBlue: '#80d8ff',
        magenta: '#ea80fc',
        brightMagenta: '#f4b8ff',
        cyan: '#18ffff',
        brightCyan: '#a7ffeb',
        white: '#b2dfdb',
        brightWhite: '#e0f2f1',
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    // ── Write boundary (belt-and-suspenders) ─────────────────────────────
    // NUL (^@, \x00) is NEVER legitimate display content; a telnet-style
    // terminal server can pad line-ends with it. Strip it at the SINGLE xterm
    // write chokepoint so a ^@ can never render, no matter which path (device
    // echo, stream, or a stray byte) produced it. Every other byte passes
    // through raw so the device echo remains the source of truth.
    const rawWrite = term.write.bind(term);
    term.write = ((data: string | Uint8Array, cb?: () => void) => {
      // TEMP TRACE (diagnostic): every byte handed to xterm.js, BEFORE the NUL
      // strip — proves whether \x00/^@ ever reaches the terminal write
      // chokepoint, and (by correlating with WS_RECV) whether it was device-sent
      // or generated locally at submit.
      {
        const s =
          typeof data === 'string'
            ? data
            : Array.from(data).map((b) => String.fromCharCode(b)).join('');
        // eslint-disable-next-line no-console
        console.log('XTERM_WRITE:', JSON.stringify(s), 'hex:', [...s].map((c) => c.charCodeAt(0)));
      }
      if (typeof data === 'string' && data.indexOf('\x00') !== -1) {
        data = data.split('\x00').join('');
      }
      return rawWrite(data as string, cb as (() => void) | undefined);
    }) as typeof term.write;
    const session: Session = {
      term, fit, inputBuffer: '', history: [], historyIdx: -1,
      ghostLen: 0, hintShown: false, confirm: null,
      cursor: 0, promptLen: 0, askMode: false, paging: false,
      awaitingConfirm: null,
      pendingBlock: [],
      rawInput: false,
      helpTokens: [], helpHead: '',
      connecting: false, streaming: false,
      lastEnterAt: 0, lastSubmitCmd: '', lastSubmitAt: 0,
      helpPending: false,
      helpPendingTimer: null,
      everConnected: false,
      authActive: false,
      authSecret: false,
      busy: false,
      queuedInput: [],
      busyTimer: null,
      feedInput: null,
      frozen: false,
      disconnected: false,
      intentionalExit: false,
      disconnectedNoticeShown: false,
      sessionGen: undefined,
      lastSeq: 0,
    };
    // Phase 7 req 3 — Ctrl+. opens the inline Fix/Explain overlay when a
    // lightbulb is armed for this device. Intercept the keydown before xterm
    // processes it so '.' is never typed into the buffer.
    term.attachCustomKeyEventHandler((e) => {
      if (
        e.type === 'keydown' &&
        (e.ctrlKey || e.metaKey) &&
        (e.key === '.' || e.code === 'Period')
      ) {
        if (lightbulbRef.current[deviceId]) {
          openLightbulbOverlay(deviceId);
          e.preventDefault();
          return false;
        }
      }
      return true;
    });

    promptRef.current[deviceId] = '';
    session.promptLen = 0;

    const handleTermData = (data: string): void => {
      // ── In-session reconnect: FREEZE input on connection loss ─────────
      // Once the device/browser socket has dropped, the terminal must NOT accept
      // or echo commands into a dead socket (this is the "prompt still accepts
      // input but nothing happens" bug). Swallow every keystroke until the
      // session is resumed (device reaches 'connected' again). Reconnect is
      // driven by the sticky banner's auto-retry / [Reconnect] button, never by
      // typing here. The value is intentionally not echoed or logged.
      if (session.disconnected || session.frozen) {
        return;
      }

      // ── Bug 10: AUTH_PASSTHROUGH — device is at an interactive login ──
      // SECURITY release-blocker. While the device is parked at a login/password
      // prompt, terminal keystrokes must NEVER be validated as commands, echoed
      // in plaintext, or logged. Credentials flow ONLY through the secure masked
      // dialog (Bug 7). Swallow every key here so a typed password can never
      // become an "Unknown command: 'cisco'" or leak into the buffer. The value
      // is intentionally NOT included in observability.
      if (session.authActive) {
        obsAuth('keystroke_swallowed', deviceId);
        return;
      }

      // ── Native --More-- pager mode — route raw keys to the device ──
      // Priority 3 (CML standard): Space = next page, Enter = next line,
      // q / Ctrl-C / Tab = quit. Tab is a convenience quit shortcut so the
      // engineer can bail out of a long output fast. Every other key is
      // swallowed so nothing can be typed into a paging session.
      if (session.paging) {
        if (data === ' ') {
          send({ type: 'pager_key', device_id: deviceId, key: 'space' });
        } else if (data === '\r' || data === '\n') {
          send({ type: 'pager_key', device_id: deviceId, key: 'enter' });
        } else if (data === 'q' || data === 'Q' || data === '\x03' || data === '\t') {
          send({ type: 'pager_key', device_id: deviceId, key: 'q' });
        }
        // All other keys: inert while paging.
        return;
      }

      // ── Destructive-action confirmation gate (blocking y/n) ────────────
      // A destructive command is HELD at the server pending an explicit answer.
      // Capture the next keystroke as that answer — NEVER echo it into the
      // command buffer — and route it to the server, which decides whether the
      // held command reaches the device. 'y' forwards, 'n' discards, anything
      // else re-prompts (the command stays held). This is the interactive gate
      // that intercepts Enter before the command reaches the device.
      if (session.awaitingConfirm) {
        if (data === 'y' || data === 'Y') {
          term.write('y\r\n');
          session.awaitingConfirm = null;
          send({ type: 'confirm_response', device_id: deviceId, answer: 'y' });
          // The device will now execute — busy until its prompt lands.
          setBusy(session);
          obsConfirm('answer_yes', deviceId);
          return;
        }
        if (data === 'n' || data === 'N') {
          term.write('n\r\n');
          session.awaitingConfirm = null;
          send({ type: 'confirm_response', device_id: deviceId, answer: 'n' });
          obsConfirm('answer_no', deviceId);
          return;
        }
        // Any other key / bare Enter: keep the command held; the server
        // re-renders the red confirmation via a fresh confirm_required.
        send({ type: 'confirm_response', device_id: deviceId, answer: data });
        obsConfirm('answer_other', deviceId);
        return;
      }

      // ── Confirm (y/n) mode — captures the answer to a "Run it?" prompt ──
      if (session.confirm) {
        const c = session.confirm;
        if (data === 'y' || data === 'Y') {
          term.write('y');
          session.confirm = null;
          const targetId = c.switchTo && c.deviceId ? c.deviceId : deviceId;
          if (c.switchTo && c.deviceId) switchRef.current?.(c.deviceId);
          send({ type: 'command', device_id: targetId, command: c.fix });
          // Busy-gate the session that actually runs the fix until its prompt lands.
          {
            const target = sessionsRef.current.get(targetId);
            if (target) setBusy(target);
          }
          session.history.unshift(c.fix);
          session.historyIdx = -1;
          return;
        }
        // n / N / Esc / Ctrl-C / Enter => decline
        if (
          data === 'n' || data === 'N' || data === '\x1b' ||
          data === '\x03' || data === '\r'
        ) {
          term.write('n\r\n');
          session.confirm = null;
          const p = promptRef.current[deviceId] ?? '';
          term.write(p ? `${p} ` : '$ ');
          return;
        }
        return; // swallow any other key while confirming
      }

      // ── Raw-terminal input pass-through ────────────────────────────────
      // Keystrokes flow STRAIGHT through as typed (like CMD/PuTTY). The prior
      // busy-gate that QUEUED keys during output and REPLAYED them once the
      // prompt landed is gone — that replay was re-submitting commands (the
      // duplication + unexpected Router>/enable mode confusion) and the
      // deferred render is what surfaced the '^@'. The device's own echo is the
      // source of truth; typed input is never queued, re-ordered, or auto-run.

      // ── Enter (Bug 2: treat both CR and LF as a command boundary) ──────
      if (data === '\r' || data === '\n') {
        // CR/LF coalescing: a terminal that sends "\r\n" must fire ONE submit,
        // not two. Swallow the LF that lands immediately after a CR.
        const nowEnter = Date.now();
        if (data === '\n' && nowEnter - session.lastEnterAt < 50) {
          session.lastEnterAt = nowEnter;
          return;
        }
        session.lastEnterAt = nowEnter;
        // Bug 4 safety: a real submit always clears the help gate so a stuck
        // flag can never block legitimate command output.
        clearHelpPending(session);

        clearGhost(session);
        clearHintBelow(session);

        // Phase 6: ask mode — submit question to CLIQ, not to device
        if (session.askMode) {
          const question = session.inputBuffer.slice(1).trim(); // strip leading '?'
          session.inputBuffer = '';
          session.cursor = 0;
          session.askMode = false;
          ghostRef.current[deviceId] = '';
          hintRef.current[deviceId] = null;
          setLiveHints((current) => ({ ...current, [deviceId]: null }));
          const CYAN = '\x1b[36m';
          const DIM2 = '\x1b[2m';
          const RST2 = '\x1b[0m';
          term.write('\r\n');
          if (question) {
            term.write(`${CYAN}⚡ CLIQ: asking…${RST2}\r\n`);
            send({ type: 'ask', device_id: deviceId, question });
          }
          const p = promptRef.current[deviceId] ?? '';
          term.write(p ? `${DIM2}${p}${RST2} ` : '$ ');
          return;
        }

        const command = session.inputBuffer.trim();

        term.write('\r\n');
        if (command.length > 0) {
          // Bug 2: duplicate-submit guard. A single Enter must never fire two
          // backend submits (key-repeat, a stray duplicate CR, or a fast double
          // Enter). Suppress an identical command resubmitted within the guard
          // window; the buffer is still reset below so input stays clean.
          const isDuplicate =
            command === session.lastSubmitCmd &&
            nowEnter - session.lastSubmitAt < 350;
          if (isDuplicate) {
            session.inputBuffer = '';
            session.cursor = 0;
            ghostRef.current[deviceId] = '';
            hintRef.current[deviceId] = null;
            session.rawInput = false;
            setLiveHints((current) => ({ ...current, [deviceId]: null }));
            return;
          }
          session.lastSubmitCmd = command;
          session.lastSubmitAt = nowEnter;
          // TEMP TRACE (diagnostic): exact bytes SENT to the device over the WS
          // at submit. The ONLY local xterm write at submit is the `\r\n` echo
          // written just above (see term.write('\r\n')); NO \x00 / synthetic
          // echo is written here. If a ^@ shows in XTERM_WRITE at this instant
          // with no matching WS_RECV, it was generated locally.
          // eslint-disable-next-line no-console
          console.log(
            'SUBMIT_TO_WS:', JSON.stringify(command),
            'hex:', [...command].map((c) => c.charCodeAt(0)),
            'localEchoAtSubmit:', JSON.stringify('\r\n'),
          );
          // Command boundary: fully reset warning state so the executed command
          // starts with a clean panel (no carryover from the prior line).
          clearDeviceErrors(deviceId);
          session.history.unshift(command); // newest at index 0
          session.historyIdx = -1;
          // Bug 4: lines that were INSERTED (config block / Insert button) are
          // plain text the engineer chose to run — bypass the recognition gate
          // so they reach the device as-is (no 'Unknown command'). Normal typed
          // commands keep full validation.
          const raw = session.rawInput === true;
          // ── Destructive-action gate (Tier 1 safety) ──────────────────
          // A typed destructive command must NOT be forwarded to the device on
          // Enter. Send it so the server HOLDS it, and arm the local keystroke
          // gate synchronously so the operator's next key (y/n) is captured as
          // the answer instead of being echoed into the buffer. The RED
          // confirmation line is rendered when the server replies with
          // confirm_required. Nothing is executed until the operator answers y.
          if (!raw && isDestructiveCommand(command)) {
            session.awaitingConfirm = { command };
            send({ type: 'command', device_id: deviceId, command, raw });
            obsConfirm('gate_triggered', deviceId);
          } else {
            send({ type: 'command', device_id: deviceId, command, raw });
            // Enter the input busy-gate: keystrokes are queued until CLIQ renders
            // the trailing prompt (device_output), so nothing interleaves with the
            // in-flight output.
            setBusy(session);
          }
          setMetrics((current) => ({
            ...current,
            [deviceId]: {
              ...(current[deviceId] ?? defaultMetrics()),
              commandsRun: (current[deviceId]?.commandsRun ?? 0) + 1,
            },
          }));
        } else {
          // Empty Enter: forward to backend so the device console receives \r\n
          // to dismiss "Press RETURN" banners, trigger router prompts, or refresh prompt.
          send({ type: 'command', device_id: deviceId, command: '', raw: false });
          setBusy(session);
        }
        // Bug 2: ALWAYS reset the input buffer on a command boundary, no matter
        // the validation outcome — nothing from this line can leak into the next.
        session.inputBuffer = '';
        session.cursor = 0;
        ghostRef.current[deviceId] = '';
        hintRef.current[deviceId] = null;
        // Bug 4: clear the raw flag now the line is submitted. If a config block
        // is still in progress, device_output re-arms it for the next step.
        session.rawInput = false;
        setLiveHints((current) => ({ ...current, [deviceId]: null }));
        return;
      }

      // ── Tab: accept ghost suggestion ───────────────────────────────────
      if (data === '\t') {
        const ghost = ghostRef.current[deviceId] ?? '';
        if (ghost && session.ghostLen > 0) {
          session.term.write('\x1b[K');
          session.ghostLen = 0;
          session.term.write(ghost);
          session.inputBuffer += ghost;
          session.cursor = session.inputBuffer.length;
          ghostRef.current[deviceId] = '';
          clearHintBelow(session);
          scheduleSuggest(deviceId, session.inputBuffer);
          setMetrics((current) => ({
            ...current,
            [deviceId]: { ...(current[deviceId] ?? defaultMetrics()), ghostText: '' },
          }));
        }
        return;
      }

      // ── Right-arrow: accept ghost at end-of-line, else move caret right ─
      if (data === '\x1b[C') {
        const ghost = ghostRef.current[deviceId] ?? '';
        if (session.cursor >= session.inputBuffer.length && ghost && session.ghostLen > 0) {
          session.term.write('\x1b[K');
          session.ghostLen = 0;
          session.term.write(ghost);
          session.inputBuffer += ghost;
          session.cursor = session.inputBuffer.length;
          ghostRef.current[deviceId] = '';
          clearHintBelow(session);
          scheduleSuggest(deviceId, session.inputBuffer);
          setMetrics((current) => ({
            ...current,
            [deviceId]: { ...(current[deviceId] ?? defaultMetrics()), ghostText: '' },
          }));
        } else if (session.cursor < session.inputBuffer.length) {
          session.cursor += 1;
          term.write('\x1b[C');
        }
        return;
      }

      // ── Left-arrow: move caret left within the input region ────────────
      if (data === '\x1b[D') {
        if (session.cursor > 0) {
          session.cursor -= 1;
          term.write('\x1b[D');
        }
        return;
      }

      // ── Up arrow: history back ─────────────────────────────────────────
      if (data === '\x1b[A') {
        const nextIdx = Math.min(session.historyIdx + 1, session.history.length - 1);
        if (nextIdx >= 0 && session.history[nextIdx] !== undefined) {
          clearGhost(session);
          clearHintBelow(session);
          // Move the caret to end before erasing so the whole line clears.
          if (session.cursor < session.inputBuffer.length) {
            term.write(`\x1b[${session.inputBuffer.length - session.cursor}C`);
          }
          term.write('\b \b'.repeat(session.inputBuffer.length));
          session.inputBuffer = session.history[nextIdx];
          session.historyIdx = nextIdx;
          session.cursor = session.inputBuffer.length;
          term.write(session.inputBuffer);
          // History recall shows the command verbatim — never overlay a ghost
          // suggestion (that made Up appear to show a different command).
          clearGhost(session);
          ghostRef.current[deviceId] = '';
        }
        return;
      }

      // ── Down arrow: history forward ────────────────────────────────────
      if (data === '\x1b[B') {
        clearGhost(session);
        clearHintBelow(session);
        if (session.cursor < session.inputBuffer.length) {
          term.write(`\x1b[${session.inputBuffer.length - session.cursor}C`);
        }
        term.write('\b \b'.repeat(session.inputBuffer.length));
        const nextIdx = session.historyIdx - 1;
        if (nextIdx < 0) {
          session.inputBuffer = '';
          session.historyIdx = -1;
        } else {
          session.inputBuffer = session.history[nextIdx] ?? '';
          session.historyIdx = nextIdx;
        }
        session.cursor = session.inputBuffer.length;
        term.write(session.inputBuffer);
        // History recall shows the command verbatim — no ghost overlay.
        clearGhost(session);
        ghostRef.current[deviceId] = '';
        return;
      }

      // ── Backspace ──────────────────────────────────────────────────────
      if (data === '\u007f') {
        clearGhost(session);
        // Any edit invalidates the previously computed hint; drop it so it can
        // never be acted on for a different command (Bug 3). A fresh hint will
        // arrive from the debounced suggest below if still applicable.
        hintRef.current[deviceId] = null;
        // Input changed: previously executed warnings are now stale — clear them.
        clearDeviceErrors(deviceId);
        // Phase 6: if backspace clears the leading '?', exit ask mode.
        if (session.askMode && session.inputBuffer === '?') {
          session.askMode = false;
          session.inputBuffer = '';
          session.cursor = 0;
          const p = promptRef.current[deviceId] ?? '';
          const RST2 = '\x1b[0m';
          session.promptLen = p.length + 1;
          term.write(`\r${RST2}${p} `);
          return;
        }
        if (session.cursor > 0) {
          const buf = session.inputBuffer;
          const cur = session.cursor;
          session.inputBuffer = buf.slice(0, cur - 1) + buf.slice(cur);
          session.cursor = cur - 1;
          const tail = session.inputBuffer.slice(session.cursor);
          // Erase one char before caret, redraw the tail, then restore caret.
          term.write('\b' + tail + ' ');
          term.write(`\x1b[${tail.length + 1}D`);
          clearHintBelow(session);
          // Bug 4: raw inserted lines never trigger recognition while edited.
          if (!session.rawInput) {
            const helpGhost = ghostFromHelp(session.helpTokens, session.helpHead, session.inputBuffer);
            if (helpGhost) {
              ghostRef.current[deviceId] = helpGhost;
              showGhost(session, helpGhost);
            }
            scheduleSuggest(deviceId, session.inputBuffer);
          }
        } else {
          clearHintBelow(session);
        }
        return;
      }

      // ── Ctrl+C: cancel input ───────────────────────────────────────────
      if (data === '\x03') {
        clearGhost(session);
        clearHintBelow(session);
        session.inputBuffer = '';
        session.cursor = 0;
        session.historyIdx = -1;
        session.askMode = false;
        session.rawInput = false;
        session.pendingBlock = [];
        hintRef.current[deviceId] = null;
        term.writeln('^C');
        const p = promptRef.current[deviceId] ?? '';
        term.write(p ? `${p} ` : '$ ');
        return;
      }

      // ── Printable characters ───────────────────────────────────────────
      if (data >= ' ') {
        clearGhost(session);
        // Invalidate the stale hint the instant the input changes; the
        // debounced suggest re-populates it for the new input only (Bug 3).
        hintRef.current[deviceId] = null;
        // Input changed: previously executed warnings are now stale — clear them.
        clearDeviceErrors(deviceId);

        // Objective 2: Cisco CLI help ('?') — whether typed at an empty line
        // (bare '?') or mid-line ('show ?'), pass directly to device context help.
        // Never hijack literal '?' to internal Ask Mode or append Enter.
        if (data === '?' && !session.askMode) {
          // Capture the partial to query the device, THEN immediately drop it.
          const partial = session.inputBuffer;
          term.write('?');
          clearHintBelow(session);
          // Cancel any in-flight debounced suggest so its (possibly empty) ghost
          // response cannot land AFTER and clobber the fresh line.
          const pending = suggestTimerRef.current[deviceId];
          if (pending) {
            clearTimeout(pending);
            delete suggestTimerRef.current[deviceId];
          }
          // ── SEV-1 ROOT-CAUSE FIX (client-side line buffer) ──────────────
          // A device-native '?' is a HELP request, never a command. Reset the
          // client's tracked input buffer to EMPTY *right now* so:
          //   (a) the stale partial can NEVER be re-submitted to the device
          //       (kills the phantom "% Ambiguous command" auto-execution), and
          //   (b) the next keystrokes start from empty (kills the 'showshow'
          //       concatenation).
          // The line IOS echoes after '?' is display-only — never fed back.
          session.inputBuffer = '';
          session.cursor = 0;
          ghostRef.current[deviceId] = '';
          // Bug 4: arm the help gate. Until the device's help reply lands and is
          // written, any prompt-redraw (device_output) is DROPPED so the
          // dangling prompt can never appear before the help text. The gate is
          // always cleared by the device_help reply (error/complete) below, and
          // auto-clears via a safety timeout if that reply never arrives.
          armHelpPending(session, deviceId);
          send({ type: 'device_help', device_id: deviceId, partial });
          return;
        }

        const buf = session.inputBuffer;
        const cur = session.cursor;
        session.inputBuffer = buf.slice(0, cur) + data + buf.slice(cur);
        session.cursor = cur + data.length;
        const tail = session.inputBuffer.slice(session.cursor);
        // Insert at the caret: write the char + redraw the tail, then move back.
        term.write(data + tail);
        if (tail.length > 0) term.write(`\x1b[${tail.length}D`);
        clearHintBelow(session);
        // Don't send suggest signals for ask-mode input (it's a question, not a
        // command), nor for raw inserted lines (Bug 4: plain text, no intel).
        if (!session.askMode && !session.rawInput) {
          // Tier 1: complete the last token from the device's own '?'-help
          // tokens FIRST and render it immediately (no debounce). This wins
          // over the backend KB/RAG/learned ghost for the live device.
          const helpGhost = ghostFromHelp(session.helpTokens, session.helpHead, session.inputBuffer);
          if (helpGhost) {
            ghostRef.current[deviceId] = helpGhost;
            showGhost(session, helpGhost);
            // eslint-disable-next-line no-console
            console.debug('[GHOST] (e) showGhost(help) %o', helpGhost);
          }
          scheduleSuggest(deviceId, session.inputBuffer);
        }
      }
    };
    // Register the raw key handler (input passes straight through — no gate).
    session.feedInput = handleTermData;
    term.onData(handleTermData);

    // Phase 7 req 3 — keep the inline 💡 marker glued to the prompt line as the
    // caret moves or the viewport scrolls (only repositions while armed/focused).
    term.onCursorMove(() => updateLightbulbAnchor(deviceId));
    term.onScroll(() => updateLightbulbAnchor(deviceId));

    sessionsRef.current.set(deviceId, session);
    const mount = mountRefs.current.get(deviceId);
    if (mount && mount.childElementCount === 0) {
      term.open(mount);
      // Defer fit() to registerMount (fonts.ready + rAF) to avoid clipping.

      // ── Click-to-position caret within the current input line ──────────
      // Clicking a word/position on the active input row repositions the
      // caret by emitting the matching left/right arrow sequences. Clicks in
      // scrollback/output (any row other than the caret's row) are ignored.
      term.element?.addEventListener('mousedown', (ev: MouseEvent) => {
        if (session.confirm) return;
        // Allow xterm's own selection on drags; only reposition on a plain click.
        if (ev.button !== 0) return;
        const screenEl = term.element?.querySelector('.xterm-screen') as HTMLElement | null;
        if (!screenEl) return;
        const rect = screenEl.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const cellW = rect.width / term.cols;
        const cellH = rect.height / term.rows;
        const clickRow = Math.floor((ev.clientY - rect.top) / cellH);
        const clickCol = Math.floor((ev.clientX - rect.left) / cellW);
        const cursorRow = term.buffer.active.cursorY; // viewport-relative
        if (clickRow !== cursorRow) return; // not on the input line → ignore
        // Editable region: columns [promptLen .. promptLen + input length].
        const minCol = session.promptLen;
        const maxCol = session.promptLen + session.inputBuffer.length;
        const targetCol = Math.max(minCol, Math.min(clickCol, maxCol));
        const targetIdx = targetCol - session.promptLen;
        const delta = targetIdx - session.cursor;
        if (delta === 0) return;
        if (delta > 0) term.write('\x1b[C'.repeat(delta));
        else term.write('\x1b[D'.repeat(-delta));
        session.cursor = targetIdx;
        ev.preventDefault();
      });
    }
    return session;
  }

  function defaultMetrics(): DeviceMetrics {
    return { mode: 'exec', status: 'disconnected', latency: 0, typosCaught: 0, commandsRun: 0, ghostText: '' };
  }

  function send(payload: WsOutbound) {
    if ('device_id' in payload && payload.device_id && payload.type !== 'connect_device') {
      const s = sessionsRef.current.get(payload.device_id);
      if (s && (s.disconnected || s.intentionalExit)) {
        return;
      }
    }
    // TEMP DIAGNOSTIC (SEV-1): trigger-labeled log of every write that reaches
    // the device, so the culprit re-submit after '?'-help can be confirmed on a
    // live run. The only legitimate device writes are user-initiated: 'command'
    // (Enter / confirm-fix), 'run_fix', and pager keys. If a '[device-write]
    // command' appears right after a '?'-help WITHOUT an Enter, that is the
    // phantom re-submit. Remove this block once traced.
    if (payload.type === 'command' || payload.type === 'run_fix' || payload.type === 'pager_key') {
      // eslint-disable-next-line no-console
      console.warn('[CLIQ device-write] trigger=%s payload=%o', payload.type, payload);
    }
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return;
    }
    outboundQueueRef.current.push(payload);
  }

  function flushOutboundQueue() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    for (const payload of outboundQueueRef.current) ws.send(JSON.stringify(payload));
    outboundQueueRef.current = [];
  }

  // ── In-session reconnect controller ──────────────────────────────────────
  // Drives the freeze → auto-retry (backoff) → resume lifecycle for a dropped
  // session, WITHOUT a page reload, re-login, or fresh terminal. Scrollback is
  // preserved implicitly (the xterm instance is never torn down). Timers live in
  // reconnectRef so the retry loop is closure-safe.

  function ensureReconnectCtrl(deviceId: string) {
    let ctrl = reconnectRef.current[deviceId];
    if (!ctrl) {
      ctrl = { attempt: 0, timer: null, watchdog: null, active: false };
      reconnectRef.current[deviceId] = ctrl;
    }
    return ctrl;
  }

  function clearReconnectTimers(deviceId: string) {
    const ctrl = reconnectRef.current[deviceId];
    if (!ctrl) return;
    if (ctrl.timer !== null) { window.clearTimeout(ctrl.timer); ctrl.timer = null; }
    if (ctrl.watchdog !== null) { window.clearTimeout(ctrl.watchdog); ctrl.watchdog = null; }
  }

  function setReconnectBanner(deviceId: string, info: ReconnectInfo | null) {
    setReconnectBanners((cur) => ({ ...cur, [deviceId]: info }));
  }

  // Structured, backend-logged reconnect telemetry (socket drop / freeze / retry
  // attempts / resume / exhaustion). Carries only the outcome — never any input.
  function obsReconnect(outcome: string, deviceId: string, detail = '') {
    send({
      type: 'client_event',
      device_id: deviceId,
      layer: 'connection',
      op: 'reconnect',
      outcome,
      detail,
    });
  }

  // Reopen the browser WebSocket under the SAME token/host by re-running the WS
  // effect. Guarded so concurrent per-device controllers only trigger one reopen
  // while a socket is already connecting.
  function reopenWebSocket() {
    const ws = wsRef.current;
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
    setWsEpoch((e) => e + 1);
  }

  // Freeze a device's input and begin the auto-reconnect sequence. Idempotent:
  // a device already reconnecting is left on its existing schedule.
  function freezeAndReconnect(deviceId: string) {
    const session = sessionsRef.current.get(deviceId);
    if (session) {
      if (session.disconnected || session.intentionalExit) {
        return;
      }
      session.frozen = true;
      clearGhost(session);
      clearHintBelow(session);
      session.pendingBlock = [];
      session.paging = false;
      session.connecting = false;
      session.awaitingConfirm = null;
      session.rawInput = false;
      releaseBusy(session);
    }
    const ctrl = ensureReconnectCtrl(deviceId);
    if (ctrl.active) return;
    ctrl.active = true;
    ctrl.attempt = 0;
    obsReconnect('frozen', deviceId);
    scheduleReconnect(deviceId);
  }

  // Schedule the next auto-reconnect attempt with exponential backoff, or fall
  // back to a manual [Reconnect] once the retry budget is exhausted.
  function scheduleReconnect(deviceId: string) {
    const ctrl = ensureReconnectCtrl(deviceId);
    clearReconnectTimers(deviceId);
    ctrl.attempt += 1;
    if (ctrl.attempt > RECONNECT_MAX_ATTEMPTS) {
      ctrl.active = false; // stop auto-retry; the manual button stays available
      setReconnectBanner(deviceId, {
        phase: 'failed',
        attempt: RECONNECT_MAX_ATTEMPTS,
        maxAttempts: RECONNECT_MAX_ATTEMPTS,
        message: 'Auto-reconnect failed. Session preserved — click Reconnect to retry.',
      });
      obsReconnect('exhausted', deviceId);
      return;
    }
    const delay = RECONNECT_BACKOFFS_MS[ctrl.attempt - 1];
    setReconnectBanner(deviceId, {
      phase: 'retrying',
      attempt: ctrl.attempt,
      maxAttempts: RECONNECT_MAX_ATTEMPTS,
      message: `Connection lost. Reconnecting… (attempt ${ctrl.attempt}/${RECONNECT_MAX_ATTEMPTS})`,
    });
    ctrl.timer = window.setTimeout(() => doReconnectAttempt(deviceId), delay);
  }

  // Perform one reconnect attempt: reopen the browser socket if it is dead, else
  // re-issue connect_device over the live socket. A watchdog schedules the next
  // backoff step if the device does not report 'connected' in time.
  function doReconnectAttempt(deviceId: string) {
    const ctrl = ensureReconnectCtrl(deviceId);
    ctrl.timer = null;
    obsReconnect(`attempt_${ctrl.attempt}`, deviceId);
    const ws = wsRef.current;
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      // Browser socket is dead — reopen it; onopen re-issues connect_device for
      // every device under the SAME token (server-side session preserved).
      reopenWebSocket();
    } else if (ws.readyState === WebSocket.OPEN) {
      // Socket alive (device-side console dropped) — re-attach this device only.
      send({ type: 'connect_device', device_id: deviceId });
    }
    // else CONNECTING: onopen will drive connect_device; just wait on the watchdog.
    ctrl.watchdog = window.setTimeout(() => scheduleReconnect(deviceId), RECONNECT_WATCHDOG_MS);
  }

  // A device reported 'connected' again — resume the session: clear timers,
  // re-arm input, clear the banner, and confirm in the terminal. Only fires when
  // a reconnect was actually in progress (frozen or active controller), so a
  // normal first connect never shows the resume affordance.
  function markReconnected(deviceId: string) {
    const ctrl = reconnectRef.current[deviceId];
    const session = sessionsRef.current.get(deviceId);
    const wasReconnecting = Boolean(session?.frozen || ctrl?.active || session?.disconnected);
    if (!wasReconnecting) return;
    clearReconnectTimers(deviceId);
    if (ctrl) { ctrl.active = false; ctrl.attempt = 0; }
    if (session) {
      session.frozen = false;
      session.disconnected = false;
      session.intentionalExit = false;
      session.disconnectedNoticeShown = false;
      const GRN = '\x1b[32m';
      const BOLD = '\x1b[1m';
      const RST = '\x1b[0m';
      session.term.write(`\r\n${GRN}${BOLD}✓ Reconnected${RST}${GRN} — session resumed.${RST}\r\n`);
    }
    setReconnectBanner(deviceId, {
      phase: 'reconnected',
      attempt: 0,
      maxAttempts: RECONNECT_MAX_ATTEMPTS,
      message: '✓ Reconnected — session resumed.',
    });
    obsReconnect('resumed', deviceId);
    // Briefly show the success state, then auto-hide the banner.
    window.setTimeout(() => setReconnectBanner(deviceId, null), 2600);
  }

  // Bug 4 (cross-cutting observability): record the ?-help render-order
  // decisions so the prompt-gating can be audited. Forwarded to the backend's
  // unified observability layer as {layer:"renderer", op:"render_order"}.
  function obsRenderOrder(outcome: string, deviceId: string) {
    // eslint-disable-next-line no-console
    console.debug('[render_order] %s device=%s', outcome, deviceId);
    send({
      type: 'client_event',
      device_id: deviceId,
      layer: 'renderer',
      op: 'render_order',
      outcome,
    });
  }

  // Bug 10 — AUTH_PASSTHROUGH observability. Records auth-mode transitions and
  // swallowed keystrokes as {layer:"auth", op:"auth_passthrough"}. NEVER carries
  // any keystroke value — only the outcome — so a credential can never leak.
  function obsAuth(outcome: string, deviceId: string) {
    send({
      type: 'client_event',
      device_id: deviceId,
      layer: 'auth',
      op: 'auth_passthrough',
      outcome,
    });
  }

  // Destructive-action confirmation gate observability. Records the gate
  // lifecycle (triggered / answer_yes / answer_no / answer_other) as
  // {layer:"safety", op:"destructive_gate"}. Carries only the outcome — never a
  // keystroke value beyond the y/n classification.
  function obsConfirm(outcome: string, deviceId: string) {
    send({
      type: 'client_event',
      device_id: deviceId,
      layer: 'safety',
      op: 'destructive_gate',
      outcome,
    });
  }

  // Bug 10 — detect an interactive console login prompt at the TAIL of device
  // output (NX-OS + IOS console AAA), case-insensitive. Returns the stage so the
  // caller knows whether to mask input. Matched at the end of the text so a
  // banner mentioning "username" in prose never trips it.
  function detectAuthPrompt(text: string): 'password' | 'username' | null {
    const tail = (text || '').replace(/[\s\r\n]+$/g, (m) => m).trimEnd();
    if (/password\s*:\s*$/i.test(tail)) return 'password';
    if (/(?:^|\s)(?:login|username)\s*:\s*$/im.test(tail)) return 'username';
    return null;
  }

  // Bug 10 — a real device prompt (hostname# / hostname> / hostname(config)#)
  // at the tail means login completed → exit AUTH_PASSTHROUGH.
  function detectReadyPrompt(text: string): boolean {
    const tail = (text || '').trimEnd();
    return /[\w-]+(\([^)]+\))?[#>]\s*$/.test(tail);
  }

  // Bug 10 — enter/exit AUTH_PASSTHROUGH for a session. Entering disables all
  // intel (ghost/hint cleared) and arms keystroke swallowing; exiting restores
  // normal command processing.
  function setAuthActive(session: Session, deviceId: string, stage: 'password' | 'username' | null) {
    if (stage) {
      if (!session.authActive) obsAuth(`enter_${stage}`, deviceId);
      session.authActive = true;
      session.authSecret = stage === 'password';
      clearGhost(session);
      clearHintBelow(session);
      ghostRef.current[deviceId] = '';
    } else if (session.authActive) {
      session.authActive = false;
      session.authSecret = false;
      obsAuth('exit', deviceId);
    }
  }

  // Bug 4 — arm the help gate with a SAFETY TIMEOUT. While helpPending is true,
  // device_output prompt redraws are dropped so the dangling prompt can never
  // render before the help body. If the device_help reply is lost/never sent
  // (e.g. an empty partial, a backend hiccup), this timer auto-clears the gate
  // so the terminal can NEVER be permanently frozen. The reply path
  // (clearHelpPending) cancels the timer on the normal, fast path.
  const HELP_PENDING_TIMEOUT_MS = 12000;
  function armHelpPending(session: Session, deviceId: string) {
    clearHelpPending(session);
    session.helpPending = true;
    session.helpPendingTimer = window.setTimeout(() => {
      if (session.helpPending) {
        session.helpPending = false;
        session.helpPendingTimer = null;
        obsRenderOrder('help_pending_timeout', deviceId);
      }
    }, HELP_PENDING_TIMEOUT_MS);
  }

  function clearHelpPending(session: Session) {
    session.helpPending = false;
    if (session.helpPendingTimer !== null) {
      window.clearTimeout(session.helpPendingTimer);
      session.helpPendingTimer = null;
    }
  }

  // ── Busy-gate NEUTRALIZED (raw terminal) ───────────────────────────────
  // There is NO input queue, NO replay, and NO input gating. Keystrokes are
  // echoed exactly once as typed and pass straight through; the DEVICE echo is
  // the source of truth. These two functions remain only as inert markers so
  // the existing call sites need no churn — they never capture, defer, or
  // re-inject a keystroke (the queue/replay was the source of the command
  // showing up ×N on the display).
  function setBusy(session: Session) {
    session.busy = true;
  }

  function releaseBusy(session: Session) {
    // Nothing is ever queued, so there is nothing to replay. Clear the flag
    // (and any stray residue) defensively.
    session.busy = false;
    session.queuedInput = [];
    if (session.busyTimer !== null) {
      window.clearTimeout(session.busyTimer);
      session.busyTimer = null;
    }
  }

  // Bug 4 (4th attempt) — the SINGLE gate every prompt-redraw path must honour.
  // While a '?'-help round-trip is pending, the ONLY code allowed to draw the
  // prompt is the device_help (help_complete) reply — see its write+callback
  // chain. Every OTHER handler that would print a prompt (error_panel,
  // intel_answer, intel_resolve_ack, device_output) MUST call this first and
  // bail out when it returns true, so a stray prompt can NEVER land between the
  // echoed 'show … ?' line and the help body. This closes the previously
  // untracked redraw paths that let 'R1#' print before the help.
  function promptSuppressedByHelp(session: Session, deviceId: string, where: string): boolean {
    if (session.helpPending) {
      obsRenderOrder(`prompt_dropped_help_pending:${where}`, deviceId);
      return true;
    }
    return false;
  }

  const observersRef = useRef<Map<string, ResizeObserver>>(new Map());

  function registerMount(deviceId: string, element: HTMLDivElement | null) {
    if (!element) {
      mountRefs.current.delete(deviceId);
      observersRef.current.get(deviceId)?.disconnect();
      observersRef.current.delete(deviceId);
      return;
    }
    mountRefs.current.set(deviceId, element);
    const session = ensureSession(deviceId);
    if (element.childElementCount === 0) {
      session.term.open(element);
      session.term.focus();
    }
    // Fit only once the container has real dimensions AND the web fonts have
    // settled. Fitting too early (hidden tab / pre-font-load) yields wrong cell
    // geometry and clips the first column of every line.
    const doFit = () => {
      if (element.offsetWidth > 0 && element.offsetHeight > 0) {
        session.fit.fit();
      }
    };
    const fontsReady = (document as Document & { fonts?: { ready: Promise<unknown> } }).fonts?.ready;
    if (fontsReady) {
      fontsReady.then(() => requestAnimationFrame(doFit));
    } else {
      requestAnimationFrame(doFit);
    }
    // Auto-refit when the container is resized (error panel expand/collapse, etc.)
    observersRef.current.get(deviceId)?.disconnect();
    const observer = new ResizeObserver(() => {
      const s = sessionsRef.current.get(deviceId);
      if (s && element.offsetWidth > 0 && element.offsetHeight > 0) s.fit.fit();
    });
    observer.observe(element);
    observersRef.current.set(deviceId, observer);
  }

  function refitAll() {
    sessionsRef.current.forEach((s) => s.fit.fit());
  }

  function focusDevice(deviceId: string) {
    focusedDeviceRef.current = deviceId;
    sessionsRef.current.get(deviceId)?.term.focus();
    // Re-anchor the 💡 marker to the newly focused terminal (hide it on others).
    updateLightbulbAnchor(deviceId);
  }

  function runFix(deviceId: string, command?: string) {
    if (!command) return;
    send({ type: 'run_fix', device_id: deviceId, command });
  }

  function dismissError(deviceId: string, index: number) {
    setErrors((current) => ({
      ...current,
      [deviceId]: (current[deviceId] ?? []).filter((_, i) => i !== index),
    }));
  }

  // Phase 6 — advisory panel helpers
  function dismissIntelItem(deviceId: string, id: string) {
    setIntelItems((current) => ({
      ...current,
      [deviceId]: (current[deviceId] ?? []).filter((item) => item.id !== id),
    }));
  }

  const markResolved = useCallback(
    (deviceId: string, issueText: string, commands: string[], platform: string, deviceName?: string, notes?: string) => {
      send({
        type: 'mark_resolved',
        device_id: deviceId,
        issue_text: issueText,
        commands,
        platform,
        device_name: deviceName,
        outcome_notes: notes,
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const recallMemory = useCallback(
    (deviceId: string, query: string) => {
      send({ type: 'recall_memory', device_id: deviceId, query });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Phase 7 — ask CLIQ a question directly from the advisory panel (no '?' prefix needed).
  const ask = useCallback(
    (deviceId: string, question: string) => {
      const q = question.trim().replace(/^\?+\s*/, '').trim();
      if (!q) return;
      send({ type: 'ask', device_id: deviceId, question: q });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Feature 2 — clear the sticky protocol context from the panel chip.
  const clearContext = useCallback(
    (deviceId: string) => {
      send({ type: 'clear_context', device_id: deviceId });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Feature 2 — explicitly set/change the sticky protocol context.
  const setContext = useCallback(
    (deviceId: string, protocol: string) => {
      send({ type: 'set_context', device_id: deviceId, protocol });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Phase 7 — Insert a command into the terminal input line for REVIEW.
  // ADVISORY ONLY: places text at the prompt; the engineer presses Enter to
  // run it. This NEVER executes the command itself.
  const insertToTerminal = useCallback(
    (deviceId: string, text: string) => {
      const session = sessionsRef.current.get(deviceId);
      if (!session) return;
      const term = session.term;
      clearGhost(session);
      clearHintBelow(session);
      session.askMode = false;
      ghostRef.current[deviceId] = '';
      hintRef.current[deviceId] = null;
      const p = promptRef.current[deviceId] ?? '';
      session.promptLen = p.length + 1;
      const clean = text.replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
      session.inputBuffer = clean;
      session.cursor = clean.length;
      // Bug 4: inserted text is PLAIN editable terminal input. It must never be
      // sent to the command-recognition/ask pipeline (no ghost, no live hint,
      // no 'Unknown command'). It runs as-is only when the engineer presses
      // Enter — see the Enter handler's `raw` flag.
      session.rawInput = true;
      // Wipe the current line, redraw prompt + inserted text (no newline → no run).
      term.write(`\r\x1b[2K${p} ${clean}`);
      term.focus();
      setLiveHints((c) => ({ ...c, [deviceId]: null }));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Phase 7 req 2 — insert an ENTIRE config block for guided, line-by-line
  // review. The first command is placed on the input line; the rest are queued
  // and auto-loaded (for review only) after each step's output returns. The
  // engineer fills <placeholders> and presses Enter per step. Never auto-runs.
  const insertBlock = useCallback(
    (deviceId: string, commands: string[]) => {
      const session = sessionsRef.current.get(deviceId);
      if (!session) return;
      const lines = commands
        .map((c) => c.replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim())
        .filter((l) => l.length > 0);
      if (lines.length === 0) return;
      const term = session.term;
      clearGhost(session);
      clearHintBelow(session);
      session.askMode = false;
      session.paging = false;
      ghostRef.current[deviceId] = '';
      hintRef.current[deviceId] = null;
      const p = promptRef.current[deviceId] ?? '';
      session.promptLen = p.length + 1;
      session.pendingBlock = lines.slice(1);
      const first = lines[0];
      session.inputBuffer = first;
      session.cursor = first.length;
      // Bug 4: every block line is plain editable text awaiting the engineer's
      // Enter — never routed through recognition/ask while it sits at the prompt.
      session.rawInput = true;
      const DIM2 = '\x1b[2m';
      const RST2 = '\x1b[0m';
      if (lines.length > 1) {
        term.write(
          `\r\n${DIM2}↘ Config block loaded — ${lines.length} steps. Fill <placeholders>, ` +
            `press Enter to run each. CLIQ never runs them for you.${RST2}\r\n`,
        );
      }
      term.write(`\r\x1b[2K${p} ${first}`);
      term.focus();
      setLiveHints((c) => ({ ...c, [deviceId]: null }));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Phase 7 — request a plain-language explanation + safety verdict for a command.
  const explain = useCallback(
    (deviceId: string, command: string) => {
      const c = command.trim();
      if (!c) return;
      send({ type: 'explain', device_id: deviceId, command: c });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const devicesKey = useMemo(
    () => devices.map((d) => d.id).sort().join(','),
    [devices],
  );
  const devicesRef = useRef(devices);
  devicesRef.current = devices;

  useEffect(() => {
    if (!token || devices.length === 0) return;
    const currentGen = ++socketGenRef.current;
    const cleanHost = host.trim();
    // Distinguishes an operator-driven teardown (unmount / host or token change)
    // from an unexpected socket death (backend killed / crashed / network drop).
    // Only the latter surfaces the "Connection lost" notice.
    let intentionalClose = false;
    const ws = new WebSocket(
      `${WS_URL}?token=${encodeURIComponent(token)}&host=${encodeURIComponent(cleanHost)}`,
    );
    wsRef.current = ws;

    // ── In-session reconnect heartbeat ────────────────────────────────────
    // Ping the server periodically; if a pong does not return within the
    // timeout, the browser socket is silently dead (half-open) — close it so the
    // onclose path freezes input + shows the reconnect banner BEFORE the
    // operator types into a dead session. Cleared on every pong.
    let heartbeatTimer: number | null = null;
    let pongTimer: number | null = null;
    const clearPongTimer = () => {
      if (pongTimer !== null) { window.clearTimeout(pongTimer); pongTimer = null; }
    };
    const stopHeartbeat = () => {
      if (heartbeatTimer !== null) { window.clearInterval(heartbeatTimer); heartbeatTimer = null; }
      clearPongTimer();
    };
    const startHeartbeat = () => {
      stopHeartbeat();
      heartbeatTimer = window.setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
        if (pongTimer === null) {
          pongTimer = window.setTimeout(() => {
            // No pong — treat the socket as dead. Closing triggers onclose, the
            // single freeze + auto-reconnect entry point.
            try { ws.close(); } catch { /* already closing */ }
          }, HEARTBEAT_TIMEOUT_MS);
        }
      }, HEARTBEAT_INTERVAL_MS);
    };

    ws.onopen = () => {
      if (currentGen !== socketGenRef.current || ws !== wsRef.current) return;
      flushOutboundQueue();
      startHeartbeat();
      devices.forEach((device) => {
        ensureSession(device.id);
        send({ type: 'connect_device', device_id: device.id });
        setMetrics((current) => ({
          ...current,
          [device.id]: { ...(current[device.id] ?? defaultMetrics()), status: 'connected' },
        }));
      });
    };

    ws.onmessage = (event) => {
      if (currentGen !== socketGenRef.current || ws !== wsRef.current) return;
      // In-session reconnect heartbeat: ANY message received over WebSocket
      // proves the connection is healthy and responsive.
      clearPongTimer();
      const msg = JSON.parse(event.data) as WsInbound;

      // A pong confirms the socket is alive (timer already cleared).
      if (msg.type === 'pong') {
        return;
      }

      // Stale session generation guard: drop events belonging to an older session generation
      if ('session_gen' in msg && msg.session_gen && 'device_id' in msg && msg.device_id) {
        const session = sessionsRef.current.get(msg.device_id);
        if (session && session.sessionGen && session.sessionGen !== msg.session_gen) {
          return;
        }
      }

      // TEMP TRACE (diagnostic): every inbound WS message BEFORE processing. For
      // output-bearing frames hex-dump the body so it can be compared byte-for
      // -byte against SSH_EMIT (backend) and XTERM_WRITE (render). If a ^@
      // appears in XTERM_WRITE but NOT here, the frontend generated it.
      if (msg.type === 'device_output' || msg.type === 'device_stream') {
        const body =
          msg.type === 'device_stream'
            ? (msg.chunk ?? '')
            : `${msg.output ?? ''}|PROMPT|${msg.prompt ?? ''}`;
        // eslint-disable-next-line no-console
        console.log('WS_RECV:', msg.type, JSON.stringify(body), 'hex:', [...body].map((c) => c.charCodeAt(0)));
      }

      if (msg.type === 'device_output') {
        const session = ensureSession(msg.device_id);
        if (msg.session_gen && !session.sessionGen) {
          session.sessionGen = msg.session_gen;
        }
        // Bug 4: while a mid-line '?' help round-trip is in flight, DROP any
        // prompt redraw. This is the dangling 'C8k#' line the user sees before
        // the help options — detect (helpPending) and drop it. The pending help
        // reply (device_help) writes the help body THEN exactly one prompt.
        if (session.helpPending) {
          obsRenderOrder('device_output_dropped', msg.device_id);
          return;
        }
        // Real device output means any held destructive command was released and
        // executed (or the gate was otherwise resolved) — clear the gate flag
        // defensively so a stale hold can never swallow subsequent keystrokes.
        session.awaitingConfirm = null;
        // Phase 3: a command's output finished streaming; leave streaming mode.
        // When output streamed live the backend sends an empty body here, so the
        // normal render below just draws the fresh prompt (no double output).
        const wasStreaming = session.streaming;
        session.streaming = false;
        // First reply after a console attach: drop the "Connecting…" state.
        if (session.connecting) {
          session.connecting = false;
          session.term.write(`\r\n${DIM}\u2713 Console ready${RESET}`);
        }
        // Native pager page: stream the text only — no prompt redraw, no input
        // reset. The pager_active/pager_done messages manage the --More-- state.
        if (msg.paging) {
          const page = (msg.output ?? '').replace(/\n/g, '\r\n');
          if (page.length > 0) session.term.write(`\r\n${page}`);
          // Priority 3: re-show the affordance after EACH page so the engineer
          // sees that Space advanced and can keep paging (no silent pager).
          session.paging = true;
          session.term.write(
            `\r\n${DIM}-- More -- (Space = next page · Enter = line · Tab = quit)${RESET}`,
          );
          // Pager owns input now — leave busy so queued keys aren't replayed
          // into the pager; releaseBusy on the final (non-paging) device_output.
          return;
        }
        clearGhost(session);
        clearHintBelow(session);
        hintRef.current[msg.device_id] = null;
        clearLightbulb(msg.device_id);
        // Auto-dismiss any stale hint/error once real output arrives.
        setLiveHints((current) => ({ ...current, [msg.device_id]: null }));
        setErrors((current) => ({ ...current, [msg.device_id]: [] }));
        const prompt = (msg.prompt ?? '').trim();
        const rawOut = msg.output ?? '';
        const output = normalizeDeviceOutput(rawOut, prompt);
        // Bug 10: a 'Password:' / 'login:' / 'Username:' at the tail of device
        // output means the console dropped to interactive auth (e.g. NX-OS
        // re-login or a mid-session enable password). Render the prompt text,
        // enter AUTH_PASSTHROUGH and open the secure masked dialog — NEVER leave
        // a bare login prompt where the next keystrokes would be validated as a
        // command ("Unknown command: 'cisco'") or echoed in plaintext.
        const authStage = detectAuthPrompt(rawOut) || detectAuthPrompt(output);
        if (authStage) {
          if (output && output.length > 0) {
            session.term.write(`\r\n${output.replace(/\n/g, '\r\n')}`);
          }
          session.inputBuffer = '';
          session.cursor = 0;
          setAuthActive(session, msg.device_id, authStage);
          setAuthPrompt((cur) =>
            cur && cur.deviceId === msg.device_id
              ? cur
              : {
                  deviceId: msg.device_id,
                  label: deviceMap.get(msg.device_id)?.label ?? msg.device_id,
                  message: authStage === 'password' ? 'Enable Secret Required — enter device enable password' : 'Authentication required — enter credentials',
                  retry: false,
                  stage: authStage,
                  stageType: authStage === 'password' ? 'enable' : undefined,
                },
          );
          // Auth dialog owns input now — leave the busy-gate (discard queued
          // keys) so nothing typed pre-auth is replayed onto a login prompt.
          session.queuedInput = [];
          releaseBusy(session);
          return;
        }
        // Only render output if non-empty to avoid extra blank lines
        if (output && output.length > 0) {
          const activeBuf = session.term.buffer?.active;
          if (activeBuf && activeBuf.cursorX > 0) {
            session.term.write('\r\n');
          }
          const formatted = output.replace(/\n/g, '\r\n');
          session.term.write(formatted);
          if (!formatted.endsWith('\r\n')) {
            session.term.write('\r\n');
          }
        } else if (wasStreaming && prompt) {
          // Streamed output ended; ensure newline before prompt if not at column 0
          const activeBuf = session.term.buffer?.active;
          if (activeBuf && activeBuf.cursorX > 0) {
            session.term.write('\r\n');
          }
        }
        if (prompt) {
          // Bug 10: a real device prompt means any prior interactive login is
          // complete — leave AUTH_PASSTHROUGH and resume normal processing.
          setAuthActive(session, msg.device_id, null);
          promptRef.current[msg.device_id] = prompt;
          session.promptLen = prompt.length + 1; // + trailing space
          session.cursor = 0;

          // Deduplicate prompt: do not render if current line already ends with prompt
          const activeBuf = session.term.buffer?.active;
          const currentLine = activeBuf?.getLine(activeBuf.cursorY)?.translateToString(true) ?? '';
          const alreadyHasPrompt = currentLine.trim().endsWith(prompt);
          if (!alreadyHasPrompt) {
            if (activeBuf && activeBuf.cursorX > 0) {
              session.term.write('\r\n');
            }
            session.term.write(`${prompt} `);
          }
          // Guided config block (req 2): auto-load the next step for review.
          // ADVISORY — the engineer still presses Enter; nothing auto-runs.
          if (session.pendingBlock.length > 0) {
            const next = session.pendingBlock.shift() as string;
            session.inputBuffer = next;
            session.cursor = next.length;
            session.term.write(next);
            // Bug 4: the auto-loaded block line is plain text, not a suggest
            // target — never route it through recognition while it waits.
            session.rawInput = true;
            // A staged block line occupies the input — discard any queued
            // keystrokes rather than corrupting the staged line.
            session.queuedInput = [];
            releaseBusy(session);
          } else {
            // Priority 4: hard-reset the client line buffer at every command
            // boundary so stale text can never prepend the next partial
            // (the 'show show' / 'showshow' concatenation).
            session.inputBuffer = '';
            session.rawInput = false;
            // Trailing prompt rendered → command cycle complete. Lift the input
            // busy-gate and REPLAY any keystrokes queued during the flight (the
            // user's own input, deferred — a queued Enter runs it now, safely
            // after the prompt, never mid-stream).
            releaseBusy(session);
          }
        } else {
          promptRef.current[msg.device_id] = '';
          session.promptLen = 0;
          session.inputBuffer = '';
          session.cursor = 0;
          releaseBusy(session);
        }
        return;
      }

      if (msg.type === 'confirm_required') {
        // Destructive-action gate: the server HELD a destructive command pending
        // an explicit y/n. Render the RED confirmation line (naming the exact
        // object) below the input and arm the keystroke gate so the next key is
        // captured as the answer. Nothing has reached the device.
        const session = ensureSession(msg.device_id);
        session.awaitingConfirm = { command: msg.command };
        // Lift any busy flag: the device is NOT executing — it is held.
        releaseBusy(session);
        clearGhost(session);
        clearHintBelow(session);
        const RED = '\x1b[1;31m';
        const RST = '\x1b[0m';
        const line = msg.ansiMessage ? msg.ansiMessage : `${RED}\u26a0 ${msg.message}${RST}`;
        // A fresh line so the red prompt is unmistakable; leave the cursor after
        // it so the typed y/n echoes inline.
        session.term.write(`\r\n${line} `);
        return;
      }

      if (msg.type === 'confirm_resolved') {
        // Destructive-action gate resolved with 'n' — the held command was
        // discarded and NEVER sent to the device. Clear the gate and redraw a
        // clean prompt so the operator can continue.
        const session = ensureSession(msg.device_id);
        session.awaitingConfirm = null;
        releaseBusy(session);
        const DIM2 = '\x1b[2m';
        const RST = '\x1b[0m';
        const GREY = '\x1b[90m';
        session.term.write(`\r\n${GREY}\u2717 Discarded — command not sent.${RST}\r\n`);
        const p = (msg.prompt && msg.prompt.trim()) || promptRef.current[msg.device_id] || '';
        session.inputBuffer = '';
        session.cursor = 0;
        if (p) {
          promptRef.current[msg.device_id] = p;
          session.promptLen = p.length + 1;
          session.term.write(`${DIM2}${p}${RST} `);
        }
        return;
      }

      if (msg.type === 'device_connecting') {
        // Phase 3 latency masking: the SSH console attach takes ~10s. Show a
        // "Connecting…" line so the terminal is never blank/frozen. The next
        // device_output/device_status clears it.
        // Bug 9: ONLY on the FIRST attach. An idle-reconnect (keepalive
        // recovery / re-attach) must be invisible — never dump the ⏳ banner
        // into the middle of an ongoing session. everConnected gates this.
        const session = ensureSession(msg.device_id);
        if (session.everConnected) {
          obsRenderOrder('connecting_banner_suppressed', msg.device_id);
          return;
        }
        if (!session.connecting) {
          session.connecting = true;
          const label = msg.label ?? msg.device_id;
          session.term.write(
            `\r\n${DIM}\u23f3 Connecting to ${label} console\u2026 (~10s)${RESET}`,
          );
        }
        return;
      }

      if (msg.type === 'device_auth_required') {
        // Bug 7: the console is parked at an interactive login. Show a clear
        // in-terminal cue, then open the secure masked credential dialog. We
        // clear any stale "Connecting…" flag so the line below reads cleanly.
        const session = ensureSession(msg.device_id);
        session.connecting = false;
        const isEnable = msg.stage_type === 'enable' || (msg.message && msg.message.toLowerCase().includes('enable'));
        const displayMsg = msg.message ?? (isEnable ? 'Enable Secret Required — enter device enable password' : 'Authentication required — enter credentials');
        const LOCK = '\x1b[33m'; // amber
        session.term.write(
          `\r\n${LOCK}\uD83D\uDD10 ${displayMsg}${RESET}`,
        );
        // Bug 10: enter AUTH_PASSTHROUGH — the terminal stops validating/echoing
        // input; the secure masked dialog below is the only credential path.
        setAuthActive(session, msg.device_id, msg.stage ?? 'password');
        setAuthPrompt({
          deviceId: msg.device_id,
          label: msg.label ?? msg.device_id,
          message: displayMsg,
          retry: msg.retry === true,
          stage: msg.stage ?? 'password',
          stageType: isEnable ? 'enable' : (msg.stage_type ?? undefined),
        });
        // Auth dialog owns input — lift the busy-gate and discard queued keys so
        // nothing typed pre-auth is replayed onto a credential prompt.
        session.queuedInput = [];
        releaseBusy(session);
        return;
      }

      if (msg.type === 'device_stream') {
        // Phase 3 latency masking: render command output live as it arrives.
        // The backend sends already-cleaned deltas (no ANSI/echo/prompt) and
        // suppresses the duplicate body in the final device_output.
        const session = ensureSession(msg.device_id);
        if (session.paging) return; // never inject into an active pager
        // Absolute last display boundary: strip control artifacts (^@/^C/C0) and
        // drop the chunk entirely if nothing real remains — a delayed/empty read
        // must never render a symbol.
        const clean = stripDisplayCtrl(msg.chunk ?? '');
        if (!clean) return;
        const chunk = clean.replace(/\r\n/g, '\n').replace(/\n/g, '\r\n');
        if (!chunk) return;
        if (!session.streaming) {
          session.streaming = true;
          clearGhost(session);
          clearHintBelow(session);
          const activeBuf = session.term.buffer?.active;
          if (activeBuf && activeBuf.cursorX > 0) {
            session.term.write('\r\n');
          }
        }
        session.term.write(chunk);
        // Bug 10: if an interactive login prompt streams in mid-output, switch
        // to AUTH_PASSTHROUGH immediately so subsequent keystrokes are never
        // validated/echoed. The dialog opens on the final device_output, but the
        // gate must arm the instant the prompt appears.
        const streamStage = detectAuthPrompt(msg.chunk ?? '');
        if (streamStage) setAuthActive(session, msg.device_id, streamStage);
        return;
      }

      if (msg.type === 'pager_active') {
        // Device is paging. Enter pager mode: suppress ghost/suggest. The
        // --More-- affordance is drawn by the device_output(paging) page that
        // accompanies this message (avoids a duplicate affordance line). Raw
        // keys are routed by term.onData.
        const session = ensureSession(msg.device_id);
        session.paging = true;
        clearGhost(session);
        clearHintBelow(session);
        clearLightbulb(msg.device_id);
        return;
      }

      if (msg.type === 'pager_done') {
        // Pager finished. The final page + prompt already arrived via
        // device_output; just leave pager mode so normal input resumes.
        const session = sessionsRef.current.get(msg.device_id);
        if (session) session.paging = false;
        return;
      }

      if (msg.type === 'ghost_text') {
        // Bug 4a — drop a stale response: a newer keystroke has already bumped
        // the sequence, so this ghost describes a prior line and must not paint.
        if (
          msg.seq != null &&
          suggestSeqRef.current[msg.device_id] != null &&
          msg.seq !== suggestSeqRef.current[msg.device_id]
        ) {
          return;
        }
        const session = sessionsRef.current.get(msg.device_id);
        let text = msg.text;
        if (session) {
          // Tier 1 device-'?'-help ghost always wins for the live device; only
          // fall back to the backend KB/RAG/learned ghost when help has none.
          const local = ghostFromHelp(session.helpTokens, session.helpHead, session.inputBuffer);
          if (local) text = local;
        }
        ghostRef.current[msg.device_id] = text;
        if (session) showGhost(session, text);
        setMetrics((current) => ({
          ...current,
          [msg.device_id]: { ...(current[msg.device_id] ?? defaultMetrics()), ghostText: text },
        }));
        return;
      }

      if (msg.type === 'live_hint') {
        // Bug 4a — ignore a stale hint superseded by a newer keystroke. Never
        // drop an afterEnter hint (it describes the just-run command, not the
        // live input line, so it isn't tied to the keystroke sequence).
        if (
          msg.afterEnter !== true &&
          msg.seq != null &&
          suggestSeqRef.current[msg.device_id] != null &&
          msg.seq !== suggestSeqRef.current[msg.device_id]
        ) {
          return;
        }
        // Live "did you mean" hint drawn INLINE one row below the input,
        // exactly like the prompt_toolkit CLI demo.
        hintRef.current[msg.device_id] = msg.hint;
        setLiveHints((current) => ({ ...current, [msg.device_id]: msg.hint }));
        const session = sessionsRef.current.get(msg.device_id);
        // afterEnter hints describe the command the engineer JUST ran (device
        // returned an error). The input line is empty by then, so arm/draw the
        // 💡 at the fresh prompt regardless of inputBuffer.
        const afterEnter = msg.afterEnter === true;
        const armCommand = afterEnter
          ? (msg.command ?? msg.hint?.fix ?? '').trim()
          : (session?.inputBuffer.trim() ?? '');
        if (session && !session.confirm) {
          if (msg.hint && (afterEnter || session.inputBuffer.trim().length > 0)) {
            drawHintBelow(session, formatHintLine(msg.hint));
            // Phase 7 req 3: arm the inline lightbulb (Ctrl+. / click 💡).
            lightbulbRef.current[msg.device_id] = {
              command: armCommand,
              fix: msg.hint.fix ?? undefined,
            };
            // Make the 💡 visible inline at the prompt right away.
            updateLightbulbAnchor(msg.device_id);
          } else {
            clearHintBelow(session);
            clearLightbulb(msg.device_id);
          }
        } else if (!msg.hint) {
          clearLightbulb(msg.device_id);
        }
        return;
      }

      if (msg.type === 'history_restore') {
        // Restored command history from session_log.sqlite after a reconnect —
        // seed the session so Up/Down arrows recall prior commands. The backend
        // returns NEWEST-first (get_history: ORDER BY id DESC), which already
        // matches the local convention (unshift on Enter puts newest at index
        // 0), so the first Up-arrow recalls the most recent command. Do NOT
        // reverse here — that would surface the oldest command first.
        const session = ensureSession(msg.device_id);
        session.history = [...msg.history];
        session.historyIdx = -1;
        return;
      }

      if (msg.type === 'clear_warnings') {
        // The input line changed (or was cleared): drop any executed-command
        // warnings that belong to a PRIOR input so the panel never shows a
        // stale state. Empty inputKey means "clear everything".
        setErrors((current) => {
          const existing = current[msg.device_id] ?? [];
          if (existing.length === 0) return current;
          return { ...current, [msg.device_id]: [] };
        });
        return;
      }

      if (msg.type === 'error_panel') {
        const session = sessionsRef.current.get(msg.device_id);
        if (session) {
          clearGhost(session);
          // A step failed (or was flagged pre-exec): abort any guided config
          // block so CLIQ never silently advances past a problem.
          session.pendingBlock = [];
          // ONE clean, CLIQ-branded line — no '%' prefix (that looks like an IOS
          // device error and confuses engineers). Cyan ⚡ marks it as CLIQ.
          // `quiet` cards (e.g. the after-Enter capability advisory) skip the
          // terminal write entirely: the device already printed its rejection
          // and prompt, and the inline 💡 live_hint covers the in-terminal cue,
          // so writing here would emit a duplicate prompt line.
          // Bug 4: NEVER draw a prompt while a '?'-help reply is pending — that
          // was an untracked redraw path that printed 'R1#' before the help.
          if (!msg.quiet && !promptSuppressedByHelp(session, msg.device_id, 'error_panel')) {
            const CLIQ = '\x1b[36m';   // cyan
            const BOLD = '\x1b[1m';
            const RST = '\x1b[0m';
            session.term.write(`\r\n${CLIQ}${BOLD}⚡ CLIQ:${RST}${CLIQ} ${msg.what}${RST}\r\n`);
            if (msg.what === 'Device not connected' || session.disconnected) {
              session.disconnected = true;
              session.frozen = true;
              promptRef.current[msg.device_id] = '';
              session.promptLen = 0;
              session.inputBuffer = '';
              session.cursor = 0;
              if (!session.disconnectedNoticeShown) {
                session.disconnectedNoticeShown = true;
                session.term.write('\r\n\x1b[33m⚡ CLIQ: Device disconnected — reconnect to continue.\x1b[0m\r\n');
              }
              setReconnectBanner(msg.device_id, {
                phase: 'failed',
                attempt: 0,
                maxAttempts: RECONNECT_MAX_ATTEMPTS,
                message: 'Device disconnected. Click Reconnect to resume.',
              });
            } else {
              const p = promptRef.current[msg.device_id] ?? '$';
              session.term.write(`${p} `);
            }
            // A TERMINAL error (execution failed / not connected) carries NO
            // inputKey and is NOT followed by a device_output, so this prompt is
            // the command boundary — lift the busy-gate and replay queued keys.
            // Advisory validation hints DO carry an inputKey and are followed by
            // the real device_output that releases busy, so leave those alone.
            if (!msg.inputKey) releaseBusy(session);
          }
        }
        // REPLACE, don't append: the panel holds a single authoritative set of
        // warnings for the CURRENT input (keyed by inputKey). A new analysis
        // cycle for a different line replaces the prior set; warnings for the
        // same line are de-duplicated by type+message so the identical
        // "WRONG DEVICE" can never render twice.
        const incoming: DeviceError = {
          type: msg.errorType,
          what: msg.what,
          how: msg.how,
          fixCommand: msg.fixCommand,
          inputKey: msg.inputKey,
        };
        setErrors((current) => {
          const prior = current[msg.device_id] ?? [];
          // Keep only warnings tied to this same input (drop stale prior-line ones).
          const sameInput = msg.inputKey
            ? prior.filter((e) => e.inputKey === msg.inputKey)
            : [];
          // De-duplicate by type + message.
          const isDup = sameInput.some(
            (e) => e.type === incoming.type && e.what === incoming.what,
          );
          const next = isDup ? sameInput : [...sameInput, incoming];
          return { ...current, [msg.device_id]: next };
        });
        if (msg.errorType === 'typo') {
          setMetrics((current) => ({
            ...current,
            [msg.device_id]: {
              ...(current[msg.device_id] ?? defaultMetrics()),
              typosCaught: (current[msg.device_id]?.typosCaught ?? 0) + 1,
            },
          }));
        }
        return;
      }

      if (msg.type === 'session_expired') {
        const session = sessionsRef.current.get(msg.device_id);
        if (msg.reason === 'closed_by_user' || (session && (session.disconnected || session.intentionalExit))) {
          promptRef.current[msg.device_id] = '';
          if (session) {
            session.disconnected = true;
            session.frozen = true;
            session.intentionalExit = true;
            session.promptLen = 0;
            session.inputBuffer = '';
            session.cursor = 0;
            clearGhost(session);
            clearHintBelow(session);
            clearReconnectTimers(msg.device_id);
            const ctrl = reconnectRef.current[msg.device_id];
            if (ctrl) ctrl.active = false;
            if (!session.disconnectedNoticeShown) {
              session.disconnectedNoticeShown = true;
              const text = msg.message ?? '⚡ CLIQ: Device disconnected — reconnect to continue.';
              session.term.write(`\r\n\x1b[33m${text}\x1b[0m\r\n`);
            }
            setReconnectBanner(msg.device_id, {
              phase: 'failed',
              attempt: 0,
              maxAttempts: RECONNECT_MAX_ATTEMPTS,
              message: 'Device disconnected. Click Reconnect to resume.',
            });
          }
          setMetrics((current) => ({
            ...current,
            [msg.device_id]: {
              ...(current[msg.device_id] ?? defaultMetrics()),
              status: 'disconnected',
              latency: 0,
            },
          }));
          return;
        }
        // Session Health Monitor: the device session died mid-use. FREEZE input
        // immediately (no commands into a dead socket), surface the sticky
        // reconnect banner at the top of the terminal, and begin auto-reconnect
        // under the SAME session token (no re-login). The backend has released
        // the runtime + console lock, so connect_device re-attaches cleanly.
        if (session) {
          const YEL = '\x1b[33m';
          const BOLD = '\x1b[1m';
          const RST = '\x1b[0m';
          const text = msg.message ?? 'Device session expired or unreachable — reconnecting…';
          session.term.write(`\r\n${YEL}${BOLD}⚠ CLIQ:${RST}${YEL} ${text}${RST}\r\n`);
        }
        freezeAndReconnect(msg.device_id);
        setMetrics((current) => ({
          ...current,
          [msg.device_id]: {
            ...(current[msg.device_id] ?? defaultMetrics()),
            status: 'disconnected',
            latency: 0,
          },
        }));
        return;
      }

      if (msg.type === 'device_disconnected') {
        promptRef.current[msg.device_id] = '';
        const session = sessionsRef.current.get(msg.device_id);
        if (session) {
          session.disconnected = true;
          session.frozen = true;
          session.intentionalExit = true;
          session.promptLen = 0;
          session.inputBuffer = '';
          session.cursor = 0;
          clearGhost(session);
          clearHintBelow(session);
          clearReconnectTimers(msg.device_id);
          const ctrl = reconnectRef.current[msg.device_id];
          if (ctrl) ctrl.active = false;
          if (!session.disconnectedNoticeShown) {
            session.disconnectedNoticeShown = true;
            const text = msg.message ?? '⚡ CLIQ: Device disconnected — reconnect to continue.';
            session.term.write(`\r\n\x1b[33m${text}\x1b[0m\r\n`);
          }
          setReconnectBanner(msg.device_id, {
            phase: 'failed',
            attempt: 0,
            maxAttempts: RECONNECT_MAX_ATTEMPTS,
            message: 'Device disconnected. Click Reconnect to resume.',
          });
        }
        setMetrics((current) => ({
          ...current,
          [msg.device_id]: {
            ...(current[msg.device_id] ?? defaultMetrics()),
            status: 'disconnected',
            latency: 0,
          },
        }));
        return;
      }

      if (msg.type === 'device_status') {
        // Bug 7: a successful connect clears any pending interactive-login
        // dialog for this device.
        setAuthPrompt((cur) => (cur && cur.deviceId === msg.device_id ? null : cur));
        // Bug 9: mark the device as having connected at least once so any later
        // idle re-attach stays silent (no ⏳ banner).
        if ((msg.status ?? 'connected') === 'connected') {
          const s = sessionsRef.current.get(msg.device_id);
          if (s) {
            if (msg.session_gen) {
              s.sessionGen = msg.session_gen;
            }
            const isFirstConnect = !s.everConnected;
            s.everConnected = true;
            s.connecting = false;
            s.disconnected = false;
            s.intentionalExit = false;
            s.frozen = false;
            s.disconnectedNoticeShown = false;
            // Bug 10: login completed (device reached a connected status) —
            // leave AUTH_PASSTHROUGH so normal command processing resumes.
            setAuthActive(s, msg.device_id, null);

            if (isFirstConnect) {
              const deviceLabel = deviceMap.get(msg.device_id)?.label ?? msg.device_id;
              s.term.writeln(`\x1b[1;32mCLIQ\x1b[0m \x1b[32mConnected to ${deviceLabel}\x1b[0m`);
            }
          }
          // In-session reconnect: if this device was mid-reconnect, resume it —
          // re-arm input, clear the sticky banner, and confirm in the terminal.
          markReconnected(msg.device_id);
        } else if (msg.status === 'disconnected') {
          promptRef.current[msg.device_id] = '';
          const s = sessionsRef.current.get(msg.device_id);
          if (s) {
            s.disconnected = true;
            s.frozen = true;
            s.promptLen = 0;
            s.inputBuffer = '';
            s.cursor = 0;
            clearGhost(s);
            clearHintBelow(s);
          }
        }
        if ('prompt' in msg && typeof msg.prompt === 'string') {
          const trimmedPrompt = msg.prompt.trim();
          promptRef.current[msg.device_id] = trimmedPrompt;
          const s = sessionsRef.current.get(msg.device_id);
          if (s) s.promptLen = trimmedPrompt ? trimmedPrompt.length + 1 : 0;
        }
        setMetrics((current) => ({
          ...current,
          [msg.device_id]: {
            ...(current[msg.device_id] ?? defaultMetrics()),
            mode: msg.mode ?? current[msg.device_id]?.mode ?? 'exec',
            status: msg.status ?? current[msg.device_id]?.status ?? 'connected',
            latency: msg.latency ?? current[msg.device_id]?.latency ?? 0,
          },
        }));
        return;
      }

      // ── Phase 6: intelligence panel messages ───────────────────────────

      if (msg.type === 'intel_answer') {
        // Advisory answers are PANEL-ONLY — never echoed into the device
        // terminal (no `session`/`term` reference here on purpose; that's
        // what makes reintroducing a terminal write for this branch
        // impossible without deliberately wiring a new terminal access point).
        // Add to intelligence panel.
        const item: import('../types/api').IntelAnswerItem = {
          kind: 'answer',
          id: `answer-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          question: msg.question,
          tier: msg.tier,
          message: msg.message,
          results: msg.results,
          groups: msg.groups,
          sequence: msg.sequence ?? [],
          toolkit: msg.toolkit ?? null,
          reference: msg.reference ?? null,
        };
        setIntelItems((current) => ({
          ...current,
          [msg.device_id]: [item, ...(current[msg.device_id] ?? [])].slice(0, 20),
        }));
        return;
      }

      if (msg.type === 'active_context') {
        // Feature 2 — server-authoritative sticky protocol context; mirror it.
        setActiveContext((current) => ({
          ...current,
          [msg.device_id]: msg.context ?? null,
        }));
        return;
      }

      if (msg.type === 'intel_memory') {
        if (!msg.resolutions || msg.resolutions.length === 0) return;
        const item: import('../types/api').IntelMemoryItem = {
          kind: 'memory',
          id: `memory-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          trigger: msg.trigger,
          resolutions: msg.resolutions,
        };
        setIntelItems((current) => ({
          ...current,
          [msg.device_id]: [item, ...(current[msg.device_id] ?? [])].slice(0, 20),
        }));
        return;
      }

      if (msg.type === 'intel_resolve_ack') {
        // Flash a brief confirmation in the terminal.
        const session = sessionsRef.current.get(msg.device_id);
        // Bug 4: never inject a prompt while a '?'-help reply is pending.
        if (session && !promptSuppressedByHelp(session, msg.device_id, 'intel_resolve_ack')) {
          const GREEN = '\x1b[32m';
          const RST2 = '\x1b[0m';
          session.term.write(`${GREEN}⚡ CLIQ: Resolution stored.${RST2}\r\n`);
          const p = promptRef.current[msg.device_id] ?? '';
          if (p) session.term.write(`${p} `);
        }
        return;
      }

      // ── Phase 7: VS Code-style explain (Fix/Explain affordance) ─────────
      if (msg.type === 'intel_explain') {
        const item: import('../types/api').IntelExplainItem = {
          kind: 'explain',
          id: `explain-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          command: msg.command,
          description: msg.description,
          source: msg.source,
          verdict: msg.verdict,
          severity: msg.severity,
          reason: msg.reason,
          suggestion: msg.suggestion,
        };
        setIntelItems((current) => ({
          ...current,
          [msg.device_id]: [item, ...(current[msg.device_id] ?? [])].slice(0, 20),
        }));
        return;
      }

      // ── Phase 7 (req 5): device-native '?' context help — passthrough ───
      // Priority 2: streamed CML-style. Each message is ONE page; `more` means
      // the device is at a --More-- prompt (Space fetches the next page).
      if (msg.type === 'device_help') {
        const session = sessionsRef.current.get(msg.device_id);
        if (session) {
          // Faithful device-error passthrough: an invalid '?'-help path makes
          // the device answer with a '%'-prefixed error (e.g. '% Unrecognized
          // command') instead of an options list. Render it EXACTLY like CML —
          // never a blank prompt — and do NOT feed it to the ghost engine.
          if (msg.isError) {
            const errText = (msg.output ?? '')
              .replace(/\r\n/g, '\n')
              .replace(/\r/g, '\n')
              .trimEnd();
            // Help is advisory/display-only: reset the line buffer and draw a
            // single fresh prompt. The partial is never re-echoed as input, and
            // the error text is NOT cached as ghost tokens.
            session.paging = false;
            const pErr = promptRef.current[msg.device_id] ?? '';
            session.promptLen = pErr.length + 1;
            session.inputBuffer = '';
            session.cursor = 0;
            ghostRef.current[msg.device_id] = '';
            clearGhost(session);
            clearHintBelow(session);
            // Bug 4: the help round-trip resolved (error) — release the gate so
            // subsequent command output renders normally.
            clearHelpPending(session);
            // Bug 4: flush the device error FIRST, then draw exactly ONE prompt.
            // Callback ordering guarantees the prompt CANNOT render until the
            // error text has been written to the terminal buffer (no premature /
            // dangling prompt line).
            const errOut = errText ? `\r\n${errText.replace(/\n/g, '\r\n')}` : '';
            const errTail = `\r\n${pErr} `;
            if (errOut) {
              session.term.write(errOut, () => {
                session.term.write(errTail);
                obsRenderOrder('help_error_flushed', msg.device_id);
              });
            } else {
              session.term.write(errTail);
              obsRenderOrder('help_error_flushed', msg.device_id);
            }
            return;
          }
          // Priority 5: render ONLY the clean help body. Defensively drop any
          // pager artifact ('--More--'), stray IOS error ('% ...') or bare
          // prompt redisplay that slipped past the backend so no premature /
          // duplicate prompt appears.
          const help = (msg.output ?? '')
            .replace(/\r\n/g, '\n')
            .replace(/\r/g, '\n')
            .split('\n')
            .filter((line) => {
              const t = line.trim();
              if (!t) return true; // keep internal blank lines
              if (/--+\s*more\s*--+/i.test(t)) return false;
              if (/^%/.test(t)) return false;
              // Bug B: drop ANY line that begins with the device prompt
              // (hostname#/>, optionally a submode) — command echoes / prompt
              // re-displays, including the stray intermediate 'R1#' line right
              // after the '?'. Help body lines never start with the prompt.
              if (/^[\w-]+(\([^)]+\))?[#>]/.test(t)) return false;
              return true;
            })
            .join('\n')
            .trim();
          // Bug 4: do NOT write the help body here. It is buffered and flushed
          // together with the trailing '-- More --' / prompt below in ONE
          // atomic write, so a prompt can never render before the help text.
          const helpOut = help ? `\r\n${help.replace(/\n/g, '\r\n')}` : '';
          // ── Priority 1: ?-help → ghost HANDOFF ──────────────────────────

          // Parse THIS page's continuation tokens and MERGE them onto the
          // cached head so the engineer's next keystrokes ghost-complete from
          // the authoritative live-device help — even mid-stream, before the
          // whole list has paged in.
          const tokens = parseHelpTokens(msg.output ?? '');
          const partialStr = msg.partial ?? '';
          const hwords = partialStr.split(/\s+/).filter(Boolean);
          const head = (
            /\s$/.test(partialStr) ? hwords.join(' ') : hwords.slice(0, -1).join(' ')
          ).toLowerCase();
          if (session.helpHead === head && session.helpTokens.length > 0) {
            const seen = new Set(session.helpTokens.map((t) => t.toLowerCase()));
            for (const t of tokens) {
              if (!seen.has(t.toLowerCase())) session.helpTokens.push(t);
            }
          } else {
            session.helpHead = head;
            session.helpTokens = tokens;
          }
          // eslint-disable-next-line no-console
          console.debug(
            '[GHOST] (handoff) head=%o cached %d token(s) more=%s',
            head, session.helpTokens.length, msg.more === true,
          );

          if (msg.more === true) {
            // Priority 2/3: more pages remain. Enter pager mode so Space fetches
            // the next page (Enter = line, q = quit). Keep the line buffer empty
            // and DON'T draw a prompt yet.
            session.paging = true;
            session.inputBuffer = '';
            session.cursor = 0;
            ghostRef.current[msg.device_id] = '';
            // Bug 4: more pages remain — KEEP the help gate armed so a stray
            // prompt redraw between pages is still dropped. Re-arm the safety
            // timer so a long pager session can never trip the auto-clear while
            // the engineer is still paging.
            armHelpPending(session, msg.device_id);
            // Bug 4: flush the buffered help body, THEN the '-- More --' line, in
            // one atomic write so the affordance never precedes the help text.
            session.term.write(
              `${helpOut}\r\n${DIM}-- More -- (Space = next page · Enter = line · Tab = quit)${RESET}`,
            );
          } else {
            // Help complete. Priority 4/5: draw exactly ONE fresh prompt (no
            // duplicate R1#) and RESTORE the command the engineer was typing
            // before the '?'. After 'show ?' the line comes back as 'show ' so
            // they can keep typing the next token instead of retyping it.
            session.paging = false;
            const p = promptRef.current[msg.device_id] ?? '';
            session.promptLen = p.length + 1;
            const restore = partialStr;
            session.inputBuffer = restore;
            session.cursor = restore.length;
            ghostRef.current[msg.device_id] = '';
            clearGhost(session);
            clearHintBelow(session);
            // Bug 4: help round-trip resolved (last page) — release the gate.
            clearHelpPending(session);
            // Bug 4: flush ALL buffered help text FIRST, then draw exactly ONE
            // prompt with the restored partial. Callback ordering gates the
            // prompt redraw behind the help flush so the prompt CANNOT render
            // until the help body has been written to the terminal buffer
            // (kills the early-prompt corruption).
            const tail = `\r\n${p} ${restore}`;
            if (helpOut) {
              session.term.write(helpOut, () => {
                session.term.write(tail);
                obsRenderOrder('help_flushed', msg.device_id);
              });
            } else {
              session.term.write(tail);
              obsRenderOrder('help_flushed', msg.device_id);
            }
          }
        }
        return;
      }
    };

    ws.onerror = () => {
      if (currentGen !== socketGenRef.current || ws !== wsRef.current) return;
      setMetrics((current) => {
        const next = { ...current };
        devices.forEach((d) => {
          next[d.id] = { ...(next[d.id] ?? defaultMetrics()), status: 'error' };
        });
        return next;
      });
    };

    ws.onclose = () => {
      // Session Health — BACKEND / browser-socket death detection. If the socket
      // closes without an operator-driven teardown (backend killed/crashed,
      // network drop, heartbeat/keepalive loss), the console must NOT be left
      // open-but-unresponsive. FREEZE input for every live session, surface the
      // sticky reconnect banner at the top of the terminal, and begin
      // auto-reconnect under the SAME token/host (no re-login, no fresh
      // terminal, scrollback preserved).
      stopHeartbeat();
      if (currentGen !== socketGenRef.current || ws !== wsRef.current || intentionalClose) return;
      const YEL = '\x1b[33m';
      const BOLD = '\x1b[1m';
      const RST = '\x1b[0m';
      const ids = new Set<string>([
        ...devices.map((d) => d.id),
        ...sessionsRef.current.keys(),
      ]);
      ids.forEach((deviceId) => {
        const session = sessionsRef.current.get(deviceId);
        if (session && (session.disconnected || session.intentionalExit)) {
          return;
        }
        // Announce the drop once (only when not already reconnecting), then
        // freeze + start the auto-reconnect controller for this device.
        if (session && !session.frozen) {
          session.term.write(
            `\r\n${YEL}${BOLD}⚠ CLIQ:${RST}${YEL} Connection lost — reconnecting…${RST}\r\n`,
          );
        }
        freezeAndReconnect(deviceId);
      });
      setMetrics((current) => {
        const next = { ...current };
        devices.forEach((d) => {
          next[d.id] = {
            ...(next[d.id] ?? defaultMetrics()),
            status: 'disconnected',
            latency: 0,
          };
        });
        return next;
      });
    };

    return () => {
      intentionalClose = true;
      stopHeartbeat();
      try {
        ws.close();
      } catch { /* already closing */ }
      if (wsRef.current === ws) {
        wsRef.current = null;
      }
      outboundQueueRef.current = [];
    };
  }, [devicesKey, host, token, wsEpoch]);

  useEffect(() => {
    const onResize = () => {
      sessionsRef.current.forEach((s) => s.fit.fit());
      // Re-anchor the inline 💡 after the terminal reflows.
      if (focusedDeviceRef.current) updateLightbulbAnchor(focusedDeviceRef.current);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const closeLightbulb = useCallback(() => setLightbulbOverlay(null), []);

  // Bug 7: submit operator-entered credentials for a parked console login. The
  // values are sent straight over the WS and the dialog is dismissed; nothing
  // is retained client-side (no state, no logging of the password).
  const submitAuth = useCallback((deviceId: string, username: string, password: string, stageType?: string) => {
    send({ type: 'auth_credentials', device_id: deviceId, username, password, stage_type: stageType });
    // Bug 10: keep AUTH_PASSTHROUGH armed until the backend confirms a connected
    // status (device_status) or re-prompts — so the terminal stays inert while
    // complete_login runs. Only dismiss the visible dialog here.
    setAuthPrompt(null);
  }, []);

  const cancelAuth = useCallback((deviceId: string) => {
    const session = sessionsRef.current.get(deviceId);
    if (session) {
      session.connecting = false;
      // User abandoned the login — leave AUTH_PASSTHROUGH so the
      // terminal is usable again (no permanently swallowed keystrokes).
      setAuthActive(session, deviceId, null);
      const p = promptRef.current[deviceId] ?? '';
      session.term.write(`\r\n${DIM}Authentication cancelled.${RESET}\r\n${p} `);
    }
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cancel_auth', device_id: deviceId }));
    }
    setAuthPrompt(null);
  }, []);

  // In-session reconnect — manual [Reconnect] from the sticky banner. Resets the
  // retry budget and fires an immediate attempt (bypasses the backoff wait).
  const reconnect = useCallback((deviceId: string) => {
    const ctrl = ensureReconnectCtrl(deviceId);
    clearReconnectTimers(deviceId);
    ctrl.active = true;
    ctrl.attempt = 1;
    const session = sessionsRef.current.get(deviceId);
    if (session) {
      session.frozen = true;
      session.disconnected = false;
      session.intentionalExit = false;
      session.disconnectedNoticeShown = false;
      session.inputBuffer = '';
      session.cursor = 0;
      session.promptLen = 0;
    }
    setReconnectBanner(deviceId, {
      phase: 'retrying',
      attempt: 1,
      maxAttempts: RECONNECT_MAX_ATTEMPTS,
      message: 'Reconnecting…',
    });
    obsReconnect('manual', deviceId);
    doReconnectAttempt(deviceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Explicitly request elevation to privileged EXEC mode ('#')
  const requestEnable = useCallback(
    (deviceId: string) => {
      send({ type: 'request_enable', device_id: deviceId });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return {
    requestEnable,
    metrics, errors, liveHints,
    intelItems,
    activeContext,
    registerMount, focusDevice, runFix, dismissError, refitAll,
    dismissIntelItem, markResolved, recallMemory, ask,
    clearContext, setContext,
    insertToTerminal, insertBlock, explain,
    lightbulbOverlay, closeLightbulb,
    lightbulbAnchor, openLightbulbOverlay,
    authPrompt, submitAuth, cancelAuth,
    reconnectBanners, reconnect,
  };
}
