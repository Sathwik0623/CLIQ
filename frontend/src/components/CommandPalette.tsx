/**
 * CommandPalette — VS Code-style quick open / command launcher (Ctrl+P / Ctrl+K).
 *
 * Free of emojis, using clean SVG icons.
 */
import React, { useEffect, useRef, useState } from 'react';
import type { Device } from '../types/api';
import type { SplitMode } from './AppBar';
import {
  SearchIcon,
  getDeviceIcon,
  TopologyIcon,
  AdvisoryIcon,
  KeyIcon,
  SplitSingleIcon,
  SplitHorizontalIcon,
  SplitVerticalIcon,
  SettingsIcon,
  CloseIcon,
} from './Icons';

export type CommandAction = {
  id: string;
  title: string;
  category: string;
  icon: React.ReactNode;
  shortcut?: string;
  run: () => void;
};

type CommandPaletteProps = {
  isOpen: boolean;
  onClose: () => void;
  devices: Device[];
  activeDeviceId: string;
  hasTopology: boolean;
  intelPanelOpen: boolean;
  canElevatePrivileges: boolean;
  splitMode: SplitMode;
  onOpenDevice: (deviceId: string) => void;
  onShowTopology: () => void;
  onToggleIntel: () => void;
  onRequestEnable?: () => void;
  onSetSplitMode?: (mode: SplitMode) => void;
  onOpenSettings?: () => void;
  onExitTerminal?: () => void;
};

export default function CommandPalette({
  isOpen,
  onClose,
  devices,
  hasTopology,
  intelPanelOpen,
  canElevatePrivileges,
  onOpenDevice,
  onShowTopology,
  onToggleIntel,
  onRequestEnable,
  onSetSplitMode,
  onOpenSettings,
  onExitTerminal,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setSelectedIndex(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  // Build command list with clean SVG icons
  const actions: CommandAction[] = [];

  // 1. Device quick switch actions
  devices.forEach((d) => {
    actions.push({
      id: `device-${d.id}`,
      title: `Switch to ${d.label} (${d.type ?? 'device'} · ${d.node_state ?? 'unknown'})`,
      category: 'Devices',
      icon: getDeviceIcon(d.type, d.label, 14, 'text-sky-400'),
      run: () => {
        onOpenDevice(d.id);
        onClose();
      },
    });
  });

  // 2. Topology
  if (hasTopology) {
    actions.push({
      id: 'view-topology',
      title: 'View Network Topology Map',
      category: 'View',
      icon: <TopologyIcon size={14} className="text-sky-400" />,
      run: () => {
        onShowTopology();
        onClose();
      },
    });
  }

  // 3. Advisory toggle
  actions.push({
    id: 'toggle-advisory',
    title: intelPanelOpen ? 'Hide CLIQ Advisory Drawer' : 'Open CLIQ Advisory Drawer',
    category: 'View',
    icon: <AdvisoryIcon size={14} className="text-violet-400" />,
    run: () => {
      onToggleIntel();
      onClose();
    },
  });

  // 4. Privileged mode elevation
  if (canElevatePrivileges && onRequestEnable) {
    actions.push({
      id: 'elevate-privilege',
      title: 'Elevate to Privileged EXEC Mode (enable)',
      category: 'Terminal',
      icon: <KeyIcon size={14} className="text-amber-400" />,
      run: () => {
        onRequestEnable();
        onClose();
      },
    });
  }

  // 5. Layout actions
  if (onSetSplitMode) {
    actions.push({
      id: 'layout-single',
      title: 'Terminal Layout: Single Full View',
      category: 'Layout',
      icon: <SplitSingleIcon size={14} className="text-slate-300" />,
      run: () => {
        onSetSplitMode('single');
        onClose();
      },
    });
    actions.push({
      id: 'layout-horizontal',
      title: 'Terminal Layout: Split Side-by-Side',
      category: 'Layout',
      icon: <SplitHorizontalIcon size={14} className="text-slate-300" />,
      run: () => {
        onSetSplitMode('horizontal');
        onClose();
      },
    });
    actions.push({
      id: 'layout-vertical',
      title: 'Terminal Layout: Split Stacked',
      category: 'Layout',
      icon: <SplitVerticalIcon size={14} className="text-slate-300" />,
      run: () => {
        onSetSplitMode('vertical');
        onClose();
      },
    });
  }

  // 6. Settings
  if (onOpenSettings) {
    actions.push({
      id: 'open-settings',
      title: 'Open Preferences & Settings',
      category: 'Settings',
      icon: <SettingsIcon size={14} className="text-slate-300" />,
      shortcut: 'Ctrl+,',
      run: () => {
        onOpenSettings();
        onClose();
      },
    });
  }

  // 7. Disconnect
  if (onExitTerminal) {
    actions.push({
      id: 'disconnect-session',
      title: 'Disconnect Lab Session',
      category: 'Session',
      icon: <CloseIcon size={14} className="text-rose-400" />,
      run: () => {
        onExitTerminal();
        onClose();
      },
    });
  }

  const filtered = actions.filter((a) =>
    a.title.toLowerCase().includes(query.toLowerCase()) ||
    a.category.toLowerCase().includes(query.toLowerCase())
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((prev) => (prev + 1) % Math.max(1, filtered.length));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex((prev) => (prev - 1 + filtered.length) % Math.max(1, filtered.length));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const target = filtered[selectedIndex];
      if (target) {
        target.run();
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-14 bg-black/60 backdrop-blur-xs select-none">
      <div className="fixed inset-0" onClick={onClose} />
      <div
        className="relative z-10 w-full max-w-xl rounded-lg shadow-2xl overflow-hidden border text-xs"
        style={{
          background: 'var(--ide-sidebar-bg)',
          borderColor: 'var(--ide-border)',
        }}
      >
        {/* Input bar */}
        <div
          className="flex items-center gap-2.5 border-b px-3.5 py-2.5"
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-bg)' }}
        >
          <SearchIcon size={14} className="text-slate-400 shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelectedIndex(0);
            }}
            onKeyDown={handleKeyDown}
            placeholder="Type a device name or IDE action..."
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-500 text-slate-100"
          />
          <kbd
            className="rounded px-1.5 py-0.5 text-[10px] font-mono shrink-0"
            style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-dim)', border: '1px solid var(--ide-border)' }}
          >
            ESC
          </kbd>
        </div>

        {/* Results list */}
        <div className="max-h-80 overflow-y-auto p-1.5">
          {filtered.length === 0 ? (
            <div className="py-8 text-center text-xs text-slate-500">
              No matching commands or devices found
            </div>
          ) : (
            filtered.map((item, index) => {
              const isSelected = index === selectedIndex;
              return (
                <div
                  key={item.id}
                  onClick={item.run}
                  onMouseEnter={() => setSelectedIndex(index)}
                  className={`flex items-center justify-between rounded px-3 py-2 cursor-pointer transition-colors ${
                    isSelected ? 'bg-[var(--ide-selected)] text-white' : 'text-slate-300 hover:bg-[var(--ide-hover)]'
                  }`}
                >
                  <div className="flex items-center gap-2.5 truncate">
                    <span className="shrink-0">{item.icon}</span>
                    <span className="truncate">{item.title}</span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span
                      className="text-[9px] uppercase tracking-wider px-1.5 py-0.2 rounded font-semibold"
                      style={{
                        background: isSelected ? 'rgba(255,255,255,0.15)' : 'var(--ide-hover)',
                        color: isSelected ? '#ffffff' : 'var(--ide-text-dim)',
                      }}
                    >
                      {item.category}
                    </span>
                    {item.shortcut && (
                      <kbd className="text-[10px] font-mono text-slate-400">
                        {item.shortcut}
                      </kbd>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        <div
          className="flex items-center justify-between border-t px-3 py-1.5 text-[10px]"
          style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-header-bg)', color: 'var(--ide-text-dim)' }}
        >
          <span>Use ↑ ↓ to navigate, Enter to select</span>
          <span>CLIQ Network Engineering Workspace</span>
        </div>
      </div>
    </div>
  );
}
