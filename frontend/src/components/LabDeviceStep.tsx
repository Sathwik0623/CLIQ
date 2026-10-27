import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { Device, Lab, TopologyLink, TopologyNode } from '../types/api';

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
  if (state === 'BOOTED' || state === 'STARTED') return '#22c55e';
  if (state === 'STOPPED') return '#94a3b8';
  if (state === 'STARTING') return '#38bdf8';
  return '#cbd5e1';
}

function iconFor(type?: string): string {
  switch ((type || '').toLowerCase()) {
    case 'router':
      return '⬚↔';
    case 'switch':
      return '▭≡';
    case 'firewall':
      return '⛨';
    case 'server':
      return '▤';
    default:
      return '◻';
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
  const nodes: Node[] = props.topologyNodes.map((node) => ({
    id: node.id,
    position: node.position,
    data: { label: node.data.label },
    style: {
      border: `1px solid ${nodeColor(node.data.node_state)}`,
      borderRadius: 14,
      background: '#ffffff',
      color: '#0f172a',
      overflow: 'hidden',
      whiteSpace: 'nowrap',
      textOverflow: 'ellipsis',
      maxWidth: '140px',
      boxShadow: node.data.node_state === 'STARTING'
        ? '0 0 16px rgba(56,189,248,0.35)'
        : '0 1px 4px rgba(15,23,42,0.08)',
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
      style: { stroke: '#94a3b8', strokeWidth: 2 },
      ...(label ? {
        label,
        labelStyle: { fill: '#0369a1', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#ffffff', fillOpacity: 0.9 },
      } : {}),
    };
  });

  return (
    <div className="min-h-screen bg-white">
    <div className="mx-auto max-w-7xl px-5 py-5 text-slate-900">
      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <label className="text-sm font-semibold text-slate-700">Lab</label>
          <select
            className="mt-2 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-slate-900 outline-none focus:border-sky-400 focus:ring-1 focus:ring-sky-400"
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
            className="mt-3 w-full rounded-xl bg-sky-500 px-4 py-2 font-semibold text-white disabled:opacity-60 hover:bg-sky-600"
            disabled={!props.selectedLabId || props.loading}
            onClick={() => void props.onFetchDevices()}
          >
            {props.loading ? 'Loading...' : 'Load Devices'}
          </button>

          {props.error && <p className="mt-3 text-sm text-red-600 font-semibold">{props.error}</p>}

          {props.devices.length > 0 && (
            <div className="mt-6 rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="font-semibold text-slate-900">Click a device to open CLI</div>
              <div className="mt-2 space-y-1 max-h-96 overflow-y-auto">
                {props.devices.map((device) => (
                  <button
                    key={device.id}
                  className="block w-full rounded-lg px-2 py-2 text-left text-slate-700 hover:bg-slate-100"
                  onClick={() => props.onOpenDevice(device.id)}
                >
                  <div className="font-medium text-slate-900">{device.label}</div>
                  <div className="text-xs text-slate-500">{device.type ?? 'unknown'} · {device.node_state ?? 'unknown'}</div>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          {props.devices.length > 0 ? (
              <div style={{ height: 480 }}>
                <ReactFlow
                  nodes={nodes}
                  edges={edges}
                  fitView
                  onNodeClick={(_, node) => props.onOpenDevice(node.id)}
                >
                  <Background color="#e2e8f0" gap={18} />
                  <Controls />
                </ReactFlow>
              </div>
          ) : (
              <div className="flex h-[480px] items-center justify-center text-slate-400 font-semibold">
              Load a lab to see topology
            </div>
          )}
        </div>
      </div>
      </div>
    </div>
  );
}