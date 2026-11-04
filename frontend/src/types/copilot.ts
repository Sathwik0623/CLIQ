/**
 * CLIQ AI Copilot TypeScript Types (Phase V).
 */

export type ChatRole = 'user' | 'assistant' | 'developer' | 'system' | 'tool';

export type ContextChipType =
  | 'device'
  | 'active_device'
  | 'all_devices'
  | 'interfaces'
  | 'routes'
  | 'config'
  | 'neighbors'
  | 'terminal_history'
  | 'file';

export interface ContextChip {
  id: string;
  type: ContextChipType;
  label: string;
  deviceId?: string;
  detail?: string;
}

export interface Attachment {
  id: string;
  filename: string;
  size_bytes: number;
  extension: string;
  line_count: number;
  conversation_id: string;
  text_content?: string;
}

export interface StoredMessage {
  id: string;
  conversation_id: string;
  role: ChatRole | string;
  content: string;
  created_at: number;
  targetDeviceId?: string;
  metadata?: {
    target_device_id?: string;
    context_chips?: ContextChip[];
    attachment_ids?: string[];
    executed_commands?: Array<{ device_id: string; device_label: string; command: string; success: boolean; error_code: string }>;
    rejected_commands?: Array<Record<string, unknown>>;
    tool_results?: Array<{
      success: boolean; device_id: string; device_label: string; lab_id: string;
      platform: string; command: string; send_command: string; stdout: string;
      stderr: string; exit_status: number; error: string; error_code: string;
      latency_ms: number; truncated: boolean; approval_required: boolean;
      ticket?: { ticket_id: string; lab_id: string; device_id: string; device_label: string; platform: string; command: string; created_at: number; expires_at: number };
    }>;
    config_plan_id?: string | null;
    model?: string;
    provider?: string;
    token_usage?: {
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
    };
  };
}

export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  model: string;
  provider: string;
  metadata?: Record<string, unknown>;
  messages?: StoredMessage[];
}

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  description: string;
  context_window: number;
  supports_streaming: boolean;
  supports_tools: boolean;
  is_default: boolean;
  available?: boolean;
}

export interface AIProviderInfo {
  id: string;
  name: string;
  is_configured: boolean;
  default_model: string;
  is_default: boolean;
}

