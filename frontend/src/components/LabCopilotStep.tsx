import { useEffect, useMemo, useRef, useState } from 'react';
import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { TopologyLink, TopologyNode } from '../types/api';
import type {
  ApprovalTicket,
  DeployJob,
  DeployTopology,
  LabBlueprint,
  PreviewResponse,
  ValidationResult,
} from '../types/labs';
import {
  getDeployStatus,
  getDeployTopology,
  postDeployConfirm,
  postDeployPrepare,
  postLabPlan,
  postLabPreview,
  postLabValidate,
} from '../lib/labsApi';
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronLeftIcon,
  CliqLogoIcon,
  CloseIcon,
  TopologyIcon,
} from './Icons';

type LabCopilotStepProps = {
  token: string;
  host: string;
  onBack: () => void;
  onOpenLab: (labId: string, title: string) => Promise<void>;
  onBackToLabs: (labId: string, title: string) => Promise<void>;
};

type Phase = 'prompt' | 'planned' | 'deploying' | 'done';

function flowNodes(nodes: TopologyNode[]): Node[] {
  return nodes.map((node) => ({
    id: node.id,
    position: node.position,
    data: { label: node.data.label },
    style: {
      border: '1px solid #4ec9b0',
      borderRadius: 10,
      background: '#252526',
      color: '#cccccc',
      fontSize: 12,
      fontWeight: 600,
      maxWidth: '140px',
      overflow: 'hidden',
      whiteSpace: 'nowrap',
      textOverflow: 'ellipsis',
    },
  }));
}

function flowEdges(links: TopologyLink[]): Edge[] {
  return links.map((link) => {
    const label = link.label || [link.sourceHandle, link.targetHandle].filter(Boolean).join(' ↔ ');
    return {
      id: link.id,
      source: link.source,
      target: link.target,
      type: 'straight',
      style: { stroke: '#5a5a5a', strokeWidth: 2 },
      ...(label
        ? {
            label,
            labelStyle: { fill: '#75beff', fontSize: 10, fontWeight: 600 },
            labelBgStyle: { fill: '#252526', fillOpacity: 0.9 },
          }
        : {}),
    };
  });
}

function stepDot(status: string): string {
  if (status === 'succeeded') return '#4ec9b0';
  if (status === 'failed') return '#f87171';
  if (status === 'running') return '#75beff';
  return '#5a5a5a';
}

export default function LabCopilotStep(props: LabCopilotStepProps) {
  const [phase, setPhase] = useState<Phase>('prompt');
  const [prompt, setPrompt] = useState('');
  const [liveCheck, setLiveCheck] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const [blueprint, setBlueprint] = useState<LabBlueprint | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [validation, setValidation] = useState<(ValidationResult & { live: { live: boolean } }) | null>(null);
  const [planWarnings, setPlanWarnings] = useState<string[]>([]);

  const [ticket, setTicket] = useState<ApprovalTicket | null>(null);
  const [showApproval, setShowApproval] = useState(false);
  const [labTitle, setLabTitle] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState('');

  const [job, setJob] = useState<DeployJob | null>(null);
  const [finalTopo, setFinalTopo] = useState<DeployTopology | null>(null);
  const pollRef = useRef<number | null>(null);

  const previewNodes = useMemo(() => flowNodes(preview?.nodes ?? []), [preview]);
  const previewEdges = useMemo(() => flowEdges(preview?.links ?? []), [preview]);
  const finalNodes = useMemo(() => flowNodes(finalTopo?.nodes ?? []), [finalTopo]);
  const finalEdges = useMemo(() => flowEdges(finalTopo?.links ?? []), [finalTopo]);

  useEffect(() => {
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  async function handlePlan() {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      const plan = await postLabPlan(props.token, prompt.trim(), liveCheck);
      const [prev, verdict] = await Promise.all([
        postLabPreview(props.token, plan.blueprint),
        postLabValidate(props.token, plan.blueprint, liveCheck),
      ]);
      setBlueprint(plan.blueprint);
      setPlanWarnings(plan.warnings ?? []);
      setPreview(prev);
      setValidation(verdict);
      setPhase('planned');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Planning failed');
    } finally {
      setBusy(false);
    }
  }

  async function handlePrepare() {
    if (!blueprint || busy) return;
    setBusy(true);
    setError('');
    try {
      const t = await postDeployPrepare(props.token, blueprint, liveCheck);
      setTicket(t);
      setLabTitle(t.lab_title);
      setAcknowledged(false);
      setIdempotencyKey(crypto.randomUUID());
      setShowApproval(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Approval preparation failed');
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!ticket || !acknowledged || busy) return;
    setBusy(true);
    setError('');
    try {
      const launched = await postDeployConfirm(
        props.token,
        ticket.ticket_id,
        idempotencyKey,
        labTitle.trim() || undefined,
      );
      setShowApproval(false);
      setJob(launched);
      setPhase('deploying');
      startPolling(launched.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Deployment confirmation failed');
    } finally {
      setBusy(false);
    }
  }

  function startPolling(jobId: string) {
    if (pollRef.current) window.clearInterval(pollRef.current);
    let inFlight = false;
    pollRef.current = window.setInterval(async () => {
      // In-flight guard: a slow backend must delay ticks, never stack them.
      if (inFlight) return;
      inFlight = true;
      try {
        const status = await getDeployStatus(props.token, jobId);
        setJob(status);
        if (status.status === 'succeeded' || status.status === 'failed') {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          if (status.status === 'succeeded') {
            try {
              const topo = await getDeployTopology(props.token, jobId);
              setFinalTopo(topo);
            } catch (e) {
              setError(e instanceof Error ? e.message : 'Topology fetch failed');
            }
          }
          setPhase('done');
        }
      } catch (e) {
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = null;
        setError(e instanceof Error ? e.message : 'Status polling failed');
        setPhase('done');
      } finally {
        inFlight = false;
      }
    }, 1500);
  }

  function resetAll() {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = null;
    setPhase('prompt');
    setBlueprint(null);
    setPreview(null);
    setValidation(null);
    setPlanWarnings([]);
    setTicket(null);
    setShowApproval(false);
    setJob(null);
    setFinalTopo(null);
    setError('');
  }

  const verdict = ticket?.validation ?? validation;
  const defCounts = preview?.summary.node_definitions ?? {};

  return (
    <div className="min-h-screen" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>
      <div className="mx-auto max-w-7xl px-5 py-6">
        <div className="mb-5 flex items-center justify-between border-b pb-3" style={{ borderColor: 'var(--ide-border)' }}>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={props.onBack}
              className="flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)]"
              style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
            >
              <ChevronLeftIcon size={12} /> Labs
            </button>
            <span className="flex items-center gap-1.5 text-xl font-bold text-sky-400">
              <CliqLogoIcon size={20} /> CLIQ
            </span>
            <span style={{ color: 'var(--ide-border)' }}>/</span>
            <span className="text-sm font-semibold text-slate-300">Lab Copilot</span>
          </div>
          <span className="text-xs" style={{ color: 'var(--ide-text-muted)' }}>
            Plan, review and deploy real CML labs
          </span>
        </div>

        {error && (
          <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs font-semibold text-rose-300" style={{ background: '#2a1215', borderColor: '#7f1d1d' }}>
            <AlertTriangleIcon size={14} className="shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {phase === 'prompt' && (
          <div className="rounded-xl border p-5 shadow-sm" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
            <label className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--ide-text-muted)' }}>
              Describe the lab to build
            </label>
            <textarea
              className="mt-2 w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-500"
              style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              rows={4}
              placeholder="Build a 3-router OSPF backbone with 2 access switches and redundant links"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            <div className="mt-3 flex items-center justify-between">
              <label className="flex items-center gap-2 text-xs" style={{ color: 'var(--ide-text-muted)' }}>
                <input
                  type="checkbox"
                  checked={liveCheck}
                  onChange={(e) => setLiveCheck(e.target.checked)}
                  className="h-3.5 w-3.5 accent-sky-500"
                />
                Validate against live controller data ({props.host})
              </label>
              <button
                type="button"
                disabled={!prompt.trim() || busy}
                onClick={() => void handlePlan()}
                className="rounded-lg px-4 py-2 text-xs font-semibold text-white disabled:opacity-60"
                style={{ background: 'var(--ide-accent)' }}
              >
                {busy ? 'Planning…' : 'Generate plan'}
              </button>
            </div>
          </div>
        )}

        {(phase === 'planned' || phase === 'deploying' || phase === 'done') && blueprint && preview && (
          <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
            <div className="rounded-xl border p-4 shadow-sm" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
              <div className="text-sm font-bold text-slate-200">{blueprint.title}</div>
              <div className="mt-1 text-[11px]" style={{ color: 'var(--ide-text-muted)' }}>
                {preview.summary.total_nodes} nodes · {preview.summary.total_links} links ·{' '}
                {preview.summary.total_vcpus} vCPU · {(preview.summary.total_ram_mb / 1024).toFixed(1)} GB RAM
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {Object.entries(defCounts).map(([def, count]) => (
                  <span key={def} className="rounded px-2 py-0.5 text-[11px] font-semibold" style={{ background: 'var(--ide-bg)', color: '#75beff' }}>
                    {def} × {count}
                  </span>
                ))}
              </div>
              {planWarnings.length > 0 && (
                <div className="mt-3 text-[11px]" style={{ color: '#fbbf24' }}>
                  {planWarnings.map((w) => (
                    <div key={w} className="mt-1">⚠ {w}</div>
                  ))}
                </div>
              )}
              {verdict && (
                <div className="mt-3 rounded-lg border p-2.5" style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-bg)' }}>
                  <div className="flex items-center gap-1.5 text-xs font-bold">
                    {verdict.valid
                      ? <CheckIcon size={13} className="text-emerald-400" />
                      : <AlertTriangleIcon size={13} className="text-rose-400" />}
                    <span className={verdict.valid ? 'text-emerald-300' : 'text-rose-300'}>
                      {verdict.valid ? 'Blueprint valid' : 'Blueprint invalid'}
                    </span>
                  </div>
                  {verdict.errors.map((e) => (
                    <div key={e.code + e.message} className="mt-1 text-[11px] text-rose-300">✕ {e.message}</div>
                  ))}
                  {verdict.warnings.map((w) => (
                    <div key={w.code + w.message} className="mt-1 text-[11px]" style={{ color: '#fbbf24' }}>⚠ {w.message}</div>
                  ))}
                </div>
              )}
              {phase === 'planned' && (
                <div className="mt-3 space-y-2">
                  <button
                    type="button"
                    disabled={busy || !(verdict?.valid)}
                    onClick={() => void handlePrepare()}
                    className="w-full rounded-lg px-4 py-2 text-xs font-semibold text-white disabled:opacity-60"
                    style={{ background: 'var(--ide-accent)' }}
                  >
                    {busy ? 'Preparing…' : 'Review & Deploy'}
                  </button>
                  <button
                    type="button"
                    onClick={resetAll}
                    className="w-full rounded-lg border px-4 py-2 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                    style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                  >
                    Start over
                  </button>
                </div>
              )}
              {(phase === 'deploying' || phase === 'done') && job && (
                <div className="mt-3">
                  <div className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--ide-text-muted)' }}>
                    Deployment {job.status}
                  </div>
                  <div className="mt-2 max-h-64 space-y-1 overflow-y-auto">
                    {job.steps.map((s) => (
                      <div key={s.seq} className="flex items-center gap-2 text-[11px]">
                        <span
                          aria-hidden
                          className="inline-block h-2 w-2 shrink-0 rounded-full"
                          style={{ background: stepDot(s.status) }}
                        />
                        <span className="truncate text-slate-300">{s.label}</span>
                      </div>
                    ))}
                  </div>
                  {job.error && <div className="mt-2 text-[11px] text-rose-300">{job.error}</div>}
                </div>
              )}
            </div>

            <div className="overflow-hidden rounded-xl border shadow-sm" style={{ background: '#1e1e1e', borderColor: 'var(--ide-border)' }}>
              {phase === 'done' && job?.status === 'succeeded' && finalTopo ? (
                <div>
                  <div className="flex items-center justify-between border-b px-4 py-2" style={{ borderColor: 'var(--ide-border)' }}>
                    <span className="flex items-center gap-2 text-xs font-semibold text-emerald-300">
                      <CheckIcon size={13} /> Created in CML: {finalTopo.lab_title} ({finalTopo.lab_state ?? 'unknown state'})
                    </span>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => void props.onBackToLabs(finalTopo.lab_id, finalTopo.lab_title)}
                        className="rounded-lg border px-3 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                        style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                      >
                        Back to labs
                      </button>
                      <button
                        type="button"
                        onClick={() => void props.onOpenLab(finalTopo.lab_id, finalTopo.lab_title)}
                        className="rounded-lg px-3 py-1.5 text-xs font-semibold text-white"
                        style={{ background: 'var(--ide-accent)' }}
                      >
                        Open lab
                      </button>
                    </div>
                  </div>
                  <div style={{ height: 480 }}>
                    <ReactFlow nodes={finalNodes} edges={finalEdges} fitView>
                      <Background color="#3c3c3c" gap={18} />
                      <Controls />
                    </ReactFlow>
                  </div>
                </div>
              ) : phase === 'done' && job?.status === 'failed' ? (
                <div className="flex h-[520px] flex-col items-center justify-center gap-2 px-6 text-center" style={{ color: 'var(--ide-text-dim)' }}>
                  <AlertTriangleIcon size={32} className="text-rose-400" />
                  <span className="text-sm font-semibold text-rose-300">Deployment failed — no silent partial state</span>
                  <span className="max-w-md text-xs">{job.error || 'The job stopped at the first failed step.'}</span>
                  <button
                    type="button"
                    onClick={resetAll}
                    className="mt-2 rounded-lg border px-4 py-2 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                    style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                  >
                    Plan a new lab
                  </button>
                </div>
              ) : (
                <div style={{ height: 520 }}>
                  <ReactFlow nodes={previewNodes} edges={previewEdges} fitView>
                    <Background color="#3c3c3c" gap={18} />
                    <Controls />
                  </ReactFlow>
                </div>
              )}
            </div>
          </div>
        )}

        {showApproval && ticket && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-label="Deployment approval">
            <div className="w-full max-w-lg rounded-xl border p-5 shadow-xl" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm font-bold text-slate-200">
                  <TopologyIcon size={16} className="text-sky-400" /> Approve real CML deployment
                </span>
                <button type="button" onClick={() => setShowApproval(false)} aria-label="Cancel approval" className="rounded p-1 hover:bg-[var(--ide-hover)]">
                  <CloseIcon size={14} />
                </button>
              </div>
              <div className="mt-3 space-y-2 text-xs text-slate-300">
                <label className="block">
                  <span className="font-semibold" style={{ color: 'var(--ide-text-muted)' }}>Lab name (created on {props.host})</span>
                  <input
                    type="text"
                    value={labTitle}
                    onChange={(e) => setLabTitle(e.target.value)}
                    className="mt-1 w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-500"
                    style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                  />
                </label>
                {ticket.lab_title_original != null && ticket.lab_title_original !== '' && (
                  <div className="text-[11px] leading-snug" style={{ color: 'var(--ide-text-muted)' }}>
                    Adjusted for the controller from blueprint title “{ticket.lab_title_original}”.
                  </div>
                )}
                <div>
                  <span className="font-semibold" style={{ color: 'var(--ide-text-muted)' }}>Topology: </span>
                  {ticket.blueprint.nodes.length} nodes, {ticket.blueprint.links.length} links ·{' '}
                  {ticket.validation.resource_estimate.total_vcpus} vCPU ·{' '}
                  {(ticket.validation.resource_estimate.total_ram_mb / 1024).toFixed(1)} GB RAM
                </div>
                <div className="max-h-32 overflow-y-auto rounded-lg border p-2" style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-bg)' }}>
                  {ticket.blueprint.nodes.map((n) => (
                    <div key={n.id} className="text-[11px]">{n.label} — {n.node_definition}{n.image_definition ? ` (${n.image_definition})` : ''}</div>
                  ))}
                </div>
                {ticket.validation.warnings.length > 0 && (
                  <div className="text-[11px]" style={{ color: '#fbbf24' }}>
                    {ticket.validation.warnings.map((w) => (
                      <div key={w.code + w.message}>⚠ {w.message}</div>
                    ))}
                  </div>
                )}
                <div className="text-[11px]" style={{ color: 'var(--ide-text-muted)' }}>
                  Validated against {ticket.live ? 'live controller data' : 'offline checks'} · ticket expires in 15 minutes ·
                  devices will NOT be configured or started automatically.
                </div>
                <label className="flex items-start gap-2 rounded-lg border p-2.5" style={{ borderColor: '#b45309', background: '#291400' }}>
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    onChange={(e) => setAcknowledged(e.target.checked)}
                    className="mt-0.5 h-3.5 w-3.5 accent-amber-500"
                  />
                  <span className="font-semibold text-amber-200">
                    I understand this creates a real lab on the CML controller. Nothing is deployed until I press Confirm.
                  </span>
                </label>
              </div>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowApproval(false)}
                  className="rounded-lg border px-4 py-2 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                  style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={!acknowledged || !labTitle.trim() || busy}
                  onClick={() => void handleConfirm()}
                  className="rounded-lg px-4 py-2 text-xs font-semibold text-white disabled:opacity-60"
                  style={{ background: acknowledged ? '#b45309' : 'var(--ide-accent)' }}
                >
                  {busy ? 'Confirming…' : 'Confirm & Deploy'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
