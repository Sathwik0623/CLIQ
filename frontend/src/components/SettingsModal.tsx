/**
 * SettingsModal — VS Code-style preferences dialog for the CLIQ IDE shell.
 *
 * Free of emojis, using clean SVG icons.
 */
import { useState, useEffect } from 'react';
import {
  SettingsIcon,
  TerminalIcon,
  LockIcon,
  CliqLogoIcon,
  CloseIcon,
  CheckIcon,
} from './Icons';

type SettingsModalProps = {
  isOpen: boolean;
  onClose: () => void;
  host: string;
  userName?: string;
  labTitle?: string;
  onApplyTerminalSettings?: (settings: { fontSize: number; cursorBlink: boolean }) => void;
};

export default function SettingsModal({
  isOpen,
  onClose,
  host,
  userName,
  labTitle,
  onApplyTerminalSettings,
}: SettingsModalProps) {
  const [fontSize, setFontSize] = useState<number>(() => {
    try {
      const saved = localStorage.getItem('cliq.terminal.fontSize');
      return saved ? parseInt(saved, 10) : 14;
    } catch {
      return 14;
    }
  });

  const [cursorBlink, setCursorBlink] = useState<boolean>(() => {
    try {
      const saved = localStorage.getItem('cliq.terminal.cursorBlink');
      return saved !== null ? saved === 'true' : true;
    } catch {
      return true;
    }
  });

  const [activeTab, setActiveTab] = useState<'terminal' | 'session' | 'about'>('terminal');

  useEffect(() => {
    try {
      localStorage.setItem('cliq.terminal.fontSize', fontSize.toString());
      localStorage.setItem('cliq.terminal.cursorBlink', cursorBlink.toString());
    } catch {
      // storage unavailable
    }
    onApplyTerminalSettings?.({ fontSize, cursorBlink });
  }, [fontSize, cursorBlink, onApplyTerminalSettings]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs select-none">
      <div className="fixed inset-0" onClick={onClose} />
      <div
        className="relative z-10 w-full max-w-lg rounded-xl shadow-2xl overflow-hidden border text-xs"
        style={{
          background: 'var(--ide-panel-bg)',
          borderColor: 'var(--ide-border)',
          color: 'var(--ide-text)',
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between border-b px-4 py-3"
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-header-bg)' }}
        >
          <div className="flex items-center gap-2">
            <SettingsIcon size={14} className="text-slate-300" />
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-200">
              Settings & Preferences
            </h2>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:text-slate-100 hover:bg-[var(--ide-hover)] transition-colors cursor-pointer"
          >
            <CloseIcon size={12} />
          </button>
        </div>

        {/* Tab navigation */}
        <div
          className="flex border-b text-xs px-2"
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-sidebar-bg)' }}
        >
          <button
            onClick={() => setActiveTab('terminal')}
            className="flex items-center gap-1.5 px-3 py-2 border-b-2 font-medium transition-colors cursor-pointer"
            style={{
              borderColor: activeTab === 'terminal' ? 'var(--ide-accent)' : 'transparent',
              color: activeTab === 'terminal' ? '#ffffff' : 'var(--ide-text-muted)',
            }}
          >
            <TerminalIcon size={12} />
            <span>Terminal</span>
          </button>
          <button
            onClick={() => setActiveTab('session')}
            className="flex items-center gap-1.5 px-3 py-2 border-b-2 font-medium transition-colors cursor-pointer"
            style={{
              borderColor: activeTab === 'session' ? 'var(--ide-accent)' : 'transparent',
              color: activeTab === 'session' ? '#ffffff' : 'var(--ide-text-muted)',
            }}
          >
            <LockIcon size={12} />
            <span>Session & Security</span>
          </button>
          <button
            onClick={() => setActiveTab('about')}
            className="flex items-center gap-1.5 px-3 py-2 border-b-2 font-medium transition-colors cursor-pointer"
            style={{
              borderColor: activeTab === 'about' ? 'var(--ide-accent)' : 'transparent',
              color: activeTab === 'about' ? '#ffffff' : 'var(--ide-text-muted)',
            }}
          >
            <CliqLogoIcon size={12} />
            <span>About</span>
          </button>
        </div>

        {/* Body */}
        <div className="p-4 space-y-4 max-h-96 overflow-y-auto">
          {activeTab === 'terminal' && (
            <div className="space-y-4">
              <div>
                <label className="block font-semibold mb-1 text-slate-200">
                  Terminal Font Size: {fontSize}px
                </label>
                <div className="flex items-center gap-2">
                  {[12, 13, 14, 16].map((size) => (
                    <button
                      key={size}
                      onClick={() => setFontSize(size)}
                      className="px-3 py-1 rounded border transition-colors cursor-pointer"
                      style={{
                        background: fontSize === size ? 'var(--ide-selected)' : 'var(--ide-hover)',
                        borderColor: fontSize === size ? 'var(--ide-accent)' : 'var(--ide-border)',
                        color: fontSize === size ? '#ffffff' : 'var(--ide-text)',
                      }}
                    >
                      {size}px
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="block font-semibold mb-1 text-slate-200">Cursor Blinking</label>
                <button
                  onClick={() => setCursorBlink((v) => !v)}
                  className="flex items-center gap-2 px-3 py-1.5 rounded border cursor-pointer"
                  style={{
                    background: 'var(--ide-hover)',
                    borderColor: 'var(--ide-border)',
                  }}
                >
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ background: cursorBlink ? '#4ec9b0' : '#858585' }}
                  />
                  <span>{cursorBlink ? 'Enabled (Smooth Blink)' : 'Disabled (Steady)'}</span>
                </button>
              </div>

              <div>
                <label className="block font-semibold mb-1 text-slate-200">Font Family</label>
                <div
                  className="rounded p-2 border font-mono text-[11px]"
                  style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text-muted)' }}
                >
                  Consolas, 'Courier New', ui-monospace, monospace
                </div>
                <p className="mt-1 text-[10px]" style={{ color: 'var(--ide-text-dim)' }}>
                  Monospace font family is synchronized with the xterm.js canvas cell geometry.
                </p>
              </div>
            </div>
          )}

          {activeTab === 'session' && (
            <div className="space-y-3">
              <div className="rounded p-3 border space-y-2" style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}>
                <div className="flex justify-between">
                  <span style={{ color: 'var(--ide-text-muted)' }}>Connected Host:</span>
                  <span className="font-mono font-medium text-slate-200">{host || 'Direct Session'}</span>
                </div>
                {userName && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--ide-text-muted)' }}>Authenticated User:</span>
                    <span className="font-mono text-slate-200">{userName}</span>
                  </div>
                )}
                {labTitle && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--ide-text-muted)' }}>Active Lab:</span>
                    <span className="font-medium text-slate-200">{labTitle}</span>
                  </div>
                )}
              </div>

              <div
                className="rounded p-3 border space-y-1.5"
                style={{ background: 'rgba(34,197,94,0.06)', borderColor: 'rgba(34,197,94,0.25)', color: '#86efac' }}
              >
                <div className="flex items-center gap-1.5 font-bold">
                  <LockIcon size={13} className="text-emerald-400 shrink-0" />
                  <span>Zero-Disk Credential Architecture</span>
                </div>
                <p className="text-[11px] leading-relaxed text-slate-300">
                  Authentication tokens and enable secrets exist strictly in transient process memory. Credentials are never written to disk, SQLite, localStorage, sessionStorage, or logs.
                </p>
              </div>
            </div>
          )}

          {activeTab === 'about' && (
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <CliqLogoIcon size={24} className="text-sky-400 shrink-0" />
                <div>
                  <h3 className="font-bold text-sm text-slate-100">CLIQ Network Engineering IDE</h3>
                  <p className="text-[11px]" style={{ color: 'var(--ide-text-muted)' }}>Enterprise Edition — Phase T.1</p>
                </div>
              </div>
              <p className="text-[11px] leading-relaxed" style={{ color: 'var(--ide-text-muted)' }}>
                High-performance terminal workspace designed for network operators. Features progressive real-time xterm streaming, CML console integration, live topology discovery, and on-demand advisory intelligence.
              </p>
              <div className="border-t pt-2 flex justify-between text-[10px]" style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text-dim)' }}>
                <span>Build: Production Vite + React 18</span>
                <span>Branch: main</span>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          className="flex justify-end border-t px-4 py-2"
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-header-bg)' }}
        >
          <button
            onClick={onClose}
            className="flex items-center gap-1 rounded px-3 py-1 font-semibold transition-colors cursor-pointer"
            style={{ background: 'var(--ide-accent)', color: '#ffffff' }}
          >
            <CheckIcon size={12} />
            <span>Done</span>
          </button>
        </div>
      </div>
    </div>
  );
}
