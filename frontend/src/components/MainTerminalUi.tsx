import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import ReactFlow, { Background, Controls, MarkerType, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Device, Lab, IntelAnswerItem, IntelExplainItem, IntelMemoryItem, IntelPanelItem, IntelReference, IntelResult, IntelSequenceStep, IntelToolkit, TopologyLink, TopologyNode } from '../types/api';
import { useTerminalSessions } from '../lib/useTerminalSessions';
import type { ReconnectInfo } from '../lib/useTerminalSessions';
import AppBar, { type SplitMode } from './AppBar';
import ExplorerSidebar from './ExplorerSidebar';
import WorkspaceTabs from './WorkspaceTabs';
import StatusBar from './StatusBar';
import CommandPalette from './CommandPalette';
import SettingsModal from './SettingsModal';
import {
  AdvisoryIcon,
  LightbulbIcon,
  AlertTriangleIcon,
  KeyIcon,
  CheckIcon,
  CloseIcon,
  TopologyIcon,
  SettingsIcon,
  CliqLogoIcon,
  WrenchIcon,
  BookOpenIcon,
  ActivityIcon,
  InsertIcon,
  getDeviceIcon,
} from './Icons';

type MainTerminalUiProps = {
  host: string;
  token: string;
  userName?: string;
  labs?: Lab[];
  selectedLabId?: string;
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
  onSelectLab?: (labId: string) => void;
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

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HEX_HASH_REGEX = /^[0-9a-f]{8,}$/i;
const ID_PREFIX_REGEX = /^(link|node|endpoint|intf|interface)[-_][0-9a-f-]+$/i;
const VALID_IFACE_REGEX = /^(?:Gi|GigabitEthernet|Te|TenGigabitEthernet|Fa|FastEthernet|Et|Ethernet|Eth|Mgmt|Management|MgmtEth|Lo|Loopback|Se|Serial|Po|Port-channel|Tu|Tunnel|Vlan|Vl|ge-|xe-|et-|eth|enp|ens)[0-9/._-]+$/i;

function isValidInterfaceName(name?: string | null): boolean {
  if (!name) return false;
  const trimmed = name.trim();
  if (!trimmed) return false;

  // Reject UUIDs, hex hashes, and internal link/node identifiers
  if (UUID_REGEX.test(trimmed)) return false;
  if (HEX_HASH_REGEX.test(trimmed)) return false;
  if (ID_PREFIX_REGEX.test(trimmed)) return false;

  // Real interface pattern matching
  if (VALID_IFACE_REGEX.test(trimmed)) return true;
  if (/^(?:LAN|WAN|port\s*\d+|slot\s*\d+|mgmt)$/i.test(trimmed)) return true;

  return false;
}

function abbreviateInterface(name: string): string {
  if (!name) return '';
  return name
    .replace(/GigabitEthernet/gi, 'Gi')
    .replace(/TenGigabitEthernet/gi, 'Te')
    .replace(/FastEthernet/gi, 'Fa')
    .replace(/Ethernet/gi, 'Et')
    .replace(/Management/gi, 'Mgmt')
    .replace(/Loopback/gi, 'Lo')
    .replace(/Serial/gi, 'Se')
    .replace(/Port-channel/gi, 'Po');
}

export type CleanLinkResult = {
  displayLabel: string;
  fullTooltip: string;
  hasRealInterfaces: boolean;
};

export function cleanLinkLabel(link: TopologyLink): CleanLinkResult {
  const src = (link.sourceHandle || '').trim();
  const tgt = (link.targetHandle || '').trim();

  const srcValid = isValidInterfaceName(src);
  const tgtValid = isValidInterfaceName(tgt);

  if (srcValid && tgtValid) {
    return {
      displayLabel: `${abbreviateInterface(src)} ↔ ${abbreviateInterface(tgt)}`,
      fullTooltip: `${src} ↔ ${tgt}`,
      hasRealInterfaces: true,
    };
  }

  if (srcValid || tgtValid) {
    const valid = srcValid ? src : tgt;
    return {
      displayLabel: abbreviateInterface(valid),
      fullTooltip: valid,
      hasRealInterfaces: true,
    };
  }

  const raw = (link.label || '').trim();
  if (raw && !UUID_REGEX.test(raw) && !HEX_HASH_REGEX.test(raw) && !ID_PREFIX_REGEX.test(raw)) {
    const match = raw.match(/(?:.*?[/:-])?([A-Za-z]+[0-9/._-]+)\s*(?:<->|<=>|↔|-|–)\s*(?:.*?[/:-])?([A-Za-z]+[0-9/._-]+)/);
    if (match && match[1] && match[2] && isValidInterfaceName(match[1]) && isValidInterfaceName(match[2])) {
      const s = match[1];
      const t = match[2];
      return {
        displayLabel: `${abbreviateInterface(s)} ↔ ${abbreviateInterface(t)}`,
        fullTooltip: `${s} ↔ ${t}`,
        hasRealInterfaces: true,
      };
    }

    if (isValidInterfaceName(raw)) {
      return {
        displayLabel: abbreviateInterface(raw),
        fullTooltip: raw,
        hasRealInterfaces: true,
      };
    }
  }

  // Fallback: no real interface metadata provided for this link.
  // Never display UUIDs, random strings, or guessed interface names.
  return {
    displayLabel: '',
    fullTooltip: 'Interface details unavailable',
    hasRealInterfaces: false,
  };
}

// ── Phase 6/7: Intel Panel card component ───────────────────────────────────

type TierMeta = { label: string; icon: string; badge: string; bar: string; tint: string };

const TIER_META: Record<string, TierMeta> = {
  exact:      { label: 'Exact match', icon: '✓', badge: '#16a34a', bar: '#22c55e', tint: 'rgba(34,197,94,0.08)' },
  suggestion: { label: 'Suggestions', icon: '≈', badge: '#d97706', bar: '#f59e0b', tint: 'rgba(245,158,11,0.08)' },
  related:    { label: 'Related', icon: '⋯', badge: '#2563eb', bar: '#60a5fa', tint: 'rgba(96,165,250,0.08)' },
  fallback:   { label: 'No match', icon: '∅', badge: '#64748b', bar: '#94a3b8', tint: 'rgba(148,163,184,0.06)' },
  memory:     { label: 'Memory', icon: 'M', badge: '#7c3aed', bar: '#a78bfa', tint: 'rgba(167,139,250,0.08)' },
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
              <LightbulbIcon size={12} className="text-amber-400 shrink-0" /> Explain
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
      Insert
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
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-violet-400">
            <AdvisoryIcon size={12} />
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
          Config block
        </span>
        <button
          onClick={() => onInsertBlock(allCommands)}
          title="Insert the whole block into the CLI for review — does NOT run it"
          className="rounded border border-sky-600 bg-sky-700/30 px-2 py-0.5 text-[10px] font-semibold text-sky-200 hover:bg-sky-700/50 hover:text-sky-50"
        >
          Insert block
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
          <ActivityIcon size={12} className="text-amber-400 shrink-0" /> {toolkit.title}
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
                          debug
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => onInsert(c.command)}
                      title="Insert into the CLI for review — does NOT run it"
                      className="shrink-0 rounded border border-slate-600 px-1.5 py-0.5 text-[9px] font-semibold text-slate-300 opacity-0 transition-opacity hover:bg-slate-700/50 hover:text-slate-100 group-hover/tk:opacity-100"
                    >
                      Insert
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
          <AlertTriangleIcon size={12} className="shrink-0 text-rose-400 mt-0.5" />
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
          <BookOpenIcon size={12} className="text-cyan-400 shrink-0" /> {reference.title}
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
                          {c.group === 'Clear' ? 'clear' : 'debug'}
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => onInsert(c.command)}
                      title="Insert into the CLI for review — does NOT run it"
                      className="shrink-0 rounded border border-slate-600 px-1.5 py-0.5 text-[9px] font-semibold text-slate-300 opacity-0 transition-opacity hover:bg-slate-700/50 hover:text-slate-100 group-hover/ref:opacity-100"
                    >
                      Insert
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
          <AlertTriangleIcon size={12} className="shrink-0 text-rose-400 mt-0.5" />
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
            <span className="shrink-0 text-violet-400"><AdvisoryIcon size={13} /></span>
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
            <ActivityIcon size={13} className="text-violet-400 shrink-0" />
            <span className="font-medium text-violet-300">Past Resolution</span>
          </div>
          <button onClick={onDismiss} title="Dismiss" className="text-slate-500 hover:text-slate-300 p-0.5">
            <CloseIcon size={11} />
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
            <span className="shrink-0 text-amber-400"><LightbulbIcon size={13} /></span>
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
        <CheckIcon size={14} className="shrink-0 text-emerald-400" />
        <span>Reconnected — session resumed.</span>
      </div>
    );
  }
  if (info.phase === 'failed') {
    return (
      <div className={`${base} border-b border-amber-500/50 bg-amber-950/90 text-amber-100`}>
        <AlertTriangleIcon size={14} className="shrink-0 text-amber-400" />
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
  stage?: string;
  stageType?: string;
  onSubmit: (username: string, password: string) => void;
  onCancel: () => void;
}) {
  const isEnable = props.stageType === 'enable' || props.message.toLowerCase().includes('enable');
  const isPasswordOnly = isEnable || props.stage === 'password';
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const userRef = useRef<HTMLInputElement>(null);
  const passRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isPasswordOnly) {
      passRef.current?.focus();
    } else {
      userRef.current?.focus();
    }
  }, [isPasswordOnly]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting) return;
    setIsSubmitting(true);
    props.onSubmit(isPasswordOnly ? '' : username, password);
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
          <h2 className="text-sm font-semibold text-slate-100">
            {isEnable ? 'Enable Secret Required' : 'Authentication required'}
          </h2>
        </div>
        <p className={`mb-2 text-xs ${props.retry ? 'text-rose-400' : 'text-slate-400'}`}>
          {props.message}
        </p>
        <p className="mb-3 text-[11px] text-slate-500">
          Device: <span className="font-mono text-slate-300">{props.label}</span>
        </p>
        {!isPasswordOnly && (
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
        )}
        <label className="block text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          {isEnable ? 'Enable Secret' : 'Password'}
          <input
            ref={passRef}
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
            disabled={isSubmitting}
            className={`rounded px-3 py-1.5 text-xs font-semibold text-white ${
              isSubmitting ? 'bg-sky-800 opacity-60 cursor-not-allowed' : 'bg-sky-600 hover:bg-sky-500'
            }`}
          >
            {isSubmitting ? 'Submitting…' : (isEnable ? 'Submit Secret' : (isPasswordOnly ? 'Submit' : 'Connect'))}
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
    requestEnable,
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

  // ── Derived state for IDE shell ───────────────────────────────────────────
  const activeMetrics = metrics[props.activeDeviceId];
  const activeErrors = errors[props.activeDeviceId] ?? [];
  const activeIntelItems = intelItems[props.activeDeviceId] ?? [];
  const activeCtx = activeContext[props.activeDeviceId] ?? null;
  const openDeviceIds = new Set(props.openDevices.map((d) => d.id));

  // Refit terminals whenever error panel expands/collapses
  useEffect(() => {
    refitAll();
  }, [activeErrors.length, refitAll]);

  // Persist panel collapse state and reflow the xterm canvas after transition
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

  // Slice last 5 errors
  const errorSliceOffset = Math.max(0, activeErrors.length - 5);
  const visibleErrors = activeErrors.slice(-5);

  // Improved topology layout: calculate generous spacing so interface labels never overlap
  const layoutPositions = useMemo(() => {
    const count = props.topologyNodes.length;
    if (count === 0) return {};

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let allZero = true;

    for (const n of props.topologyNodes) {
      const x = n.position?.x ?? 0;
      const y = n.position?.y ?? 0;
      if (x !== 0 || y !== 0) allZero = false;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }

    const posMap: Record<string, { x: number; y: number }> = {};

    if (allZero || (maxX === minX && maxY === minY)) {
      const radius = Math.max(160, count * 55);
      const angleStep = (2 * Math.PI) / count;
      props.topologyNodes.forEach((n, idx) => {
        posMap[n.id] = {
          x: Math.round(280 + radius * Math.cos(idx * angleStep)),
          y: Math.round(220 + radius * Math.sin(idx * angleStep)),
        };
      });
      return posMap;
    }

    const spanX = maxX - minX;
    const scale = spanX > 0 && spanX < count * 140 ? 1.7 : 1.25;

    props.topologyNodes.forEach((n) => {
      posMap[n.id] = {
        x: Math.round((n.position.x - minX) * scale + 100),
        y: Math.round((n.position.y - minY) * scale + 80),
      };
    });

    return posMap;
  }, [props.topologyNodes]);

  // Topology ReactFlow nodes with real identifiable device icons and status dots
  const nodes: Node[] = props.topologyNodes.map((node) => {
    const label = node.data.label ?? node.id;
    const isBooted = node.data.node_state === 'BOOTED';
    const isOpen = openDeviceIds.has(node.id);
    const pos = layoutPositions[node.id] || node.position;

    const rawType = (node.data.type || '').toUpperCase();
    const displayType = rawType && rawType !== 'UNKNOWN' ? rawType : 'ROUTER';

    return {
      id: node.id,
      position: pos,
      data: {
        label: (
          <div className="flex items-center gap-2.5 px-3 py-2 select-none">
            {/* Separate boot status dot */}
            <span
              className={`h-2 w-2 rounded-full shrink-0 ${
                isBooted ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]' : 'bg-slate-500'
              }`}
              title={`Status: ${node.data.node_state ?? 'unknown'}`}
            />
            {/* Real identifiable device SVG icon */}
            <span className="shrink-0 text-slate-300">
              {getDeviceIcon(node.data.type, node.data.label, 16)}
            </span>
            {/* Node label and hardware type */}
            <div className="flex flex-col text-left min-w-0">
              <span className="text-xs font-bold text-slate-100 truncate tracking-wide">
                {label}
              </span>
              <span className="text-[9px] font-mono text-slate-400 uppercase tracking-wider">
                {displayType}
              </span>
            </div>
          </div>
        ),
      },
      style: {
        background: isOpen ? '#1e3a5f' : '#252526',
        border: isOpen
          ? '1.5px solid #007acc'
          : isBooted
          ? '1.5px solid #3c3c3c'
          : '1.5px solid #2d2d2d',
        borderRadius: 8,
        padding: 0,
        color: '#f1f5f9',
        boxShadow: isOpen
          ? '0 0 0 1px #007acc, 0 4px 12px rgba(0,0,0,0.5)'
          : '0 2px 8px rgba(0,0,0,0.4)',
        cursor: 'pointer',
      },
    };
  });

  const edges: Edge[] = props.topologyLinks.map((link) => {
    const linkInfo = cleanLinkLabel(link);
    return {
      id: link.id,
      source: link.source,
      target: link.target,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, color: '#64748b', width: 14, height: 14 },
      style: { stroke: '#475569', strokeWidth: 1.5 },
      label: linkInfo.displayLabel || undefined,
      labelBgStyle: linkInfo.displayLabel
        ? { fill: '#18181b', fillOpacity: 0.95, rx: 5, ry: 5 }
        : undefined,
      labelBgPadding: [6, 3] as [number, number],
      labelStyle: {
        fill: '#94a3b8',
        fontSize: 10,
        fontWeight: 600,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      },
      data: {
        tooltip: linkInfo.fullTooltip,
      },
      animated: false,
    };
  });

  const hasTopology = props.topologyNodes.length > 0 || props.topologyLinks.length > 0;

  // ── IDE-level UI state: show topology tab by default when no devices are open
  const [showTopologyTab, setShowTopologyTab] = useState(() => hasTopology && props.openDevices.length === 0);
  const [splitMode, setSplitMode] = useState<SplitMode>('single');
  const [splitDeviceId, setSplitDeviceId] = useState<string>('');
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() =>
    readPersistedFlag('cliq.sidebar.collapsed', false),
  );

  // Global Ctrl+P / Ctrl+K keyboard shortcut for Command Palette
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'p' || e.key === 'k')) {
        e.preventDefault();
        setCommandPaletteOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  useEffect(() => {
    writePersistedFlag('cliq.sidebar.collapsed', sidebarCollapsed);
    const t = window.setTimeout(() => refitAll(), 220);
    return () => window.clearTimeout(t);
  }, [sidebarCollapsed, refitAll]);

  const handleSetSplitMode = (mode: SplitMode) => {
    setSplitMode(mode);
    window.setTimeout(() => refitAll(), 150);
  };

  const otherDevices = props.openDevices.filter((d) => d.id !== props.activeDeviceId);
  const effectiveSplitDeviceId = (
    otherDevices.some((d) => d.id === splitDeviceId)
      ? splitDeviceId
      : otherDevices[0]?.id
  ) ?? '';

  return (
    <div className="flex h-screen flex-col overflow-hidden" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>

      {/* ── AppBar (top chrome) ─────────────────────────────────────────── */}
      <AppBar
        labTitle={props.labTitle}
        labExpiresAt={props.labExpiresAt}
        userName={props.userName}
        intelCount={activeIntelItems.length}
        intelPanelOpen={intelPanelOpen}
        splitMode={splitMode}
        openDeviceCount={props.openDevices.length}
        onToggleIntel={() => setIntelPanelOpen((v) => !v)}
        onSetSplitMode={handleSetSplitMode}
        onOpenCommandPalette={() => setCommandPaletteOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
        onExitTerminal={props.onExitTerminal}
        onChangeLab={props.onChangeLab}
      />

      {/* ── Main workspace row: Explorer (left), Central Workspace (middle), Advisory (right) ── */}
      <div className="flex min-h-0 flex-1 overflow-hidden">

        {/* ── Explorer sidebar (exclusively on the left, full height) ─────── */}
        <ExplorerSidebar
          allDevices={props.allDevices}
          openDeviceIds={openDeviceIds}
          activeDeviceId={showTopologyTab ? '' : props.activeDeviceId}
          metrics={metrics}
          labs={props.labs}
          selectedLabId={props.selectedLabId}
          labTitle={props.labTitle}
          hasTopology={hasTopology}
          topologyActive={showTopologyTab}
          collapsed={sidebarCollapsed}
          onSelectLab={props.onSelectLab}
          onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
          onOpenDevice={(id) => { setShowTopologyTab(false); props.onOpenDevice(id); }}
          onCloseDevice={props.onCloseDevice}
          onShowTopology={() => setShowTopologyTab(true)}
        />

        {/* ── Central Workspace Area ────────────────────────────────────── */}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">

          {/* ── WorkspaceTabs (editor-style tab strip at TOP of central workspace) ─ */}
          <WorkspaceTabs
            openDevices={props.openDevices}
            activeDeviceId={props.activeDeviceId}
            metrics={metrics}
            hasTopology={hasTopology}
            topologyActive={showTopologyTab}
            onSetActive={(id) => { setShowTopologyTab(false); props.onSetActiveDevice(id); }}
            onClose={props.onCloseDevice ?? (() => {})}
            onCloseTopology={() => setShowTopologyTab(false)}
            onShowTopology={() => setShowTopologyTab(true)}
          />

          {/* Topology view or Terminal canvas */}
          {showTopologyTab && hasTopology ? (
            <div className="flex-1 overflow-hidden" style={{ background: '#1e1e1e' }}>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                fitView
                onNodeClick={(_, node) => { setShowTopologyTab(false); props.onOpenDevice(node.id); }}
              >
                <Background color="#3c3c3c" gap={20} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              {/* Terminal canvas area with single / split view */}
              <div className="relative min-h-0 flex-1" style={{ background: '#0d1117' }}>
                {/* Visual split headers when split mode is active with >= 2 devices */}
                {splitMode === 'horizontal' && props.openDevices.length >= 2 && (
                  <div className="absolute top-0 left-0 right-0 h-7 flex border-b z-10" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
                    <div className="w-1/2 flex items-center justify-between px-3 text-xs border-r" style={{ borderColor: 'var(--ide-border)' }}>
                      <span className="font-semibold text-sky-400 truncate">
                        {props.openDevices.find((d) => d.id === props.activeDeviceId)?.label}
                        <span className="ml-1.5 text-[9px] font-normal text-slate-400">(Focused)</span>
                      </span>
                      <span className="flex items-center text-[10px] text-emerald-400"><span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-emerald-400" />Active</span>
                    </div>
                    <div className="w-1/2 flex items-center justify-between px-3 text-xs">
                      <div className="flex items-center gap-1.5">
                        <span className="text-[10px] text-slate-400">Split Pane:</span>
                        <select
                          value={effectiveSplitDeviceId}
                          onChange={(e) => setSplitDeviceId(e.target.value)}
                          className="bg-transparent text-xs font-semibold text-slate-200 outline-none cursor-pointer"
                        >
                          {otherDevices.map((d) => (
                            <option key={d.id} value={d.id} className="bg-[#252526] text-white">
                              {d.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <button
                        onClick={() => props.onSetActiveDevice(effectiveSplitDeviceId)}
                        className="text-[10px] text-sky-400 hover:text-sky-300 transition-colors"
                      >
                        Focus Pane 2 ↘
                      </button>
                    </div>
                  </div>
                )}

                {splitMode === 'vertical' && props.openDevices.length >= 2 && (
                  <div className="absolute top-0 left-0 right-0 h-7 flex items-center justify-between px-3 text-xs border-b z-10" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
                    <span className="font-semibold text-sky-400">
                      {props.openDevices.find((d) => d.id === props.activeDeviceId)?.label} (Upper Pane)
                    </span>
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-slate-400">Lower:</span>
                      <select
                        value={effectiveSplitDeviceId}
                        onChange={(e) => setSplitDeviceId(e.target.value)}
                        className="bg-transparent text-xs font-semibold text-slate-200 outline-none cursor-pointer"
                      >
                        {otherDevices.map((d) => (
                          <option key={d.id} value={d.id} className="bg-[#252526] text-white">
                            {d.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}

                {/* Terminal mounts positioned per layout mode */}
                {props.openDevices.map((device) => {
                  const isPrimary = device.id === props.activeDeviceId;
                  const isSecondary = device.id === effectiveSplitDeviceId;

                  let placementClass = 'hidden';
                  let inlineStyle: React.CSSProperties | undefined = undefined;

                  if (splitMode === 'single' || props.openDevices.length < 2) {
                    placementClass = isPrimary ? 'absolute inset-0 block' : 'hidden';
                  } else if (splitMode === 'horizontal') {
                    if (isPrimary) {
                      placementClass = 'absolute top-7 bottom-0 left-0 w-1/2 block border-r border-[#3c3c3c]';
                    } else if (isSecondary) {
                      placementClass = 'absolute top-7 bottom-0 right-0 w-1/2 block';
                    }
                  } else if (splitMode === 'vertical') {
                    if (isPrimary) {
                      placementClass = 'absolute top-7 left-0 right-0 block border-b border-[#3c3c3c]';
                      inlineStyle = { height: 'calc(50% - 14px)' };
                    } else if (isSecondary) {
                      placementClass = 'absolute bottom-0 left-0 right-0 block';
                      inlineStyle = { height: 'calc(50% - 14px)' };
                    }
                  }

                  return (
                    <div
                      key={device.id}
                      className={placementClass}
                      style={inlineStyle}
                      onClick={() => {
                        if (device.id !== props.activeDeviceId) {
                          props.onSetActiveDevice(device.id);
                        }
                      }}
                      ref={(el) => registerMount(device.id, el)}
                    />
                  );
                })}

                {/* Reconnect banner */}
                {props.activeDeviceId && reconnectBanners[props.activeDeviceId] && (
                  <ReconnectBanner
                    info={reconnectBanners[props.activeDeviceId]!}
                    onReconnect={() => reconnect(props.activeDeviceId)}
                  />
                )}

                {props.openDevices.length === 0 && (
                  <div className="flex h-full flex-col items-center justify-center gap-4" style={{ color: 'var(--ide-text-muted)' }}>
                    <CliqLogoIcon size={36} className="text-slate-600" />
                    <p className="text-sm font-medium" style={{ color: 'var(--ide-text-dim)' }}>
                      Select a device from the Explorer to open a terminal.
                    </p>
                    {hasTopology && (
                      <button
                        onClick={() => setShowTopologyTab(true)}
                        className="rounded px-3 py-1.5 text-xs font-semibold transition-colors"
                        style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-muted)', border: '1px solid var(--ide-border)' }}
                      >
                        View Topology
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* Terminal footer: stats + ghost-text */}
              <div
                className="shrink-0 border-t"
                style={{ background: 'rgba(13,17,23,0.95)', borderColor: 'var(--ide-border)' }}
              >
                <div className="flex items-center gap-4 px-4 py-1 text-xs" style={{ color: 'var(--ide-text-dim)' }}>
                  <span className="tabular-nums">{activeMetrics?.latency ?? 0} ms</span>
                  <span>{activeMetrics?.commandsRun ?? 0} cmds</span>
                  <span>{activeMetrics?.typosCaught ?? 0} typos</span>

                  {/* Privileged mode controls */}
                  {activeMetrics?.mode === 'exec' && (
                    <button
                      type="button"
                      onClick={() => requestEnable(props.activeDeviceId)}
                      className="ml-2 rounded px-2 py-0.5 text-xs font-semibold transition-colors"
                      style={{ background: '#451a03', color: '#fbbf24', border: '1px solid #92400e' }}
                      title="Elevate to privileged EXEC (#) mode"
                    >
                      Enter Privileged Mode
                    </button>
                  )}
                  {activeMetrics?.mode === 'enable' && (
                    <span
                      className="ml-2 flex items-center gap-1 rounded px-2 py-0.5 text-xs font-semibold"
                      style={{ background: '#052e16', color: '#4ade80', border: '1px solid #166534' }}
                    >
                      ✓ Privileged (#)
                    </span>
                  )}

                  <span className="ml-auto text-[10px]" style={{ color: 'var(--ide-text-dim)' }}>
                    Tip: type <kbd className="rounded px-1" style={{ background: 'var(--ide-border)', color: 'var(--ide-text-muted)' }}>?</kbd> then your question to ask CLIQ inline
                  </span>
                </div>

                {activeMetrics?.ghostText && (
                  <div
                    className="flex items-center gap-2 border-t px-4 py-1"
                    style={{ borderColor: 'var(--ide-border)' }}
                  >
                    <span className="text-[11px]" style={{ color: 'var(--ide-text-dim)' }}>Suggestion</span>
                    <code
                      className="rounded px-2 py-0.5 font-mono text-xs"
                      style={{ background: 'var(--ide-hover)', color: '#75beff' }}
                    >
                      {activeMetrics.ghostText}
                    </code>
                    <span className="text-[11px]" style={{ color: 'var(--ide-text-dim)' }}>
                      Press <kbd className="rounded px-1" style={{ background: 'var(--ide-border)', color: 'var(--ide-text-muted)' }}>Tab</kbd> to accept
                    </span>
                  </div>
                )}
              </div>

              {/* Error / command intelligence panel */}
              {visibleErrors.length > 0 && (
                <div
                  className="shrink-0 max-h-56 space-y-2 overflow-auto border-t p-3"
                  style={{ background: 'var(--ide-panel-bg)', borderColor: 'var(--ide-border)' }}
                >
                  {visibleErrors.map((item, i) => {
                    const s = errorStyle(item.type);
                    const actualIdx = errorSliceOffset + i;
                    return (
                      <div
                        key={`${item.type}-${actualIdx}`}
                        className="flex items-start gap-3 rounded-lg border-l-4 px-3 py-2.5 shadow-sm"
                        style={{ borderColor: s.accent, background: s.bg }}
                      >
                        <span
                          className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded text-[11px] font-bold text-white"
                          style={{ background: s.accent }}
                        >
                          {s.icon}
                        </span>
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
                          {item.how && <p className="mt-0.5 text-xs text-slate-600">{item.how}</p>}
                          {item.fixCommand && (
                            <div className="mt-2 flex items-center gap-2">
                              <code
                                className="rounded border px-2 py-0.5 font-mono text-xs"
                                style={{ borderColor: s.accent, color: s.accent, background: '#ffffff' }}
                              >
                                {item.fixCommand}
                              </code>
                              <button
                                onClick={() => { if (item.fixCommand) navigator.clipboard?.writeText(item.fixCommand); }}
                                className="rounded px-2.5 py-0.5 text-xs font-semibold text-white transition-opacity hover:opacity-80"
                                style={{ background: s.accent }}
                                title="Copy suggestion"
                              >
                                Copy
                              </button>
                            </div>
                          )}
                        </div>
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
            </div>
          )}
        </div>

        {/* ── CLIQ Advisory panel (right drawer) ─────────────────────────── */}
        <aside
          className={`flex shrink-0 flex-col overflow-hidden border-l transition-[width] duration-200 ease-in-out ${
            intelPanelOpen ? 'w-80' : 'w-0 border-l-0'
          }`}
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-panel-bg)' }}
          aria-hidden={!intelPanelOpen}
        >
          <div className="flex h-full w-80 flex-col">
            {/* Panel header */}
            <div
              className="flex shrink-0 items-center justify-between border-b px-3 py-2"
              style={{ borderColor: 'var(--ide-border)', background: '#2d2d2d' }}
            >
              <div className="flex items-center gap-2">
                <span className="flex items-center gap-1.5 text-[10px] font-bold tracking-widest" style={{ color: '#a78bfa' }}><AdvisoryIcon size={12} /> CLIQ ADVISORY</span>
                <span
                  className="rounded-full px-1.5 py-0.5 text-[9px]"
                  style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-muted)' }}
                >
                  advisory only · never executes
                </span>
              </div>
              <div className="flex items-center gap-1">
                {props.activeDeviceId && (
                  <button
                    onClick={() => recallMemory(props.activeDeviceId, 'recent issues')}
                    className="rounded px-1.5 py-0.5 text-[10px] transition-colors"
                    style={{ color: 'var(--ide-text-muted)' }}
                    title="Recall past resolutions from memory"
                  >
                    Recall
                  </button>
                )}
                {activeIntelItems.length > 0 && (
                  <button
                    onClick={() => { activeIntelItems.forEach((item) => dismissIntelItem(props.activeDeviceId, item.id)); }}
                    className="rounded px-1.5 py-0.5 text-[10px] transition-colors"
                    style={{ color: 'var(--ide-text-muted)' }}
                    title="Clear all advisory items"
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>

            {/* Feature 2 — sticky protocol context chip */}
            {activeCtx && (
              <div
                className="flex shrink-0 items-center gap-1.5 border-b px-3 py-1.5"
                style={{ borderColor: 'var(--ide-border)', background: 'rgba(45,45,45,0.2)' }}
              >
                <span className="text-[9px] font-semibold uppercase tracking-wide" style={{ color: 'var(--ide-text-muted)' }}>Context</span>
                <span
                  className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                  style={{ background: 'rgba(124,58,237,0.25)', color: '#c4b5fd', border: '1px solid rgba(124,58,237,0.4)' }}
                  title={`Suggestions biased toward ${activeCtx.protocol.toUpperCase()}`}
                >
                  {activeCtx.protocol.toUpperCase()}
                </span>
                <button
                  onClick={() => clearContext(props.activeDeviceId)}
                  className="ml-auto rounded px-1.5 py-0.5 text-[10px] transition-colors"
                  style={{ color: 'var(--ide-text-muted)' }}
                  title="Clear the protocol context"
                >
                  Clear ✕
                </button>
              </div>
            )}

            {/* Panel body (scrollable list of advisory cards) */}
            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
              {activeIntelItems.length === 0 && (
                <div className="flex flex-col items-center justify-center gap-2 pt-10 text-center">
                  <AdvisoryIcon size={28} className="text-violet-400 opacity-40" />
                  <p className="text-xs font-medium" style={{ color: 'var(--ide-text-muted)' }}>Ask CLIQ anything</p>
                  <p className="text-[11px] leading-relaxed" style={{ color: 'var(--ide-text-dim)' }}>
                    Use the box above, or type{' '}
                    <kbd className="rounded px-1 text-[10px]" style={{ background: 'var(--ide-border)', color: 'var(--ide-text-muted)' }}>?</kbd>{' '}
                    then your question in the terminal.<br />
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

            {/* Ask CLIQ input pinned to bottom */}
            <div
              className="shrink-0 border-t px-3 py-2"
              style={{ borderColor: 'var(--ide-border)', background: 'rgba(45,45,45,0.4)' }}
            >
              <AskBox
                disabled={!props.activeDeviceId}
                onAsk={(q) => ask(props.activeDeviceId, q)}
              />
            </div>

            {/* Panel footer */}
            <div
              className="shrink-0 border-t px-3 py-1.5"
              style={{ borderColor: 'var(--ide-border)', background: 'rgba(45,45,45,0.5)' }}
            >
              <p className="text-[10px]" style={{ color: 'var(--ide-text-dim)' }}>
                CLIQ never executes commands · engineer controls the device
              </p>
            </div>
          </div>
        </aside>
      </div>

      {/* ── Status bar (bottom chrome) ──────────────────────────────────── */}
      <StatusBar
        connectionState={
          activeMetrics?.status === 'connected' ? 'connected'
          : activeMetrics?.status === 'error' ? 'error'
          : props.openDevices.length > 0 ? 'connecting'
          : undefined
        }
        activeDeviceLabel={props.openDevices.find((d) => d.id === props.activeDeviceId)?.label}
        activeDeviceMode={activeMetrics?.mode}
        labTitle={props.labTitle}
        latencyMs={activeMetrics?.latency}
      />

      {/* ── Phase 7 req 3 — always-visible 💡 marker ─────────────────────── */}
      {lightbulbAnchor && !lightbulbOverlay && (
        <button
          type="button"
          title="Suggestion available — click for Fix / Explain (Ctrl+.)"
          aria-label="Command suggestion available"
          onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); }}
          onClick={() => openLightbulbOverlay(lightbulbAnchor.deviceId)}
          className="fixed z-30 flex animate-pulse items-center justify-center rounded-full text-xs leading-none ring-1 ring-amber-400/40 transition-transform hover:scale-125 hover:animate-none"
          style={{
            top: lightbulbAnchor.top,
            left: lightbulbAnchor.left,
            height: Math.max(lightbulbAnchor.height, 14),
            width: Math.max(lightbulbAnchor.height, 14),
            background: 'rgba(13,17,23,0.85)',
            textShadow: '0 0 3px rgba(0,0,0,0.85)',
          }}
        >
          <LightbulbIcon size={14} className="text-amber-300" />
        </button>
      )}

      {/* ── Phase 7 req 3 — Fix/Explain overlay ─────────────────────────── */}
      {lightbulbOverlay && (
        <>
          <div className="fixed inset-0 z-40" onClick={closeLightbulb} />
          <div
            className="fixed z-50 w-64 overflow-hidden rounded-md shadow-xl"
            style={{
              top: Math.min(lightbulbOverlay.top, window.innerHeight - 140),
              left: Math.min(lightbulbOverlay.left, window.innerWidth - 270),
              background: 'var(--ide-panel-bg)',
              border: '1px solid var(--ide-border)',
            }}
          >
            <div
              className="flex items-center gap-1.5 border-b px-2.5 py-1.5"
              style={{ borderColor: 'var(--ide-border)', background: 'rgba(0,0,0,0.3)' }}
            >
              <LightbulbIcon size={13} className="text-amber-400 shrink-0" />
              <code
                className="truncate font-mono text-[11px]"
                style={{ color: '#4ec9b0' }}
                title={lightbulbOverlay.command}
              >
                {lightbulbOverlay.command || 'command'}
              </code>
            </div>
            <div className="flex flex-col">
              <button
                onClick={() => { explain(lightbulbOverlay.deviceId, lightbulbOverlay.command); closeLightbulb(); }}
                className="flex items-center gap-2 px-2.5 py-2 text-left transition-colors"
                style={{ color: 'var(--ide-text)' }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--ide-hover)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = '')}
              >
                <LightbulbIcon size={13} className="text-amber-400 shrink-0" />
                <span>
                  <span className="font-semibold text-xs">Explain</span>
                  <span className="block text-[10px]" style={{ color: 'var(--ide-text-muted)' }}>Plain-language + safety verdict</span>
                </span>
              </button>
              {lightbulbOverlay.fix && (
                <button
                  onClick={() => { if (lightbulbOverlay.fix) insertToTerminal(lightbulbOverlay.deviceId, lightbulbOverlay.fix); closeLightbulb(); }}
                  className="flex items-center gap-2 border-t px-2.5 py-2 text-left transition-colors"
                  style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--ide-hover)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = '')}
                >
                  <WrenchIcon size={13} className="text-sky-400 shrink-0" />
                  <span className="min-w-0">
                    <span className="font-semibold text-xs">Fix</span>
                    <span className="block truncate text-[10px]" style={{ color: '#75beff' }}>
                      Insert: {lightbulbOverlay.fix}
                    </span>
                  </span>
                </button>
              )}
            </div>
            <p
              className="border-t px-2.5 py-1 text-[9px]"
              style={{ borderColor: 'var(--ide-border)', background: 'rgba(0,0,0,0.2)', color: 'var(--ide-text-dim)' }}
            >
              Advisory only · Insert never runs the command
            </p>
          </div>
        </>
      )}

      {/* ── Bug 7 — interactive console login dialog ──────────────────────── */}
      {authPrompt && (
        <AuthPromptDialog
          key={`${authPrompt.deviceId}-${authPrompt.retry}`}
          label={authPrompt.label}
          message={authPrompt.message}
          retry={authPrompt.retry}
          stage={authPrompt.stage}
          stageType={authPrompt.stageType}
          onSubmit={(u, p) => submitAuth(authPrompt.deviceId, u, p, authPrompt.stageType)}
          onCancel={() => cancelAuth(authPrompt.deviceId)}
        />
      )}

      {/* ── Command Palette (Quick Open / Device Switch) ── */}
      <CommandPalette
        isOpen={commandPaletteOpen}
        onClose={() => setCommandPaletteOpen(false)}
        devices={props.allDevices}
        activeDeviceId={props.activeDeviceId}
        hasTopology={hasTopology}
        intelPanelOpen={intelPanelOpen}
        canElevatePrivileges={activeMetrics?.mode === 'exec'}
        splitMode={splitMode}
        onOpenDevice={(id) => { setShowTopologyTab(false); props.onOpenDevice(id); }}
        onShowTopology={() => setShowTopologyTab(true)}
        onToggleIntel={() => setIntelPanelOpen((v) => !v)}
        onRequestEnable={() => requestEnable(props.activeDeviceId)}
        onSetSplitMode={handleSetSplitMode}
        onOpenSettings={() => setSettingsOpen(true)}
        onExitTerminal={props.onExitTerminal}
      />

      {/* ── Settings & Preferences Modal ── */}
      <SettingsModal
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        host={props.host}
        userName={props.userName}
        labTitle={props.labTitle}
      />
    </div>
  );
}