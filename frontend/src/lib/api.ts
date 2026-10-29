import type { AuthResponse, DevicesResponse, SshConnectFields, SshConnectResponse } from '../types/api';

// Same-origin API base: the app calls the API on whatever host/port it was
// served from (empty string → relative `/api/...` URLs). This lets ANY machine
// that opens the link reach the backend without a hardcoded `localhost:8000`.
// In `npm run dev` (Vite :5173) the dev-server proxy (see vite.config.ts)
// forwards `/api` and `/ws` to the backend, so the same relative URLs work.
const REST_BASE = '';

export async function postAuth(host: string, username: string, password: string): Promise<AuthResponse> {
  const cleanHost = host.trim();
  const res = await fetch(`${REST_BASE}/api/auth`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ host: cleanHost, username, password }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    // The gate FAILS CLOSED with a non-2xx on any auth failure. Surface the real
    // reason from the JSON body (FastAPI wraps it under `detail`) so the login
    // page can show it and stay put — never advance to the device list.
    const detail = (body && (body.detail ?? body)) as
      | { error?: string; errorKind?: string }
      | string
      | null;
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.error || `Authentication failed (${res.status})`;
    throw new Error(message);
  }
  return body as AuthResponse;
}

export async function postDevices(host: string, token: string, labId: string): Promise<DevicesResponse> {
  const cleanHost = host.trim();
  const res = await fetch(`${REST_BASE}/api/devices`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ host: cleanHost, token, lab_id: labId }),
  });
  if (!res.ok) {
    throw new Error(`Device fetch failed (${res.status})`);
  }
  return (await res.json()) as DevicesResponse;
}

/**
 * Connect to a REAL device via Direct SSH or a terminal-server SSH console.
 * Returns a session token plus a single synthetic device that the WebSocket terminal drives.
 * Passwords stay in transient memory and are never stored in browser storage or logged.
 */
export async function postSshConnect(
  fieldsOrHost: SshConnectFields | string,
  consoleUser?: string,
  consolePort?: string,
  password?: string,
  deviceType = 'cisco_ios',
): Promise<SshConnectResponse> {
  let payload: Record<string, unknown>;

  if (typeof fieldsOrHost === 'object' && fieldsOrHost !== null) {
    const f = fieldsOrHost;
    payload = {
      host: f.host.trim(),
      username: f.username.trim(),
      console_user: f.username.trim(),
      password: f.password,
      secret: f.secret ? f.secret.trim() : undefined,
      port: f.port ? parseInt(f.port, 10) : undefined,
      console_port: (f.consolePort || '').trim(),
      device_type: f.deviceType || 'cisco_ios',
      method: f.method || 'direct',
      label: (f.label || '').trim(),
    };
  } else {
    payload = {
      host: fieldsOrHost.trim(),
      username: (consoleUser || '').trim(),
      console_user: (consoleUser || '').trim(),
      console_port: (consolePort || '').trim(),
      password: password || '',
      device_type: deviceType,
      method: consolePort ? 'terminal_server' : 'direct',
    };
  }

  const res = await fetch(`${REST_BASE}/api/ssh/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (body && (body.detail ?? body)) as
      | { error?: string }
      | string
      | null;
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.error || `SSH connect failed (${res.status})`;
    throw new Error(msg);
  }
  return body as SshConnectResponse;
}

/** Gracefully close and clean up an active session and its device connections. */
export async function postDisconnect(token: string): Promise<void> {
  if (!token) return;
  await fetch(`${REST_BASE}/api/session/disconnect?token=${encodeURIComponent(token)}`, {
    method: 'POST',
  }).catch(() => {});
}
