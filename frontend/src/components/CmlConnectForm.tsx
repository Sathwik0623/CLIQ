import React, { useState } from 'react';
import { GlobeIcon, LockIcon, AlertTriangleIcon } from './Icons';

type CmlConnectFormProps = {
  loading: boolean;
  error: string;
  onBack: () => void;
  onSubmit: (host: string, username: string, password: string) => Promise<void>;
};

export default function CmlConnectForm({
  loading,
  error,
  onBack,
  onSubmit,
}: CmlConnectFormProps) {
  const [controllerHost, setControllerHost] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [labLabel, setLabLabel] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    void onSubmit(controllerHost.trim(), username.trim(), password);
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-12" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>
      <div
        className="w-full max-w-lg rounded-2xl border p-8 shadow-2xl"
        style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)' }}
      >
        {/* Back Button */}
        <button
          type="button"
          onClick={onBack}
          disabled={loading}
          className="inline-flex items-center gap-1.5 text-xs font-semibold transition mb-5 cursor-pointer disabled:opacity-50 hover:text-sky-400"
          style={{ color: 'var(--ide-text-muted)' }}
        >
          <span aria-hidden>←</span>
          <span>Back to Sandbox Hub</span>
        </button>

        <div className="flex items-center justify-between">
          <h1 className="font-display text-2xl font-bold flex items-center gap-2 text-slate-100">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-sky-500/20 text-sky-400 border border-sky-400/30 text-base">
              <GlobeIcon size={18} />
            </span>
            CML Sandbox Lab
          </h1>
          <span className="rounded-full px-3 py-0.5 text-xs font-semibold" style={{ background: 'rgba(0,122,204,0.15)', border: '1px solid #007acc', color: '#75beff' }}>
            Cisco Modeling Labs
          </span>
        </div>

        <p className="mt-2 text-xs leading-relaxed" style={{ color: 'var(--ide-text-muted)' }}>
          Connect to your Cisco Modeling Labs controller to discover deployed network simulations, inspect live interactive ReactFlow topologies, and launch node console terminals.
        </p>

        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <label className="grid gap-1.5 text-xs font-medium" style={{ color: 'var(--ide-text-muted)' }}>
            CML Controller Host or URL *
            <input
              type="text"
              className="rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-400"
              style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              placeholder="e.g. 10.10.20.160 or cml.example.org"
              value={controllerHost}
              onChange={(e) => setControllerHost(e.target.value)}
              required
              autoFocus
              disabled={loading}
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1.5 text-xs font-medium" style={{ color: 'var(--ide-text-muted)' }}>
              Username *
              <input
                type="text"
                className="rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-400"
                style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                placeholder="e.g. developer"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                disabled={loading}
              />
            </label>

            <label className="grid gap-1.5 text-xs font-medium" style={{ color: 'var(--ide-text-muted)' }}>
              Password *
              <input
                type="password"
                className="rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-400"
                style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
                placeholder="CML account password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                disabled={loading}
              />
            </label>
          </div>

          <label className="grid gap-1.5 text-xs font-medium" style={{ color: 'var(--ide-text-muted)' }}>
            Lab / Deployment Label (Optional)
            <input
              type="text"
              className="rounded-lg border px-3 py-2 text-sm outline-none focus:border-sky-400"
              style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
              placeholder="e.g. CCNA Enterprise Lab"
              value={labLabel}
              onChange={(e) => setLabLabel(e.target.value)}
              disabled={loading}
            />
          </label>

          <div
            className="rounded-lg border p-3 text-xs space-y-1"
            style={{ background: 'rgba(0,122,204,0.06)', borderColor: 'rgba(0,122,204,0.3)', color: '#93c5fd' }}
          >
            <div className="flex items-start gap-2">
              <span className="text-sky-400 font-bold mt-0.5"><LockIcon size={14} /></span>
              <span>
                <strong>Transient In-Memory Session:</strong> CML credentials and session tokens stay in transient memory for this session only and are never saved to disk, SQLite, browser storage (localStorage / sessionStorage), or logs.
              </span>
            </div>
            <div className="flex items-start gap-2 text-slate-400">
              <span className="mt-0.5"><GlobeIcon size={14} /></span>
              <span>
                <strong>Management Plane vs Node Console:</strong> CLIQ authenticates against the CML REST API to discover topology, then bridges directly to node consoles over WebSocket.
              </span>
            </div>
          </div>

          <button
            type="submit"
            className="mt-2 rounded-lg px-4 py-2.5 font-semibold text-white shadow-sm transition hover:opacity-90 disabled:opacity-60 flex items-center justify-center gap-2 cursor-pointer"
            style={{ background: 'var(--ide-accent)' }}
            disabled={loading}
          >
            {loading ? (
              <>
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                Connecting to CML Sandbox...
              </>
            ) : (
              'Connect to CML Sandbox'
            )}
          </button>
        </form>

        {error && (
          <div
            className="mt-4 rounded-lg border p-3 text-xs flex items-start gap-2"
            style={{ background: 'rgba(239,68,68,0.1)', borderColor: 'rgba(239,68,68,0.3)', color: '#fca5a5' }}
          >
            <span className="text-rose-400 font-bold mt-0.5"><AlertTriangleIcon size={14} /></span>
            <span>{error}</span>
          </div>
        )}
      </div>
    </div>
  );
}
