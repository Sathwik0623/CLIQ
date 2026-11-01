/**
 * StatusBar — VS Code-style bottom status strip for the CLIQ IDE shell.
 *
 * Shows:
 * - Real connection status dot
 * - Active device name and mode
 * - Active lab title
 * - Latency in ms (tabular numerals)
 * Height: 22px, background var(--ide-status-bg).
 */
import React from 'react';
import { TerminalIcon } from './Icons';

type StatusBarProps = {
  connectionState?: 'connected' | 'connecting' | 'error' | 'disconnected';
  activeDeviceLabel?: string;
  activeDeviceMode?: string;
  labTitle?: string;
  latencyMs?: number;
};

const MODE_LABEL: Record<string, string> = {
  exec:           'EXEC',
  enable:         'PRIV EXEC (#)',
  config:         'CONFIG',
  'config-if':    'CONFIG-IF',
  'config-router':'CONFIG-ROUTER',
  'config-line':  'CONFIG-LINE',
  'config-vlan':  'CONFIG-VLAN',
};

function connDot(state?: string): string {
  if (state === 'connected')  return '#4ec9b0';
  if (state === 'connecting') return '#cca700';
  if (state === 'error')      return '#f44747';
  return '#858585';
}

export default function StatusBar({
  connectionState,
  activeDeviceLabel,
  activeDeviceMode,
  labTitle,
  latencyMs,
}: StatusBarProps) {
  const modeLabel = MODE_LABEL[activeDeviceMode ?? ''] ?? (activeDeviceMode?.toUpperCase() ?? '');

  return (
    <div
      className="flex shrink-0 items-center gap-3 overflow-hidden px-3 select-none"
      style={{
        height: 22,
        background: 'var(--ide-status-bg)',
        color: 'var(--ide-status-text)',
        fontSize: 11,
      }}
    >
      {/* ── Connection Status ── */}
      <div className="flex shrink-0 items-center gap-1.5">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{ background: connDot(connectionState) }}
        />
        <span className="font-medium">
          {connectionState === 'connecting'
            ? 'Connecting…'
            : connectionState === 'connected'
            ? 'Connected'
            : connectionState === 'error'
            ? 'Error'
            : 'Idle'}
        </span>
      </div>

      {/* ── Active Device ── */}
      {activeDeviceLabel && (
        <>
          <span className="opacity-40">│</span>
          <div className="flex shrink-0 items-center gap-1 font-mono font-medium">
            <TerminalIcon size={11} className="opacity-80" />
            <span>{activeDeviceLabel}</span>
          </div>
        </>
      )}

      {/* ── Active Mode ── */}
      {modeLabel && (
        <>
          <span className="opacity-40">│</span>
          <span className="shrink-0 font-mono font-semibold opacity-90">{modeLabel}</span>
        </>
      )}

      {/* ── Active Lab ── */}
      {labTitle && (
        <>
          <span className="opacity-40">│</span>
          <span className="min-w-0 truncate opacity-80">{labTitle}</span>
        </>
      )}

      {/* ── Latency ── */}
      {latencyMs !== undefined && latencyMs > 0 && (
        <span className="ml-auto shrink-0 tabular-nums font-mono opacity-80">
          {latencyMs} ms
        </span>
      )}
    </div>
  );
}
