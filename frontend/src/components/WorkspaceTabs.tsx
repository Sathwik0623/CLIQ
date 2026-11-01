/**
 * WorkspaceTabs — VS Code-style editor tab strip for the CLIQ IDE central workspace.
 *
 * Renders:
 * - A compact 32px tab strip at the top of the central workspace
 * - Chrome/VS Code-inspired tabs with rounded-t-[4px] corners
 * - Dark tab surfaces with clear active/inactive contrast
 * - Subtle accent bar (2px #007acc) on active tab
 * - SVG device icons from Icons.tsx (Router / Switch / Firewall / Server / PC)
 * - Connection status dot
 * - Mode indicator badges (EXEC, #, CFG)
 * - Tab close button (subtle on active, visible on hover for inactive)
 * - Integrated Topology tab with close/switch capability
 */
import React from 'react';
import type { Device } from '../types/api';
import { getDeviceIcon, TopologyIcon, CloseIcon } from './Icons';

type WorkspaceTabsProps = {
  openDevices: Device[];
  activeDeviceId: string;
  metrics: Record<string, { mode: string; status: string } | undefined>;
  hasTopology: boolean;
  topologyActive: boolean;
  onSetActive: (deviceId: string) => void;
  onClose: (deviceId: string) => void;
  onCloseTopology?: () => void;
  onShowTopology: () => void;
};

const MODE_LABEL: Record<string, string> = {
  exec:           'EXEC',
  enable:         '#',
  config:         'CFG',
  'config-if':    'IF',
  'config-router':'RTR',
  'config-line':  'LN',
  'config-vlan':  'VL',
};

function connDotColor(status?: string): string {
  if (status === 'connected') return '#10b981';
  if (status === 'error')     return '#ef4444';
  return '#94a3b8';
}

export default function WorkspaceTabs({
  openDevices,
  activeDeviceId,
  metrics,
  hasTopology,
  topologyActive,
  onSetActive,
  onClose,
  onCloseTopology,
  onShowTopology,
}: WorkspaceTabsProps) {
  if (openDevices.length === 0 && !hasTopology) return null;

  return (
    <div
      className="flex shrink-0 items-center overflow-x-auto border-b select-none"
      style={{
        background: '#141414',
        borderColor: '#252526',
        height: 32,
      }}
    >
      {/* ── Open Device Terminal Tabs ── */}
      {openDevices.map((device) => {
        const isActive = device.id === activeDeviceId && !topologyActive;
        const m = metrics[device.id];
        const modeLabel = m ? (MODE_LABEL[m.mode] ?? '') : '';

        return (
          <div
            key={device.id}
            className="group relative flex shrink-0 items-center h-full cursor-pointer transition-colors rounded-t-[4px]"
            style={{
              background: isActive ? '#1e1e1e' : '#141414',
              borderColor: '#252526',
              borderRight: '1px solid #252526',
              borderTop: isActive ? '2px solid #007acc' : '2px solid transparent',
            }}
            onClick={() => onSetActive(device.id)}
            title={`Device: ${device.label} (${device.type || 'Router'})`}
          >
            <div className="flex items-center gap-1.5 whitespace-nowrap pl-3 pr-1.5 py-1 text-xs">
              {/* Separate connection status dot */}
              <span
                className="h-1.5 w-1.5 rounded-full shrink-0"
                style={{ background: connDotColor(m?.status) }}
                title={`Status: ${m?.status ?? 'connected'}`}
              />

              {/* Real identifiable device SVG icon */}
              <span
                className="shrink-0 transition-colors"
                style={{ color: isActive ? '#38bdf8' : '#64748b' }}
              >
                {getDeviceIcon(device.type, device.label, 15)}
              </span>

              {/* Tab label */}
              <span
                className={`text-xs transition-colors ${
                  isActive ? 'font-medium text-slate-100' : 'text-slate-400 group-hover:text-slate-200'
                }`}
              >
                {device.label}
              </span>

              {/* Mode badge */}
              {modeLabel && (
                <span
                  className="rounded px-1 py-0.2 text-[9px] font-mono font-bold"
                  style={{
                    background: isActive ? 'rgba(0,122,204,0.18)' : 'rgba(255,255,255,0.05)',
                    color: isActive ? '#75beff' : '#94a3b8',
                    border: '1px solid rgba(255,255,255,0.08)',
                  }}
                >
                  {modeLabel}
                </span>
              )}
            </div>

            {/* Tab close button */}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onClose(device.id);
              }}
              className={`mr-2 p-0.5 rounded text-slate-400 hover:text-white hover:bg-[#333333] transition-all ${
                isActive ? 'opacity-60 hover:opacity-100' : 'opacity-0 group-hover:opacity-100'
              }`}
              title={`Close ${device.label} terminal`}
            >
              <CloseIcon size={11} />
            </button>
          </div>
        );
      })}

      {/* ── Topology Tab (when map view is open/active) ── */}
      {hasTopology && topologyActive && (
        <div
          className="group relative flex shrink-0 items-center h-full cursor-pointer transition-colors rounded-t-[4px]"
          style={{
            background: '#1e1e1e',
            borderRight: '1px solid #252526',
            borderTop: '2px solid #007acc',
          }}
          onClick={onShowTopology}
          title="Network Topology View"
        >
          <div className="flex items-center gap-1.5 whitespace-nowrap pl-3 pr-1.5 py-1 text-xs text-sky-400 font-medium">
            <TopologyIcon size={15} className="shrink-0 text-sky-400" />
            <span>Topology Map</span>
          </div>
          {openDevices.length > 0 && onCloseTopology && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onCloseTopology();
              }}
              className="mr-2 p-0.5 rounded text-slate-400 hover:text-white hover:bg-[#333333] opacity-60 hover:opacity-100 transition-opacity"
              title="Close Topology View (return to active terminal tab)"
            >
              <CloseIcon size={11} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
