import { useState } from 'react';
import type { Lab } from '../types/api';

export type SshConnectFields = {
  host: string;
  consoleUser: string;
  consolePort: string;
  password: string;
};

type LoginStepProps = {
  loading: boolean;
  error: string;
  labs: Lab[];
  onSubmit: (host: string, username: string, password: string) => Promise<void>;
  onSubmitSsh: (fields: SshConnectFields) => Promise<void>;
};

type ConnectionType = 'cml' | 'ssh';

export default function LoginStep({ loading, error, labs, onSubmit, onSubmitSsh }: LoginStepProps) {
  const [connectionType, setConnectionType] = useState<ConnectionType>('cml');
  const [host, setHost] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // SSH terminal-server console fields (defaults match the lab jump host).
  const [sshHost, setSshHost] = useState('');
  const [sshUser, setSshUser] = useState('');
  const [sshPort, setSshPort] = useState('19');
  const [sshPassword, setSshPassword] = useState('');

  const tabClass = (active: boolean) =>
    `flex-1 rounded-xl px-4 py-2 text-sm font-semibold transition ${
      active ? 'bg-sky-500 text-white shadow' : 'bg-sky-50 text-slate-600 hover:bg-sky-100'
    }`;

  return (
    <div className="min-h-screen bg-white flex items-center justify-center px-6">
      <div className="w-full max-w-md rounded-3xl border border-sky-200 bg-white p-8 shadow-lg">
        <h1 className="font-display text-3xl font-bold text-slate-900">CLIQ Live Terminal</h1>
        <p className="mt-2 text-slate-600">
          {connectionType === 'cml'
            ? 'Authenticate to CML and start isolated device terminals.'
            : 'Connect to a real device over an SSH terminal-server console.'}
        </p>

        <div className="mt-6 flex gap-2 rounded-2xl border border-sky-100 bg-white p-1">
          <button
            type="button"
            className={tabClass(connectionType === 'cml')}
            onClick={() => setConnectionType('cml')}
            disabled={loading}
          >
            CML Lab
          </button>
          <button
            type="button"
            className={tabClass(connectionType === 'ssh')}
            onClick={() => setConnectionType('ssh')}
            disabled={loading}
          >
            SSH Device
          </button>
        </div>

        {connectionType === 'cml' ? (
          <form
            className="mt-8 grid gap-5"
            onSubmit={(event) => {
              event.preventDefault();
              void onSubmit(host, username, password);
            }}
          >
            <label className="grid gap-2 text-sm">
              CML Host
              <input
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                placeholder="https://cml.example.local"
                value={host}
                onChange={(event) => setHost(event.target.value)}
                required
              />
            </label>

            <label className="grid gap-2 text-sm">
              Username
              <input
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                placeholder="admin"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
              />
            </label>

            <label className="grid gap-2 text-sm">
              Password
              <input
                type="password"
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                placeholder="Enter your password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </label>

            <button
              type="submit"
              className="rounded-xl bg-sky-500 px-4 py-2 font-semibold text-white transition hover:bg-sky-400 disabled:opacity-60"
              disabled={loading}
            >
              {loading ? 'Authenticating...' : 'Login'}
            </button>
          </form>
        ) : (
          <form
            className="mt-8 grid gap-5"
            onSubmit={(event) => {
              event.preventDefault();
              void onSubmitSsh({
                host: sshHost,
                consoleUser: sshUser,
                consolePort: sshPort,
                password: sshPassword,
              });
            }}
          >
            <label className="grid gap-2 text-sm">
              Terminal Server Host
              <input
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                placeholder="ts01.example.org"
                value={sshHost}
                onChange={(event) => setSshHost(event.target.value)}
                required
              />
            </label>

            <div className="grid grid-cols-3 gap-4">
              <label className="col-span-2 grid gap-2 text-sm">
                Console User
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                  placeholder="operator"
                  value={sshUser}
                  onChange={(event) => setSshUser(event.target.value)}
                  required
                />
              </label>

              <label className="grid gap-2 text-sm">
                Line / Port
                <input
                  className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                  placeholder="19"
                  value={sshPort}
                  onChange={(event) => setSshPort(event.target.value)}
                  required
                />
              </label>
            </div>

            <label className="grid gap-2 text-sm">
              Password
              <input
                type="password"
                className="rounded-xl border border-sky-200 bg-white px-3 py-2 text-slate-900 placeholder:text-slate-500 outline-none focus:border-sky-400"
                placeholder="Enter your password"
                value={sshPassword}
                onChange={(event) => setSshPassword(event.target.value)}
                required
              />
            </label>

            <button
              type="submit"
              className="rounded-xl bg-sky-500 px-4 py-2 font-semibold text-white transition hover:bg-sky-400 disabled:opacity-60"
              disabled={loading}
            >
              {loading ? 'Connecting...' : 'Connect'}
            </button>
          </form>
        )}

        {error && <p className="mt-4 text-sm text-red-500">{error}</p>}

        {connectionType === 'cml' && labs.length > 0 && (
          <div className="mt-6 rounded-xl border border-sky-100 bg-sky-50 p-4 text-sm text-slate-700">
            Auth success: {labs.length} labs available.
          </div>
        )}
      </div>
    </div>
  );
}