import React, { useRef, useState, useEffect } from 'react';
import type { Attachment, ContextChip } from '../../types/copilot';
import {
  SendIcon,
  StopIcon,
  PaperclipIcon,
  AtSignIcon,
  CloseIcon,
  FileTextIcon,
} from './CopilotIcons';

interface CopilotComposerProps {
  input: string;
  setInput: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  isStreaming: boolean;
  contextChips: ContextChip[];
  onRemoveChip: (chipId: string) => void;
  attachments: Attachment[];
  onRemoveAttachment: (attId: string) => void;
  onFileUpload: (files: FileList) => Promise<void>;
  isUploading: boolean;
  onOpenContextModal: () => void;
  activeModelName?: string;
  disabled?: boolean;
}

export default function CopilotComposer({
  input,
  setInput,
  onSend,
  onStop,
  isStreaming,
  contextChips,
  onRemoveChip,
  attachments,
  onRemoveAttachment,
  onFileUpload,
  isUploading,
  onOpenContextModal,
  activeModelName = 'muse-spark-1.3',
  disabled = false,
}: CopilotComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isFocused, setIsFocused] = useState(false);

  // Auto-resize textarea based on content
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      const scrollHeight = textareaRef.current.scrollHeight;
      textareaRef.current.style.height = `${Math.min(Math.max(scrollHeight, 40), 160)}px`;
    }
  }, [input]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!isStreaming && (input.trim() || attachments.length > 0) && !disabled) {
        onSend();
      }
    }
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      await onFileUpload(e.target.files);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const canSend = (input.trim().length > 0 || attachments.length > 0) && !disabled;

  return (
    <div className="flex flex-col border-t border-neutral-800 bg-neutral-900/90 backdrop-blur-sm p-3 gap-2">
      {/* Removable Context Chips */}
      {contextChips.length > 0 && (
        <div className="flex flex-wrap gap-1.5 items-center max-h-20 overflow-y-auto pb-1">
          <span className="text-[10px] font-medium uppercase tracking-wider text-neutral-400 mr-1">
            Context:
          </span>
          {contextChips.map((chip) => (
            <span
              key={chip.id}
              className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-cyan-950/80 border border-cyan-700/60 text-cyan-200 text-[11px] font-mono shadow-sm"
            >
              <span>{chip.label}</span>
              <button
                type="button"
                onClick={() => onRemoveChip(chip.id)}
                className="hover:text-cyan-100 p-0.5 rounded hover:bg-cyan-800/50 transition"
                title="Remove context chip"
              >
                <CloseIcon size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Removable Attachment File Pills */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5 items-center max-h-20 overflow-y-auto pb-1">
          <span className="text-[10px] font-medium uppercase tracking-wider text-neutral-400 mr-1">
            Attached:
          </span>
          {attachments.map((att) => (
            <span
              key={att.id}
              className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-neutral-800 border border-neutral-700 text-neutral-200 text-[11px] font-mono shadow-sm"
            >
              <FileTextIcon size={12} className="text-cyan-400" />
              <span className="truncate max-w-[140px]" title={att.filename}>
                {att.filename}
              </span>
              <span className="text-[9px] text-neutral-400">
                ({formatFileSize(att.size_bytes)})
              </span>
              <button
                type="button"
                onClick={() => onRemoveAttachment(att.id)}
                className="hover:text-neutral-100 p-0.5 rounded hover:bg-neutral-700 transition"
                title="Remove attachment"
              >
                <CloseIcon size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Input container */}
      <div
        className={`relative flex flex-col rounded-xl border bg-neutral-950/80 transition shadow-inner ${
          isFocused
            ? 'border-cyan-500/70 ring-1 ring-cyan-500/30'
            : 'border-neutral-800 hover:border-neutral-700'
        }`}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setIsFocused(false)}
          onKeyDown={handleKeyDown}
          placeholder="Ask CLIQ Copilot about network topology, configs, routing, or device issues... (Enter to send, Shift+Enter for new line)"
          rows={1}
          disabled={disabled || isStreaming}
          className="w-full resize-none bg-transparent px-3 py-2.5 text-xs text-neutral-100 placeholder-neutral-500 focus:outline-none disabled:opacity-50 leading-relaxed font-sans"
        />

        {/* Action Toolbar */}
        <div className="flex items-center justify-between px-2.5 py-1.5 border-t border-neutral-900 bg-neutral-950/40">
          <div className="flex items-center gap-1">
            {/* Add Context Button */}
            <button
              type="button"
              onClick={onOpenContextModal}
              disabled={disabled || isStreaming}
              className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-neutral-300 hover:text-cyan-300 hover:bg-neutral-800/80 transition border border-transparent hover:border-neutral-700"
              title="Attach live device configs, routing tables, interfaces or terminal history"
            >
              <AtSignIcon size={13} className="text-cyan-400" />
              <span>Add Context</span>
            </button>

            {/* Attach File Button */}
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled || isStreaming || isUploading}
              className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-neutral-300 hover:text-cyan-300 hover:bg-neutral-800/80 transition border border-transparent hover:border-neutral-700"
              title="Attach configuration, log, or script file (.cfg, .log, .py, .yaml, .json, .pdf, .txt)"
            >
              <PaperclipIcon size={13} className="text-neutral-400" />
              <span>{isUploading ? 'Uploading...' : 'Attach'}</span>
            </button>

            <input
              type="file"
              ref={fileInputRef}
              onChange={handleFileChange}
              multiple
              accept=".txt,.log,.py,.yaml,.yml,.json,.xml,.cfg,.conf,.csv,.pdf,.md"
              className="hidden"
            />
          </div>

          <div className="flex items-center gap-2">

            {/* Send or Stop button */}
            {isStreaming ? (
              <button
                type="button"
                onClick={onStop}
                className="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition shadow"
                title="Stop generation"
              >
                <StopIcon size={12} />
                <span>Stop</span>
              </button>
            ) : (
              <button
                type="button"
                onClick={onSend}
                disabled={!canSend}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium transition shadow ${
                  canSend
                    ? 'bg-cyan-600 hover:bg-cyan-500 text-white cursor-pointer'
                    : 'bg-neutral-800 text-neutral-500 cursor-not-allowed opacity-60'
                }`}
                title="Send query to CLIQ Copilot (Enter)"
              >
                <SendIcon size={12} />
                <span>Send</span>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

