/**
 * MarkdownView — Clean, lightweight Markdown & Code renderer for CLIQ Copilot.
 *
 * Supports headings, bold/italic, lists, tables, and fenced code blocks
 * with language tags, terminal command insertion (Insert into Terminal),
 * copy-to-clipboard, and command explanation actions.
 */

import React, { useState, useEffect, useRef } from 'react';
import type { Device } from '../../types/api';
import {
  CheckIcon,
  CopyIcon,
  InsertIcon,
  ExplainIcon,
  ChevronDownIcon,
  TerminalIcon,
} from './CopilotIcons';

export interface MarkdownViewProps {
  content: string;
  devices?: Device[];
  openDevices?: Device[];
  activeDeviceId?: string;
  focusedDeviceId?: string | null;
  deviceMetrics?: Record<string, { status: string; mode: string }>;
  onInsertCommand?: (deviceId: string, command: string) => void;
  onExplainCommand?: (deviceId: string, command: string) => void;
}

export type SequenceState =
  | 'READY'
  | 'COMMAND_INSERTED'
  | 'AWAITING_USER_ENTER'
  | 'COMMAND_RUNNING'
  | 'WAITING_FOR_AUTHENTIC_PROMPT'
  | 'INSERTING_NEXT_COMMAND'
  | 'PAUSED'
  | 'COMPLETED'
  | 'CANCELLED'
  | 'ERROR';

export default function MarkdownView({
  content,
  devices = [],
  openDevices,
  activeDeviceId,
  focusedDeviceId,
  deviceMetrics,
  onInsertCommand,
  onExplainCommand,
}: MarkdownViewProps) {
  // Parse content into blocks (code blocks, tables, and text)
  const blocks = parseMarkdownBlocks(content);

  return (
    <div className="space-y-3 text-xs leading-relaxed text-slate-200 select-text max-w-full overflow-hidden">
      {blocks.map((block, idx) => {
        if (block.type === 'code') {
          return (
            <CodeBlock
              key={idx}
              language={block.language}
              code={block.text}
              devices={devices}
              openDevices={openDevices}
              activeDeviceId={activeDeviceId}
              focusedDeviceId={focusedDeviceId}
              deviceMetrics={deviceMetrics}
              onInsertCommand={onInsertCommand}
              onExplainCommand={onExplainCommand}
            />
          );
        }
        if (block.type === 'table') {
          return <TableBlock key={idx} rows={block.rows || []} />;
        }
        return <TextBlock key={idx} text={block.text} />;
      })}
    </div>
  );
}

function isCliCommandBlock(language: string, code: string): boolean {
  const lang = (language || '').toLowerCase().trim();
  const cliLangs = [
    'cisco', 'ios', 'iosxe', 'ios-xe', 'junos', 'eos', 'cli',
    'sh', 'bash', 'shell', 'router', 'switch', 'terminal', 'network',
  ];
  if (cliLangs.includes(lang)) {
    return true;
  }
  const nonCliLangs = [
    'json', 'yaml', 'yml', 'python', 'py', 'javascript', 'js',
    'typescript', 'ts', 'html', 'css', 'xml', 'md', 'markdown',
  ];
  if (nonCliLangs.includes(lang)) {
    return false;
  }

  // Heuristic for unlabeled blocks
  const lines = code.trim().split('\n');
  if (lines.length === 0) return false;
  const first = lines[0].trim().toLowerCase();
  return (
    first.startsWith('show ') ||
    first.startsWith('conf ') ||
    first.startsWith('configure ') ||
    first.startsWith('interface ') ||
    first.startsWith('router ') ||
    first.startsWith('ip route ') ||
    first.startsWith('ip address ') ||
    first.startsWith('ping ') ||
    first.startsWith('traceroute ') ||
    first.startsWith('no ') ||
    first.startsWith('do ') ||
    first.startsWith('enable') ||
    first.startsWith('vlan ') ||
    first.startsWith('switchport ') ||
    first.startsWith('spanning-tree ') ||
    first.startsWith('standby ') ||
    first.startsWith('cdp ') ||
    first.startsWith('lldp ') ||
    first.startsWith('line ')
  );
}

// Workstream A: Approved read-only diagnostic command prefixes
const READ_ONLY_PREFIXES = [
  'show ',
  'ping ',
  'traceroute ',
  'trace ',
  'display ',
  'which ',
  'whereis ',
  'uname',
  'ip a',
  'ip addr',
  'ip route',
  'ip link',
  'ip neigh',
  'ifconfig',
  'cat ',
  'ls ',
  'netstat',
  'arp ',
  'ps ',
  'top',
  'uptime',
  'df ',
  'free',
];

// Configuration-changing patterns that disqualify automatic sequential insertion
const CONFIG_PATTERNS = [
  /^conf(?:ig(?:ure)?)?\b/i,
  /^int(?:erface)?\b/i,
  /^router\b/i,
  /^ip\s+(?:address|route\s+\d|nat|access-list|vrf|dhcp)\b/i,
  /^shut(?:down)?\b/i,
  /^no\s+/i,
  /^reload\b/i,
  /^write\b/i,
  /^erase\b/i,
  /^delete\b/i,
  /^copy\b/i,
  /^reboot\b/i,
  /^line\s+/i,
  /^vlan\b/i,
  /^switchport\b/i,
  /^spanning-tree\b/i,
  /^standby\b/i,
  /^crypto\b/i,
  /^snmp\b/i,
  /^aaa\b/i,
  /^username\b/i,
  /^enable\s+secret\b/i,
  /^service\b/i,
  /^hostname\b/i,
  /^ip\s+domain\b/i,
  /^ntp\b/i,
  /^logging\b/i,
  /^banner\b/i,
];

function isReadOnlyLine(line: string): boolean {
  const trimmed = line.trim().toLowerCase();
  if (!trimmed || trimmed.startsWith('!') || trimmed.startsWith('#')) return true;
  for (const pat of CONFIG_PATTERNS) {
    if (pat.test(trimmed)) return false;
  }
  return READ_ONLY_PREFIXES.some((prefix) => trimmed.startsWith(prefix) || trimmed === prefix.trim());
}

function isReadOnlySequence(lines: string[]): boolean {
  const substantive = lines.filter((l) => l.trim().length > 0 && !l.trim().startsWith('!') && !l.trim().startsWith('#'));
  if (substantive.length === 0) return false;
  return substantive.every((l) => isReadOnlyLine(l));
}

interface CodeBlockProps {
  language: string;
  code: string;
  devices?: Device[];
  openDevices?: Device[];
  activeDeviceId?: string;
  focusedDeviceId?: string | null;
  deviceMetrics?: Record<string, { status: string; mode: string }>;
  onInsertCommand?: (deviceId: string, command: string) => void;
  onExplainCommand?: (deviceId: string, command: string) => void;
}

function CodeBlock({
  language,
  code,
  devices = [],
  openDevices,
  activeDeviceId,
  focusedDeviceId,
  deviceMetrics,
  onInsertCommand,
  onExplainCommand,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [insertFeedback, setInsertFeedback] = useState<string | null>(null);
  const [lineInsertFeedback, setLineInsertFeedback] = useState<{ [index: number]: string }>({});
  const [copiedLineIdx, setCopiedLineIdx] = useState<number | null>(null);

  // Active open devices: only open terminals are eligible targets
  const openDevicesList = openDevices ?? devices;

  // Optional manual device override (secondary to genuine focus)
  const [overrideDeviceId, setOverrideDeviceId] = useState<string | null>(null);
  const [isDeviceMenuOpen, setIsDeviceMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // If override device was closed or removed from openDevices, reset override
  useEffect(() => {
    if (overrideDeviceId && !openDevicesList.some((d) => d.id === overrideDeviceId)) {
      setOverrideDeviceId(null);
    }
  }, [openDevicesList, overrideDeviceId]);

  // Target device is strictly focusedDeviceId unless the user manually selected an override.
  // P0 REQUIREMENT: NEVER fall back to devices[0] or R1 when no terminal is focused!
  // A valid target MUST be in openDevicesList.
  const validOverride = overrideDeviceId && openDevicesList.some((d) => d.id === overrideDeviceId) ? overrideDeviceId : null;
  const validFocused = focusedDeviceId && openDevicesList.some((d) => d.id === focusedDeviceId) ? focusedDeviceId : null;
  const effectiveTargetId = validOverride || validFocused || null;

  const targetDevice = effectiveTargetId ? openDevicesList.find((d) => d.id === effectiveTargetId) : null;
  const targetLabel = targetDevice?.label || effectiveTargetId || '';

  // Check connection status
  const targetMetric = effectiveTargetId ? deviceMetrics?.[effectiveTargetId] : null;
  const isDisconnected = effectiveTargetId ? (targetMetric?.status === 'disconnected' || targetMetric?.status === 'error') : false;

  const isNoTerminalOpen = openDevicesList.length === 0;
  const isTerminalOpenButNotFocused = openDevicesList.length > 0 && !effectiveTargetId;
  const isTargetEligible = Boolean(effectiveTargetId && targetDevice && !isDisconnected);

  // Bound device ID for running sequences (stays bound to original device even if focus moves)
  const boundDeviceIdRef = useRef<string>('');
  const boundDeviceLabel = boundDeviceIdRef.current
    ? (openDevicesList.find((d) => d.id === boundDeviceIdRef.current)?.label || boundDeviceIdRef.current)
    : targetLabel;

  // Close menu on click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setIsDeviceMenuOpen(false);
      }
    };
    if (isDeviceMenuOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [isDeviceMenuOpen]);

  const cleanCode = code.trim();
  const isCli = isCliCommandBlock(language, cleanCode);

  // Split into individual command lines
  const rawLines = cleanCode.split(/\r?\n/);
  const commandLines = rawLines
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  const isMultiCommandCli = isCli && commandLines.length > 1;
  const isReadOnly = isMultiCommandCli && isReadOnlySequence(commandLines);

  // 10-state Sequential Command Insertion State Machine
  const [sequenceStatus, setSequenceStatus] = useState<SequenceState>('READY');
  const [currentStep, setCurrentStep] = useState<number>(0);
  const [sequenceError, setSequenceError] = useState<string | null>(null);

  const sequenceStatusRef = useRef<SequenceState>(sequenceStatus);
  sequenceStatusRef.current = sequenceStatus;

  const currentStepRef = useRef<number>(currentStep);
  currentStepRef.current = currentStep;

  const stepSubmittedRef = useRef<boolean>(false);
  const stepOutputReceivedRef = useRef<boolean>(false);

  // Safeguard: If bound terminal is closed or disconnects during sequence, stop safely
  useEffect(() => {
    if (sequenceStatus === 'READY' || sequenceStatus === 'COMPLETED' || sequenceStatus === 'CANCELLED') {
      return;
    }
    const boundDevId = boundDeviceIdRef.current;
    if (!boundDevId) return;

    const isStillOpen = openDevicesList.some((d) => d.id === boundDevId);
    const metric = deviceMetrics?.[boundDevId];
    const isNowDead = metric?.status === 'disconnected' || metric?.status === 'error';

    if (!isStillOpen || isNowDead) {
      setSequenceStatus('ERROR');
      setSequenceError(
        !isStillOpen
          ? `Terminal ${boundDeviceLabel || boundDevId} was closed. Sequence stopped.`
          : `Terminal ${boundDeviceLabel || boundDevId} disconnected. Sequence stopped.`
      );
      boundDeviceIdRef.current = '';
    }
  }, [openDevicesList, deviceMetrics, sequenceStatus, boundDeviceLabel]);

  const handleStartSequence = () => {
    // Runtime target re-validation
    if (!effectiveTargetId || !openDevicesList.some((d) => d.id === effectiveTargetId)) {
      setInsertFeedback('No active terminal. Open and focus a connected terminal first.');
      setTimeout(() => setInsertFeedback(null), 3000);
      return;
    }
    if (isDisconnected) {
      setInsertFeedback(`${targetLabel} is disconnected`);
      setTimeout(() => setInsertFeedback(null), 3000);
      return;
    }
    if (!onInsertCommand || commandLines.length === 0) return;

    boundDeviceIdRef.current = effectiveTargetId;
    stepSubmittedRef.current = false;
    stepOutputReceivedRef.current = false;
    setSequenceError(null);
    setCurrentStep(0);
    currentStepRef.current = 0;
    setSequenceStatus('COMMAND_INSERTED');

    // Inserts ONLY first command into terminal buffer (NEVER auto-executes)
    onInsertCommand(effectiveTargetId, commandLines[0]);

    setTimeout(() => {
      if (sequenceStatusRef.current === 'COMMAND_INSERTED') {
        setSequenceStatus('AWAITING_USER_ENTER');
      }
    }, 40);
  };

  const handlePauseSequence = () => {
    if (sequenceStatus !== 'READY' && sequenceStatus !== 'COMPLETED' && sequenceStatus !== 'CANCELLED') {
      setSequenceStatus('PAUSED');
    }
  };

  const handleResumeSequence = () => {
    if (sequenceStatus === 'PAUSED' && boundDeviceIdRef.current) {
      if (!openDevicesList.some((d) => d.id === boundDeviceIdRef.current) || isDisconnected) {
        setSequenceStatus('ERROR');
        setSequenceError(`Target ${boundDeviceLabel} is no longer available.`);
        return;
      }
      setSequenceStatus('AWAITING_USER_ENTER');
      stepSubmittedRef.current = false;
      stepOutputReceivedRef.current = false;
      if (onInsertCommand && commandLines[currentStepRef.current]) {
        onInsertCommand(boundDeviceIdRef.current, commandLines[currentStepRef.current]);
      }
    }
  };

  const handleCancelSequence = () => {
    setSequenceStatus('CANCELLED');
    setTimeout(() => {
      setSequenceStatus('READY');
      setCurrentStep(0);
      currentStepRef.current = 0;
      boundDeviceIdRef.current = '';
      stepSubmittedRef.current = false;
      stepOutputReceivedRef.current = false;
    }, 500);
  };

  useEffect(() => {
    if (sequenceStatus === 'READY' || sequenceStatus === 'CANCELLED') return;

    const onSubmitted = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.deviceId && detail.deviceId === boundDeviceIdRef.current) {
        if (sequenceStatusRef.current === 'AWAITING_USER_ENTER' || sequenceStatusRef.current === 'COMMAND_INSERTED') {
          stepSubmittedRef.current = true;
          setSequenceStatus('COMMAND_RUNNING');
        }
      }
    };

    const onOutput = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.deviceId && detail.deviceId === boundDeviceIdRef.current) {
        if (sequenceStatusRef.current === 'COMMAND_RUNNING') {
          stepOutputReceivedRef.current = true;
          setSequenceStatus('WAITING_FOR_AUTHENTIC_PROMPT');
        }
      }
    };

    const onPromptReady = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.deviceId && detail.deviceId === boundDeviceIdRef.current) {
        const currentStat = sequenceStatusRef.current;
        // CRITICAL: We only advance if the command was actually submitted by user pressing Enter!
        // A prompt appearing before user submitted must NEVER advance the queue.
        if (
          (currentStat === 'COMMAND_RUNNING' || currentStat === 'WAITING_FOR_AUTHENTIC_PROMPT') &&
          stepSubmittedRef.current
        ) {
          stepSubmittedRef.current = false;
          stepOutputReceivedRef.current = false;

          const nextIdx = currentStepRef.current + 1;
          if (nextIdx < commandLines.length) {
            setSequenceStatus('INSERTING_NEXT_COMMAND');
            setCurrentStep(nextIdx);
            currentStepRef.current = nextIdx;

            // Insert next command into bound terminal buffer (NEVER auto-executes)
            if (onInsertCommand && boundDeviceIdRef.current) {
              onInsertCommand(boundDeviceIdRef.current, commandLines[nextIdx]);
            }

            // Advance to AWAITING_USER_ENTER
            setTimeout(() => {
              if (sequenceStatusRef.current === 'INSERTING_NEXT_COMMAND') {
                setSequenceStatus('AWAITING_USER_ENTER');
              }
            }, 40);
          } else {
            setSequenceStatus('COMPLETED');
            setTimeout(() => {
              setSequenceStatus('READY');
              setCurrentStep(0);
              currentStepRef.current = 0;
              boundDeviceIdRef.current = '';
            }, 3500);
          }
        }
      }
    };

    window.addEventListener('cliq:command_submitted', onSubmitted);
    window.addEventListener('cliq:command_output', onOutput);
    window.addEventListener('cliq:prompt_ready', onPromptReady);

    return () => {
      window.removeEventListener('cliq:command_submitted', onSubmitted);
      window.removeEventListener('cliq:command_output', onOutput);
      window.removeEventListener('cliq:prompt_ready', onPromptReady);
    };
  }, [sequenceStatus, commandLines, onInsertCommand]);

  // Single-command copy or multi-command Copy All
  const handleCopyAll = () => {
    navigator.clipboard.writeText(cleanCode);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Per-line quick copy
  const handleCopyLine = (idx: number, lineCmd: string) => {
    navigator.clipboard.writeText(lineCmd);
    setCopiedLineIdx(idx);
    setTimeout(() => setCopiedLineIdx(null), 2000);
  };

  // Single-command insert (inserts exactly one command line into target terminal buffer)
  const handleInsertSingle = (overrideDevId?: string) => {
    const devId = overrideDevId || effectiveTargetId;
    if (!devId || !openDevicesList.some((d) => d.id === devId)) {
      setInsertFeedback('No active terminal. Open and focus a connected terminal first.');
      setTimeout(() => setInsertFeedback(null), 3000);
      return;
    }
    if (isDisconnected) {
      setInsertFeedback(`${targetLabel} is disconnected`);
      setTimeout(() => setInsertFeedback(null), 3000);
      return;
    }
    if (!onInsertCommand) return;

    const dev = openDevicesList.find((d) => d.id === devId);
    const label = dev?.label || devId;
    const cmd = commandLines[0] || cleanCode;

    // Safety rule: Never executes command automatically. Populates terminal input buffer only.
    onInsertCommand(devId, cmd);
    setInsertFeedback(`Inserted in ${label}`);
    setIsDeviceMenuOpen(false);
    setTimeout(() => setInsertFeedback(null), 2500);
  };

  // Multi-command per-line insert (inserts ONLY this specific line into target terminal buffer)
  const handleInsertSingleLine = (idx: number, lineCmd: string) => {
    const devId = sequenceStatus !== 'READY' && boundDeviceIdRef.current ? boundDeviceIdRef.current : effectiveTargetId;
    if (!devId || !openDevicesList.some((d) => d.id === devId)) {
      setLineInsertFeedback((prev) => ({
        ...prev,
        [idx]: 'No active terminal. Open and focus a connected terminal first.',
      }));
      setTimeout(() => {
        setLineInsertFeedback((prev) => {
          const next = { ...prev };
          delete next[idx];
          return next;
        });
      }, 3000);
      return;
    }
    if (isDisconnected) {
      setLineInsertFeedback((prev) => ({
        ...prev,
        [idx]: `${targetLabel} is disconnected`,
      }));
      setTimeout(() => {
        setLineInsertFeedback((prev) => {
          const next = { ...prev };
          delete next[idx];
          return next;
        });
      }, 3000);
      return;
    }
    if (!onInsertCommand) return;
    const trimmed = lineCmd.trim();
    if (!trimmed) return;

    onInsertCommand(devId, trimmed);
    const label = openDevicesList.find((d) => d.id === devId)?.label || devId;
    setLineInsertFeedback((prev) => ({ ...prev, [idx]: `Inserted in ${label}` }));
    setTimeout(() => {
      setLineInsertFeedback((prev) => {
        const next = { ...prev };
        delete next[idx];
        return next;
      });
    }, 2500);
  };

  const handleExplain = () => {
    if (!onExplainCommand || !effectiveTargetId) return;
    onExplainCommand(effectiveTargetId, cleanCode);
  };

  return (
    <div className="my-2.5 max-w-full overflow-hidden rounded-lg border border-neutral-700/80 bg-[#12151c] shadow-md">
      {/* Code Block Header Toolbar */}
      <div className="flex items-center justify-between border-b border-neutral-700/70 bg-[#1a1e27] px-2.5 py-1.5 text-[11px] font-mono text-neutral-400 gap-1.5 overflow-x-auto min-w-0">
        <div className="flex items-center gap-1.5 shrink-0">
          <TerminalIcon size={12} className="text-cyan-400" />
          <span className="font-semibold uppercase tracking-wider text-cyan-400">
            {language || (isCli ? 'cisco' : 'code')}
          </span>
          {isMultiCommandCli && (
            <span className="rounded bg-neutral-800/80 px-1.5 py-0.2 text-[9.5px] text-neutral-400 font-mono border border-neutral-700/50">
              {commandLines.length} commands
            </span>
          )}
        </div>

        {/* Action Toolbar */}
        <div className="flex items-center gap-1 shrink-0 whitespace-nowrap">
          {/* CLI Target Device Indicator and Actions */}
          {isCli && onInsertCommand && (
            <>
              {isNoTerminalOpen ? (
                /* No terminal open */
                <span
                  className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-neutral-800/80 text-neutral-400 border border-neutral-700/60 shrink-0 select-none"
                  title="No active terminal. Open and focus a connected terminal first."
                >
                  No active terminal. Open and focus a connected terminal first.
                </span>
              ) : isTerminalOpenButNotFocused ? (
                /* Terminal open but not focused */
                <span
                  className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-neutral-800/80 text-neutral-400 border border-neutral-700/60 shrink-0 select-none"
                  title="Click or focus any open terminal to insert commands"
                >
                  Please focus a terminal to insert
                </span>
              ) : isDisconnected ? (
                /* Target terminal disconnected */
                <span
                  className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-red-950/60 text-red-300 border border-red-800/60 shrink-0 select-none"
                  title={`Selected terminal ${targetLabel} is disconnected`}
                >
                  {targetLabel} is disconnected
                </span>
              ) : !isMultiCommandCli ? (
                /* Single Command: Compact Indicator "↵ Insert into <device>" */
                <div className="relative inline-flex items-center" ref={menuRef}>
                  <button
                    type="button"
                    onClick={() => handleInsertSingle()}
                    disabled={!isTargetEligible || Boolean(insertFeedback)}
                    className={`flex items-center gap-1 px-2 py-0.5 rounded text-[10.5px] font-medium transition-colors whitespace-nowrap ${
                      insertFeedback
                        ? insertFeedback.startsWith('No active') || insertFeedback.includes('disconnected')
                          ? 'bg-red-950/80 text-red-300 border border-red-700'
                          : 'bg-emerald-950/80 text-emerald-300 border border-emerald-700'
                        : 'bg-cyan-950/70 hover:bg-cyan-900 text-cyan-200 border border-cyan-800/80 hover:text-white'
                    } disabled:opacity-50 disabled:cursor-not-allowed`}
                    title={`Insert command into ${targetLabel}'s terminal input buffer (does not execute)`}
                  >
                    {insertFeedback ? (
                      <>
                        <CheckIcon size={11} className="text-emerald-400 shrink-0" />
                        <span className="whitespace-nowrap">{insertFeedback}</span>
                      </>
                    ) : (
                      <>
                        <span className="text-cyan-400 font-bold shrink-0">↵</span>
                        <span className="whitespace-nowrap">Insert into {targetLabel}</span>
                      </>
                    )}
                  </button>
                  {openDevicesList.length > 1 && (
                    <button
                      type="button"
                      onClick={() => setIsDeviceMenuOpen((prev) => !prev)}
                      className="ml-0.5 px-1 py-0.5 rounded bg-neutral-800/60 hover:bg-neutral-700 text-neutral-400 hover:text-cyan-300 transition border border-neutral-700/60"
                      title="Switch target device (override)"
                    >
                      <ChevronDownIcon size={10} />
                    </button>
                  )}
                  {isDeviceMenuOpen && openDevicesList.length > 0 && (
                    <div className="absolute right-0 top-full z-50 mt-1 min-w-[160px] rounded-md border border-neutral-700 bg-neutral-900/95 p-1 shadow-xl backdrop-blur-sm">
                      <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-neutral-400 border-b border-neutral-800">
                        Target Device
                      </div>
                      <div className="mt-1 space-y-0.5 max-h-40 overflow-y-auto">
                        {openDevicesList.map((dev) => {
                          const isSelected = dev.id === effectiveTargetId;
                          const isFocused = dev.id === focusedDeviceId;
                          return (
                            <button
                              key={dev.id}
                              type="button"
                              onClick={() => {
                                setOverrideDeviceId(dev.id);
                                setIsDeviceMenuOpen(false);
                                handleInsertSingle(dev.id);
                              }}
                              className={`flex items-center justify-between w-full px-2 py-1 rounded text-[11px] text-left transition ${
                                isSelected
                                  ? 'bg-cyan-950/80 text-cyan-200 font-semibold'
                                  : 'text-neutral-300 hover:bg-neutral-800 hover:text-white'
                              }`}
                            >
                              <div className="flex items-center gap-1.5 truncate">
                                <span className="truncate">{dev.label}</span>
                                {isFocused && (
                                  <span className="rounded bg-cyan-500/20 px-1 py-0.2 text-[9px] font-mono text-cyan-400 border border-cyan-500/40">
                                    Focused
                                  </span>
                                )}
                              </div>
                              {isSelected && <CheckIcon size={11} className="text-cyan-400 shrink-0" />}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              ) : isReadOnly ? (
                /* Multi Command (Read-Only): Compact "↵ Insert Sequence into <device>" */
                <div className="relative inline-flex items-center gap-1" ref={menuRef}>
                  <button
                    type="button"
                    onClick={handleStartSequence}
                    disabled={!isTargetEligible || sequenceStatus !== 'READY'}
                    className="flex items-center gap-1 px-2 py-0.5 rounded text-[10.5px] font-medium transition-colors whitespace-nowrap bg-cyan-950/80 hover:bg-cyan-900 text-cyan-200 border border-cyan-700/80 hover:text-white shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    title={`Step-by-step insert ${commandLines.length} commands into ${targetLabel} (awaits your manual Enter for each)`}
                  >
                    <span className="text-cyan-400 font-bold shrink-0">↵</span>
                    <span>Insert Sequence into {targetLabel}</span>
                    <span className="rounded bg-cyan-900/60 px-1 py-0.2 text-[9px] font-mono text-cyan-300 border border-cyan-600/40">
                      {commandLines.length}
                    </span>
                  </button>
                  {openDevicesList.length > 1 && sequenceStatus === 'READY' && (
                    <button
                      type="button"
                      onClick={() => setIsDeviceMenuOpen((prev) => !prev)}
                      className="px-1 py-0.5 rounded bg-neutral-800/60 hover:bg-neutral-700 text-neutral-400 hover:text-cyan-300 transition border border-neutral-700/60"
                      title="Switch target device (override)"
                    >
                      <ChevronDownIcon size={10} />
                    </button>
                  )}
                  {sequenceStatus !== 'READY' && sequenceStatus !== 'CANCELLED' && focusedDeviceId && focusedDeviceId !== boundDeviceIdRef.current && (
                    <span className="text-[9.5px] font-mono text-amber-400/90 shrink-0">
                      (Bound to {boundDeviceLabel})
                    </span>
                  )}
                  {isDeviceMenuOpen && openDevicesList.length > 0 && (
                    <div className="absolute right-0 top-full z-50 mt-1 min-w-[160px] rounded-md border border-neutral-700 bg-neutral-900/95 p-1 shadow-xl backdrop-blur-sm">
                      <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-neutral-400 border-b border-neutral-800">
                        Target Device
                      </div>
                      <div className="mt-1 space-y-0.5 max-h-40 overflow-y-auto">
                        {openDevicesList.map((dev) => {
                          const isSelected = dev.id === effectiveTargetId;
                          const isFocused = dev.id === focusedDeviceId;
                          return (
                            <button
                              key={dev.id}
                              type="button"
                              onClick={() => {
                                setOverrideDeviceId(dev.id);
                                setIsDeviceMenuOpen(false);
                              }}
                              className={`flex items-center justify-between w-full px-2 py-1 rounded text-[11px] text-left transition ${
                                isSelected
                                  ? 'bg-cyan-950/80 text-cyan-200 font-semibold'
                                  : 'text-neutral-300 hover:bg-neutral-800 hover:text-white'
                              }`}
                            >
                              <div className="flex items-center gap-1.5 truncate">
                                <span className="truncate">{dev.label}</span>
                                {isFocused && (
                                  <span className="rounded bg-cyan-500/20 px-1 py-0.2 text-[9px] font-mono text-cyan-400 border border-cyan-500/40">
                                    Focused
                                  </span>
                                )}
                              </div>
                              {isSelected && <CheckIcon size={11} className="text-cyan-400 shrink-0" />}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                /* Multi Command (Config Changes): Safety notice */
                <span
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-950/60 text-amber-300 border border-amber-700/60 shrink-0"
                  title="Contains configuration-changing commands. Auto-sequencing disabled for safety — use individual line Insert."
                >
                  <span className="text-amber-400 font-bold">⚠</span>
                  <span>Config changes (individual insert only)</span>
                </span>
              )}
            </>
          )}

          {/* Explain Action Button */}
          {isCli && onExplainCommand && (
            <button
              onClick={handleExplain}
              type="button"
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-medium text-neutral-300 transition-colors hover:bg-neutral-800 hover:text-cyan-300 whitespace-nowrap"
              title={`Ask Copilot to explain ${isMultiCommandCli ? 'these commands' : 'this command'} for ${targetLabel}`}
            >
              <ExplainIcon size={11} className="text-amber-400" />
              <span>Explain</span>
            </button>
          )}

          {/* Copy Button (or Copy All for multiline) */}
          <button
            onClick={handleCopyAll}
            type="button"
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-medium text-neutral-300 transition-colors hover:bg-neutral-800 hover:text-neutral-100 whitespace-nowrap"
            title={isMultiCommandCli ? "Copy all commands in this block" : "Copy command to clipboard"}
          >
            {copied ? (
              <>
                <CheckIcon size={11} className="text-emerald-400" />
                <span className="text-emerald-400">{isMultiCommandCli ? 'Copied All' : 'Copied'}</span>
              </>
            ) : (
              <>
                <CopyIcon size={11} />
                <span>{isMultiCommandCli ? 'Copy All' : 'Copy'}</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Code Block Body */}
      {isMultiCommandCli ? (
        /* Multi-command interactive row list */
        <div className="divide-y divide-neutral-800/60 bg-[#12151c] max-w-full overflow-hidden">
          {/* Active sequence progress banner */}
          {sequenceStatus !== 'READY' && (
            <div className={`flex items-center justify-between px-3 py-2 text-[11px] font-mono select-none border-b ${
              sequenceStatus === 'ERROR'
                ? 'bg-red-950/60 border-red-800/70 text-red-200'
                : 'bg-[#0e1726] border-cyan-800/60 text-cyan-200'
            }`}>
              <div className="flex items-center gap-2 min-w-0">
                <span className="relative flex h-2 w-2 shrink-0">
                  {(sequenceStatus === 'COMMAND_RUNNING' || sequenceStatus === 'WAITING_FOR_AUTHENTIC_PROMPT' || sequenceStatus === 'INSERTING_NEXT_COMMAND') && (
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-75"></span>
                  )}
                  <span
                    className={`relative inline-flex rounded-full h-2 w-2 ${
                      sequenceStatus === 'COMPLETED'
                        ? 'bg-emerald-400'
                        : sequenceStatus === 'PAUSED'
                        ? 'bg-amber-400'
                        : sequenceStatus === 'ERROR'
                        ? 'bg-rose-500'
                        : 'bg-cyan-400'
                    }`}
                  ></span>
                </span>
                <span className="truncate font-medium">
                  {sequenceStatus === 'COMPLETED'
                    ? `✓ Sequence complete (${commandLines.length} of ${commandLines.length} commands)`
                    : sequenceStatus === 'PAUSED'
                    ? `Paused: Command ${currentStep + 1} of ${commandLines.length}`
                    : (sequenceStatus === 'COMMAND_RUNNING' || sequenceStatus === 'WAITING_FOR_AUTHENTIC_PROMPT' || sequenceStatus === 'INSERTING_NEXT_COMMAND')
                    ? `Command ${currentStep + 1} of ${commandLines.length} — executing on ${boundDeviceLabel}...`
                    : sequenceStatus === 'ERROR'
                    ? (sequenceError || 'Sequence stopped')
                    : sequenceStatus === 'CANCELLED'
                    ? 'Sequence cancelled'
                    : `Command ${currentStep + 1} of ${commandLines.length} — awaiting Enter on ${boundDeviceLabel || targetLabel}`}
                </span>
              </div>

              <div className="flex items-center gap-1.5 shrink-0 ml-2">
                {sequenceStatus !== 'COMPLETED' && sequenceStatus !== 'ERROR' && sequenceStatus !== 'CANCELLED' && (
                  <>
                    {sequenceStatus === 'PAUSED' ? (
                      <button
                        type="button"
                        onClick={handleResumeSequence}
                        className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-900/80 hover:bg-emerald-800 text-emerald-200 border border-emerald-700/70 transition"
                        title="Resume command sequence"
                      >
                        Resume
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={handlePauseSequence}
                        className="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-900/80 hover:bg-amber-800 text-amber-200 border border-amber-700/70 transition"
                        title="Pause sequence before next command"
                      >
                        Pause
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={handleCancelSequence}
                      className="px-2 py-0.5 rounded text-[10px] font-semibold bg-rose-950/80 hover:bg-rose-900 text-rose-300 border border-rose-800/70 transition"
                      title="Cancel command sequence"
                    >
                      Cancel
                    </button>
                  </>
                )}
                {sequenceStatus === 'ERROR' && (
                  <button
                    type="button"
                    onClick={() => {
                      setSequenceStatus('READY');
                      setSequenceError(null);
                      setCurrentStep(0);
                    }}
                    className="px-2 py-0.5 rounded text-[10px] font-semibold bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 transition"
                  >
                    Dismiss
                  </button>
                )}
              </div>
            </div>
          )}

          {commandLines.map((cmd, idx) => {
            const feedback = lineInsertFeedback[idx];
            const isLineCopied = copiedLineIdx === idx;
            const isCurrentStep = sequenceStatus !== 'READY' && sequenceStatus !== 'CANCELLED' && idx === currentStep;
            const isPastStep = sequenceStatus !== 'READY' && sequenceStatus !== 'CANCELLED' && idx < currentStep;

            return (
              <div
                key={idx}
                className={`flex items-center justify-between px-3 py-1.5 transition gap-2 group ${
                  isCurrentStep
                    ? 'bg-cyan-950/40 border-l-2 border-cyan-400'
                    : 'hover:bg-neutral-800/30'
                }`}
              >
                {/* Command text with line number */}
                <div className="flex items-center gap-2.5 min-w-0 flex-1 overflow-x-auto py-0.5">
                  <span className="text-[10px] font-mono text-neutral-500 select-none w-4 shrink-0 text-right">
                    {idx + 1}
                  </span>
                  <code className="text-emerald-300 font-mono text-[11.5px] whitespace-nowrap selection:bg-cyan-900/60 selection:text-white">
                    {cmd}
                  </code>
                  {isCurrentStep && (
                    <span className={`rounded px-1.5 py-0.2 text-[9px] font-mono uppercase tracking-wide border ${
                      sequenceStatus === 'COMMAND_RUNNING' || sequenceStatus === 'WAITING_FOR_AUTHENTIC_PROMPT' || sequenceStatus === 'INSERTING_NEXT_COMMAND'
                        ? 'bg-cyan-900/60 text-cyan-300 border-cyan-500/50'
                        : sequenceStatus === 'PAUSED'
                        ? 'bg-amber-900/60 text-amber-300 border-amber-500/50'
                        : sequenceStatus === 'ERROR'
                        ? 'bg-rose-900/60 text-rose-300 border-rose-500/50'
                        : 'bg-cyan-900/60 text-cyan-300 border-cyan-500/50'
                    }`}>
                      {sequenceStatus === 'COMMAND_RUNNING' || sequenceStatus === 'WAITING_FOR_AUTHENTIC_PROMPT' || sequenceStatus === 'INSERTING_NEXT_COMMAND'
                        ? 'Running'
                        : sequenceStatus === 'PAUSED'
                        ? 'Paused'
                        : sequenceStatus === 'ERROR'
                        ? 'Error'
                        : 'Awaiting Enter'}
                    </span>
                  )}
                  {isPastStep && (
                    <span className="rounded bg-emerald-950/60 px-1.5 py-0.2 text-[9px] font-mono text-emerald-400 border border-emerald-700/40">
                      ✓ Done
                    </span>
                  )}
                </div>

                {/* Per-command actions */}
                <div className="flex items-center gap-1.5 shrink-0 whitespace-nowrap">
                  {/* Individual Insert Action */}
                  {onInsertCommand && (
                    <button
                      type="button"
                      onClick={() => handleInsertSingleLine(idx, cmd)}
                      disabled={!isTargetEligible || sequenceStatus !== 'READY' || Boolean(feedback)}
                      className={`flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors whitespace-nowrap ${
                        feedback
                          ? feedback.startsWith('No active') || feedback.includes('disconnected')
                            ? 'bg-red-950/80 text-red-300 border border-red-700'
                            : 'bg-emerald-950/80 text-emerald-300 border border-emerald-700'
                          : 'bg-cyan-950/70 hover:bg-cyan-900 text-cyan-200 border border-cyan-800/80 hover:text-white'
                      } disabled:opacity-50 disabled:cursor-not-allowed`}
                      title={
                        isNoTerminalOpen
                          ? 'No active terminal. Open and focus a connected terminal first.'
                          : isTerminalOpenButNotFocused
                          ? 'Please focus a terminal to insert'
                          : isDisconnected
                          ? `Selected terminal ${targetLabel} is disconnected`
                          : `Insert "${cmd}" into ${targetLabel}'s terminal input buffer (does not execute)`
                      }
                    >
                      {feedback ? (
                        <>
                          <CheckIcon size={10} className="text-emerald-400 shrink-0" />
                          <span>{feedback}</span>
                        </>
                      ) : (
                        <>
                          <InsertIcon size={10} className="text-cyan-400 shrink-0" />
                          <span>Insert</span>
                          {effectiveTargetId && (
                            <span className="rounded bg-cyan-900/60 px-1 py-0.2 text-[9px] font-mono text-cyan-200 border border-cyan-700/50 max-w-[50px] truncate shrink-0">
                              {targetLabel}
                            </span>
                          )}
                        </>
                      )}
                    </button>
                  )}

                  {/* Per-command Copy Action */}
                  <button
                    type="button"
                    onClick={() => handleCopyLine(idx, cmd)}
                    className="p-1 rounded text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800 transition"
                    title={`Copy "${cmd}" to clipboard`}
                  >
                    {isLineCopied ? (
                      <CheckIcon size={10} className="text-emerald-400" />
                    ) : (
                      <CopyIcon size={10} />
                    )}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        /* Single-command standard pre code */
        <pre className="max-w-full overflow-x-auto p-3 font-mono text-[11.5px] leading-relaxed text-emerald-300 selection:bg-cyan-900/60 selection:text-white">
          <code>{cleanCode}</code>
        </pre>
      )}
    </div>
  );
}

function TableBlock({ rows }: { rows: string[][] }) {
  if (rows.length === 0) return null;
  const header = rows[0];
  const body = rows.slice(1);

  return (
    <div className="my-2.5 max-w-full overflow-x-auto rounded-lg border border-neutral-700/80 shadow-sm">
      <table className="w-full border-collapse text-left text-[11px]">
        <thead>
          <tr className="border-b border-neutral-700 bg-neutral-800/80 font-semibold text-neutral-200">
            {header.map((col, cIdx) => (
              <th key={cIdx} className="px-3 py-1.5 border-r border-neutral-700/50 last:border-0 font-medium">
                {col.trim()}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rIdx) => (
            <tr
              key={rIdx}
              className={`border-b border-neutral-700/40 last:border-0 ${
                rIdx % 2 === 0 ? 'bg-neutral-900/40' : 'bg-neutral-900/80'
              } hover:bg-neutral-800/40 transition-colors`}
            >
              {row.map((cell, cIdx) => (
                <td key={cIdx} className="px-3 py-1.5 border-r border-neutral-700/40 last:border-0 font-mono text-neutral-300">
                  {cell.trim()}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TextBlock({ text }: { text: string }) {
  const lines = text.split('\n');

  return (
    <div className="space-y-1">
      {lines.map((line, idx) => {
        const trimmed = line.trim();

        // Header 3
        if (trimmed.startsWith('### ')) {
          return (
            <h3 key={idx} className="pt-2 text-xs font-bold uppercase tracking-wider text-cyan-300">
              {renderInlineStyles(trimmed.slice(4))}
            </h3>
          );
        }
        // Header 2 / 1
        if (trimmed.startsWith('## ') || trimmed.startsWith('# ')) {
          const t = trimmed.replace(/^#+\s*/, '');
          return (
            <h2 key={idx} className="pt-2.5 text-sm font-bold text-neutral-100">
              {renderInlineStyles(t)}
            </h2>
          );
        }
        // Bullet list item
        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
          return (
            <div key={idx} className="flex items-start gap-2 pl-2">
              <span className="text-cyan-400 leading-normal">•</span>
              <span className="flex-1 text-neutral-300">{renderInlineStyles(trimmed.slice(2))}</span>
            </div>
          );
        }
        // Numbered list item
        const numMatch = trimmed.match(/^(\d+)\.\s+(.*)$/);
        if (numMatch) {
          return (
            <div key={idx} className="flex items-start gap-2 pl-2">
              <span className="font-semibold text-cyan-400">{numMatch[1]}.</span>
              <span className="flex-1 text-neutral-300">{renderInlineStyles(numMatch[2])}</span>
            </div>
          );
        }
        // GitHub style alert (e.g. > [!WARNING])
        if (trimmed.startsWith('> [!')) {
          const alertType = trimmed.includes('WARNING') ? 'warning' : trimmed.includes('NOTE') ? 'note' : 'info';
          return (
            <div
              key={idx}
              className={`p-2.5 my-2 rounded-md border text-[11px] ${
                alertType === 'warning'
                  ? 'bg-amber-950/40 border-amber-800/60 text-amber-200'
                  : 'bg-cyan-950/40 border-cyan-800/60 text-cyan-200'
              }`}
            >
              {renderInlineStyles(trimmed.replace(/^>\s*/, ''))}
            </div>
          );
        }
        // Blockquote
        if (trimmed.startsWith('> ')) {
          return (
            <blockquote key={idx} className="border-l-2 border-cyan-500/50 pl-2.5 my-1 text-neutral-400 italic">
              {renderInlineStyles(trimmed.slice(2))}
            </blockquote>
          );
        }
        // Blank line
        if (!trimmed) {
          return <div key={idx} className="h-1.5" />;
        }
        // Normal paragraph
        return (
          <p key={idx} className="leading-relaxed text-neutral-300">
            {renderInlineStyles(line)}
          </p>
        );
      })}
    </div>
  );
}

function renderInlineStyles(text: string): React.ReactNode {
  // Regex to split bold (**text**), inline code (`code`), italics (*text*)
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g);

  return parts.map((part, i) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code
          key={i}
          className="rounded bg-neutral-800 px-1 py-0.5 font-mono text-[11px] text-amber-300 border border-neutral-700/60"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return (
        <strong key={i} className="font-semibold text-neutral-100">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      return (
        <em key={i} className="italic text-neutral-300">
          {part.slice(1, -1)}
        </em>
      );
    }
    return part;
  });
}

interface ParsedBlock {
  type: 'text' | 'code' | 'table';
  text: string;
  language: string;
  rows?: string[][];
}

function parseMarkdownBlocks(raw: string): ParsedBlock[] {
  const blocks: ParsedBlock[] = [];
  const lines = raw.split('\n');
  let inCode = false;
  let codeLang = '';
  let codeBuffer: string[] = [];
  let textBuffer: string[] = [];
  let tableBuffer: string[] = [];

  const flushText = () => {
    if (textBuffer.length > 0) {
      blocks.push({ type: 'text', text: textBuffer.join('\n'), language: '' });
      textBuffer = [];
    }
  };

  const flushTable = () => {
    if (tableBuffer.length > 0) {
      const rows = tableBuffer
        .filter((l) => !/^\s*\|?\s*[-:]+[-| :]*\|?\s*$/.test(l))
        .map((l) =>
          l
            .split('|')
            .map((c) => c.trim())
            .filter((_, idx, arr) => (idx > 0 && idx < arr.length - 1) || (idx === 0 && arr.length <= 2)),
        )
        .filter((r) => r.length > 0);
      if (rows.length > 0) {
        blocks.push({ type: 'table', text: '', language: '', rows });
      }
      tableBuffer = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.trim().startsWith('```')) {
      if (!inCode) {
        flushText();
        flushTable();
        inCode = true;
        codeLang = line.trim().slice(3).trim();
        codeBuffer = [];
      } else {
        inCode = false;
        blocks.push({ type: 'code', text: codeBuffer.join('\n'), language: codeLang });
        codeBuffer = [];
      }
      continue;
    }

    if (inCode) {
      codeBuffer.push(line);
      continue;
    }

    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
      flushText();
      tableBuffer.push(line);
      continue;
    } else if (tableBuffer.length > 0) {
      flushTable();
    }

    textBuffer.push(line);
  }

  if (inCode && codeBuffer.length > 0) {
    blocks.push({ type: 'code', text: codeBuffer.join('\n'), language: codeLang });
  }
  flushText();
  flushTable();

  return blocks;
}
