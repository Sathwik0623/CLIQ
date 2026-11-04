/**
 * CLIQ AI Copilot API Client (Phase V).
 */

import type {
  AIProviderInfo,
  Attachment,
  ContextChip,
  Conversation,
  ModelInfo,
} from '../types/copilot';

const BASE_URL = '';

export async function fetchCopilotProviders(): Promise<{
  providers: AIProviderInfo[];
  models: ModelInfo[];
}> {
  const res = await fetch(`${BASE_URL}/api/copilot/providers`);
  if (!res.ok) throw new Error(`Failed to fetch providers: ${res.statusText}`);
  return res.json();
}

export async function fetchConversations(token?: string, query?: string): Promise<Conversation[]> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  if (query) params.set('query', query);
  const url = `${BASE_URL}/api/copilot/conversations?${params.toString()}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to list conversations: ${res.statusText}`);
  const data = await res.json();
  return data.conversations ?? [];
}

export async function createConversation(
  title: string = 'New Conversation',
  model: string = 'muse-spark-1.3',
  provider: string = 'meta',
  token?: string,
): Promise<Conversation> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const url = `${BASE_URL}/api/copilot/conversations?${params.toString()}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, model, provider }),
  });
  if (!res.ok) throw new Error(`Failed to create conversation: ${res.statusText}`);
  return res.json();
}

export async function fetchConversation(id: string, token?: string): Promise<Conversation> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const url = `${BASE_URL}/api/copilot/conversations/${encodeURIComponent(id)}?${params.toString()}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch conversation: ${res.statusText}`);
  return res.json();
}

export async function renameConversation(id: string, title: string, token?: string): Promise<void> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const url = `${BASE_URL}/api/copilot/conversations/${encodeURIComponent(id)}?${params.toString()}`;
  const res = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(`Failed to rename conversation: ${res.statusText}`);
}

export async function deleteConversation(id: string, token?: string): Promise<void> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const url = `${BASE_URL}/api/copilot/conversations/${encodeURIComponent(id)}?${params.toString()}`;
  const res = await fetch(url, { method: 'DELETE' });
  if (!res.ok) throw new Error(`Failed to delete conversation: ${res.statusText}`);
}

export async function uploadAttachment(
  conversationId: string,
  filename: string,
  content: string,
  isBase64: boolean = false,
  token?: string,
): Promise<Attachment> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/upload?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename,
      content,
      conversation_id: conversationId,
      is_base64: isBase64,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Upload failed: ${res.statusText}`);
  }
  const data = await res.json();
  return data.attachment;
}

export async function fetchContextDevices(token?: string): Promise<Array<{ id: string; label: string; node_type: string; status: string }>> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/context/devices?${params.toString()}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.devices ?? [];
}

export interface StreamChatOptions {
  model?: string;
  provider?: string;
  contextChips?: ContextChip[];
  attachments?: Attachment[];
  token?: string;
  onChunk: (delta: string) => void;
  onDone: (metadata?: any) => void;
  onError: (errorMsg: string) => void;
  onRename?: (title: string) => void;
  onToolEvent?: (event: ToolStreamEvent) => void;
  signal?: AbortSignal;
}

// ── Autonomous device execution (M1.2) ─────────────────────────────────────

export interface ExecApprovalTicket {
  ticket_id: string;
  lab_id: string;
  device_id: string;
  device_label: string;
  platform: string;
  command: string;
  created_at: number;
  expires_at: number;
}

export interface ExecResultPayload {
  success: boolean;
  device_id: string;
  device_label: string;
  lab_id: string;
  platform: string;
  command: string;
  send_command: string;
  stdout: string;
  stderr: string;
  exit_status: number;
  error: string;
  error_code: string;
  latency_ms: number;
  truncated: boolean;
  approval_required: boolean;
  ticket?: ExecApprovalTicket;
}

export interface ToolCallEvent {
  type: 'tool_call';
  tool: string;
  lab_id: string;
  device_id: string;
  device_label: string;
  command: string;
}

export interface ToolResultEvent {
  type: 'tool_result';
  tool: string;
  result: ExecResultPayload;
}

export interface ApprovalRequiredEvent {
  type: 'approval_required';
  lab_id: string;
  device_id: string;
  device_label: string;
  command: string;
  ticket: ExecApprovalTicket;
}

export interface ToolRejectedEvent {
  type: 'tool_rejected';
  tool: string;
  lab_id: string;
  device_id: string;
  device_label: string;
  command: string;
  error_code: string;
  message: string;
}

export type ToolStreamEvent = ToolCallEvent | ToolResultEvent | ApprovalRequiredEvent | ToolRejectedEvent | ConfigPlanEvent;

// ── Autonomous network configuration (M1.4) ────────────────────────────────

export interface PlanStepView {
  step_id: string;
  device_id: string;
  command: string;
  purpose: string;
  read_only: boolean;
  depends_on: string[];
  critical: boolean;
  target: Record<string, any>;
}

export interface DevicePlanView {
  device_id: string;
  device_label: string;
  platform: string;
  role: string;
  node_type: string;
  steps: PlanStepView[];
  unverified: boolean;
}

export interface PlanQuestionView {
  qid: string;
  kind: string;
  message: string;
  blocking: boolean;
  device_id: string;
  options: string[];
  context: Record<string, any>;
  resolved: boolean;
  resolution: string;
}

export interface ConnectivityView {
  exp_id: string;
  src_device: string;
  dst: string;
  dst_ip: string;
  kind: string;
}

export interface ConfigPlanView {
  plan_id: string;
  lab_id: string;
  lab_title: string;
  title: string;
  summary: string;
  version: number;
  fingerprint: string;
  status: string;
  devices: DevicePlanView[];
  questions: PlanQuestionView[];
  connectivity: ConnectivityView[];
  assumptions: string[];
  evidence: Record<string, any>;
  created_at: number;
  updated_at: number;
  blocking_open: string[];
}

export interface ConfigPlanEvent {
  type: 'config_plan';
  plan: ConfigPlanView;
}

export interface StepResultView {
  step_id: string;
  device_id: string;
  device_label: string;
  command: string;
  send_command: string;
  purpose: string;
  status: string;
  stdout: string;
  stderr: string;
  exit_status: number;
  error: string;
  error_code: string;
  latency_ms: number;
}

export interface PlanExecutionView {
  plan_id: string;
  version: number;
  fingerprint: string;
  lab_id: string;
  status: string;
  steps: StepResultView[];
  started_at: number;
  finished_at: number;
  summary: string;
}

export type ConfigExecEvent =
  | { type: 'plan_started'; plan_id: string; version: number; lab_id: string; total_steps: number }
  | { type: 'step_started'; step_id: string; device_id: string; device_label: string; command: string; purpose: string }
  | { type: 'step_result'; step: StepResultView }
  | { type: 'plan_finished'; execution: PlanExecutionView }
  | { type: 'plan_rejected'; error_code: string; message: string }
  | { type: 'error'; message: string };

export interface BaselineCheckView {
  check_id: string;
  device_id: string;
  device_label: string;
  description: string;
  status: string;
  detail: string;
  source_command: string;
}

export interface BaselineRecordView {
  baseline_id: string;
  plan_id: string;
  lab_id: string;
  collected_at: number;
  devices: Record<string, { device_id: string; device_label: string; platform: string; outputs: Record<string, { stdout: string; truncated: boolean; exit_status: number; error: string; error_code: string; latency_ms: number; success: boolean }> }>;
  checks: BaselineCheckView[];
  connectivity: Array<{ exp_id: string; src_device: string; dst: string; dst_ip: string; status: string; detail: string; stdout?: string }>;
  verdict: string;
  explanation: string;
  audit: Record<string, any>;
}

export interface ToolRun {
  key: string;
  lab_id: string;
  device_id: string;
  device_label: string;
  command: string;
  status: 'running' | 'success' | 'error' | 'approval_required' | 'approved_running' | 'rejected' | 'invalid';
  result?: ExecResultPayload;
  ticket?: ExecApprovalTicket;
  note?: string;
}

export function toolRunKey(deviceId: string, command: string): string {
  return `${deviceId}::${command}`;
}

export async function streamChatMessage(
  conversationId: string,
  content: string,
  options: StreamChatOptions,
): Promise<void> {
  const params = new URLSearchParams();
  if (options.token) params.set('token', options.token);

  const url = `${BASE_URL}/api/copilot/conversations/${encodeURIComponent(conversationId)}/messages?${params.toString()}`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({
        content,
        model: options.model,
        provider: options.provider || 'meta',
        context_chips: options.contextChips || [],
        attachments: options.attachments || [],
      }),
      signal: options.signal,
    });

    if (!response.ok) {
      const errText = await response.text();
      options.onError(`Failed to stream message (${response.status}): ${errText}`);
      return;
    }

    if (!response.body) {
      options.onError('No response body received from server.');
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const dataStr = trimmed.slice(5).trim();
        if (dataStr === '[DONE]') {
          options.onDone();
          return;
        }

        try {
          const event = JSON.parse(dataStr);
          if (event.type === 'chunk' && event.delta) {
            options.onChunk(event.delta);
          } else if (
            (event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'approval_required' || event.type === 'tool_rejected' || event.type === 'config_plan') &&
            options.onToolEvent
          ) {
            options.onToolEvent(event as ToolStreamEvent);
          } else if (event.type === 'rename' && event.title && options.onRename) {
            options.onRename(event.title);
          } else if (event.type === 'error') {
            options.onError(event.message || 'Stream error occurred.');
            return;
          } else if (event.type === 'done') {
            options.onDone(event);
            return;
          }
        } catch {
          // Ignore partial or non-json lines
        }
      }
    }
    options.onDone();
  } catch (err: any) {
    if (err.name === 'AbortError') {
      options.onDone();
      return;
    }
    options.onError(err.message || 'Network error while streaming.');
  }
}

export async function confirmExecTicket(
  ticketId: string,
  token?: string,
): Promise<{ approval_token: string; expires_at: number; lab_id: string; device_id: string; command: string }> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/exec/confirm?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_id: ticketId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Approval failed: ${res.statusText}`);
  }
  return res.json();
}

export interface DirectExecArgs {
  lab_id: string;
  device_id: string;
  command: string;
  timeout?: number;
  approval_token?: string;
}

export async function fetchConfigPlan(planId: string, token?: string): Promise<ConfigPlanView> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/plan/${encodeURIComponent(planId)}?${params.toString()}`);
  if (!res.ok) throw new Error(`Failed to fetch plan: ${res.statusText}`);
  return res.json();
}

export async function answerPlanQuestions(planId: string, answers: Record<string, string>, token?: string): Promise<ConfigPlanView> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/plan/${encodeURIComponent(planId)}/answer?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to submit answers: ${res.statusText}`);
  }
  return res.json();
}

export async function prepareConfigPlan(planId: string, token?: string): Promise<{ approval_required: boolean; ticket: { ticket_id: string; plan_id: string; version: number; fingerprint: string; lab_id: string; expires_at: number } }> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/prepare?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Prepare failed: ${res.statusText}`);
  }
  return res.json();
}

export async function approveConfigPlan(ticketId: string, token?: string): Promise<{ approval_token: string; plan_id: string; version: number; fingerprint: string; expires_at: number }> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/approve?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket_id: ticketId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Approve failed: ${res.statusText}`);
  }
  return res.json();
}

export async function executePlanStream(
  planId: string,
  approvalToken: string,
  onEvent: (event: ConfigExecEvent) => void,
  token?: string,
  timeout: number = 30,
): Promise<void> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/execute?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ plan_id: planId, approval_token: approvalToken, timeout }),
  });
  if (!res.ok || !res.body) {
    onEvent({ type: 'error', message: `Execute failed: ${res.statusText}` });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      const dataStr = trimmed.slice(5).trim();
      if (dataStr === '[DONE]') return;
      try {
        onEvent(JSON.parse(dataStr) as ConfigExecEvent);
      } catch {
        // ignore partial lines
      }
    }
  }
}

export async function collectPlanBaseline(planId: string, token?: string): Promise<BaselineRecordView> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/config/baseline?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Baseline failed: ${res.statusText}`);
  }
  return res.json();
}

export async function runExecDirect(args: DirectExecArgs, token?: string): Promise<ExecResultPayload> {
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  const res = await fetch(`${BASE_URL}/api/copilot/exec?${params.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  });
  // The backend always returns the structured result body (success or not).
  const body = await res.json().catch(() => null);
  if (body && typeof body === 'object' && 'success' in body) {
    return body as ExecResultPayload;
  }
  throw new Error(`Execution request failed: ${res.status} ${res.statusText}`);
}

