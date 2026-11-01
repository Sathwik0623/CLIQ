import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Device, Lab, TopologyLink, TopologyNode } from '../types/api';
import { CliqLogoIcon, TopologyIcon } from './Icons';

type LabDeviceStepProps = {
  labs: Lab[];
  selectedLabId: string;
  devices: Device[];
  topologyNodes: TopologyNode[];
  topologyLinks: TopologyLink[];
  loading: boolean;
  error: string;
  onSelectLab: (labId: string) => void;
  onFetchDevices: () => Promise<void>;
  onOpenDevice: (deviceId: string) => void;
};

function nodeColor(state?: string): string {
  if (state === 'BOOTED' || state === 'STARTED') return '#4ec9b0';
  if (state === 'STOPPED') return '#858585';
  if (state === 'STARTING') return '#cca700';
  return '#5a5a5a';
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
  const nodes: Node[] = props.topologyNodes.map((node) => ({
    id: node.id,
    position: node.position,
    data: { label: node.data.label },
    style: {
      border: `1px solid ${nodeColor(node.data.node_state)}`,
      borderRadius: 10,
      background: '#252526',
      color: '#cccccc',
      overflow: 'hidden',
      whiteSpace: 'nowrap',
      textOverflow: 'ellipsis',
      maxWidth: '140px',
      fontSize: 12,
      fontWeight: 600,
      boxShadow: node.data.node_state === 'STARTING'
        ? '0 0 16px rgba(56,189,248,0.35)'
        : '0 1px 4px rgba(0,0,0,0.4)',
      animation: node.data.node_state === 'STARTING' ? 'pulseNode 1.2s ease-in-out infinite' : undefined,
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
            <span className="text-sm font-semibold text-slate-300">Lab & Device Selection</span>
          </div>
          <span className="text-xs" style={{ color: 'var(--ide-text-muted)' }}>
            Select a CML lab simulation and choose devices to open
          </span>
        </div>

        <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <div className="rounded-xl border p-4 shadow-sm" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
            <label className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--ide-text-muted)' }}>
              Lab Simulation
            </label>
            <select
              className="mt-2 w-full rounded-lg border px-3 py-2 text-xs outline-none focus:border-sky-500"
              style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              value={props.selectedLabId}
              onChange={(event) => props.onSelectLab(event.target.value)}
            >
              <option value="">Select a lab</option>
              {props.labs.map((lab) => (
                <option key={lab.id} value={lab.id}>
                  {lab.title}
                </option>
              ))}
            </select>

            <button
              className="mt-3 w-full rounded-lg px-4 py-2 text-xs font-semibold text-white disabled:opacity-60 transition-colors cursor-pointer"
              style={{ background: 'var(--ide-accent)' }}
              disabled={!props.selectedLabId || props.loading}
              onClick={() => void props.onFetchDevices()}
            >
              {props.loading ? 'Loading Devices...' : 'Load Lab Devices'}
            </button>

            {props.error && <p className="mt-3 text-xs text-rose-400 font-semibold">{props.error}</p>}

            {props.devices.length > 0 && (
              <div className="mt-5 rounded-lg border p-3" style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}>
                <div className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--ide-text-muted)' }}>
                  Click a device to open terminal
                </div>
                <div className="mt-2 space-y-1 max-h-96 overflow-y-auto">
                  {props.devices.map((device) => (
                    <button
                      key={device.id}
                      className="block w-full rounded px-2.5 py-2 text-left transition-colors hover:bg-[var(--ide-hover)] cursor-pointer"
                      style={{ border: '1px solid transparent' }}
                      onClick={() => props.onOpenDevice(device.id)}
                    >
                      <div className="font-medium text-xs text-slate-200">{device.label}</div>
                      <div className="text-[10px]" style={{ color: 'var(--ide-text-muted)' }}>
                        {device.type ?? 'device'} · {device.node_state ?? 'unknown'}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="overflow-hidden rounded-xl border shadow-sm" style={{ background: '#1e1e1e', borderColor: 'var(--ide-border)' }}>
            {props.devices.length > 0 ? (
              <div style={{ height: 520 }}>
                <ReactFlow
                  nodes={nodes}
                  edges={edges}
                  fitView
                  onNodeClick={(_, node) => props.onOpenDevice(node.id)}
                >
                  <Background color="#3c3c3c" gap={18} />
                  <Controls />
                </ReactFlow>
              </div>
            ) : (
              <div className="flex h-[520px] flex-col items-center justify-center gap-2" style={{ color: 'var(--ide-text-dim)' }}>
                <TopologyIcon size={36} className="opacity-30" />
                <span className="text-xs font-medium">Load a lab to view interactive network topology</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
