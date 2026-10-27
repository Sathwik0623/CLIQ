import type { AuthResponse, DevicesResponse, SshConnectResponse } from '../types/api';

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
 * Connect to a REAL device via a terminal-server SSH console. Returns a session
 * token plus a single synthetic device that the SAME WebSocket terminal drives.
 * The password is sent once over HTTPS and is never echoed back.
 */
export async function postSshConnect(
  host: string,
  consoleUser: string,
  consolePort: string,
  password: string,
  deviceType = 'cisco_ios',
): Promise<SshConnectResponse> {
  const res = await fetch(`${REST_BASE}/api/ssh/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: host.trim(),
      console_user: consoleUser.trim(),
      console_port: consolePort.trim(),
      password,
      device_type: deviceType,
    }),
  });
  if (!res.ok) {
    throw new Error(`SSH connect failed (${res.status})`);
  }
  return (await res.json()) as SshConnectResponse;
}
