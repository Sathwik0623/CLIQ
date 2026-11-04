/**
 * CopilotExecCard — Autonomous device execution card (M1.2).
 *
 * Renders one Copilot-initiated device command run: target device, command,
 * live status, actual device output (collapsible), and the explicit
 * Approve/Reject controls for configuration-changing commands.
 */

import React, { useEffect, useState } from 'react';
import type { ToolRun } from '../../lib/copilotApi';

/** Terminal states collapse by default; live/actionable states stay open. */
const COLLAPSED_STATES: Array<ToolRun['status']> = ['success', 'error', 'rejected', 'invalid'];

interface CopilotExecCardProps {
  run: ToolRun;
  approving: boolean;
  onApprove: (run: ToolRun) => void;
  onReject: (run: ToolRun) => void;
}

function statusBadge(status: ToolRun['status']): { text: string; cls: string } {
  switch (status) {
    case 'running':
      return { text: 'Running', cls: 'bg-cyan-950/60 border-cyan-700/60 text-cyan-300' };
    case 'approved_running':
      return { text: 'Running (approved)', cls: 'bg-cyan-950/60 border-cyan-700/60 text-cyan-300' };
    case 'success':
      return { text: 'Done', cls: 'bg-emerald-950/60 border-emerald-800/60 text-emerald-300' };
    case 'approval_required':
      return { text: 'Approval required', cls: 'bg-amber-950/60 border-amber-700/60 text-amber-300' };
    case 'rejected':
      return { text: 'Rejected', cls: 'bg-neutral-900 border-neutral-700 text-neutral-400' };
    case 'invalid':
      return { text: 'Not executed', cls: 'bg-neutral-900 border-neutral-700 text-neutral-400' };
    case 'error':
    default:
      return { text: 'Failed', cls: 'bg-red-950/60 border-red-800/60 text-red-300' };
  }
}

export default function CopilotExecCard({ run, approving, onApprove, onReject }: CopilotExecCardProps) {
  const [showOutput, setShowOutput] = useState(false);
  // Per-card open state: completed cards collapse, live ones stay open.
  const [open, setOpen] = useState(!COLLAPSED_STATES.includes(run.status));
  const badge = statusBadge(run.status);
  const result = run.result;
  const output = result?.stdout || '';
  const outputLines = output ? output.split('\n').length : 0;
  const isLive = run.status === 'running' || run.status === 'approved_running';

  useEffect(() => {
    setOpen(!COLLAPSED_STATES.includes(run.status));
  }, [run.status, run.key]);

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/60 px-2.5 py-1.5 space-y-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 min-w-0 text-left"
      >
        <span className={`text-[9.5px] font-mono px-1.5 py-0.5 rounded-full border shrink-0 ${badge.cls}${isLive ? ' animate-pulse' : ''}`}>
          {badge.text}
        </span>
        <span className="text-[10.5px] font-semibold text-neutral-200 truncate" title={run.device_label}>
          {run.device_label}
        </span>
        <code className="text-[10.5px] font-mono text-cyan-200/90 truncate flex-1" title={run.command}>
          {run.command}
        </code>
        {result && result.latency_ms > 0 && (
          <span className="text-[9.5px] font-mono text-neutral-500 shrink-0">
            {Math.round(result.latency_ms)} ms
          </span>
        )}
        <span className="text-[10px] text-neutral-500 shrink-0">{open ? '▾' : '▸'}</span>
      </button>

      {open && result && result.error && run.status !== 'approval_required' && (
        <div className="text-[10.5px] text-red-300/90 leading-snug break-words">
          {result.error}
          {result.error_code && (
            <span className="ml-1 font-mono text-[9.5px] text-neutral-500">({result.error_code})</span>
          )}
        </div>
      )}

      {open && run.status === 'approval_required' && (
        <div className="space-y-1.5">
          <div className="text-[10.5px] text-amber-200/90 leading-snug">
            This command may change configuration on {run.device_label}. Review it, then approve to run it on the live console.
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={approving}
              onClick={() => onApprove(run)}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-amber-500/15 border border-amber-600/60 text-amber-200 hover:bg-amber-500/25 transition disabled:opacity-50"
            >
              {approving ? 'Approving…' : 'Approve & run'}
            </button>
            <button
              type="button"
              disabled={approving}
              onClick={() => onReject(run)}
              className="px-2.5 py-1 rounded text-[11px] font-medium bg-neutral-800 border border-neutral-700 text-neutral-300 hover:bg-neutral-700 transition disabled:opacity-50"
            >
              Reject
            </button>
          </div>
        </div>
      )}

      {open && run.note && (
        <div className="text-[10.5px] text-neutral-400 leading-snug">{run.note}</div>
      )}

      {open && result?.send_command && result.send_command !== run.command && (
        <div className="text-[10px] font-mono text-neutral-500">
          Sent as <code className="text-neutral-300">{result.send_command}</code>
        </div>
      )}

      {open && output && (
        <div>
          <button
            type="button"
            onClick={() => setShowOutput((v) => !v)}
            className="text-[10px] font-mono text-neutral-400 hover:text-cyan-300 transition"
          >
            {showOutput ? 'Hide output' : `Show output (${outputLines} lines${result?.truncated ? ', truncated' : ''})`}
          </button>
          {showOutput && (
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-neutral-950 border border-neutral-800 p-2 text-[10px] font-mono leading-relaxed text-neutral-300 whitespace-pre-wrap break-words select-text">
              {output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
