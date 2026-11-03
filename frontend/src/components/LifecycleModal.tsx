import { useEffect, useRef, useState } from 'react';
import type { AvailableLab, StartOperation } from '../types/labs';
import { getLabOperation, getStartOp, postStartLab, postStopLab } from '../lib/labsApi';
import { AlertTriangleIcon, CheckIcon, CloseIcon } from './Icons';

type LifecycleModalProps = {
  token: string;
  lab: AvailableLab;
  mode: 'start' | 'stop';
  onClose: () => void;
  onFinished: (labId: string) => void;
};

const COPY = {
  start: {
    title: 'Start lab',
    blurb:
      'Starting boots every node in this lab on the CML controller. This consumes controller CPU/RAM and can take several minutes. Nodes are started as defined — CLIQ never pushes device configuration.',
    ack: 'I understand this boots all nodes in',
    confirm: 'Confirm & Start',
    working: 'Starting…',
    done: 'Lab is running',
    failed: 'Start failed',
    polling: 'Starting — polling controller…',
  },
  stop: {
    title: 'Stop lab',
    blurb:
      'Stopping shuts down every node in this lab on the CML controller and frees its CPU/RAM. Open consoles will disconnect. Nothing is deleted — the lab definition stays intact.',
    ack: 'I understand this stops all nodes in',
    confirm: 'Confirm & Stop',
    working: 'Stopping…',
    done: 'Lab is stopped',
    failed: 'Stop failed',
    polling: 'Stopping — polling controller…',
  },
} as const;

const POLL_INTERVAL_MS = 2000;
const MAX_TRANSIENT_POLL_ERRORS = 10;
const MAX_POLL_BACKOFF_MS = 30_000;

function terminalPollMessage(raw: string): string | null {
  // Terminal poll failures: the operation can never complete from here, so
  // polling halts with a clear message instead of retrying forever.
  if (raw.includes('→ 401:')) return 'Session expired — please log in again.';
  if (raw.includes('→ 404:')) {
    return 'Operation is no longer tracked (backend restarted?). The lab itself is unaffected — Refresh the dashboard for live state.';
  }
  if (raw.includes('→ 409:') || raw.includes('→ 422:')) return raw;
  return null;
}

export default function LifecycleModal(props: LifecycleModalProps) {
  const copy = COPY[props.mode];
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [op, setOp] = useState<StartOperation | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [polling, setPolling] = useState(false);
  const [recovering, setRecovering] = useState(true);
  const pollRef = useRef<number | null>(null);
  const tickRef = useRef<number | null>(null);
  const busyRef = useRef(false);
  const errorsRef = useRef(0);
  const nextAtRef = useRef(0);

  function stopTicker() {
    if (tickRef.current) window.clearInterval(tickRef.current);
    tickRef.current = null;
  }

  function haltPolling() {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = null;
    busyRef.current = false;
    setPolling(false);
    stopTicker();
  }

  function beginPolling(opId: string) {
    haltPolling();
    errorsRef.current = 0;
    nextAtRef.current = 0;
    setNotice('');
    setElapsed(0);
    stopTicker();
    tickRef.current = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    setPolling(true);
    pollRef.current = window.setInterval(async () => {
      // In-flight guard: a slow controller must delay ticks, never stack them.
      if (busyRef.current) return;
      if (Date.now() < nextAtRef.current) return;
      busyRef.current = true;
      try {
        const status = await getStartOp(props.token, opId);
        errorsRef.current = 0;
        setNotice('');
        setOp(status);
        const finished = props.mode === 'start'
          ? status.status === 'running' || status.status === 'failed'
          : status.status === 'stopped' || status.status === 'failed';
        if (finished) {
          haltPolling();
          if (status.status === 'failed') {
            setError(status.error || `The lab failed to ${props.mode}.`);
          } else {
            props.onFinished(props.lab.id);
          }
        }
      } catch (e) {
        const raw = e instanceof Error ? e.message : 'Status polling failed';
        const terminal = terminalPollMessage(raw);
        if (terminal) {
          haltPolling();
          setError(terminal);
          return;
        }
        errorsRef.current += 1;
        if (errorsRef.current > MAX_TRANSIENT_POLL_ERRORS) {
          haltPolling();
          setError(
            `Polling failed ${MAX_TRANSIENT_POLL_ERRORS}× in a row — last: ${raw}. ` +
            'The operation continues on the server; Resume polling or Refresh the dashboard.',
          );
          return;
        }
        // Transient blip: back off and keep polling; the operation record on
        // the server is untouched by our read failures.
        const waitMs = Math.min(POLL_INTERVAL_MS * 2 ** (errorsRef.current - 1), MAX_POLL_BACKOFF_MS);
        nextAtRef.current = Date.now() + waitMs;
        setNotice(`Poll hiccup (${errorsRef.current}) — retrying…`);
      } finally {
        busyRef.current = false;
      }
    }, POLL_INTERVAL_MS);
  }

  // Recovery: an in-flight operation for this lab is resumed WITHOUT
  // issuing a new start/stop — the dialog never depends on the original
  // polling request surviving.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const existing = await getLabOperation(props.token, props.lab.id);
        if (cancelled) return;
        if (existing && existing.kind === props.mode
            && (existing.status === 'starting' || existing.status === 'stopping')) {
          setOp(existing);
          setNotice('Resumed tracking the in-flight operation — no new request was sent.');
          beginPolling(existing.op_id);
        } else if (existing && existing.kind !== props.mode
            && (existing.status === 'starting' || existing.status === 'stopping')) {
          setNotice(`A ${existing.kind} operation is already in flight for this lab.`);
        } else if (existing && existing.kind === props.mode
            && (existing.status === 'running' || existing.status === 'stopped'
                || existing.status === 'failed')) {
          setOp(existing);
        }
      } catch {
        if (!cancelled) {
          // Best effort only: confirming still resumes any in-flight
          // operation server-side (no duplicate request), so fall through.
          setNotice('Could not check for in-flight operations — confirming will resume one if present.');
        }
      } finally {
        if (!cancelled) setRecovering(false);
      }
    })();
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearInterval(pollRef.current);
      if (tickRef.current) window.clearInterval(tickRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const succeeded = props.mode === 'start' ? op?.status === 'running' : op?.status === 'stopped';
  const terminal = succeeded || op?.status === 'failed';

  async function handleConfirm() {
    if (!acknowledged || busy || op) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const launched = props.mode === 'start'
        ? await postStartLab(props.token, props.lab.id)
        : await postStopLab(props.token, props.lab.id);
      setOp(launched);
      const done = props.mode === 'start'
        ? launched.status === 'running'
        : launched.status === 'stopped';
      if (done) {
        props.onFinished(props.lab.id);
        return;
      }
      if (launched.status === 'failed') {
        setError(launched.error || `The controller refused to ${props.mode} the lab.`);
        return;
      }
      beginPolling(launched.op_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : `Failed to ${props.mode} the lab`);
    } finally {
      setBusy(false);
    }
  }

  function handleResume() {
    if (!op || terminal || polling) return;
    setError('');
    beginPolling(op.op_id);
  }

  function handleRetryFresh() {
    // Terminal failure of our own kind: explicit user choice to try again.
    setOp(null);
    setError('');
    setNotice('');
    setAcknowledged(false);
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-label={`${copy.title} confirmation`}>
      <div className="w-full max-w-md rounded-xl border p-5 shadow-xl" style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}>
        <div className="flex items-center justify-between">
          <span className="text-sm font-bold text-slate-200">{copy.title}</span>
          <button type="button" onClick={props.onClose} aria-label="Close" className="rounded p-1 hover:bg-[var(--ide-hover)]">
            <CloseIcon size={14} />
          </button>
        </div>

        <div className="mt-3 space-y-2 text-xs text-slate-300">
          <div>
            <span className="font-semibold" style={{ color: 'var(--ide-text-muted)' }}>Lab: </span>
            <span className="font-bold">{props.lab.title}</span>
            <span style={{ color: 'var(--ide-text-muted)' }}> ({props.lab.state_display})</span>
          </div>
          <div style={{ color: 'var(--ide-text-muted)' }}>{copy.blurb}</div>

          {recovering && (
            <div className="text-[11px]" style={{ color: 'var(--ide-text-muted)' }}>
              Checking for an in-flight operation to resume…
            </div>
          )}

          {error && (
            <div role="alert" className="flex items-start gap-2 rounded-lg border px-3 py-2 text-xs font-semibold text-rose-300" style={{ background: '#2a1215', borderColor: '#7f1d1d' }}>
              <AlertTriangleIcon size={14} className="shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {notice && !error && (
            <div className="text-[11px]" style={{ color: 'var(--ide-text-muted)' }}>
              {notice}
            </div>
          )}

          {op && (
            <div className="rounded-lg border p-2.5 text-[11px]" style={{ borderColor: 'var(--ide-border)', background: 'var(--ide-bg)' }}>
              <div className="flex items-center gap-1.5 font-bold">
                {succeeded ? (
                  <CheckIcon size={13} className="text-emerald-400" />
                ) : op.status === 'failed' ? (
                  <AlertTriangleIcon size={13} className="text-rose-400" />
                ) : (
                  <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
                )}
                <span>{succeeded ? copy.done : op.status === 'failed' ? copy.failed : copy.polling}</span>
              </div>
              {op.detail && <div className="mt-1" style={{ color: 'var(--ide-text-muted)' }}>{op.detail}</div>}
              {op.raw_state && <div style={{ color: 'var(--ide-text-muted)' }}>Controller state: {op.raw_state}</div>}
              <div style={{ color: 'var(--ide-text-muted)' }}>
                Converged: {op.converged == null ? '—' : op.converged ? 'yes' : 'not yet'}
                {op.nodes_booted != null && op.nodes_total != null
                  ? ` · nodes booted ${op.nodes_booted}/${op.nodes_total}`
                  : ''}
                {op.nodes_active != null && op.nodes_total != null
                  ? ` · ${op.nodes_active}/${op.nodes_total} active`
                  : ''}
                {` · ${elapsed}s elapsed`}
              </div>
              {!terminal && !polling && !recovering && (
                <button
                  type="button"
                  onClick={handleResume}
                  className="mt-2 rounded-lg border px-3 py-1.5 text-xs font-semibold hover:bg-[var(--ide-hover)]"
                  style={{ borderColor: 'var(--ide-accent)', color: 'var(--ide-text)' }}
                  title="Resume tracking this operation without sending a new request"
                >
                  Resume polling
                </button>
              )}
              {!terminal && (
                <div style={{ color: 'var(--ide-text-muted)' }}>
                  You can close this dialog — the operation continues on the server and the dashboard tracks it.
                </div>
              )}
            </div>
          )}

          {!op && (
            <label className="flex items-start gap-2 rounded-lg border p-2.5" style={{ borderColor: '#b45309', background: '#291400' }}>
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
                className="mt-0.5 h-3.5 w-3.5 accent-amber-500"
              />
              <span className="font-semibold text-amber-200">
                {copy.ack} “{props.lab.title}” on the real controller.
              </span>
            </label>
          )}
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={props.onClose}
            className="rounded-lg border px-4 py-2 text-xs font-semibold hover:bg-[var(--ide-hover)]"
            style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
          >
            {terminal ? 'Close' : 'Cancel'}
          </button>
          {!op && (
            <button
              type="button"
              disabled={!acknowledged || busy || recovering}
              onClick={() => void handleConfirm()}
              className="rounded-lg px-4 py-2 text-xs font-semibold text-white disabled:opacity-60"
              style={{ background: acknowledged ? (props.mode === 'start' ? '#15803d' : '#b91c1c') : 'var(--ide-accent)' }}
            >
              {busy ? copy.working : copy.confirm}
            </button>
          )}
          {op && op.status === 'failed' && op.kind === props.mode && (
            <button
              type="button"
              onClick={handleRetryFresh}
              className="rounded-lg border px-4 py-2 text-xs font-semibold hover:bg-[var(--ide-hover)]"
              style={{ borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              title="Dismiss this failure and start a fresh confirmation"
            >
              Try again
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
