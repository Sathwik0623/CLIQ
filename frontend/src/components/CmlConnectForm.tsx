import React, { useState } from 'react';

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
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-lg rounded-3xl border border-sky-200 bg-white p-8 shadow-xl">
        {/* Back Button */}
        <button
          type="button"
          onClick={onBack}
          disabled={loading}
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-sky-600 transition mb-5 cursor-pointer disabled:opacity-50"
        >
          <span aria-hidden>←</span>
          <span>Back to Sandbox Hub</span>
        </button>

        <div className="flex items-center justify-between">
          <h1 className="font-display text-2xl font-bold text-slate-900 flex items-center gap-2">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-sky-500 text-white text-base">
              🌐
            </span>
            CML Sandbox Lab
          </h1>
          <span className="rounded-full bg-sky-50 border border-sky-200 px-3 py-0.5 text-xs font-semibold text-sky-700">
            Cisco Modeling Labs
          </span>
        </div>

        <p className="mt-2 text-sm text-slate-600">
          Connect to your Cisco Modeling Labs controller to discover deployed network simulations, inspect live interactive ReactFlow topologies, and launch node console terminals.
        </p>

        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <label className="grid gap-1.5 text-xs font-medium text-slate-700">
            CML Controller Host or URL *
            <input
              type="text"
              className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
              placeholder="e.g. 10.10.20.160 or cml.example.org"
              value={controllerHost}
              onChange={(e) => setControllerHost(e.target.value)}
              required
              autoFocus
              disabled={loading}
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1.5 text-xs font-medium text-slate-700">
              Username *
              <input
                type="text"
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                placeholder="e.g. developer"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                disabled={loading}
              />
            </label>

            <label className="grid gap-1.5 text-xs font-medium text-slate-700">
              Password *
              <input
                type="password"
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                placeholder="CML account password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                disabled={loading}
              />
            </label>
          </div>

          <label className="grid gap-1.5 text-xs font-medium text-slate-700">
            Lab / Deployment Label (Optional)
            <input
              type="text"
              className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
              placeholder="e.g. CCNA Enterprise Lab"
              value={labLabel}
              onChange={(e) => setLabLabel(e.target.value)}
              disabled={loading}
            />
          </label>

          <div className="rounded-xl bg-sky-50 border border-sky-100 p-3 text-xs text-sky-800 space-y-1">
            <div className="flex items-start gap-2">
              <span className="text-sky-500 font-bold">🔒</span>
              <span>
                <strong>Transient In-Memory Session:</strong> CML credentials and session tokens stay in transient memory for this session only and are never saved to disk, SQLite, browser storage (localStorage / sessionStorage), or logs.
              </span>
            </div>
            <div className="flex items-start gap-2 text-slate-600">
              <span>🌐</span>
              <span>
                <strong>Management Plane vs Node Console:</strong> CLIQ authenticates against the CML REST API to discover topology, then bridges directly to node consoles over WebSocket.
              </span>
            </div>
          </div>

          <button
            type="submit"
            className="mt-2 rounded-xl bg-sky-500 px-4 py-2.5 font-semibold text-white shadow-sm transition hover:bg-sky-400 disabled:opacity-60 flex items-center justify-center gap-2 cursor-pointer"
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
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-700 flex items-start gap-2">
            <span className="text-red-500 font-bold">⚠️</span>
            <span>{error}</span>
          </div>
        )}
      </div>
    </div>
  );
}

