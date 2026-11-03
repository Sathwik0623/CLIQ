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
): Promise<Attachment> {
  const res = await fetch(`${BASE_URL}/api/copilot/upload`, {
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
  signal?: AbortSignal;
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

