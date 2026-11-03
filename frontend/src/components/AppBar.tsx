/**
 * AppBar — top application title bar of the CLIQ IDE shell.
 *
 * Single source of truth for:
 * - Brand & active lab context
 * - Command Palette (Ctrl+P / Ctrl+K)
 * - Single Topology view navigation toggle
 * - Terminal split layout selector
 * - Advisory panel toggle
 * - Settings & Disconnect actions
 *
 * Free of emojis, using clean SVG icons.
 */
import React from 'react';
import {
  CliqLogoIcon,
  SearchIcon,
  TopologyIcon,
  SplitSingleIcon,
  SplitHorizontalIcon,
  SplitVerticalIcon,
  AdvisoryIcon,
  SettingsIcon,
  CloseIcon,
  AlertTriangleIcon,
} from './Icons';
import { SparklesIcon } from './copilot/CopilotIcons';

export type SplitMode = 'single' | 'horizontal' | 'vertical';

type AppBarProps = {
  labTitle?: string;
  labExpiresAt?: string | null;
  userName?: string;
  intelCount?: number;
  intelPanelOpen?: boolean;
  copilotOpen?: boolean;
  onToggleCopilot?: () => void;
  hasTopology?: boolean;
  topologyActive?: boolean;
  splitMode: SplitMode;
  openDeviceCount: number;
  onToggleIntel?: () => void;
  onToggleTopology?: () => void;
  onSetSplitMode?: (mode: SplitMode) => void;
  onOpenCommandPalette?: () => void;
  onOpenSettings?: () => void;
  onExitTerminal?: () => void;
  onChangeLab?: () => void;
  unverifiedSession?: boolean;
};

export default function AppBar({
  labTitle,
  userName,
  intelCount = 0,
  intelPanelOpen,
  copilotOpen = false,
  onToggleCopilot,
  hasTopology = false,
  topologyActive = false,
  splitMode,
  openDeviceCount,
  onToggleIntel,
  onToggleTopology,
  onSetSplitMode,
  onOpenCommandPalette,
  onOpenSettings,
  onExitTerminal,
  onChangeLab,
  unverifiedSession,
}: AppBarProps) {
  return (
    <header
      className="flex shrink-0 items-center justify-between gap-3 border-b px-3 select-none text-xs"
      style={{
        height: 35,
        background: 'var(--ide-header-bg)',
        borderColor: 'var(--ide-border)',
      }}
    >
      {/* ── Left: Brand & Lab Context ── */}
      <div className="flex min-w-0 items-center gap-2">
        <div className="flex shrink-0 items-center gap-1.5 font-bold tracking-wider text-sky-400">
          <CliqLogoIcon size={14} className="text-sky-400" />
          <span className="font-semibold text-slate-100">CLIQ</span>
        </div>

        {labTitle && (
          <div className="flex items-center gap-1.5 truncate">
            <span style={{ color: 'var(--ide-border)' }}>/</span>
            <button
              onClick={onChangeLab}
              className="truncate transition-colors hover:text-sky-300 font-medium"
              style={{ color: 'var(--ide-text-muted)' }}
              title={`Active Lab: ${labTitle} — click to switch lab`}
            >
              {labTitle}
            </button>
          </div>
        )}

        {unverifiedSession && (
          <span
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium"
            style={{ background: '#451a03', color: '#fbbf24', border: '1px solid #92400e' }}
            title="Reservation cannot be verified — may be operating an expired lab"
          >
            <AlertTriangleIcon size={11} className="text-amber-400 shrink-0" />
            <span>Unverified</span>
          </span>
        )}
      </div>

      {/* ── Center: Quick Open / Command Palette Trigger ── */}
      <div className="flex-1 max-w-sm mx-2">
        <button
          onClick={onOpenCommandPalette}
          className="flex w-full items-center justify-between gap-2 rounded px-2.5 py-1 text-xs transition-colors group cursor-pointer"
          style={{
            background: 'var(--ide-bg)',
            border: '1px solid var(--ide-border)',
            color: 'var(--ide-text-muted)',
          }}
          title="Quick Open devices & commands (Ctrl+P / Ctrl+K)"
        >
          <span className="flex items-center gap-1.5 truncate">
            <SearchIcon size={13} className="opacity-60 shrink-0" />
            <span className="truncate group-hover:text-slate-200">Search devices or commands...</span>
          </span>
          <kbd
            className="rounded px-1.5 py-0.2 text-[10px] font-mono shrink-0"
            style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-dim)', border: '1px solid var(--ide-border)' }}
          >
            Ctrl+P
          </kbd>
        </button>
      </div>

      {/* ── Right: Split Layout, Advisory, Settings, Disconnect ── */}
      <div className="flex shrink-0 items-center gap-1.5">
        {/* Single Split Terminal Layout Control */}
        {onSetSplitMode && (
          <div
            className="flex items-center rounded overflow-hidden p-0.5"
            style={{ background: 'var(--ide-hover)', border: '1px solid var(--ide-border)' }}
            title="Terminal Split Layout"
          >
            <button
              onClick={() => onSetSplitMode('single')}
              className="p-1 rounded transition-colors cursor-pointer"
              style={{
                background: splitMode === 'single' ? 'var(--ide-accent)' : 'transparent',
                color: splitMode === 'single' ? '#ffffff' : 'var(--ide-text-muted)',
              }}
              title="Single terminal layout"
            >
              <SplitSingleIcon size={13} />
            </button>
            <button
              onClick={() => onSetSplitMode('horizontal')}
              disabled={openDeviceCount < 2 && splitMode !== 'horizontal'}
              className="p-1 rounded transition-colors disabled:opacity-40 cursor-pointer"
              style={{
                background: splitMode === 'horizontal' ? 'var(--ide-accent)' : 'transparent',
                color: splitMode === 'horizontal' ? '#ffffff' : 'var(--ide-text-muted)',
              }}
              title={openDeviceCount < 2 ? 'Open 2+ devices to split side-by-side' : 'Split side-by-side'}
            >
              <SplitHorizontalIcon size={13} />
            </button>
            <button
              onClick={() => onSetSplitMode('vertical')}
              disabled={openDeviceCount < 2 && splitMode !== 'vertical'}
              className="p-1 rounded transition-colors disabled:opacity-40 cursor-pointer"
              style={{
                background: splitMode === 'vertical' ? 'var(--ide-accent)' : 'transparent',
                color: splitMode === 'vertical' ? '#ffffff' : 'var(--ide-text-muted)',
              }}
              title={openDeviceCount < 2 ? 'Open 2+ devices to split stacked' : 'Split stacked'}
            >
              <SplitVerticalIcon size={13} />
            </button>
          </div>
        )}



                {/* CLIQ Copilot toggle */}
        {onToggleCopilot && (
          <button
            onClick={onToggleCopilot}
            className="flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-semibold transition-colors cursor-pointer"
            style={{
              background: copilotOpen ? '#0891b2' : 'var(--ide-hover)',
              color: copilotOpen ? '#ffffff' : 'var(--ide-text-muted)',
              border: copilotOpen ? '1px solid #06b6d4' : '1px solid var(--ide-border)',
            }}
            title={copilotOpen ? 'Hide CLIQ Copilot' : 'Open CLIQ Copilot (Meta Llama 3.3)'}
          >
            <SparklesIcon size={13} className="shrink-0 text-cyan-300" />
            <span>Copilot</span>
          </button>
        )}

        {/* Settings button */}
        {onOpenSettings && (
          <button
            onClick={onOpenSettings}
            className="flex items-center justify-center rounded p-1.5 transition-colors hover:text-slate-100 cursor-pointer"
            style={{
              background: 'var(--ide-hover)',
              color: 'var(--ide-text-muted)',
              border: '1px solid var(--ide-border)',
            }}
            title="Settings & Preferences (Ctrl+,)"
          >
            <SettingsIcon size={13} />
          </button>
        )}

        {userName && (
          <span className="text-xs px-1 text-slate-400 hidden lg:inline truncate max-w-[100px]">
            {userName}
          </span>
        )}

        {/* Disconnect button */}
        {onExitTerminal && (
          <button
            onClick={onExitTerminal}
            className="flex items-center gap-1 rounded px-2 py-1 text-xs font-medium transition-colors cursor-pointer"
            style={{
              background: 'rgba(239,68,68,0.15)',
              color: '#fca5a5',
              border: '1px solid rgba(239,68,68,0.3)',
            }}
            title="Disconnect active session and return to Sandbox Hub"
          >
            <CloseIcon size={12} className="shrink-0" />
            <span>Disconnect</span>
          </button>
        )}
      </div>
    </header>
  );
}
