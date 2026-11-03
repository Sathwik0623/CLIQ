import { useState } from 'react';
import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Device, Lab, TopologyLink, TopologyNode } from '../types/api';
import type { AvailableLab, LabDisplayState } from '../types/labs';
import { consoleBlockedReason, isConsoleBlocked } from '../lib/deviceState';
import LifecycleModal from './LifecycleModal';
import { CliqLogoIcon, TopologyIcon } from './Icons';

type LabDeviceStepProps = {
  labs: Lab[];
  availableLabs: AvailableLab[];
  labsLoading: boolean;
  selectedLabId: string;
  highlightLabId?: string | null;
  devices: Device[];
  topologyNodes: TopologyNode[];
  topologyLinks: TopologyLink[];
  loading: boolean;
  error: string;
  labsError?: string;
  token: string;
  onSelectLab: (labId: string) => void;
  onFetchDevices: () => Promise<void>;
  onOpenDevice: (deviceId: string) => void;
  onPlanNewLab?: () => void;
  onRefreshLabs: () => Promise<void>;
  onLifecycleEnded: (labId: string) => Promise<void>;
};

function stateBadge(display: LabDisplayState): { bg: string; text: string } {
  switch (display) {
    case 'Running':
      return { bg: 'rgba(78,201,176,0.15)', text: '#4ec9b0' };
    case 'Starting':
    case 'Stopping':
      return { bg: 'rgba(251,191,36,0.15)', text: '#fbbf24' };
    case 'Failed':
      return { bg: 'rgba(248,113,113,0.15)', text: '#f87171' };
    case 'Defined':
      return { bg: 'rgba(117,190,255,0.12)', text: '#75beff' };
    default:
      return { bg: 'rgba(148,163,184,0.15)', text: '#94a3b8' };
  }
}

function linkLabel(link: TopologyLink): string {
  if (link.label) {
    return link.label;
  }

  const src = link.sourceHandle || '';
  const tgt = link.targetHandle || '';
  const parts = [src, tgt].filter(Boolean);
  return parts.join(' ↔ ');
}

export default function LabDeviceStep(props: LabDeviceStepProps) {
  const [lifecycleTarget, setLifecycleTarget] = useState<{ lab: AvailableLab; mode: 'start' | 'stop' } | null>(null);

  const live = props.availableLabs.length > 0;
  const cards = live
    ? props.availableLabs
    : props.labs.map((l): AvailableLab => ({
        id: l.id,
        title: l.title,
        state: l.state ?? 'unknown',
        state_display: 'Unknown' as LabDisplayState,
        owner: '',
        device_count: null,
        converged: null,
        nodes_booted: null,
        nodes_total: null,
      }));
  const selected = cards.find((l) => l.id === props.selectedLabId);
  const runningCount = cards.filter((l) => l.state_display === 'Running').length;
  const stoppedCount = cards.filter((l) => l.state_display === 'Defined' || l.state_display === 'Failed').length;
  const totalDevices = cards.reduce((sum, l) => sum + (l.device_count ?? 0), 0);
  const countsKnown = cards.some((l) => l.device_count != null);

  const nodes: Node[] = props.topologyNodes.map((node) => ({
    id: node.id,
    position: node.position,
    data: { label: node.data.label },
    style: {
      border: '1px solid #4ec9b0',
      borderRadius: 10,
      background: '#252526',
      color: '#cccccc',
      overflow: 'hidden',
      whiteSpace: 'nowrap',
      textOverflow: 'ellipsis',
      maxWidth: '140px',
      fontSize: 12,
      fontWeight: 600,
      boxShadow: '0 1px 4px rgba(0,0,0,0.4)',
    },
  }));

  const edges: Edge[] = props.topologyLinks.map((link) => {
    const label = linkLabel(link);
    return {
      id: link.id,
      source: link.source,
      target: link.target,
      type: 'straight',
      style: { stroke: '#5a5a5a', strokeWidth: 2 },
      ...(label ? {
        label,
        labelStyle: { fill: '#75beff', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#252526', fillOpacity: 0.9 },
      } : {}),
    };
  });

  return (
    <div className="min-h-screen" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>
      <div className="mx-auto max-w-7xl px-5 py-6">
        <div className="mb-5 flex items-center justify-between border-b pb-3" style={{ borderColor: 'var(--ide-border)' }}>
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1.5 text-xl font-bold text-sky-400">
              <CliqLogoIcon size={20} /> CLIQ
            </span>
            <span style={{ color: 'var(--ide-border)' }}>/</span>
            <span className="text-sm font-semibold text-slate-300">
              Lab Selection ({cards.length} lab{cards.length === 1 ? '' : 's'})
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void props.onRefreshLabs()}
              disabled={props.labsLoading}
              className="rounded-lg border px-3 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)] disabled:opacity-60"
              style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              title="Refresh lab list and states from the controller"
            >
              {props.labsLoading ? 'Refreshing…' : 'Refresh'}
            </button>
            {props.onPlanNewLab && (
              <button
                type="button"
                onClick={props.onPlanNewLab}
                className="rounded-lg px-4 py-1.5 text-xs font-bold text-white"
                style={{ background: 'var(--ide-accent)' }}
                title="Plan and deploy a brand-new lab with Copilot"
              >
                + Create New Lab with Copilot
              </button>
            )}
          </div>
        </div>

        {props.labsError && (
          <div role="alert" className="mb-4 rounded-lg border px-3 py-2 text-xs font-semibold text-amber-200" style={{ background: '#291400', borderColor: '#b45309' }}>
            Live lab data unavailable: {props.labsError} States and device counts below may be stale or unknown — fix the connection, then press Refresh.
          </div>
        )}

        {props.error && (
          <p className="mb-4 text-xs text-rose-400 font-semibold">{props.error}</p>
        )}

        <div className="mb-4 flex flex-wrap gap-2 text-[11px] font-semibold" style={{ color: 'var(--ide-text-muted)' }}>
          <span className="rounded border px-2 py-1" style={{ borderColor: 'var(--ide-border)' }}>
            {cards.length} lab{cards.length === 1 ? '' : 's'} total
          </span>
          <span className="rounded border px-2 py-1 text-emerald-300" style={{ borderColor: 'var(--ide-border)' }}>
            {runningCount} running
          </span>
          <span className="rounded border px-2 py-1 text-sky-300" style={{ borderColor: 'var(--ide-border)' }}>
            {stoppedCount} stopped
          </span>
          <span className="rounded border px-2 py-1" style={{ borderColor: 'var(--ide-border)' }}>
            {countsKnown ? `${totalDevices} devices total` : 'device counts unknown'}
          </span>
          {!live && (
            <span className="rounded border px-2 py-1 text-amber-200" style={{ borderColor: '#b45309' }}>
              offline snapshot — live states unavailable
            </span>
          )}
        </div>

        {cards.length === 0 && !props.labsLoading ? (
          <div className="flex flex-col items-center justify-center gap-3 rounded-xl border py-16" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text-dim)' }}>
            <TopologyIcon size={36} className="opacity-30" />
            <span className="text-sm font-medium">No labs on this controller yet</span>
            {props.onPlanNewLab && (
              <button
                type="button"
                onClick={props.onPlanNewLab}
                className="rounded-lg px-4 py-2 text-xs font-bold text-white"
                style={{ background: 'var(--ide-accent)' }}
              >
                + Create New Lab with Copilot
              </button>
            )}
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {cards.map((lab) => {
              const badge = stateBadge(lab.state_display);
              const isSelected = lab.id === props.selectedLabId;
              const isHighlight = lab.id === props.highlightLabId;
              return (
                <div
                  key={lab.id}
                  className="rounded-xl border p-4 shadow-sm"
                  style={{
                    background: 'var(--ide-sidebar-bg)',
                    borderColor: isSelected || isHighlight ? 'var(--ide-accent)' : 'var(--ide-border)',
                    boxShadow: isHighlight ? '0 0 0 1px var(--ide-accent)' : undefined,
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-bold text-slate-200" title={lab.title}>
                        {lab.title}
                      </div>
                      <div className="mt-0.5 text-[11px]" style={{ color: 'var(--ide-text-muted)' }} title={`Controller state: ${lab.state}`}>
                        {lab.device_count == null ? '— devices' : `${lab.device_count} device${lab.device_count === 1 ? '' : 's'}`}
                        {lab.nodes_booted != null && lab.nodes_total != null && lab.nodes_total > 0
                          ? ` · ${lab.nodes_booted}/${lab.nodes_total} booted`
                          : ''}
                      </div>
                      {lab.error ? (
                        <div className="mt-0.5 text-[11px] font-semibold text-amber-200" title={lab.error}>
                          Live state unavailable — Refresh to retry
                        </div>
                      ) : null}
                    </div>
                    <span
                      className="shrink-0 rounded px-2 py-0.5 text-[11px] font-bold"
                      style={{ background: badge.bg, color: badge.text }}
                      title={`Controller state: ${lab.state}`}
                    >
                      {lab.state_display}
                    </span>
                  </div>
                  {isHighlight && (
                    <div className="mt-1 text-[11px] font-bold text-emerald-300">Newly created — just deployed</div>
                  )}
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      disabled={props.loading}
                      onClick={() => props.onSelectLab(lab.id)}
                      className="flex-1 rounded-lg px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60"
                      style={{ background: 'var(--ide-accent)' }}
                    >
                      {props.loading && isSelected ? 'Opening…' : 'Open'}
                    </button>
                    {(lab.state_display === 'Defined' || lab.state_display === 'Failed' || lab.state_display === 'Unknown') && (
                      <button
                        type="button"
                        onClick={() => setLifecycleTarget({ lab, mode: 'start' })}
                        className="flex-1 rounded-lg border px-3 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                        style={{ borderColor: '#15803d', color: '#4ade80' }}
                        title="Boot all nodes in this lab"
                      >
                        Start
                      </button>
                    )}
                    {lab.state_display === 'Running' && (
                      <button
                        type="button"
                        onClick={() => setLifecycleTarget({ lab, mode: 'stop' })}
                        className="flex-1 rounded-lg border px-3 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                        style={{ borderColor: '#7f1d1d', color: '#f87171' }}
                        title="Shut down all nodes in this lab (nothing is deleted)"
                      >
                        Stop
                      </button>
                    )}
                    {(lab.state_display === 'Starting' || lab.state_display === 'Stopping') && (
                      <button
                        type="button"
                        onClick={() => setLifecycleTarget({ lab, mode: lab.state_display === 'Starting' ? 'start' : 'stop' })}
                        className="flex-1 rounded-lg border px-3 py-1.5 text-xs font-semibold text-amber-300 hover:bg-[var(--ide-hover)]"
                        style={{ borderColor: '#78350f' }}
                        title="Reopen progress tracking for the in-flight operation (no new request)"
                      >
                        {lab.state_display}… Track
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {selected && (
          <div className="mt-4 grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
            <div className="rounded-xl border p-4 shadow-sm" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
              <div className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--ide-text-muted)' }}>
                {props.loading ? 'Loading devices…' : `Devices in ${selected.title} — click to open terminal`}
              </div>
              <div className="mt-2 space-y-1 max-h-96 overflow-y-auto">
                {props.devices.map((device) => {
                  const blocked = isConsoleBlocked(device.node_state);
                  return (
                    <button
                      key={device.id}
                      className="block w-full rounded px-2.5 py-2 text-left transition-colors hover:bg-[var(--ide-hover)] disabled:cursor-not-allowed disabled:opacity-50"
                      style={{ border: '1px solid transparent' }}
                      disabled={blocked}
                      title={blocked ? consoleBlockedReason(device.node_state) : `Open ${device.label} terminal`}
                      onClick={() => props.onOpenDevice(device.id)}
                    >
                      <div className="font-medium text-xs text-slate-200">{device.label}</div>
                      <div className="text-[10px]" style={{ color: 'var(--ide-text-muted)' }}>
                        {device.type ?? 'device'} · {device.node_state ?? 'unknown'}
                        {blocked ? ' · console unavailable' : ''}
                      </div>
                    </button>
                  );
                })}
                {props.devices.length === 0 && !props.loading && (
                  <div className="text-[11px]" style={{ color: 'var(--ide-text-dim)' }}>
                    No devices loaded yet.
                  </div>
                )}
              </div>
            </div>

            <div className="overflow-hidden rounded-xl border shadow-sm" style={{ background: '#1e1e1e', borderColor: 'var(--ide-border)' }}>
              {props.topologyNodes.length > 0 ? (
                <div style={{ height: 420 }}>
                  <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    fitView
                    onNodeClick={(_, node) => {
                      const dev = props.devices.find((d) => d.id === node.id);
                      if (dev && !isConsoleBlocked(dev.node_state)) props.onOpenDevice(node.id);
                    }}
                  >
                    <Background color="#3c3c3c" gap={18} />
                    <Controls />
                  </ReactFlow>
                </div>
              ) : (
                <div className="flex h-[420px] flex-col items-center justify-center gap-2" style={{ color: 'var(--ide-text-dim)' }}>
                  <TopologyIcon size={36} className="opacity-30" />
                  <span className="text-xs font-medium">Open a lab to view its topology</span>
                </div>
              )}
            </div>
          </div>
        )}

        {lifecycleTarget && (
          <LifecycleModal
            token={props.token}
            lab={lifecycleTarget.lab}
            mode={lifecycleTarget.mode}
            onClose={() => setLifecycleTarget(null)}
            onFinished={(labId) => void props.onLifecycleEnded(labId)}
          />
        )}
      </div>
    </div>
  );
}
