import { useEffect, useRef, useState, type ReactNode } from 'react';
import ReactFlow, { Background, Controls, MarkerType, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Device, IntelAnswerItem, IntelExplainItem, IntelMemoryItem, IntelPanelItem, IntelReference, IntelResult, IntelSequenceStep, IntelToolkit, TopologyLink, TopologyNode } from '../types/api';
import { useTerminalSessions } from '../lib/useTerminalSessions';
import type { ReconnectInfo } from '../lib/useTerminalSessions';

type MainTerminalUiProps = {
  host: string;
  token: string;
  userName?: string;
  labTitle?: string;
  labExpiresAt?: string | null;
  precheckDeviceId?: string | null;
  allDevices: Device[];
  openDevices: Device[];
  topologyNodes: TopologyNode[];
  topologyLinks: TopologyLink[];
  activeDeviceId: string;
  onOpenDevice: (deviceId: string) => void;
  onSetActiveDevice: (deviceId: string) => void;
  onCloseDevice?: (deviceId: string) => void;
  onExitTerminal?: () => void;
  onChangeLab?: () => void;
};

const MODE_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  exec:            { bg: '#dcfce7', text: '#15803d', label: 'EXEC' },
  enable:          { bg: '#dbeafe', text: '#1d4ed8', label: 'ENABLE' },
  config:          { bg: '#fef9c3', text: '#a16207', label: 'CONFIG' },
  'config-if':     { bg: '#fff7ed', text: '#c2410c', label: 'CONFIG-IF' },
  'config-router': { bg: '#fdf4ff', text: '#7e22ce', label: 'CONFIG-ROUTER' },
  'config-line':   { bg: '#fdf4ff', text: '#7e22ce', label: 'CONFIG-LINE' },
  'config-vlan':   { bg: '#ecfdf5', text: '#065f46', label: 'CONFIG-VLAN' },
};

function getModeStyle(mode?: string) {
  return (
    MODE_STYLES[mode ?? 'exec'] ?? { bg: '#f1f5f9', text: '#475569', label: (mode ?? 'exec').toUpperCase() }
  );
}

// Read a persisted boolean flag from localStorage (UI layout prefs). Falls back
// to `fallback` when unset or storage is unavailable (e.g. private mode).
function readPersistedFlag(key: string, fallback: boolean): boolean {
  try {
    const v = window.localStorage.getItem(key);
    return v === null ? fallback : v === 'true';
  } catch {
    return fallback;
  }
}

function writePersistedFlag(key: string, value: boolean): void {
  try {
    window.localStorage.setItem(key, value ? 'true' : 'false');
  } catch {
    /* storage unavailable — ignore */
  }
}

function errorStyle(type: 'typo' | 'wrong_mode' | 'missing_arg' | 'wrong_device') {
  if (type === 'typo')
    return { border: '#f59e0b', bg: '#fffbeb', accent: '#f59e0b', label: 'TYPO', icon: '~' };
  if (type === 'wrong_mode')
    return { border: '#ef4444', bg: '#fef2f2', accent: '#ef4444', label: 'WRONG MODE', icon: '!' };
  if (type === 'wrong_device')
    return { border: '#a855f7', bg: '#faf5ff', accent: '#a855f7', label: 'WRONG DEVICE', icon: '?' };
  return { border: '#f97316', bg: '#fff7ed', accent: '#f97316', label: 'MISSING ARG', icon: '+' };
}

function statusDot(state?: string): string {
  if (state === 'BOOTED' || state === 'STARTED') return '#22c55e';
  if (state === 'STOPPED') return '#94a3b8';
  if (state === 'STARTING') return '#f59e0b';
  return '#cbd5e1';
}

function linkLabel(link: TopologyLink): string {
  if (link.label) {
    return link.label;
  }

  const srcIface = link.sourceHandle || '';
  const tgtIface = link.targetHandle || '';

  return [srcIface, tgtIface].filter(Boolean).join(' - ');
}

// ── Phase 6/7: Intel Panel card component ───────────────────────────────────

type TierMeta = { label: string; icon: string; badge: string; bar: string; tint: string };

const TIER_META: Record<string, TierMeta> = {
  exact:      { label: 'Exact match', icon: '✓', badge: '#16a34a', bar: '#22c55e', tint: 'rgba(34,197,94,0.08)' },
  suggestion: { label: 'Suggestions', icon: '≈', badge: '#d97706', bar: '#f59e0b', tint: 'rgba(245,158,11,0.08)' },
  related:    { label: 'Related', icon: '⋯', badge: '#2563eb', bar: '#60a5fa', tint: 'rgba(96,165,250,0.08)' },
  fallback:   { label: 'No match', icon: '∅', badge: '#64748b', bar: '#94a3b8', tint: 'rgba(148,163,184,0.06)' },
  memory:     { label: 'Memory', icon: '🧠', badge: '#7c3aed', bar: '#a78bfa', tint: 'rgba(167,139,250,0.08)' },
};

function tierMeta(tier: string): TierMeta {
  return TIER_META[tier] ?? TIER_META.fallback;
}

// Cross-platform advisory note appended by the server, e.g.
// "(from iosxe reference — verify on your platform)".
const CROSS_PLATFORM_RE = /\(from\s+[\w-]+\s+reference[^)]*\)/i;

function splitAnnotation(message: string): { base: string; annotation: string | null } {
  const m = message.match(CROSS_PLATFORM_RE);
  if (!m) return { base: message, annotation: null };
  return {
    base: message.replace(CROSS_PLATFORM_RE, '').trim(),
    annotation: m[0].replace(/^\(|\)$/g, '').trim(),
  };
}

// Reduce a long PDF-style description to a single scannable sentence.
function firstSentence(text: string, max = 150): string {
  if (!text) return '';
  const trimmed = text.replace(/\s+/g, ' ').trim();
  const m = trimmed.match(/^.*?[.!?](?=\s|$)/);
  let s = m ? m[0] : trimmed;
  if (s.length > max) s = `${s.slice(0, max).trimEnd()}…`;
  return s;
}

function Badge({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="rounded bg-slate-700/70 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-slate-300"
    >
      {children}
    </span>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        const done = () => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1300);
        };
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(() => undefined);
        }
      }}
      title="Copy command — advisory only, paste & run it yourself"
      className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-semibold transition-colors ${
        copied
          ? 'border-emerald-600 bg-emerald-600/20 text-emerald-300'
          : 'border-slate-600 bg-slate-700/40 text-slate-300 hover:border-slate-500 hover:text-slate-100'
      }`}
    >
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  );
}

function ResultRow({ result, onInsert, onExplain }: {
  result: IntelResult;
  onInsert?: (command: string) => void;
  onExplain?: (command: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const summary = firstSentence(result.description);
  const full = (result.description ?? '').replace(/\s+/g, ' ').trim();
  const hasMore = full.length > summary.length && full.length > 0;
  // Full DITA syntax notation (reference only) — shown alongside the clean,
  // insertable command whenever it differs (product-CR web-sourced commands).
  const fullSyntax = (result.syntax_variants ?? []).find(
    (s) => s && s.trim() !== result.command.trim(),
  );
  return (
    <li className="px-2.5 py-2">
      <div className="flex items-start justify-between gap-2">
        <code className="break-all font-mono text-[12px] font-semibold leading-tight text-emerald-400">
          {result.command}
        </code>
        <div className="flex shrink-0 items-center gap-1">
          {onInsert && <InsertButton onClick={() => onInsert(result.command)} />}
          <CopyButton text={result.command} />
        </div>
      </div>
      {fullSyntax && (
        <p
          className="mt-0.5 break-all font-mono text-[10px] leading-snug text-slate-500"
          title="Full command reference syntax — Insert uses the clean command above"
        >
          Syntax: {fullSyntax}
        </p>
      )}
      {summary && <p className="mt-1 text-[11px] leading-snug text-slate-400">{summary}</p>}
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        {result.platform && <Badge>{result.platform}</Badge>}
        {result.mode && <Badge>{result.mode}</Badge>}
        {result.is_candidate && <Badge>candidate</Badge>}
        {result.source && (
          <span className="max-w-[120px] truncate text-[9px] text-slate-600" title={result.source}>
            {result.source}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {onExplain && (
            <button
              onClick={() => onExplain(result.command)}
              className="flex items-center gap-0.5 text-[10px] font-medium text-amber-400 hover:text-amber-300"
              title="Explain this command (advisory)"
            >
              <span>💡</span> Explain
            </button>
          )}
          {hasMore && (
            <button
              onClick={() => setOpen((v) => !v)}
              className="text-[10px] font-medium text-sky-400 hover:text-sky-300"
            >
              {open ? 'Hide details' : 'Show details'}
            </button>
          )}
        </div>
      </div>
      {open && hasMore && (
        <p className="mt-1.5 whitespace-pre-wrap rounded bg-slate-900/60 p-2 text-[10px] leading-relaxed text-slate-400">
          {full}
        </p>
      )}
    </li>
  );
}

// Phase 7 — Insert button: places a command at the terminal prompt for REVIEW.
// ADVISORY ONLY — labelled "Insert", never "Run". The engineer presses Enter.
function InsertButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title="Insert into terminal for review — does NOT run it"
      className="shrink-0 rounded border border-sky-700 bg-sky-700/30 px-1.5 py-0.5 text-[9px] font-semibold text-sky-300 transition-colors hover:border-sky-500 hover:bg-sky-700/50 hover:text-sky-100"
    >
      ↘ Insert
    </button>
  );
}

// Phase 7 — dedicated "Ask CLIQ" input so engineers can ask without the '?' prefix.
function AskBox({ disabled, onAsk }: { disabled: boolean; onAsk: (question: string) => void }) {
  const [value, setValue] = useState('');
  const submit = () => {
    const q = value.trim();
    if (!q) return;
    onAsk(q);
    setValue('');
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[12px] text-violet-400">
            ⚡
          </span>
          <input
            type="text"
            value={value}
            disabled={disabled}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={disabled ? 'Connect a device to ask…' : 'Ask CLIQ a question…'}
            className="w-full rounded-md border border-slate-700 bg-slate-900 py-1.5 pl-7 pr-2 text-[11px] text-slate-200 placeholder:text-slate-600 focus:border-violet-500 focus:outline-none focus:ring-1 focus:ring-violet-500/40 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </div>
        <button
          onClick={submit}
          disabled={disabled || value.trim().length === 0}
          className="shrink-0 rounded-md bg-violet-600 px-2.5 py-1.5 text-[11px] font-semibold text-white transition-colors hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Ask
        </button>
      </div>
      <p className="text-[9px] text-slate-600">
        e.g. “how do I check OSPF neighbors” — or type{' '}
        <kbd className="rounded bg-slate-800 px-1 text-slate-400">?</kbd> in the terminal.
      </p>
    </div>
  );
}

type IntelPanelCardProps = {
  item: IntelPanelItem;
  deviceId: string;
  onDismiss: () => void;
  onMarkResolved: (issueText: string, commands: string[], platform: string) => void;
  onInsert: (command: string) => void;
  onInsertBlock: (commands: string[]) => void;
  onExplain: (command: string) => void;
};

// Phase 7 req 2 — a single bordered config block (ordered config → verify) with
// ONE Insert button that loads the whole block into the CLI for guided review.
function SequenceBlock({ steps, onInsertBlock }: {
  steps: IntelSequenceStep[];
  onInsertBlock: (commands: string[]) => void;
}) {
  const config = steps.filter((s) => s.kind === 'config');
  const verify = steps.filter((s) => s.kind === 'verify');
  const allCommands = steps.map((s) => s.command);
  let n = 0;
  return (
    <div className="border-t border-slate-700/50 bg-slate-900/30 p-2.5">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-sky-300">
          ⚙ Config block
        </span>
        <button
          onClick={() => onInsertBlock(allCommands)}
          title="Insert the whole block into the CLI for review — does NOT run it"
          className="rounded border border-sky-600 bg-sky-700/30 px-2 py-0.5 text-[10px] font-semibold text-sky-200 hover:bg-sky-700/50 hover:text-sky-50"
        >
          ↘ Insert block
        </button>
      </div>
      {/* The bordered, monospace recipe with clear <placeholders>. */}
      <pre className="overflow-x-auto rounded-md border border-slate-700 bg-[#0b1020] p-2.5 font-mono text-[12px] leading-relaxed text-emerald-300">
        {config.length > 0 && (
          <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-400/80">! configure</div>
        )}
        {config.map((s, i) => (
          <div key={`c-${i}`}>
            <span className="select-none text-slate-600">{String(++n).padStart(2, ' ')}  </span>
            {s.command}
          </div>
        ))}
        {verify.length > 0 && (
          <div className="mt-1 text-[10px] font-semibold uppercase tracking-wide text-green-400/80">! verify</div>
        )}
        {verify.map((s, i) => (
          <div key={`v-${i}`}>
            <span className="select-none text-slate-600">{String(++n).padStart(2, ' ')}  </span>
            {s.command}
          </div>
        ))}
      </pre>
      <p className="mt-1.5 text-[9px] text-slate-600">
        Fill each &lt;placeholder&gt;, then press Enter per step — CLIQ never executes config for you.
      </p>
    </div>
  );
}

// Phase 5 — scenario-based advisory. A compact, grouped panel of curated
// diagnostic commands (the "preliminary toolkit"), ordered by diagnostic phase.
// Each command has a one-click Insert (loads it into the CLI for review — never
// runs it). Impactful debug commands are clearly marked and carry a warning.
function ToolkitBlock({ toolkit, onInsert }: {
  toolkit: IntelToolkit;
  onInsert: (command: string) => void;
}) {
  return (
    <div className="border-t border-slate-700/50 bg-slate-900/30 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-amber-300">
          🩺 {toolkit.title}
        </span>
      </div>
      <div className="space-y-2">
        {toolkit.phases.map((phase) => (
          <div key={phase.name}>
            <p className="mb-1 text-[9px] font-semibold uppercase tracking-wide text-slate-500">
              {phase.name}
            </p>
            <ul className="space-y-1">
              {phase.commands.map((c, i) => (
                <li
                  key={`${c.base_command}-${i}`}
                  className="group/tk rounded-md border border-slate-700/60 bg-[#0b1020] px-2 py-1.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <code className="truncate font-mono text-[11px] text-emerald-300" title={c.command}>
                        {c.command}
                      </code>
                      {c.impactful && (
                        <span
                          className="shrink-0 rounded bg-rose-900/50 px-1 py-0.5 text-[8px] font-bold uppercase tracking-wide text-rose-300 ring-1 ring-rose-700/50"
                          title="Impactful debug command — can flood the console on a live device. Never auto-run."
                        >
                          ⚠ debug
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => onInsert(c.command)}
                      title="Insert into the CLI for review — does NOT run it"
                      className="shrink-0 rounded border border-slate-600 px-1.5 py-0.5 text-[9px] font-semibold text-slate-300 opacity-0 transition-opacity hover:bg-slate-700/50 hover:text-slate-100 group-hover/tk:opacity-100"
                    >
                      ↘ Insert
                    </button>
                  </div>
                  {c.description && (
                    <p className="mt-0.5 text-[9px] leading-snug text-slate-500">{c.description}</p>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {toolkit.safety_note && (
        <div className="mt-2 flex items-start gap-1.5 rounded border border-rose-900/40 bg-rose-950/30 px-2 py-1.5">
          <span className="shrink-0 text-rose-400">⚠</span>
          <span className="text-[9px] leading-snug text-rose-300/90">{toolkit.safety_note}</span>
        </div>
      )}
      <p className="mt-1.5 text-[9px] text-slate-600">
        Advisory only — CLIQ never runs these. Insert a command, review it, then press Enter yourself.
      </p>
    </div>
  );
}

/** Per-source badge (Device / Learned / KB) for a discovery-reference command. */
function SourceBadge({ source }: { source: 'device' | 'learned' | 'kb' }) {
  const meta =
    source === 'device'
      ? { label: 'Device', cls: 'bg-emerald-900/50 text-emerald-300 ring-emerald-700/50', title: 'Advertised by this device’s own ? help — verified on this platform.' }
      : source === 'learned'
        ? { label: 'Learned', cls: 'bg-sky-900/50 text-sky-300 ring-sky-700/50', title: 'Previously accepted by a device — a learned template.' }
        : { label: 'KB', cls: 'bg-slate-700/60 text-slate-300 ring-slate-600/50', title: 'From the knowledge base — UNVERIFIED on this platform.' };
  return (
    <span
      className={`shrink-0 rounded px-1 py-0.5 text-[8px] font-bold uppercase tracking-wide ring-1 ${meta.cls}`}
      title={meta.title}
    >
      {meta.label}
    </span>
  );
}

/** Feature 3 — live-validated command reference (Device + Learned + KB). */
function ReferenceBlock({ reference, onInsert }: {
  reference: IntelReference;
  onInsert: (command: string) => void;
}) {
  return (
    <div className="border-t border-slate-700/50 bg-slate-900/30 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-cyan-300">
          📖 {reference.title}
        </span>
        {!reference.device_available && (
          <span
            className="shrink-0 rounded bg-amber-900/50 px-1 py-0.5 text-[8px] font-bold uppercase tracking-wide text-amber-300 ring-1 ring-amber-700/50"
            title="Device offline — assembled from learned + KB only."
          >
            offline
          </span>
        )}
      </div>
      {reference.unverified_note && (
        <div className="mb-1.5 flex items-start gap-1.5 rounded border border-amber-900/40 bg-amber-950/30 px-2 py-1">
          <span className="shrink-0 text-amber-400">ℹ</span>
          <span className="text-[9px] leading-snug text-amber-300/90">{reference.unverified_note}</span>
        </div>
      )}
      <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
        {reference.groups.map((group) => (
          <div key={group.name}>
            <p className="mb-1 text-[9px] font-semibold uppercase tracking-wide text-slate-500">
              {group.name}
            </p>
            <ul className="space-y-1">
              {group.commands.map((c, i) => (
                <li
                  key={`${c.base_command}-${i}`}
                  className="group/ref rounded-md border border-slate-700/60 bg-[#0b1020] px-2 py-1.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <code className="truncate font-mono text-[11px] text-emerald-300" title={c.command}>
                        {c.command}
                      </code>
                      <SourceBadge source={c.source} />
                      {c.impactful && (
                        <span
                          className="shrink-0 rounded bg-rose-900/50 px-1 py-0.5 text-[8px] font-bold uppercase tracking-wide text-rose-300 ring-1 ring-rose-700/50"
                          title="Impactful (debug/clear) command — can flood the console or clear state. Never auto-run."
                        >
                          ⚠ {c.group === 'Clear' ? 'clear' : 'debug'}
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => onInsert(c.command)}
                      title="Insert into the CLI for review — does NOT run it"
                      className="shrink-0 rounded border border-slate-600 px-1.5 py-0.5 text-[9px] font-semibold text-slate-300 opacity-0 transition-opacity hover:bg-slate-700/50 hover:text-slate-100 group-hover/ref:opacity-100"
                    >
                      ↘ Insert
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {reference.safety_note && (
        <div className="mt-2 flex items-start gap-1.5 rounded border border-rose-900/40 bg-rose-950/30 px-2 py-1.5">
          <span className="shrink-0 text-rose-400">⚠</span>
          <span className="text-[9px] leading-snug text-rose-300/90">{reference.safety_note}</span>
        </div>
      )}
      <p className="mt-1.5 text-[9px] text-slate-600">
        Advisory only — CLIQ never runs these. Device &gt; Learned &gt; KB; KB entries are unverified on this platform.
      </p>
    </div>
  );
}

function IntelPanelCard({ item, onDismiss, onMarkResolved, onInsert, onInsertBlock, onExplain }: IntelPanelCardProps) {
  if (item.kind === 'answer') {
    const a = item as IntelAnswerItem;
    const meta = tierMeta(a.tier);
    const { base, annotation } = splitAnnotation(a.message);
    const flatResults = [...a.results, ...a.groups.flatMap((g) => g.results)];
    const useGroups = a.results.length === 0 && a.groups.length > 0;
    const sequence = a.sequence ?? [];
    const toolkit = a.toolkit ?? null;
    const reference = a.reference ?? null;
    // Req 2: procedural queries return ONE bordered config block — not scattered
    // per-command cards. When a sequence is present we hide the result cards.
    // Phase 5: a troubleshooting toolkit likewise takes over the card body.
    // Feature 3: a discovery reference likewise takes over the card body.
    const hasSequence = sequence.length > 0;
    const hasToolkit = !!toolkit && toolkit.phases.length > 0;
    const hasReference = !!reference && reference.groups.length > 0;
    const hideResults = hasSequence || hasToolkit || hasReference;
    return (
      <div
        className="overflow-hidden rounded-lg border border-slate-700 bg-slate-800 text-xs shadow-sm"
        style={{ borderLeft: `3px solid ${meta.bar}` }}
      >
        {/* Card header */}
        <div
          className="flex items-center justify-between gap-2 border-b border-slate-700 px-2.5 py-1.5"
          style={{ background: meta.tint }}
        >
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-violet-400">⚡</span>
            <span className="truncate font-medium text-slate-200" title={a.question}>
              {a.question}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <span
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-bold uppercase text-white"
              style={{ background: meta.badge }}
              title={`Tier: ${meta.label}`}
            >
              <span>{meta.icon}</span>
              {meta.label}
            </span>
            <button onClick={onDismiss} className="text-slate-500 hover:text-slate-300" title="Dismiss">
              ✕
            </button>
          </div>
        </div>
        {/* Message */}
        {base && (
          <p className="border-b border-slate-700/50 px-2.5 py-1.5 text-[11px] text-slate-400">{base}</p>
        )}
        {/* Cross-platform advisory note (non-alarming) */}
        {annotation && (
          <div className="flex items-start gap-1.5 border-b border-slate-700/50 bg-sky-950/30 px-2.5 py-1.5">
            <span className="shrink-0 text-sky-400">ℹ</span>
            <span className="text-[10px] leading-snug text-sky-300/90">{annotation}</span>
          </div>
        )}
        {/* Results */}
        {!hideResults && !useGroups && flatResults.length > 0 && (
          <ul className="divide-y divide-slate-700/40">
            {flatResults.slice(0, 6).map((r, i) => (
              <ResultRow key={`${r.command}-${i}`} result={r} onInsert={onInsert} onExplain={onExplain} />
            ))}
          </ul>
        )}
        {/* Grouped results */}
        {!hideResults && useGroups && (
          <div className="divide-y divide-slate-700/40">
            {a.groups.map((g) => (
              <div key={g.label}>
                <p className="px-2.5 pt-1.5 text-[9px] font-semibold uppercase tracking-wide text-slate-500">
                  {g.label}
                </p>
                <ul className="divide-y divide-slate-700/30">
                  {g.results.slice(0, 4).map((r, i) => (
                    <ResultRow key={`${g.label}-${r.command}-${i}`} result={r} onInsert={onInsert} onExplain={onExplain} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
        {/* Phase 7 req 2 — single bordered config block (config → verify) */}
        {hasSequence && (
          <SequenceBlock steps={sequence} onInsertBlock={onInsertBlock} />
        )}
        {/* Phase 5 — scenario-based preliminary troubleshooting toolkit */}
        {hasToolkit && toolkit && (
          <ToolkitBlock toolkit={toolkit} onInsert={onInsert} />
        )}
        {/* Feature 3 — live-validated command discovery reference */}
        {hasReference && reference && (
          <ReferenceBlock reference={reference} onInsert={onInsert} />
        )}
      </div>
    );
  }

  if (item.kind === 'memory') {
    const m = item as IntelMemoryItem;
    const meta = tierMeta('memory');
    return (
      <div className="overflow-hidden rounded-lg border border-violet-900/50 bg-slate-800 text-xs shadow-sm">
        <div className="flex items-center justify-between gap-2 border-b border-violet-900/40 bg-violet-950/30 px-2.5 py-1.5">
          <div className="flex items-center gap-1.5">
            <span className="text-violet-400">🧠</span>
            <span className="font-medium text-violet-300">Past Resolution</span>
          </div>
          <button onClick={onDismiss} className="text-slate-500 hover:text-slate-300" title="Dismiss">
            ✕
          </button>
        </div>
        <ul className="divide-y divide-slate-700/40">
          {m.resolutions.slice(0, 3).map((r) => (
            <li key={r.memory_id} className="space-y-1.5 px-2.5 py-2">
              <p className="line-clamp-2 text-[11px] font-medium text-slate-300">{r.issue_text}</p>
              {r.commands.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {r.commands.slice(0, 4).map((cmd, i) => (
                    <span key={i} className="group/cmd inline-flex items-center gap-1">
                      <code className="rounded bg-slate-700 px-1.5 py-0.5 font-mono text-[10px] text-emerald-400">
                        {cmd}
                      </code>
                      <CopyButton text={cmd} />
                    </span>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                {r.platform && <Badge>{r.platform}</Badge>}
                {r.device_name && <Badge>{r.device_name}</Badge>}
                <span
                  className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase text-white"
                  style={{ background: meta.badge }}
                >
                  {r.outcome}
                </span>
                <span className="text-[9px] text-slate-600">{Math.round(r.similarity * 100)}% similar</span>
                <button
                  onClick={() => onMarkResolved(r.issue_text, r.commands, r.platform)}
                  className="ml-auto rounded bg-violet-700 px-1.5 py-0.5 text-[9px] font-semibold text-white transition-colors hover:bg-violet-600"
                  title="Mark this issue as resolved"
                >
                  Mark Resolved
                </button>
              </div>
              {r.outcome_notes && <p className="line-clamp-2 text-[10px] text-slate-500">{r.outcome_notes}</p>}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (item.kind === 'explain') {
    const e = item as IntelExplainItem;
    const sev = e.severity ?? 'info';
    const sevColor = sev === 'critical' ? '#ef4444' : sev === 'warning' ? '#f59e0b' : '#38bdf8';
    const sevBg = sev === 'critical' ? 'rgba(239,68,68,0.08)' : sev === 'warning' ? 'rgba(245,158,11,0.08)' : 'rgba(56,189,248,0.08)';
    return (
      <div
        className="overflow-hidden rounded-lg border border-slate-700 bg-slate-800 text-xs shadow-sm"
        style={{ borderLeft: `3px solid ${sevColor}` }}
      >
        <div
          className="flex items-center justify-between gap-2 border-b border-slate-700 px-2.5 py-1.5"
          style={{ background: sevBg }}
        >
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-amber-400">💡</span>
            <code className="truncate font-mono text-[11px] font-semibold text-emerald-400" title={e.command}>
              {e.command}
            </code>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <span
              className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase text-white"
              style={{ background: sevColor }}
              title={`Safety verdict: ${e.verdict}`}
            >
              {e.verdict}
            </span>
            <button onClick={onDismiss} className="text-slate-500 hover:text-slate-300" title="Dismiss">
              ✕
            </button>
          </div>
        </div>
        {e.description && (
          <p className="border-b border-slate-700/50 px-2.5 py-1.5 text-[11px] leading-snug text-slate-300">
            {e.description}
          </p>
        )}
        {e.reason && (
          <p className="px-2.5 py-1.5 text-[10px] leading-relaxed text-slate-400">{e.reason}</p>
        )}
        {e.suggestion && (
          <div className="flex items-start gap-1.5 border-t border-slate-700/50 bg-emerald-950/20 px-2.5 py-1.5">
            <span className="shrink-0 text-emerald-400">→</span>
            <div className="min-w-0 flex-1">
              <p className="text-[10px] font-semibold text-emerald-300">Suggested fix</p>
              <div className="mt-1 flex items-center justify-between gap-2">
                <code className="break-all font-mono text-[11px] text-emerald-400">{e.suggestion}</code>
                <div className="flex shrink-0 items-center gap-1">
                  <InsertButton onClick={() => onInsert(e.suggestion as string)} />
                  <CopyButton text={e.suggestion as string} />
                </div>
              </div>
            </div>
          </div>
        )}
        <div className="flex items-center gap-2 border-t border-slate-700/50 px-2.5 py-1.5">
          <InsertButton onClick={() => onInsert(e.command)} />
          {e.source && (
            <span className="truncate text-[9px] text-slate-600" title={e.source}>
              {e.source}
            </span>
          )}
        </div>
      </div>
    );
  }

  return null;
}

// In-session reconnect banner — pinned at the TOP of the terminal canvas while a
// dropped session is being resumed. The session is preserved server-side (keyed
// by token) so there is NO re-login and NO fresh terminal; scrollback stays
// intact and input is frozen until reconnect succeeds. Auto-reconnect runs with
// backoff; if it exhausts, a manual [Reconnect] button is offered.
function ReconnectBanner({ info, onReconnect }: {
  info: ReconnectInfo;
  onReconnect: () => void;
}) {
  const base =
    'pointer-events-auto absolute inset-x-0 top-0 z-20 flex items-center gap-3 px-4 py-2 text-sm font-medium shadow-lg';
  if (info.phase === 'reconnected') {
    return (
      <div className={`${base} border-b border-emerald-500/40 bg-emerald-950/90 text-emerald-200`}>
        <span aria-hidden>✓</span>
        <span>Reconnected — session resumed.</span>
      </div>
    );
  }
  if (info.phase === 'failed') {
    return (
      <div className={`${base} border-b border-amber-500/50 bg-amber-950/90 text-amber-100`}>
        <span aria-hidden>⚠</span>
        <span>Connection lost.</span>
        <button
          type="button"
          onClick={onReconnect}
          className="rounded bg-amber-500/90 px-2.5 py-0.5 text-xs font-semibold text-amber-950 transition hover:bg-amber-400"
        >
          Reconnect
        </button>
        <span className="text-xs text-amber-300/80">(session preserved — no re-login needed)</span>
      </div>
    );
  }
  // retrying
  return (
    <div className={`${base} border-b border-amber-500/40 bg-amber-950/85 text-amber-100`}>
      <span
        aria-hidden
        className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-amber-300/40 border-t-amber-200"
      />
      <span>Connection lost — reconnecting…</span>
      <span className="text-xs text-amber-300/80">
        (attempt {info.attempt}/{info.maxAttempts} · session preserved — no re-login needed)
      </span>
    </div>
  );
}

// Bug 7: secure interactive-login dialog. Shown when a device console is parked
// at a username/password prompt. Local state holds the typed credentials only
// for the lifetime of the dialog; on submit they are handed to the WS sender
// and this component unmounts, so nothing lingers. The password field is masked
// and autocomplete is disabled so the browser never caches it.
function AuthPromptDialog(props: {
  label: string;
  message: string;
  retry: boolean;
  onSubmit: (username: string, password: string) => void;
  onCancel: () => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const userRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    userRef.current?.focus();
  }, []);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    props.onSubmit(username, password);
  };
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60">
      <form
        onSubmit={submit}
        className="w-80 rounded-lg border border-slate-600 bg-slate-800 p-5 shadow-2xl"
        autoComplete="off"
      >
        <div className="mb-3 flex items-center gap-2">
          <span className="text-lg">🔐</span>
          <h2 className="text-sm font-semibold text-slate-100">Authentication required</h2>
        </div>
        <p className={`mb-2 text-xs ${props.retry ? 'text-rose-400' : 'text-slate-400'}`}>
          {props.message}
        </p>
        <p className="mb-3 text-[11px] text-slate-500">
          Device: <span className="font-mono text-slate-300">{props.label}</span>
        </p>
        <label className="mb-2 block text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          Username
          <input
            ref={userRef}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded border border-slate-600 bg-slate-900 px-2 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
          />
        </label>
        <label className="block text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            className="mt-1 w-full rounded border border-slate-600 bg-slate-900 px-2 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-sky-500"
          />
        </label>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={props.onCancel}
            className="rounded px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-700"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="rounded bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-500"
          >
            Connect
          </button>
        </div>
        <p className="mt-3 text-[9px] text-slate-600">
          Credentials are sent to the device only — never stored or logged.
        </p>
      </form>
    </div>
  );
}

export default function MainTerminalUi(props: MainTerminalUiProps) {
  const [topologyWidth, setTopologyWidth] = useState(320);
  const [leftSectionMode, setLeftSectionMode] = useState<'topology' | 'devices'>('topology');
  // Change 1: collapsible Topology panel — persisted across reloads.
  const [topologyCollapsed, setTopologyCollapsed] = useState<boolean>(
    () => readPersistedFlag('cliq.topology.collapsed', false),
  );
  // Change 2: CLIQ Advisory is HIDDEN by default on first load; persisted.
  // (Advisories still generate/queue in the background — see hook below.)
  const [intelPanelOpen, setIntelPanelOpen] = useState<boolean>(
    () => !readPersistedFlag('cliq.advisory.collapsed', true),
  );
  const isDraggingRef = useRef(false);

  const {
    metrics, errors, registerMount, focusDevice, runFix, dismissError, refitAll,
    intelItems, dismissIntelItem, markResolved, recallMemory, ask,
    activeContext, clearContext,
    insertToTerminal, insertBlock, explain, lightbulbOverlay, closeLightbulb,
    lightbulbAnchor, openLightbulbOverlay,
    authPrompt, submitAuth, cancelAuth,
    reconnectBanners, reconnect,
  } = useTerminalSessions(
    props.host,
    props.token,
    props.openDevices,
    props.onSetActiveDevice,
  );

  useEffect(() => {
    if (!props.activeDeviceId && props.openDevices[0]) {
      props.onSetActiveDevice(props.openDevices[0].id);
    }
  }, [props.activeDeviceId, props.openDevices, props.onSetActiveDevice]);

  useEffect(() => {
    if (props.activeDeviceId) focusDevice(props.activeDeviceId);
  }, [focusDevice, props.activeDeviceId]);

  const activeMetrics = metrics[props.activeDeviceId];
  const activeErrors = errors[props.activeDeviceId] ?? [];
  const activeIntelItems = intelItems[props.activeDeviceId] ?? [];
  const activeCtx = activeContext[props.activeDeviceId] ?? null;

  // Refit terminals whenever the error panel expands or collapses (changes terminal height)
  useEffect(() => {
    refitAll();
  }, [activeErrors.length, refitAll]);

  // Change 1 & 2: persist panel collapse state and reflow the xterm canvas after
  // the ~200ms width transition so the terminal reclaims/yields space cleanly.
  useEffect(() => {
    writePersistedFlag('cliq.topology.collapsed', topologyCollapsed);
    const t = window.setTimeout(() => refitAll(), 220);
    return () => window.clearTimeout(t);
  }, [topologyCollapsed, refitAll]);

  useEffect(() => {
    writePersistedFlag('cliq.advisory.collapsed', !intelPanelOpen);
    const t = window.setTimeout(() => refitAll(), 220);
    return () => window.clearTimeout(t);
  }, [intelPanelOpen, refitAll]);
  const openDeviceIds = new Set(props.openDevices.map((d) => d.id));

  // Slice last 5 errors but track true array index for dismiss
  const errorSliceOffset = Math.max(0, activeErrors.length - 5);
  const visibleErrors = activeErrors.slice(-5);

  const nodes: Node[] = props.topologyNodes.map((node) => {
    // Use exact CML label as-is from the API - no modification
    const label = node.data.label ?? node.id;
    return {
      id: node.id,
      position: node.position,
      data: { label },
      style: {
        border: `2px solid ${statusDot(node.data.node_state)}`,
        borderRadius: 8,
        background: openDeviceIds.has(node.id) ? '#e0f2fe' : '#ffffff',
        color: '#0f172a',
        fontSize: 12,
        fontWeight: 600,
        padding: '6px 12px',
        overflow: 'hidden',
        whiteSpace: 'nowrap',
        textOverflow: 'ellipsis',
        boxShadow: '0 1px 3px rgba(15,23,42,0.15)',
      },
    };
  });

  const edges: Edge[] = props.topologyLinks.map((link) => {
    const label = linkLabel(link);
    return {
      id: link.id,
      source: link.source,
      target: link.target,
      type: 'straight',
      markerEnd: { type: MarkerType.ArrowClosed, color: '#64748b' },
      style: { stroke: '#94a3b8', strokeWidth: 2.5, opacity: 0.9 },
      label,
      labelBgStyle: {
        background: '#ffffff',
        fill: '#ffffff',
        padding: '2px 6px',
        borderRadius: '3px',
        fontSize: '11px',
        fontWeight: 600,
      },
      labelStyle: {
        fill: '#334155',
        fontSize: 12,
        fontWeight: 700,
        background: '#ffffff',
        padding: '2px 6px',
        borderRadius: '3px',
      },
      animated: false,
    };
  });

  const handleMouseDown = () => {
    isDraggingRef.current = true;
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      // Calculate new topology width based on mouse position.
      // Constrain between 200px and 800px for reasonable bounds.
      const newWidth = Math.max(200, Math.min(800, e.clientX));
      setTopologyWidth(newWidth);
    };

    const handleMouseUp = () => {
      isDraggingRef.current = false;
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-slate-950 text-slate-100">

      {/* Top header: brand + device tabs (laid out horizontally in a row) */}
      <header className="flex shrink-0 flex-row items-center gap-3 border-b border-slate-800 bg-slate-900 px-4 py-2">
        {/* Brand */}
        <span className="shrink-0 text-sm font-bold tracking-widest text-sky-400">CLIQ</span>
        <span className="text-slate-700">|</span>

        {/* Device tabs */}
        <div className="flex min-w-0 flex-1 flex-row flex-nowrap items-center gap-1.5 overflow-x-auto">
          {props.openDevices.map((device) => {
            const m = metrics[device.id];
            const ms = getModeStyle(m?.mode);
            const isActive = device.id === props.activeDeviceId;
            const dotColor =
              m?.status === 'connected' ? '#22c55e' : m?.status === 'error' ? '#ef4444' : '#94a3b8';
            return (
              <div
                key={device.id}
                className={`flex shrink-0 flex-row items-center rounded-md transition-colors ${
                  isActive
                    ? 'bg-sky-600 text-white shadow'
                    : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                }`}
              >
                <button
                  onClick={() => props.onSetActiveDevice(device.id)}
                  className="flex items-center gap-1.5 whitespace-nowrap px-2 py-1 text-xs font-medium"
                >
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: dotColor }}
                  />
                  {device.label}
                  <span
                    className="ml-1 rounded px-1.5 py-0.5 text-[10px] font-bold"
                    style={{ background: ms.bg, color: ms.text }}
                  >
                    {ms.label}
                  </span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    props.onCloseDevice?.(device.id);
                  }}
                  className="px-2 py-1 text-xs opacity-60 hover:opacity-100 whitespace-nowrap"
                  title="Close device session"
                >
                  x
                </button>
              </div>
            );
          })}

          {/* New Tab button */}
          {props.allDevices.some((d) => !openDeviceIds.has(d.id)) && (
            <button
              onClick={() => {
                const unopened = props.allDevices.find((d) => !openDeviceIds.has(d.id));
                if (unopened) props.onOpenDevice(unopened.id);
              }}
              className="shrink-0 whitespace-nowrap rounded-md bg-slate-800 px-2 py-1 text-xs text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-300"
              title="Open a new device tab"
            >
              + New Tab
            </button>
          )}
        </div>

        {/* Right side: User, CLIQ toggle & Exit */}
        <div className="ml-auto flex shrink-0 items-center gap-3">
          {/* Phase 6: CLIQ Advisory panel toggle */}
          <button
            onClick={() => setIntelPanelOpen((v) => !v)}
            className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-semibold transition-colors ${
              intelPanelOpen
                ? 'bg-violet-600 text-white'
                : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
            }`}
            title={intelPanelOpen ? 'Hide CLIQ Advisory panel' : 'Show CLIQ Advisory panel'}
          >
            ⚡ CLIQ
            {activeIntelItems.length > 0 && (
              <span className="rounded-full bg-violet-400 px-1.5 text-[10px] font-bold text-white">
                {activeIntelItems.length}
              </span>
            )}
          </button>
          {props.userName && (
            <span className="text-xs text-slate-400">{props.userName}</span>
          )}
          {props.onExitTerminal && (
            <button
              onClick={props.onExitTerminal}
              className="rounded-md bg-rose-600 px-3 py-1 text-xs font-medium text-white transition-colors hover:bg-rose-700"
              title="Exit terminal and return to lab selection"
            >
              Exit Terminal
            </button>
          )}
        </div>
      </header>

      {/* Main body */}
      <div className="flex min-h-0 flex-1 overflow-hidden bg-slate-950">

        {/* Left sidebar: Topology / Devices panel */}
        <aside
          className="flex flex-col border-r border-slate-700 bg-white overflow-hidden transition-[width] duration-200 ease-in-out"
          style={{ width: topologyCollapsed ? 0 : `${topologyWidth}px` }}
        >

          {/* Left section header */}
          <div className="shrink-0 border-b border-slate-200 bg-white px-3 py-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-widest text-slate-700">
              Topology
            </span>
            <button
              onClick={() => setLeftSectionMode(leftSectionMode === 'topology' ? 'devices' : 'topology')}
              className={`rounded p-2 text-sm font-medium transition-colors ${
                leftSectionMode === 'topology'
                  ? 'bg-slate-200 text-slate-700 hover:bg-slate-300'
                  : 'bg-sky-100 text-sky-700 hover:bg-sky-200'
              }`}
              title={leftSectionMode === 'topology' ? 'Show device list' : 'Show topology'}
              aria-label={leftSectionMode === 'topology' ? 'Show device list' : 'Show topology'}
            >
              {leftSectionMode === 'topology' ? 'List' : 'Map'}
            </button>
          </div>

          {leftSectionMode === 'topology' ? (
            <div className="min-h-0 flex-1 bg-white">
              <ReactFlow
                nodes={nodes}
                edges={edges}
                fitView
                onNodeClick={(_, node) => props.onOpenDevice(node.id)}
              >
                <Background color="#e2e8f0" gap={20} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-y-auto bg-white p-3">
              <div className="space-y-1.5">
                {props.allDevices.map((device) => {
                  const isOpen = openDeviceIds.has(device.id);
                  const isActive = device.id === props.activeDeviceId;
                  const m = metrics[device.id];
                  const ms = getModeStyle(m?.mode);
                  return (
                    <div
                      key={device.id}
                      className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2.5 text-xs transition-colors ${
                        isActive
                          ? 'bg-sky-700 text-white shadow-md'
                          : isOpen
                          ? 'bg-sky-100 text-sky-900 hover:bg-sky-200'
                          : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                      }`}
                    >
                      <button
                        onClick={() => props.onOpenDevice(device.id)}
                        className="flex min-w-0 items-center gap-2 flex-1 text-left"
                      >
                        <span
                          className="h-2 w-2 shrink-0 rounded-full"
                          style={{ background: statusDot(device.node_state) }}
                        />
                        <div className="min-w-0">
                          <div className="truncate font-semibold">{device.label}</div>
                          <div className={`text-[10px] ${isActive ? 'text-sky-200' : 'text-slate-500'}`}>
                            {device.node_state ?? 'unknown'}
                          </div>
                        </div>
                      </button>
                      <div className="flex items-center gap-1 shrink-0">
                        {isOpen && (
                          <span
                            className={`rounded px-1.5 py-0.5 text-[9px] font-bold whitespace-nowrap ${
                              isActive
                                ? 'bg-sky-600 text-white'
                                : 'bg-slate-200 text-slate-700'
                            }`}
                          >
                            {ms.label}
                          </span>
                        )}
                        {isOpen && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              props.onCloseDevice?.(device.id);
                            }}
                            className={`text-xs opacity-60 hover:opacity-100 transition-opacity ${
                              isActive ? 'text-white' : 'text-slate-600'
                            }`}
                            title="Close this device session"
                          >
                            x
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </aside>

        {/* Splitter + collapse chevron (Change 1) */}
        <div className="relative flex w-4 shrink-0 flex-col items-stretch bg-slate-800">
          {/* Chevron toggle — collapse to a thin rail / restore previous width */}
          <button
            onClick={() => setTopologyCollapsed((v) => !v)}
            onMouseDown={(e) => e.stopPropagation()}
            className="flex h-7 shrink-0 items-center justify-center text-[11px] font-bold text-slate-300 transition-colors hover:bg-sky-600 hover:text-white"
            title={topologyCollapsed ? 'Expand topology panel' : 'Collapse topology panel'}
            aria-label={topologyCollapsed ? 'Expand topology panel' : 'Collapse topology panel'}
          >
            {topologyCollapsed ? '\u203a' : '\u2039'}
          </button>
          {/* Drag-to-resize handle (only when expanded) */}
          {!topologyCollapsed && (
            <div
              className="flex-1 cursor-col-resize transition-colors hover:bg-sky-500"
              onMouseDown={handleMouseDown}
              title="Drag to resize panel"
            />
          )}
        </div>

        {/* Terminal column + CLIQ advisory panel (right) */}
        <div className="flex min-w-0 flex-1 overflow-hidden">

        {/* Terminal column */}
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">

          <div className="flex min-h-0 flex-1 flex-col bg-[#0d1117]">
            {/* xterm canvas area */}
            <div className="relative min-h-0 flex-1 bg-[#0d1117]">
              {props.openDevices.map((device) => (
                <div
                  key={device.id}
                  className={`absolute inset-0 ${device.id === props.activeDeviceId ? 'block' : 'hidden'}`}
                  ref={(el) => registerMount(device.id, el)}
                />
              ))}
              {/* In-session reconnect banner — sticky at the TOP of the terminal
                  for the active device while a dropped session is being resumed.
                  Session state is preserved server-side (no re-login), scrollback
                  is intact, and input is frozen until reconnect succeeds. */}
              {props.activeDeviceId && reconnectBanners[props.activeDeviceId] && (
                <ReconnectBanner
                  info={reconnectBanners[props.activeDeviceId]!}
                  onReconnect={() => reconnect(props.activeDeviceId)}
                />
              )}
              {props.openDevices.length === 0 && (
                <div className="flex h-full items-center justify-center text-sm text-slate-600">
                  Select a device from the sidebar to open a terminal.
                </div>
              )}
            </div>

            {/* Terminal footer: no overlay, so output stays visible */}
            <div className="shrink-0 border-t border-slate-700/70 bg-slate-950/90 backdrop-blur-sm">
              <div className="flex items-center gap-4 px-4 py-1 text-xs text-slate-500">
                <span className="tabular-nums">{activeMetrics?.latency ?? 0} ms</span>
                <span>{activeMetrics?.commandsRun ?? 0} cmds</span>
                <span>{activeMetrics?.typosCaught ?? 0} typos</span>
                <span className="ml-auto text-[10px] text-slate-600">
                  Tip: type <kbd className="rounded bg-slate-800 px-1 text-slate-400">?</kbd> then your question to ask CLIQ inline
                </span>
              </div>

              {activeMetrics?.ghostText && (
                <div className="flex items-center gap-2 border-t border-slate-700/70 px-4 py-1.5">
                  <span className="text-[11px] text-slate-500">Suggestion</span>
                  <code className="rounded bg-slate-800 px-2 py-0.5 font-mono text-xs text-sky-300">
                    {activeMetrics.ghostText}
                  </code>
                  <span className="text-[11px] text-slate-600">
                    Press <kbd className="rounded bg-slate-700 px-1 text-slate-300">Tab</kbd> to accept
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Error / command intelligence panel */}
          {visibleErrors.length > 0 && (
            <div className="shrink-0 max-h-56 space-y-2 overflow-auto border-t border-slate-800 bg-slate-900 p-3">
              {visibleErrors.map((item, i) => {
                const s = errorStyle(item.type);
                const actualIdx = errorSliceOffset + i;
                return (
                  <div
                    key={`${item.type}-${actualIdx}`}
                    className="flex items-start gap-3 rounded-lg border-l-4 bg-white px-3 py-2.5 shadow-sm"
                    style={{ borderColor: s.accent, background: s.bg }}
                  >
                    {/* Icon */}
                    <span
                      className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded text-[11px] font-bold text-white"
                      style={{ background: s.accent }}
                    >
                      {s.icon}
                    </span>

                    {/* Body */}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white"
                          style={{ background: s.accent }}
                        >
                          {s.label}
                        </span>
                        <span className="text-sm font-semibold" style={{ color: s.accent }}>
                          {item.what}
                        </span>
                      </div>
                      {item.how && (
                        <p className="mt-0.5 text-xs text-slate-600">{item.how}</p>
                      )}
                      {item.fixCommand && (
                        <div className="mt-2 flex items-center gap-2">
                          <code
                            className="rounded border px-2 py-0.5 font-mono text-xs"
                            style={{ borderColor: s.accent, color: s.accent, background: '#ffffff' }}
                          >
                            {item.fixCommand}
                          </code>
                          <button
                            onClick={() => {
                              if (item.fixCommand) navigator.clipboard?.writeText(item.fixCommand);
                            }}
                            className="rounded px-2.5 py-0.5 text-xs font-semibold text-white transition-opacity hover:opacity-80"
                            style={{ background: s.accent }}
                            title="Copy suggestion"
                          >
                            Copy
                          </button>
                        </div>
                      )}
                    </div>

                    {/* Dismiss */}
                    <button
                      onClick={() => dismissError(props.activeDeviceId, actualIdx)}
                      className="shrink-0 text-xs text-slate-400 hover:text-slate-600"
                      title="Dismiss"
                    >
                      x
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </main>

        {/* Change 2: CLIQ Advisory panel — ALWAYS mounted so advisories keep
            generating/queuing in the background; slides in/out via a width
            transition and is hidden by default on first load. */}
        <aside
          className={`flex shrink-0 flex-col overflow-hidden border-slate-700 bg-slate-900 transition-[width] duration-200 ease-in-out ${
            intelPanelOpen ? 'w-80 border-l' : 'w-0 border-l-0'
          }`}
          aria-hidden={!intelPanelOpen}
        >
          <div className="flex h-full w-80 flex-col">
            {/* Panel header */}
            <div className="flex shrink-0 items-center justify-between border-b border-slate-700 bg-slate-800 px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-bold tracking-widest text-violet-400">⚡ CLIQ ADVISORY</span>
                <span className="rounded-full bg-slate-700 px-1.5 py-0.5 text-[9px] text-slate-400">
                  advisory only · never executes
                </span>
              </div>
              <div className="flex items-center gap-1">
                {props.activeDeviceId && (
                  <button
                    onClick={() => recallMemory(props.activeDeviceId, 'recent issues')}
                    className="rounded px-1.5 py-0.5 text-[10px] text-slate-400 hover:bg-slate-700 hover:text-slate-200 transition-colors"
                    title="Recall past resolutions from memory"
                  >
                    Recall
                  </button>
                )}
                {activeIntelItems.length > 0 && (
                  <button
                    onClick={() => {
                      activeIntelItems.forEach((item) => dismissIntelItem(props.activeDeviceId, item.id));
                    }}
                    className="rounded px-1.5 py-0.5 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                    title="Clear all advisory items"
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>

            {/* Ask CLIQ input */}
            <div className="shrink-0 border-b border-slate-700 bg-slate-800/40 px-3 py-2">
              <AskBox
                disabled={!props.activeDeviceId}
                onAsk={(q) => ask(props.activeDeviceId, q)}
              />
            </div>

            {/* Feature 2 — sticky protocol context chip */}
            {activeCtx && (
              <div className="flex shrink-0 items-center gap-1.5 border-b border-slate-700 bg-slate-800/20 px-3 py-1.5">
                <span className="text-[9px] font-semibold uppercase tracking-wide text-slate-500">Context</span>
                <span
                  className="flex items-center gap-1 rounded-full bg-violet-900/40 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-violet-300 ring-1 ring-violet-700/50"
                  title={`Suggestions are biased toward ${activeCtx.protocol.toUpperCase()} (set from ${activeCtx.source === 'cli' ? 'your CLI activity' : 'your question'}). Unrelated commands still complete normally.`}
                >
                  🎯 {activeCtx.protocol.toUpperCase()}
                </span>
                <button
                  onClick={() => clearContext(props.activeDeviceId)}
                  className="ml-auto rounded px-1.5 py-0.5 text-[10px] text-slate-500 transition-colors hover:bg-slate-700 hover:text-slate-300"
                  title="Clear the protocol context"
                >
                  Clear ✕
                </button>
              </div>
            )}

            {/* Panel body */}
            <div className="min-h-0 flex-1 overflow-y-auto p-2 space-y-2">
              {activeIntelItems.length === 0 && (
                <div className="flex flex-col items-center justify-center gap-2 pt-10 text-center text-slate-600">
                  <span className="text-2xl">⚡</span>
                  <p className="text-xs font-medium text-slate-500">Ask CLIQ anything</p>
                  <p className="text-[11px] leading-relaxed text-slate-600">
                    Use the box above, or type{' '}
                    <kbd className="rounded bg-slate-700 px-1 text-[10px] text-slate-400">?</kbd> then your
                    question in the terminal.<br />
                    Answers are advisory — CLIQ never runs them for you.
                  </p>
                </div>
              )}

              {activeIntelItems.map((item) => (
                <IntelPanelCard
                  key={item.id}
                  item={item}
                  deviceId={props.activeDeviceId}
                  onDismiss={() => dismissIntelItem(props.activeDeviceId, item.id)}
                  onMarkResolved={(issueText, commands, platform) =>
                    markResolved(props.activeDeviceId, issueText, commands, platform)
                  }
                  onInsert={(command) => insertToTerminal(props.activeDeviceId, command)}
                  onInsertBlock={(commands) => insertBlock(props.activeDeviceId, commands)}
                  onExplain={(command) => explain(props.activeDeviceId, command)}
                />
              ))}
            </div>

            {/* Panel footer hint */}
            <div className="shrink-0 border-t border-slate-700 bg-slate-800/50 px-3 py-1.5">
              <p className="text-[10px] text-slate-600">
                CLIQ never executes commands · engineer controls the device
              </p>
            </div>
          </div>
        </aside>

        </div>
      </div>

      {/* Phase 7 req 3 — the always-visible, clickable 💡 marker anchored inline
          at the terminal prompt line. Clicking it opens the Fix/Explain overlay
          (same as Ctrl+.). Hidden while the overlay is open to avoid overlap. */}
      {lightbulbAnchor && !lightbulbOverlay && (
        <button
          type="button"
          title="Suggestion available — click for Fix / Explain (Ctrl+.)"
          aria-label="Command suggestion available"
          onMouseDown={(e) => {
            // Don't steal terminal focus / selection on click.
            e.preventDefault();
            e.stopPropagation();
          }}
          onClick={() => openLightbulbOverlay(lightbulbAnchor.deviceId)}
          className="fixed z-30 flex animate-pulse items-center justify-center rounded-full bg-slate-900/85 text-xs leading-none ring-1 ring-amber-400/40 transition-transform hover:scale-125 hover:animate-none"
          style={{
            // Change 3: the anchor's left is already clamped to the terminal
            // panel's left edge in the hook, so it never overlaps the white
            // topology panel. Keep a dark chip + text-shadow for legibility on
            // any background.
            top: lightbulbAnchor.top,
            left: lightbulbAnchor.left,
            height: Math.max(lightbulbAnchor.height, 14),
            width: Math.max(lightbulbAnchor.height, 14),
            textShadow: '0 0 3px rgba(0,0,0,0.85)',
          }}
        >
          💡
        </button>
      )}

      {/* Phase 7 req 3 — inline lightbulb overlay (VS Code-style, opened via Ctrl+.) */}
      {lightbulbOverlay && (
        <>
          {/* click-away backdrop */}
          <div className="fixed inset-0 z-40" onClick={closeLightbulb} />
          <div
            className="fixed z-50 w-64 overflow-hidden rounded-md border border-slate-600 bg-slate-800 text-xs shadow-xl"
            style={{
              top: Math.min(lightbulbOverlay.top, window.innerHeight - 140),
              left: Math.min(lightbulbOverlay.left, window.innerWidth - 270),
            }}
          >
            <div className="flex items-center gap-1.5 border-b border-slate-700 bg-slate-900/60 px-2.5 py-1.5">
              <span className="text-amber-400">💡</span>
              <code className="truncate font-mono text-[11px] text-emerald-400" title={lightbulbOverlay.command}>
                {lightbulbOverlay.command || 'command'}
              </code>
            </div>
            <div className="flex flex-col">
              <button
                onClick={() => {
                  explain(lightbulbOverlay.deviceId, lightbulbOverlay.command);
                  closeLightbulb();
                }}
                className="flex items-center gap-2 px-2.5 py-2 text-left text-slate-200 hover:bg-slate-700/60"
              >
                <span>💡</span>
                <span>
                  <span className="font-semibold">Explain</span>
                  <span className="block text-[10px] text-slate-500">Plain-language + safety verdict</span>
                </span>
              </button>
              {lightbulbOverlay.fix && (
                <button
                  onClick={() => {
                    if (lightbulbOverlay.fix) insertToTerminal(lightbulbOverlay.deviceId, lightbulbOverlay.fix);
                    closeLightbulb();
                  }}
                  className="flex items-center gap-2 border-t border-slate-700/60 px-2.5 py-2 text-left text-slate-200 hover:bg-slate-700/60"
                >
                  <span>🔧</span>
                  <span className="min-w-0">
                    <span className="font-semibold">Fix</span>
                    <span className="block truncate text-[10px] text-sky-400">
                      Insert: {lightbulbOverlay.fix}
                    </span>
                  </span>
                </button>
              )}
            </div>
            <p className="border-t border-slate-700/60 bg-slate-900/40 px-2.5 py-1 text-[9px] text-slate-600">
              Advisory only · Insert never runs the command
            </p>
          </div>
        </>
      )}

      {/* Bug 7 — interactive console login dialog (masked credentials). */}
      {authPrompt && (
        <AuthPromptDialog
          key={`${authPrompt.deviceId}-${authPrompt.retry}`}
          label={authPrompt.label}
          message={authPrompt.message}
          retry={authPrompt.retry}
          onSubmit={(u, p) => submitAuth(authPrompt.deviceId, u, p)}
          onCancel={() => cancelAuth(authPrompt.deviceId)}
        />
      )}
    </div>
  );
}
