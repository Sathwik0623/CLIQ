/**
 * ExplorerSidebar — VS Code-style left Explorer panel for the CLIQ IDE shell.
 *
 * Hierarchical tree containing:
 * - Active Lab context (with optional switcher if multiple labs exist)
 * - Discovered CML devices with real status dots, SVG device icons, mode badges
 * - Quick device filter input
 * - Secondary Topology entry point under Views
 * - Collapsible to thin rail
 */
import { useState } from 'react';
import type { Device, Lab } from '../types/api';
import { consoleBlockedReason, isConsoleBlocked } from '../lib/deviceState';
import {
  getDeviceIcon,
  TopologyIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CloseIcon,
  SearchIcon,
  ServerIcon,
} from './Icons';

type ExplorerSidebarProps = {
  allDevices: Device[];
  openDeviceIds: Set<string>;
  activeDeviceId: string;
  metrics: Record<string, { mode: string; status: string; latency: number } | undefined>;
  labs?: Lab[];
  selectedLabId?: string;
  labTitle?: string;
  hasTopology: boolean;
  topologyActive: boolean;
  collapsed?: boolean;
  onSelectLab?: (labId: string) => void;
  onToggleCollapse?: () => void;
  onOpenDevice: (deviceId: string) => void;
  onCloseDevice?: (deviceId: string) => void;
  onShowTopology: () => void;
};

const MODE_ABBR: Record<string, string> = {
  exec:           'EXEC',
  enable:         '#',
  config:         'CFG',
  'config-if':    'IF',
  'config-router':'RTR',
  'config-line':  'LN',
  'config-vlan':  'VL',
};

function stateColor(state?: string): string {
  // Green means confirmed ready (BOOTED, the SDK-verified marker) so the dot
  // always agrees with console gating. Transitional/active states from the
  // SDK-verified set read amber; stopped/defined read neutral gray; failure
  // strings read red. Matching is case-insensitive; raw values stay in the
  // tooltip untouched.
  const clean = (state ?? '').trim().toUpperCase();
  if (clean === 'BOOTED') return '#4ec9b0';
  if (clean === 'STARTED' || clean === 'STARTING' || clean === 'QUEUED') return '#cca700';
  if (clean === 'STOPPED' || clean === 'DEFINED') return '#858585';
  if (clean.includes('FAIL') || clean.includes('ERROR')) return '#f44747';
  return '#5a5a5a';
}

function connColor(status?: string): string {
  if (status === 'connected') return '#4ec9b0';
  if (status === 'error')     return '#f44747';
  return '#858585';
}

export default function ExplorerSidebar({
  allDevices,
  openDeviceIds,
  activeDeviceId,
  metrics,
  labs = [],
  selectedLabId,
  labTitle,
  hasTopology,
  topologyActive,
  collapsed = false,
  onSelectLab,
  onToggleCollapse,
  onOpenDevice,
  onCloseDevice,
  onShowTopology,
}: ExplorerSidebarProps) {
  const [filter, setFilter] = useState('');

  if (collapsed) {
    return (
      <aside
        className="flex h-full w-10 shrink-0 flex-col items-center py-2.5 border-r select-none"
        style={{
          background: 'var(--ide-sidebar-bg)',
          borderColor: 'var(--ide-border)',
        }}
      >
        <button
          onClick={onToggleCollapse}
          className="rounded p-1 text-slate-400 hover:text-slate-100 hover:bg-[var(--ide-hover)] transition-colors cursor-pointer"
          title="Expand Explorer Sidebar"
        >
          <ChevronRightIcon size={14} />
        </button>

        <div className="mt-6 flex flex-col gap-3 items-center">
          <span
            className="text-[10px] font-bold tracking-widest uppercase rotate-90 my-4 text-sky-400"
          >
            EXPLORER
          </span>

          <span
            className="rounded px-1 text-[9px] font-bold"
            style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-muted)' }}
          >
            {allDevices.length}
          </span>

          {hasTopology && (
            <button
              onClick={onShowTopology}
              className={`p-1.5 rounded transition-colors cursor-pointer ${
                topologyActive ? 'text-sky-400 bg-[var(--ide-selected)]' : 'text-slate-400 hover:bg-[var(--ide-hover)]'
              }`}
              title="Network Topology Map"
            >
              <TopologyIcon size={14} />
            </button>
          )}
        </div>
      </aside>
    );
  }

  const filteredDevices = allDevices.filter((d) =>
    d.label.toLowerCase().includes(filter.toLowerCase()) ||
    (d.type ?? '').toLowerCase().includes(filter.toLowerCase())
  );

  return (
    <aside
      className="flex h-full w-56 shrink-0 flex-col overflow-hidden border-r select-none text-xs"
      style={{
        background: 'var(--ide-sidebar-bg)',
        borderColor: 'var(--ide-border)',
      }}
    >
      {/* ── Section: Explorer Title ── */}
      <div
        className="flex shrink-0 items-center justify-between border-b px-3 py-2"
        style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-sidebar-bg)' }}
      >
        <span
          className="text-[10px] font-bold uppercase tracking-widest text-slate-400"
        >
          Explorer
        </span>
        {onToggleCollapse && (
          <button
            onClick={onToggleCollapse}
            className="p-0.5 rounded text-slate-400 hover:text-slate-200 hover:bg-[var(--ide-hover)] transition-colors cursor-pointer"
            title="Collapse Sidebar"
          >
            <ChevronLeftIcon size={14} />
          </button>
        )}
      </div>

      {/* ── Section: Active Lab / Lab Switcher ── */}
      {labs.length > 1 && onSelectLab ? (
        <div className="shrink-0 border-b p-2" style={{ borderColor: 'var(--ide-border-light)' }}>
          <label className="block text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
            Active Lab
          </label>
          <select
            value={selectedLabId}
            onChange={(e) => onSelectLab(e.target.value)}
            className="w-full rounded px-2 py-1 text-xs outline-none cursor-pointer"
            style={{
              background: 'var(--ide-bg)',
              color: 'var(--ide-text)',
              border: '1px solid var(--ide-border)',
            }}
          >
            {labs.map((l) => (
              <option key={l.id} value={l.id} className="bg-[#252526] text-white">
                {l.title}
              </option>
            ))}
          </select>
        </div>
      ) : labTitle ? (
        <div
          className="shrink-0 border-b px-3 py-1.5 flex items-center justify-between"
          style={{ borderColor: 'var(--ide-border-light)' }}
        >
          <span className="text-[10px] text-slate-500 truncate">
            Lab: <span className="font-medium text-slate-300">{labTitle}</span>
          </span>
        </div>
      ) : null}

      {/* ── Quick Device Filter ── */}
      {allDevices.length > 3 && (
        <div className="p-2 border-b" style={{ borderColor: 'var(--ide-border-light)' }}>
          <div className="relative flex items-center">
            <SearchIcon size={12} className="absolute left-2 text-slate-500 pointer-events-none" />
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter devices..."
              className="w-full rounded pl-6 pr-2 py-1 text-xs outline-none"
              style={{
                background: 'var(--ide-bg)',
                color: 'var(--ide-text)',
                border: '1px solid var(--ide-border)',
              }}
            />
            {filter && (
              <button
                onClick={() => setFilter('')}
                className="absolute right-2 text-slate-500 hover:text-slate-300"
              >
                <CloseIcon size={10} />
              </button>
            )}
          </div>
        </div>
      )}

      {/* ── Section: Discovered CML Devices Tree ── */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div
          className="shrink-0 flex items-center justify-between px-3 py-1.5 border-b"
          style={{ borderColor: 'var(--ide-border-light)' }}
        >
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
            Devices
          </span>
          <span
            className="rounded px-1.5 py-0.2 text-[9px] font-bold"
            style={{ background: 'var(--ide-hover)', color: 'var(--ide-text-muted)' }}
          >
            {filteredDevices.length}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {allDevices.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-slate-500">
              No devices discovered.
            </div>
          ) : filteredDevices.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs text-slate-500">
              No matches for "{filter}".
            </div>
          ) : (
            <ul className="py-1">
              {filteredDevices.map((device) => {
                const isOpen = openDeviceIds.has(device.id);
                const isActive = device.id === activeDeviceId && !topologyActive;
                const m = metrics[device.id];
                const modeAbbr = m ? (MODE_ABBR[m.mode] ?? m.mode?.toUpperCase() ?? '') : '';
                // Consoles open only for booted devices; open sessions are
                // never stranded and unknown states stay permissive (V.3).
                const blocked = !isOpen && isConsoleBlocked(device.node_state);

                return (
                  <li key={device.id}>
                    <div
                      className={`group flex items-center gap-2 px-3 py-1.5 transition-colors ${
                        blocked
                          ? 'opacity-50 cursor-not-allowed'
                          : 'cursor-pointer'
                      } ${
                        isActive
                          ? 'bg-[var(--ide-selected)] text-white'
                          : 'text-slate-300 hover:bg-[var(--ide-hover)]'
                      }`}
                      onClick={blocked ? undefined : () => onOpenDevice(device.id)}
                      title={blocked ? consoleBlockedReason(device.node_state) : `Open ${device.label} terminal`}
                    >
                      {/* State dot */}
                      <span
                        className="inline-block h-2 w-2 shrink-0 rounded-full"
                        style={{ background: stateColor(device.node_state) }}
                        title={`Status: ${device.node_state ?? 'unknown'}`}
                      />

                      {/* Device SVG Icon - 20px in fixed w-6 h-6 container for consistent alignment */}
                      <span className="w-6 h-6 flex items-center justify-center shrink-0 text-slate-400 group-hover:text-slate-200">
                        {getDeviceIcon(device.type, device.label, 20)}
                      </span>

                      {/* Device Label */}
                      <span className="min-w-0 flex-1 truncate text-xs font-medium">
                        {device.label}
                      </span>

                      {/* Active Mode badge */}
                      {isOpen && modeAbbr && (
                        <span
                          className="shrink-0 rounded px-1 py-0.2 text-[9px] font-mono font-bold"
                          style={{
                            background: isActive ? 'rgba(255,255,255,0.2)' : 'var(--ide-hover)',
                            color: isActive ? '#ffffff' : connColor(m?.status),
                            border: '1px solid var(--ide-border)',
                          }}
                        >
                          {modeAbbr}
                        </span>
                      )}

                      {/* Close button if session open */}
                      {isOpen && onCloseDevice && (
                        <button
                          className="shrink-0 p-0.5 rounded text-slate-400 hover:text-slate-100 opacity-0 group-hover:opacity-100 transition-opacity"
                          onClick={(e) => {
                            e.stopPropagation();
                            onCloseDevice(device.id);
                          }}
                          title="Close terminal session"
                        >
                          <CloseIcon size={11} />
                        </button>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      {/* ── Section: Views (Single Secondary Topology Entry) ── */}
      {hasTopology && (
        <div
          className="shrink-0 border-t"
          style={{ borderColor: 'var(--ide-border)' }}
        >
          <div className="flex items-center gap-1 px-3 py-1.5 border-b" style={{ borderColor: 'var(--ide-border-light)' }}>
            <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
              Views
            </span>
          </div>
          <button
            onClick={onShowTopology}
            className={`flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors cursor-pointer ${
              topologyActive
                ? 'bg-[var(--ide-selected)] text-sky-300 font-medium'
                : 'text-slate-300 hover:bg-[var(--ide-hover)]'
            }`}
          >
            <span className="w-6 h-6 flex items-center justify-center shrink-0 text-sky-400">
              <TopologyIcon size={18} />
            </span>
            <span className="text-xs font-medium">Topology Map</span>
          </button>
        </div>
      )}
    </aside>
  );
}
