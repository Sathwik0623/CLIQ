import type {
  ApprovalTicket,
  AvailableLabsResponse,
  DeployJob,
  DeployTopology,
  LabBlueprint,
  PlanResponse,
  PreviewResponse,
  StartOperation,
  ValidationResult,
} from '../types/labs';

// Same-origin API base, matching lib/api.ts. All Lab Copilot calls carry the
// CLIQ session token (require_session gate, 401 fail-closed).
const REST_BASE = '';

function authHeaders(token: string): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  };
}

function extractError(body: unknown, status: number, fallback: string): string {
  const detail = (body && typeof body === 'object' && 'detail' in body
    ? (body as { detail: unknown }).detail
    : body) as string | { error?: string } | null;
  if (typeof detail === 'string' && detail) return detail;
  if (detail && typeof detail === 'object' && detail.error) return detail.error;
  return `${fallback} (${status})`;
}

function staleBackendHint(status: number, detail: string): string {
  // A bare "Not Found" (or an empty body) means the running backend does not
  // serve this route at all — i.e. it predates the frontend build. Say so
  // explicitly instead of surfacing a bare framework message.
  if (status === 404 && detail === 'Not Found') {
    return 'Not Found — the running backend may predate this screen. Restart the CLIQ backend and retry.';
  }
  return detail;
}

const DEFAULT_TIMEOUT_MS = 120_000;

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

function timeoutError(method: string, path: string, timeoutMs: number): Error {
  return new Error(
    `${method} ${path} → timed out after ${Math.round(timeoutMs / 1000)}s ` +
    `(controller slow or unreachable; safe to retry)`,
  );
}

async function postJson<T>(token: string, path: string, payload: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetchWithTimeout(`${REST_BASE}${path}`, {
      method: 'POST',
      headers: authHeaders(token),
      body: JSON.stringify(payload),
    }, DEFAULT_TIMEOUT_MS);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw timeoutError('POST', path, DEFAULT_TIMEOUT_MS);
    }
    throw e;
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = staleBackendHint(res.status, extractError(body, res.status, 'Request failed'));
    throw new Error(`POST ${path} → ${res.status}: ${detail}`);
  }
  if (body == null) {
    throw new Error(`POST ${path} → ${res.status}: empty response (is the backend up to date?)`);
  }
  return body as T;
}

async function getJson<T>(token: string, path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetchWithTimeout(`${REST_BASE}${path}`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
    }, DEFAULT_TIMEOUT_MS);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw timeoutError('GET', path, DEFAULT_TIMEOUT_MS);
    }
    throw e;
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = staleBackendHint(res.status, extractError(body, res.status, 'Request failed'));
    throw new Error(`GET ${path} → ${res.status}: ${detail}`);
  }
  if (body == null) {
    throw new Error(`GET ${path} → ${res.status}: empty response (is the backend up to date?)`);
  }
  return body as T;
}

export async function postLabPlan(token: string, prompt: string): Promise<PlanResponse> {
  return postJson<PlanResponse>(token, '/api/labs/plan', { prompt });
}

export async function postLabPreview(token: string, blueprint: LabBlueprint): Promise<PreviewResponse> {
  return postJson<PreviewResponse>(token, '/api/labs/preview', { blueprint });
}

export async function postLabValidate(
  token: string,
  blueprint: LabBlueprint,
  live: boolean,
): Promise<ValidationResult & { live: { live: boolean } }> {
  return postJson(token, '/api/labs/validate', { blueprint, live });
}

export async function postDeployPrepare(
  token: string,
  blueprint: LabBlueprint,
  live: boolean,
  labTitle?: string,
): Promise<ApprovalTicket> {
  return postJson<ApprovalTicket>(token, '/api/labs/deploy/prepare', {
    blueprint,
    live,
    ...(labTitle ? { lab_title: labTitle } : {}),
  });
}

export async function postDeployConfirm(
  token: string,
  ticketId: string,
  idempotencyKey: string,
  labTitle?: string,
): Promise<DeployJob & { created: boolean }> {
  return postJson(token, '/api/labs/deploy/confirm', {
    ticket_id: ticketId,
    idempotency_key: idempotencyKey,
    ...(labTitle ? { lab_title: labTitle } : {}),
  });
}

export async function getDeployStatus(token: string, jobId: string): Promise<DeployJob> {
  return getJson<DeployJob>(token, `/api/labs/deploy/${encodeURIComponent(jobId)}`);
}

export async function getDeployTopology(token: string, jobId: string): Promise<DeployTopology> {
  return getJson<DeployTopology>(token, `/api/labs/deploy/${encodeURIComponent(jobId)}/topology`);
}

export async function getAvailableLabs(token: string): Promise<AvailableLabsResponse> {
  return getJson<AvailableLabsResponse>(token, '/api/labs/available');
}

export async function postStartLab(
  token: string,
  labId: string,
): Promise<StartOperation & { created: boolean }> {
  return postJson(token, `/api/labs/${encodeURIComponent(labId)}/start`, {});
}

export async function getStartOp(token: string, opId: string): Promise<StartOperation> {
  return getJson<StartOperation>(token, `/api/labs/start/${encodeURIComponent(opId)}`);
}

export async function getLabOperation(token: string, labId: string): Promise<StartOperation | null> {
  // Latest tracked operation for one lab (read-only recovery). 404 means
  // this session never tracked one (e.g. after a backend restart) — that
  // is a normal "nothing to resume" signal, not an error.
  try {
    return await getJson<StartOperation>(token, `/api/labs/${encodeURIComponent(labId)}/operation`);
  } catch (e) {
    if (e instanceof Error && e.message.includes('→ 404:')) return null;
    throw e;
  }
}

export async function postStopLab(
  token: string,
  labId: string,
): Promise<StartOperation & { created: boolean }> {
  return postJson(token, `/api/labs/${encodeURIComponent(labId)}/stop`, {});
}
