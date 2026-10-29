import React, { useState } from 'react';
import type { Lab, SshConnectFields } from '../types/api';
import type { PlatformId } from '../types/platform';
import SandboxHub from './SandboxHub';
import CmlConnectForm from './CmlConnectForm';

export type { SshConnectFields };

type LoginStepProps = {
  loading: boolean;
  error: string;
  labs?: Lab[];
  onSubmit?: (host: string, username: string, password: string) => Promise<void>;
  onSubmitSsh: (fields: SshConnectFields) => Promise<void>;
};

type SshMode = 'direct' | 'terminal_server';

export default function LoginStep({ loading, error, onSubmit, onSubmitSsh }: LoginStepProps) {
  // Navigation between Sandbox Hub and specific platform connection forms
  const [activePlatform, setActivePlatform] = useState<PlatformId | 'hub'>('hub');

  // Direct Device SSH mode state (direct vs terminal_server)
  const [sshMode, setSshMode] = useState<SshMode>('direct');

  // Direct SSH state
  const [directLabel, setDirectLabel] = useState('');
  const [directHost, setDirectHost] = useState('');
  const [directPort, setDirectPort] = useState('22');
  const [directUser, setDirectUser] = useState('');
  const [directPassword, setDirectPassword] = useState('');
  const [directSecret, setDirectSecret] = useState('');
  const [directPlatform, setDirectPlatform] = useState('cisco_ios');

  // Terminal-server console state
  const [tsLabel, setTsLabel] = useState('');
  const [tsHost, setTsHost] = useState('');
  const [tsSshPort, setTsSshPort] = useState('22');
  const [tsLine, setTsLine] = useState('');
  const [tsUser, setTsUser] = useState('');
  const [tsPassword, setTsPassword] = useState('');
  const [tsSecret, setTsSecret] = useState('');
  const [tsPlatform, setTsPlatform] = useState('cisco_ios');

  const tabClass = (active: boolean) =>
    `flex-1 rounded-xl px-4 py-2 text-sm font-semibold transition cursor-pointer ${
      active ? 'bg-sky-500 text-white shadow' : 'bg-sky-50 text-slate-600 hover:bg-sky-100'
    }`;

  const handleDirectSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    void onSubmitSsh({
      method: 'direct',
      label: directLabel.trim(),
      host: directHost.trim(),
      port: directPort.trim() || '22',
      username: directUser.trim(),
      password: directPassword,
      secret: directSecret.trim() || undefined,
      deviceType: directPlatform,
    });
  };

  const handleTsSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    void onSubmitSsh({
      method: 'terminal_server',
      label: tsLabel.trim(),
      host: tsHost.trim(),
      port: tsSshPort.trim() || '22',
      consolePort: tsLine.trim(),
      username: tsUser.trim(),
      password: tsPassword,
      secret: tsSecret.trim() || undefined,
      deviceType: tsPlatform,
    });
  };

  // 1. Landing View: Multi-Platform Sandbox Hub
  if (activePlatform === 'hub') {
    return <SandboxHub onSelectPlatform={(id) => setActivePlatform(id)} />;
  }

  // 2. CML Platform View: Cisco Modeling Labs Form
  if (activePlatform === 'cml') {
    return (
      <CmlConnectForm
        loading={loading}
        error={error}
        onBack={() => setActivePlatform('hub')}
        onSubmit={async (cmlHost, user, pwd) => {
          if (onSubmit) {
            await onSubmit(cmlHost, user, pwd);
          }
        }}
      />
    );
  }

  // 3. Direct Sandbox Device View: Direct SSH & Terminal-Server Console Form
  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-lg rounded-3xl border border-sky-200 bg-white p-8 shadow-xl">
        {/* Back Button */}
        <button
          type="button"
          onClick={() => setActivePlatform('hub')}
          disabled={loading}
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-sky-600 transition mb-5 cursor-pointer disabled:opacity-50"
        >
          <span aria-hidden>←</span>
          <span>Back to Sandbox Hub</span>
        </button>

        <div className="flex items-center justify-between">
          <h1 className="font-display text-2xl font-bold text-slate-900 flex items-center gap-2">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-sky-500 text-white text-base">
              ⚡
            </span>
            CLIQ — Sandbox Device Terminal
          </h1>
          <span className="rounded-full bg-emerald-50 border border-emerald-200 px-3 py-0.5 text-xs font-semibold text-emerald-700">
            Direct Sandbox
          </span>
        </div>

        <p className="mt-2 text-sm text-slate-600">
          Connect directly to your active Cisco sandbox switch, router, or terminal server console.
          Enter your current 24-hour sandbox reservation credentials below.
        </p>

        {/* Connection method selector */}
        <div className="mt-6 flex gap-2 rounded-2xl border border-sky-100 bg-sky-50/50 p-1">
          <button
            type="button"
            className={tabClass(sshMode === 'direct')}
            onClick={() => setSshMode('direct')}
            disabled={loading}
          >
            Direct Device SSH
          </button>
          <button
            type="button"
            className={tabClass(sshMode === 'terminal_server')}
            onClick={() => setSshMode('terminal_server')}
            disabled={loading}
          >
            Terminal-Server Console
          </button>
        </div>

        {sshMode === 'direct' ? (
          <form className="mt-6 grid gap-4" onSubmit={handleDirectSubmit}>
            <div className="grid grid-cols-3 gap-3">
              <label className="grid col-span-2 gap-1.5 text-xs font-medium text-slate-700">
                Device IP or Hostname *
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="e.g. 10.10.20.181 or sandbox IP"
                  value={directHost}
                  onChange={(e) => setDirectHost(e.target.value)}
                  required
                  autoFocus
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                SSH Port *
                <input
                  type="number"
                  min="1"
                  max="65535"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="22"
                  value={directPort}
                  onChange={(e) => setDirectPort(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Username *
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="e.g. developer"
                  value={directUser}
                  onChange={(e) => setDirectUser(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Target Platform
                <select
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  value={directPlatform}
                  onChange={(e) => setDirectPlatform(e.target.value)}
                  disabled={loading}
                >
                  <option value="cisco_ios">Cisco IOS / IOS-XE (ISR / Catalyst)</option>
                  <option value="cisco_xe">Cisco IOS-XE (Native)</option>
                  <option value="cisco_nxos">Cisco NX-OS (Nexus)</option>
                  <option value="cisco_xr">Cisco IOS-XR</option>
                </select>
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                SSH Login Password *
                <input
                  type="password"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="SSH login password"
                  value={directPassword}
                  onChange={(e) => setDirectPassword(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Enable Secret (Optional)
                <input
                  type="password"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="Enable secret for (#) mode"
                  value={directSecret}
                  onChange={(e) => setDirectSecret(e.target.value)}
                  title="Collected at login for privileged EXEC (#) mode. Stored only in transient session memory; never reprompted."
                  disabled={loading}
                />
              </label>
            </div>

            <label className="grid gap-1.5 text-xs font-medium text-slate-700">
              Friendly Label (Optional)
              <input
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                placeholder="e.g. Catalyst 9200CX Sandbox"
                value={directLabel}
                onChange={(e) => setDirectLabel(e.target.value)}
                disabled={loading}
              />
            </label>

            <div className="rounded-xl bg-sky-50 border border-sky-100 p-3 text-xs text-sky-800 space-y-1">
              <div className="flex items-start gap-2">
                <span className="text-sky-500 font-bold">🔒</span>
                <span>
                  <strong>Transient In-Memory Session:</strong> Passwords and enable secrets stay in transient memory for this session only and are never saved to disk, SQLite, browser storage (localStorage / sessionStorage), or logs.
                </span>
              </div>
              <div className="flex items-start gap-2 text-slate-600">
                <span>⏱️</span>
                <span>
                  <strong>24-Hour Sandbox Expiration:</strong> Cisco sandboxes rotate every 24 hours. When your reservation rotates, update the IP and credentials above to reconnect.
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
                  Connecting to Sandbox Device...
                </>
              ) : (
                'Connect to Sandbox Device'
              )}
            </button>
          </form>
        ) : (
          <form className="mt-6 grid gap-4" onSubmit={handleTsSubmit}>
            <div className="grid grid-cols-3 gap-3">
              <label className="grid col-span-2 gap-1.5 text-xs font-medium text-slate-700">
                Terminal Server Host *
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="e.g. ts01.example.org"
                  value={tsHost}
                  onChange={(e) => setTsHost(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                SSH Port *
                <input
                  type="number"
                  min="1"
                  max="65535"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="22"
                  value={tsSshPort}
                  onChange={(e) => setTsSshPort(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Console Line / Port *
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="e.g. 19"
                  value={tsLine}
                  onChange={(e) => setTsLine(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Username *
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="e.g. operator"
                  value={tsUser}
                  onChange={(e) => setTsUser(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Password *
                <input
                  type="password"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="Console password"
                  value={tsPassword}
                  onChange={(e) => setTsPassword(e.target.value)}
                  required
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Enable Secret (Opt)
                <input
                  type="password"
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  placeholder="Enable secret (#)"
                  value={tsSecret}
                  onChange={(e) => setTsSecret(e.target.value)}
                  title="Collected at login for privileged EXEC (#) mode. Stored only in transient session memory; never reprompted."
                  disabled={loading}
                />
              </label>

              <label className="grid gap-1.5 text-xs font-medium text-slate-700">
                Target Platform
                <select
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                  value={tsPlatform}
                  onChange={(e) => setTsPlatform(e.target.value)}
                  disabled={loading}
                >
                  <option value="cisco_ios">Cisco IOS / IOS-XE</option>
                  <option value="cisco_xe">Cisco IOS-XE</option>
                  <option value="cisco_nxos">Cisco NX-OS</option>
                  <option value="cisco_xr">Cisco IOS-XR</option>
                </select>
              </label>
            </div>

            <label className="grid gap-1.5 text-xs font-medium text-slate-700">
              Friendly Label (Optional)
              <input
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
                placeholder="e.g. Lab Router R1 (Line 19)"
                value={tsLabel}
                onChange={(e) => setTsLabel(e.target.value)}
                disabled={loading}
              />
            </label>

            <div className="rounded-xl bg-sky-50 border border-sky-100 p-3 text-xs text-sky-800 space-y-1">
              <div className="flex items-start gap-2">
                <span className="text-sky-500 font-bold">🔒</span>
                <span>
                  <strong>Transient In-Memory Session:</strong> Passwords and enable secrets stay in transient memory for this session only and are never saved to disk, SQLite, browser storage (localStorage / sessionStorage), or logs.
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
                  Connecting to Console...
                </>
              ) : (
                'Connect to Sandbox Console'
              )}
            </button>
          </form>
        )}

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
