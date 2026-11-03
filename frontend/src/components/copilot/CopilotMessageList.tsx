import React, { useEffect, useRef } from 'react';
import type { StoredMessage } from '../../types/copilot';
import type { Device } from '../../types/api';
import MarkdownView from './MarkdownView';
import {
  SparklesIcon,
  CopyIcon,
  CheckIcon,
  RefreshIcon,
  AtSignIcon,
} from './CopilotIcons';

interface CopilotMessageListProps {
  messages: StoredMessage[];
  isStreaming: boolean;
  streamingMessageId?: string | null;
  onSelectSuggestion?: (prompt: string) => void;
  onRetryLast?: () => void;
  activeModelName?: string;
  devices?: Device[];
  openDevices?: Device[];
  activeDeviceId?: string;
  focusedDeviceId?: string | null;
  deviceMetrics?: Record<string, { status: string; mode: string }>;
  onInsertCommand?: (deviceId: string, command: string) => void;
  onExplainCommand?: (deviceId: string, command: string) => void;
}

const STARTER_SUGGESTIONS = [
  {
    title: 'Explain networking concept',
    prompt: 'Explain the difference between a routing table and an ARP table, including how each is populated and used.',
    category: 'Concept',
  },
  {
    title: 'Troubleshoot device issue',
    prompt: 'Analyze the interface status of R1 and identify any down interfaces, duplex mismatches, or error counters.',
    category: 'Troubleshoot',
  },
  {
    title: 'Review configuration',
    prompt: 'Review the running configuration for security best practices, unused interfaces, and syntax consistency.',
    category: 'Configuration',
  },
  {
    title: 'Analyze connectivity',
    prompt: 'Compare interface configurations and routing paths between devices to identify potential connectivity issues.',
    category: 'Connectivity',
  },
];

export default function CopilotMessageList({
  messages,
  isStreaming,
  streamingMessageId,
  onSelectSuggestion,
  onRetryLast,
  activeModelName = 'muse-spark-1.3',
  devices = [],
  openDevices,
  activeDeviceId,
  focusedDeviceId,
  deviceMetrics,
  onInsertCommand,
  onExplainCommand,
}: CopilotMessageListProps) {
  const scrollBottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [copiedId, setCopiedId] = React.useState<string | null>(null);

  // Auto-scroll on new messages or during streaming
  useEffect(() => {
    if (scrollBottomRef.current) {
      scrollBottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, isStreaming]);

  const handleCopy = (id: string, text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-neutral-300 overflow-y-auto">
        <div className="w-12 h-12 rounded-2xl bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center text-cyan-400 mb-3 shadow-lg shadow-cyan-950/40">
          <SparklesIcon size={24} />
        </div>
        <h3 className="text-base font-semibold text-neutral-100 tracking-tight">
          CLIQ Network Intelligence Copilot
        </h3>
        <p className="text-xs text-neutral-400 max-w-sm mt-1 leading-relaxed">
          Ask questions, analyze routing anomalies, inspect running configs, or troubleshoot network topologies across your connected devices.
        </p>

        {/* Suggestion Cards */}
        {onSelectSuggestion && (
          <div className="grid grid-cols-1 gap-2 w-full max-w-md mt-6">
            {STARTER_SUGGESTIONS.map((item) => (
              <button
                key={item.title}
                onClick={() => onSelectSuggestion(item.prompt)}
                className="flex flex-col items-start p-3 rounded-lg border border-neutral-800 bg-neutral-900/60 hover:bg-neutral-800/80 hover:border-cyan-500/40 transition text-left group"
              >
                <div className="flex items-center justify-between w-full">
                  <span className="text-xs font-medium text-neutral-200 group-hover:text-cyan-300 transition">
                    {item.title}
                  </span>
                  <span className="text-[10px] text-neutral-500 font-mono bg-neutral-950 px-1.5 py-0.5 rounded border border-neutral-800">
                    {item.category}
                  </span>
                </div>
                <p className="text-[11px] text-neutral-400 mt-1 line-clamp-2">
                  {item.prompt}
                </p>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="flex-1 overflow-y-auto p-4 space-y-4 scroll-smooth"
    >
      {messages.map((msg, index) => {
        const isUser = msg.role === 'user';
        const isCurrentlyStreaming = isStreaming && msg.id === streamingMessageId;
        const isLastAssistant =
          !isUser &&
          index === messages.length - 1 &&
          !isStreaming;

        return (
          <div
            key={msg.id || index}
            className={`flex flex-col ${isUser ? 'items-end' : 'items-start'} max-w-full`}
          >
            {/* Header / Avatar */}
            <div className="flex items-center gap-2 mb-1 px-1">
              {isUser ? (
                <span className="text-[11px] font-semibold text-neutral-400">
                  You
                </span>
              ) : (
                <div className="flex items-center gap-1.5">
                  <div className="w-4 h-4 rounded-full bg-cyan-500/20 text-cyan-400 flex items-center justify-center border border-cyan-500/40">
                    <SparklesIcon size={10} />
                  </div>
                  <span className="text-[11px] font-semibold text-cyan-300">
                    CLIQ Copilot
                  </span>
                </div>
              )}
            </div>

            {/* Bubble Container */}
            <div
              className={`rounded-xl px-4 py-3 text-xs leading-relaxed max-w-[94%] transition shadow-sm ${
                isUser
                  ? 'bg-neutral-800 text-neutral-100 border border-neutral-700/80'
                  : 'bg-neutral-900/90 text-neutral-200 border border-neutral-800 w-full'
              }`}
            >
              {/* Context chips if stored on message */}
              {msg.metadata?.context_chips && msg.metadata.context_chips.length > 0 && (
                <div className="flex flex-wrap gap-1 mb-2 pb-2 border-b border-neutral-700/60">
                  {msg.metadata.context_chips.map((chip) => (
                    <span
                      key={chip.id}
                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-cyan-950/60 border border-cyan-800 text-[10px] font-mono text-cyan-300"
                    >
                      <AtSignIcon size={10} />
                      {chip.label}
                    </span>
                  ))}
                </div>
              )}

              {/* Message Content */}
              {isUser ? (
                <div className="whitespace-pre-wrap font-sans break-words">
                  {msg.content}
                </div>
              ) : (
                <div>
                  <MarkdownView
                    content={msg.content}
                    devices={devices}
                    openDevices={openDevices}
                    activeDeviceId={activeDeviceId}
                    focusedDeviceId={focusedDeviceId}
                    deviceMetrics={deviceMetrics}
                    onInsertCommand={onInsertCommand}
                    onExplainCommand={onExplainCommand}
                  />
                  {isCurrentlyStreaming && (
                    <span className="inline-block w-2 h-4 ml-1 bg-cyan-400 animate-pulse rounded-sm align-middle" />
                  )}
                </div>
              )}

              {/* Assistant Message Actions (Token counts hidden from UI per specification) */}
              {!isUser && !isCurrentlyStreaming && (
                <div className="flex items-center justify-between mt-3 pt-2 border-t border-neutral-800/80 text-[10px] text-neutral-500">
                  <div className="flex items-center gap-2">
                    <span className="text-neutral-500 font-mono text-[9.5px]">
                      Advisory guidance
                    </span>
                  </div>

                  <div className="flex items-center gap-1.5">
                    <button
                      onClick={() => handleCopy(msg.id, msg.content)}
                      className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-neutral-800 hover:text-neutral-300 transition"
                      title="Copy message to clipboard"
                    >
                      {copiedId === msg.id ? (
                        <>
                          <CheckIcon size={11} className="text-emerald-400" />
                          <span className="text-emerald-400">Copied</span>
                        </>
                      ) : (
                        <>
                          <CopyIcon size={11} />
                          <span>Copy</span>
                        </>
                      )}
                    </button>

                    {isLastAssistant && onRetryLast && (
                      <button
                        onClick={onRetryLast}
                        className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-neutral-800 hover:text-neutral-300 transition"
                        title="Regenerate this response"
                      >
                        <RefreshIcon size={11} />
                        <span>Retry</span>
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}

      <div ref={scrollBottomRef} />
    </div>
  );
}
