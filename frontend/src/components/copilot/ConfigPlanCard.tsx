/**
 * ConfigPlanCard — Multi-device configuration workflow (M1.4).
 *
 * Renders a configuration plan end to end: open questions with answers,
 * per-device commands, prepare → approve → execute controls, live execution
 * progress with actual output, and the baseline health summary.
 */

import React, { useEffect, useRef, useState } from 'react';
import type {
  BaselineRecordView,
  ConfigExecEvent,
  ConfigPlanView,
  PlanExecutionView,
  StepResultView,
} from '../../lib/copilotApi';
import {
  answerPlanQuestions,
  approveConfigPlan,
  collectPlanBaseline,
  executePlanStream,
  prepareConfigPlan,
} from '../../lib/copilotApi';

interface ConfigPlanCardProps {
  plan: ConfigPlanView;
  token?: string;
  onPlanUpdated: (plan: ConfigPlanView) => void;
}

function statusBadge(status: string): string {
  switch (status) {
    case 'completed':
      return 'bg-emerald-950/60 border-emerald-800/60 text-emerald-300';
    case 'failed':
      return 'bg-red-950/60 border-red-800/60 text-red-300';
    case 'executing':
      return 'bg-cyan-950/60 border-cyan-700/60 text-cyan-300';
    case 'approved':
    case 'pending_approval':
      return 'bg-amber-950/60 border-amber-700/60 text-amber-300';
    default:
      return 'bg-neutral-900 border-neutral-700 text-neutral-300';
  }
}

function verdictBadge(verdict: string): string {
  if (verdict === 'HEALTHY') return 'bg-emerald-950/60 border-emerald-800/60 text-emerald-300';
  if (verdict === 'DEGRADED') return 'bg-amber-950/60 border-amber-700/60 text-amber-300';
  return 'bg-red-950/60 border-red-800/60 text-red-300';
}

function stepDot(status: string): string {
  if (status === 'success') return 'bg-emerald-400';
  if (status === 'failed') return 'bg-red-400';
  if (status === 'skipped') return 'bg-neutral-500';
  return 'bg-cyan-400 animate-pulse';
}

export default function ConfigPlanCard({ plan, token, onPlanUpdated }: ConfigPlanCardProps) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<{ ticket_id: string; version: number; fingerprint: string; expires_at: number } | null>(null);
  const [approvalToken, setApprovalToken] = useState<string | null>(null);
  const [running, setRunning] = useState<Record<string, StepResultView | { status: 'running' }>>({});
  const [execution, setExecution] = useState<PlanExecutionView | null>(null);
  const [baseline, setBaseline] = useState<BaselineRecordView | null>(null);
  const [openDevices, setOpenDevices] = useState<Record<string, boolean>>({});
  const [openOutputs, setOpenOutputs] = useState<Record<string, boolean>>({});
  // Per-card collapse: finished/reviewed plans start collapsed; drafts open.
  const [collapsed, setCollapsed] = useState(
    ['completed', 'failed', 'approved', 'pending_approval'].includes(plan.status),
  );
  const planRevRef = useRef(`${plan.plan_id}:v${plan.version}`);

  // Any plan change (new version from answers) resets the approval flow:
  // outstanding tickets/tokens are bound to the old version server-side.
  useEffect(() => {
    setTicket(null);
    setApprovalToken(null);
    setRunning({});
    setExecution(null);
    setBaseline(null);
    setError(null);
    setAnswers({});
    const rev = `${plan.plan_id}:v${plan.version}`;
    if (planRevRef.current !== rev) {
      planRevRef.current = rev;
      setCollapsed(false);
    }
  }, [plan.plan_id, plan.version]);

  // Collapse once execution completes; the header keeps status + verdict.
  useEffect(() => {
    if (execution) setCollapsed(true);
  }, [execution]);

  const blockingOpen = plan.questions.filter((q) => q.blocking && !q.resolved);
  const totalSteps = plan.devices.reduce((n, d) => n + d.steps.length, 0);

  const handleAnswers = async () => {
    const filled: Record<string, string> = {};
    for (const [k, v] of Object.entries(answers)) {
      if (v.trim()) filled[k] = v.trim();
    }
    if (Object.keys(filled).length === 0) return;
    setBusy('answers');
    setError(null);
    try {
      const updated = await answerPlanQuestions(plan.plan_id, filled, token);
      onPlanUpdated(updated);
    } catch (e: any) {
      setError(e?.message || 'Failed to submit answers.');
    } finally {
      setBusy(null);
    }
  };

  const handlePrepare = async () => {
    setBusy('prepare');
    setError(null);
    try {
      const res = await prepareConfigPlan(plan.plan_id, token);
      setTicket(res.ticket);
    } catch (e: any) {
      setError(e?.message || 'Prepare failed.');
    } finally {
      setBusy(null);
    }
  };

  const handleApprove = async () => {
    if (!ticket) return;
    setBusy('approve');
    setError(null);
    try {
      const res = await approveConfigPlan(ticket.ticket_id, token);
      setApprovalToken(res.approval_token);
    } catch (e: any) {
      setError(e?.message || 'Approve failed.');
      setTicket(null);
    } finally {
      setBusy(null);
    }
  };

  const handleBaseline = async () => {
    setBusy('baseline');
    setError(null);
    try {
      const rec = await collectPlanBaseline(plan.plan_id, token);
      setBaseline(rec);
    } catch (e: any) {
      setError(e?.message || 'Baseline collection failed.');
    } finally {
      setBusy(null);
    }
  };

  const handleExecute = async () => {
    if (!approvalToken) return;
    setBusy('execute');
    setError(null);
    setRunning({});
    setExecution(null);
    const onEvent = (event: ConfigExecEvent) => {
      if (event.type === 'step_started') {
        setRunning((prev) => ({ ...prev, [event.step_id]: { status: 'running' } as StepResultView }));
      } else if (event.type === 'step_result') {
        setRunning((prev) => ({ ...prev, [event.step.step_id]: event.step }));
      } else if (event.type === 'plan_finished') {
        setExecution(event.execution);
        const finalSteps: Record<string, StepResultView> = {};
        for (const s of event.execution.steps) finalSteps[s.step_id] = s;
        setRunning(finalSteps);
        setBusy(null);
        // Baseline is read-only: collect it immediately after execution.
        collectPlanBaseline(plan.plan_id, token).then(setBaseline).catch(() => undefined);
      } else if (event.type === 'plan_rejected' || event.type === 'error') {
        setError(event.message);
        setBusy(null);
      }
    };
    try {
      await executePlanStream(plan.plan_id, approvalToken, onEvent, token);
    } catch (e: any) {
      setError(e?.message || 'Execution failed.');
      setBusy(null);
    }
  };

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/60 px-2.5 py-1.5 space-y-2">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        aria-expanded={!collapsed}
        className="w-full flex items-center gap-2 min-w-0 flex-wrap text-left"
      >
        <span className={`text-[9.5px] font-mono px-1.5 py-0.5 rounded-full border shrink-0 ${statusBadge(execution?.status || plan.status)}`}>
          {execution?.status || plan.status}
        </span>
        <span className="text-[11px] font-semibold text-neutral-100 truncate flex-1" title={plan.title}>
          {plan.title}
        </span>
        {baseline && (
          <span className={`text-[9.5px] font-mono px-1.5 py-0.5 rounded-full border shrink-0 ${verdictBadge(baseline.verdict)}`}>
            {baseline.verdict}
          </span>
        )}
        <span className="text-[9.5px] font-mono text-neutral-500 shrink-0">v{plan.version}</span>
        <span className="text-[9.5px] font-mono text-neutral-600 shrink-0" title={`Plan ${plan.plan_id} v${plan.version}`}>
          {plan.plan_id}
        </span>
        <span className="text-[10px] text-neutral-500 shrink-0">{collapsed ? '▸' : '▾'}</span>
      </button>

      {!collapsed && (
        <>
      <div className="text-[10.5px] text-neutral-400 leading-snug">{plan.summary}</div>

      {blockingOpen.length > 0 && (
        <div className="space-y-1.5 rounded bg-neutral-950/60 border border-neutral-800 p-2">
          <div className="text-[9.5px] font-semibold uppercase tracking-wider text-neutral-500">
            Questions — answers needed before review
          </div>
          {blockingOpen.map((q) => (
            <div key={q.qid} className="space-y-1">
              <div className="text-[10.5px] text-neutral-300 leading-snug">{q.message}</div>
              {q.options.length > 0 ? (
                <select
                  value={answers[q.qid] || ''}
                  onChange={(e) => setAnswers((prev) => ({ ...prev, [q.qid]: e.target.value }))}
                  className="w-full bg-neutral-900 border border-neutral-700 text-neutral-200 text-[11px] rounded px-1.5 py-1 focus:outline-none focus:border-cyan-500"
                >
                  <option value="">Select…</option>
                  {q.options.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={answers[q.qid] || ''}
                  onChange={(e) => setAnswers((prev) => ({ ...prev, [q.qid]: e.target.value }))}
                  placeholder="Type the answer…"
                  className="w-full bg-neutral-900 border border-neutral-700 text-neutral-200 text-[11px] rounded px-1.5 py-1 focus:outline-none focus:border-cyan-500 placeholder-neutral-600"
                />
              )}
            </div>
          ))}
          <button
            type="button"
            disabled={busy !== null}
            onClick={handleAnswers}
            className="px-2.5 py-1 rounded text-[11px] font-medium bg-cyan-500/15 border border-cyan-600/60 text-cyan-200 hover:bg-cyan-500/25 transition disabled:opacity-50"
          >
            {busy === 'answers' ? 'Submitting…' : 'Submit answers'}
          </button>
        </div>
      )}

      {plan.assumptions.length > 0 && (
        <div className="text-[10px] text-neutral-500 leading-snug">
          <span className="font-semibold">Assumptions to confirm: </span>
          {plan.assumptions.join(' ')}
        </div>
      )}

      <div className="space-y-1.5">
        {plan.devices.map((d) => (
          <div key={d.device_id} className="rounded border border-neutral-800 bg-neutral-950/40">
            <button
              type="button"
              onClick={() => setOpenDevices((prev) => ({ ...prev, [d.device_id]: !prev[d.device_id] }))}
              className="w-full flex items-center gap-2 px-2 py-1.5 text-left"
            >
              <span className="text-[10.5px] font-semibold text-neutral-200">{d.device_label}</span>
              <span className="text-[9.5px] font-mono text-neutral-500">{d.role}/{d.node_type}</span>
              {d.unverified && (
                <span className="text-[9.5px] font-mono text-amber-300">unverified</span>
              )}
              <span className="ml-auto text-[9.5px] font-mono text-neutral-500">
                {d.steps.length} cmds {openDevices[d.device_id] ? '▾' : '▸'}
              </span>
            </button>
            {openDevices[d.device_id] && (
              <div className="px-2 pb-2 space-y-1">
                {d.steps.map((s) => (
                  <div key={s.step_id} className="flex items-start gap-2">
                    <code className="text-[10px] font-mono text-cyan-200/90 break-all">{s.command}</code>
                    <span className="text-[9.5px] text-neutral-500 ml-auto shrink-0 text-right">{s.purpose}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {plan.connectivity.length > 0 && (
        <div className="text-[10px] font-mono text-neutral-400">
          <span className="font-semibold text-neutral-500">Expected connectivity: </span>
          {plan.connectivity.map((c) => `${c.src_device} → ${c.dst}${c.dst_ip ? ` (${c.dst_ip})` : ''}`).join('; ')}
        </div>
      )}

      {error && (
        <div className="text-[10.5px] text-red-300 leading-snug break-words">{error}</div>
      )}

      {blockingOpen.length === 0 && totalSteps > 0 && !execution && (
        <div className="flex items-center gap-2 flex-wrap">
          {!ticket && !approvalToken && (
            <button
              type="button"
              disabled={busy !== null}
              onClick={handlePrepare}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-cyan-500/15 border border-cyan-600/60 text-cyan-200 hover:bg-cyan-500/25 transition disabled:opacity-50"
            >
              {busy === 'prepare' ? 'Reviewing…' : 'Review & prepare'}
            </button>
          )}
          {ticket && !approvalToken && (
            <>
              <span className="text-[10px] font-mono text-neutral-400">
                ticket {ticket.ticket_id} · v{ticket.version} · {ticket.fingerprint.slice(0, 8)}
              </span>
              <button
                type="button"
                disabled={busy !== null}
                onClick={handleApprove}
                className="px-2.5 py-1 rounded text-[11px] font-medium bg-amber-500/15 border border-amber-600/60 text-amber-200 hover:bg-amber-500/25 transition disabled:opacity-50"
              >
                {busy === 'approve' ? 'Approving…' : 'Approve exact commands'}
              </button>
            </>
          )}
          {approvalToken && (
            <button
              type="button"
              disabled={busy !== null}
              onClick={handleExecute}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-emerald-500/15 border border-emerald-600/60 text-emerald-200 hover:bg-emerald-500/25 transition disabled:opacity-50"
            >
              {busy === 'execute' ? 'Executing…' : `Execute ${totalSteps} approved commands`}
            </button>
          )}
        </div>
      )}

      {Object.keys(running).length > 0 && (
        <div className="space-y-1">
          <div className="text-[9.5px] font-semibold uppercase tracking-wider text-neutral-500">
            Execution progress
          </div>
          {plan.devices.flatMap((d) => d.steps).map((s) => {
            const r = running[s.step_id] as StepResultView | undefined;
            if (!r) return null;
            const isRunning = (r as { status: string }).status === 'running' && !('command' in r);
            const res = r as StepResultView;
            return (
              <div key={s.step_id} className="rounded bg-neutral-950/60 border border-neutral-800 px-2 py-1">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${stepDot(isRunning ? 'running' : res.status)}`} />
                  <code className="text-[10px] font-mono text-neutral-200 truncate flex-1" title={s.command}>{s.command}</code>
                  <span className="text-[9.5px] font-mono text-neutral-500 shrink-0">
                    {isRunning ? 'running' : `${res.status}${res.latency_ms ? ` · ${Math.round(res.latency_ms)}ms` : ''}`}
                  </span>
                </div>
                {!isRunning && (res.stdout || res.error) && (
                  <div>
                    <button
                      type="button"
                      onClick={() => setOpenOutputs((prev) => ({ ...prev, [s.step_id]: !prev[s.step_id] }))}
                      className="text-[10px] font-mono text-neutral-400 hover:text-cyan-300 transition"
                    >
                      {openOutputs[s.step_id] ? 'Hide output' : 'Show output'}
                    </button>
                    {openOutputs[s.step_id] && (
                      <pre className="mt-1 max-h-40 overflow-auto rounded bg-neutral-950 border border-neutral-800 p-2 text-[10px] font-mono leading-relaxed text-neutral-300 whitespace-pre-wrap break-words select-text">
                        {res.stdout || res.error}
                      </pre>
                    )}
                  </div>
                )}
                {!isRunning && res.status !== 'success' && res.error && (
                  <div className="text-[10px] text-red-300/90 break-words">{res.error}</div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {execution && (
        <div className="text-[10.5px] leading-snug">
          <span className={`font-mono text-[9.5px] px-1.5 py-0.5 rounded-full border ${statusBadge(execution.status)}`}>
            {execution.status}
          </span>
          <span className="ml-2 text-neutral-300">{execution.summary}</span>
        </div>
      )}

      {(baseline || (execution && busy === null)) && (
        <div className="space-y-1.5 rounded bg-neutral-950/60 border border-neutral-800 p-2">
          <div className="flex items-center gap-2">
            <div className="text-[9.5px] font-semibold uppercase tracking-wider text-neutral-500 flex-1">
              Baseline health
            </div>
            {baseline && (
              <span className={`text-[9.5px] font-mono px-1.5 py-0.5 rounded-full border ${verdictBadge(baseline.verdict)}`}>
                {baseline.verdict}
              </span>
            )}
            <button
              type="button"
              disabled={busy !== null}
              onClick={handleBaseline}
              className="text-[10px] font-mono text-neutral-400 hover:text-cyan-300 transition disabled:opacity-50"
            >
              {busy === 'baseline' ? 'Collecting…' : 'Refresh'}
            </button>
          </div>
          {baseline ? (
            <>
              <div className="text-[10.5px] text-neutral-300 leading-snug">{baseline.explanation}</div>
              <div className="space-y-0.5 max-h-44 overflow-y-auto">
                {baseline.checks.map((c) => (
                  <div key={c.check_id} className="flex items-start gap-2 text-[10px]">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 mt-1 ${c.status === 'passed' ? 'bg-emerald-400' : 'bg-red-400'}`} />
                    <span className="text-neutral-300">{c.description}</span>
                    <span className="text-neutral-500 ml-auto text-right shrink-0 max-w-[55%] truncate" title={c.detail}>{c.detail}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="text-[10.5px] text-neutral-500">Collecting fresh evidence…</div>
          )}
        </div>
      )}
        </>
      )}
    </div>
  );
}
