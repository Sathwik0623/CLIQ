import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import type {
  Conversation,
  StoredMessage,
  ContextChip,
  Attachment,
  ModelInfo,
  AIProviderInfo,
} from '../../types/copilot';
import type { Device } from '../../types/api';
import {
  fetchCopilotProviders,
  fetchConversations,
  createConversation,
  fetchConversation,
  renameConversation,
  deleteConversation,
  uploadAttachment,
  streamChatMessage,
} from '../../lib/copilotApi';
import CopilotMessageList from './CopilotMessageList';
import CopilotComposer from './CopilotComposer';
import AddContextModal from './AddContextModal';
import {
  SparklesIcon,
  PlusIcon,
  CloseIcon,
  SearchIcon,
  TrashIcon,
  EditIcon,
  CheckIcon,
  HistoryIcon,
} from './CopilotIcons';

interface CopilotPanelProps {
  isOpen: boolean;
  onClose: () => void;
  devices: Device[];
  openDevices?: Device[];
  activeDeviceId?: string;
  focusedDeviceId?: string | null;
  deviceMetrics?: Record<string, { status: string; mode: string }>;
  token?: string;
  onInsertCommand?: (deviceId: string, command: string) => void;
  onExplainCommand?: (deviceId: string, command: string) => void;
  onRefitTerminals?: () => void;
}

export default function CopilotPanel({
  isOpen,
  onClose,
  devices = [],
  openDevices,
  activeDeviceId,
  focusedDeviceId,
  deviceMetrics,
  token,
  onInsertCommand,
  onExplainCommand,
  onRefitTerminals,
}: CopilotPanelProps) {
  // Resizable Drawer Width
  const [panelWidth, setPanelWidth] = useState<number>(440);
  const [isDragging, setIsDragging] = useState(false);
  const isDraggingRef = useRef(false);

  // AI Catalog
  const [providers, setProviders] = useState<AIProviderInfo[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('muse-spark-1.3');

  // Conversations & Messages
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [searchFilter, setSearchFilter] = useState('');
  const [isHistoryOpen, setIsHistoryOpen] = useState(false);

  // Composer State
  const [input, setInput] = useState('');
  const [contextChips, setContextChips] = useState<ContextChip[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingMsgId, setStreamingMsgId] = useState<string | null>(null);
  const [isContextModalOpen, setIsContextModalOpen] = useState(false);

  // Rename modal / inline edit state
  const [editingConvId, setEditingConvId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');

  // Abort controller ref
  const abortControllerRef = useRef<AbortController | null>(null);
  const skipNextLoadRef = useRef<string | null>(null);

  // 1. Initial Load: Providers, Models & Conversations
  useEffect(() => {
    let mounted = true;
    const init = async () => {
      try {
        const { providers: provs, models: mods } = await fetchCopilotProviders();
        if (!mounted) return;
        setProviders(provs);
        setModels(mods);
        if (mods.length > 0) {
          const availableMods = mods.filter((m) => m.available !== false);
          const defaultMod = availableMods.find((m) => m.is_default) || availableMods[0] || mods[0];
          setSelectedModel(defaultMod.id);
        }

        const convs = await fetchConversations(token);
        if (!mounted) return;
        setConversations(convs);

        if (convs.length > 0) {
          setActiveConvId(convs[0].id);
        } else {
          // Auto-create initial conversation if none exist
          const newConv = await createConversation('CLIQ Copilot Session', undefined, undefined, token);
          if (!mounted) return;
          setConversations([newConv]);
          setActiveConvId(newConv.id);
        }
      } catch (err) {
        console.error('Failed to initialize Copilot:', err);
      }
    };

    if (isOpen) {
      init();
    }

    return () => {
      mounted = false;
    };
  }, [isOpen, token]);

  // 2. Load conversation messages when active conversation changes
  const loadConversation = useCallback(async (convId: string) => {
    if (skipNextLoadRef.current === convId) {
      skipNextLoadRef.current = null;
      return;
    }
    try {
      const conv = await fetchConversation(convId, token);
      if (skipNextLoadRef.current === convId) {
        skipNextLoadRef.current = null;
        return;
      }
      setMessages(conv.messages || []);
      if (conv.model) {
        setSelectedModel((prev) => {
          const validModels = ['muse-spark-1.3', 'muse-wonder-1.0', 'muse-spark-1.2'];
          if (validModels.includes(conv.model)) {
            return conv.model;
          }
          return prev && validModels.includes(prev) ? prev : 'muse-spark-1.3';
        });
      }
    } catch (err) {
      console.error('Failed to fetch conversation details:', err);
    }
  }, [token]);

  useEffect(() => {
    if (activeConvId) {
      loadConversation(activeConvId);
    } else {
      setMessages([]);
    }
  }, [activeConvId, loadConversation]);

  // 3. New Conversation
  const handleNewConversation = async () => {
    try {
      const newConv = await createConversation(
        'New Conversation',
        selectedModel,
        'meta',
        token
      );
      setConversations((prev) => [newConv, ...prev]);
      setActiveConvId(newConv.id);
      setMessages([]);
      setAttachments([]);
      // Keep history sidebar state intact; do not force close if user wants to view list
    } catch (err) {
      console.error('Failed to create new conversation:', err);
    }
  };

  // 4. Rename Conversation
  const handleSaveRename = async (convId: string) => {
    if (!editingTitle.trim()) return;
    try {
      await renameConversation(convId, editingTitle.trim(), token);
      setConversations((prev) =>
        prev.map((c) => (c.id === convId ? { ...c, title: editingTitle.trim() } : c))
      );
      setEditingConvId(null);
      setEditingTitle('');
    } catch (err) {
      console.error('Failed to rename conversation:', err);
    }
  };

  // 5. Delete Conversation
  const handleDeleteConversation = async (convId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm('Are you sure you want to delete this conversation?')) return;

    try {
      await deleteConversation(convId, token);
      const remaining = conversations.filter((c) => c.id !== convId);
      setConversations(remaining);

      if (activeConvId === convId) {
        if (remaining.length > 0) {
          setActiveConvId(remaining[0].id);
        } else {
          setActiveConvId(null);
          setMessages([]);
        }
      }
    } catch (err) {
      console.error('Failed to delete conversation:', err);
    }
  };

  // 6. File Upload
  const handleFileUpload = async (files: FileList) => {
    if (!activeConvId) return;
    setIsUploading(true);

    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const isPdf = file.name.toLowerCase().endsWith('.pdf');

        if (isPdf) {
          // Read as Base64 data URL
          const reader = new FileReader();
          reader.readAsDataURL(file);
          await new Promise<void>((resolve, reject) => {
            reader.onload = async () => {
              try {
                const base64Content = (reader.result as string).split(',')[1] || '';
                const att = await uploadAttachment(activeConvId, file.name, base64Content, true);
                setAttachments((prev) => [...prev, att]);
                resolve();
              } catch (err) {
                reject(err);
              }
            };
            reader.onerror = () => reject(reader.error);
          });
        } else {
          // Read as text
          const text = await file.text();
          const att = await uploadAttachment(activeConvId, file.name, text, false);
          setAttachments((prev) => [...prev, att]);
        }
      }
    } catch (err: any) {
      alert(`File upload failed: ${err.message || 'Unknown error'}`);
    } finally {
      setIsUploading(false);
    }
  };

  // 7. Context Chips
  const handleAddChip = (chip: ContextChip) => {
    setContextChips((prev) => {
      if (prev.some((c) => c.id === chip.id)) return prev;
      return [...prev, chip];
    });
  };

  const handleAddChips = (newChips: ContextChip[]) => {
    setContextChips((prev) => {
      const existing = new Set(prev.map((c) => c.id));
      const filtered = newChips.filter((c) => !existing.has(c.id));
      return [...prev, ...filtered];
    });
  };

  const handleRemoveChip = (chipId: string) => {
    setContextChips((prev) => prev.filter((c) => c.id !== chipId));
  };

  const handleRemoveAttachment = (attId: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== attId));
  };

  // 8. Send Message with SSE Streaming
  const handleSendMessage = async (userPrompt?: string) => {
    const textToSend = userPrompt !== undefined ? userPrompt : input;
    if ((!textToSend.trim() && attachments.length === 0) || isStreaming) return;

    let convId = activeConvId;
    if (!convId) {
      const newConv = await createConversation(
        textToSend.slice(0, 30) || 'New Conversation',
        selectedModel,
        'meta',
        token
      );
      setConversations((prev) => [newConv, ...prev]);
      skipNextLoadRef.current = newConv.id;
      setActiveConvId(newConv.id);
      convId = newConv.id;
    }

    const currentChips = [...contextChips];
    const currentAtts = [...attachments];

    // Resolve intended target device ID for this turn:
    // 1. Explicit device context chip
    // 2. Mention of device label/id in user text
    // 3. Current active/focused device at time of message creation
    let intendedTargetId: string | undefined;
    const deviceChip = currentChips.find((c) => c.type === 'device' && c.deviceId);
    if (deviceChip?.deviceId) {
      intendedTargetId = deviceChip.deviceId;
    } else {
      for (const d of devices) {
        const lbl = d.label || d.id;
        const esc = lbl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        if (new RegExp(`\\b${esc}\\b`, 'i').test(textToSend)) {
          intendedTargetId = d.id;
          break;
        }
      }
      if (!intendedTargetId) {
        intendedTargetId = focusedDeviceId || activeDeviceId || undefined;
      }
    }

    // Create optimistic user message
    const userMsgId = `user_${Date.now()}`;
    const userMsg: StoredMessage = {
      id: userMsgId,
      conversation_id: convId,
      role: 'user',
      content: textToSend,
      created_at: Math.floor(Date.now() / 1000),
      targetDeviceId: intendedTargetId,
      metadata: {
        target_device_id: intendedTargetId,
        context_chips: currentChips,
        attachment_ids: currentAtts.map((a) => a.id),
      },
    };

    // Create optimistic assistant message placeholder
    const assistantMsgId = `asst_${Date.now()}`;
    const assistantMsg: StoredMessage = {
      id: assistantMsgId,
      conversation_id: convId,
      role: 'assistant',
      content: '',
      created_at: Math.floor(Date.now() / 1000),
      targetDeviceId: intendedTargetId,
      metadata: {
        target_device_id: intendedTargetId,
        model: selectedModel,
        provider: 'meta',
      },
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput('');
    setAttachments([]);
    setIsStreaming(true);
    setStreamingMsgId(assistantMsgId);

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    try {
      await streamChatMessage(convId, textToSend, {
        model: selectedModel,
        provider: 'meta',
        token,
        contextChips: currentChips,
        attachments: currentAtts,
        signal: abortController.signal,
        onChunk: (delta: string) => {
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id === assistantMsgId) {
                const nextContent = m.content + delta;
                let devId = m.targetDeviceId;
                if (!devId) {
                  for (const d of devices) {
                    const lbl = d.label || d.id;
                    const esc = lbl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                    if (new RegExp(`\\b${esc}\\b`, 'i').test(nextContent)) {
                      devId = d.id;
                      break;
                    }
                  }
                }
                return { ...m, content: nextContent, targetDeviceId: devId };
              }
              return m;
            })
          );
        },
        onDone: (data: any) => {
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id === assistantMsgId) {
                return {
                  ...m,
                  id: data?.message_id || m.id,
                  metadata: {
                    ...m.metadata,
                    token_usage: data?.usage,
                  },
                };
              }
              return m;
            })
          );
          setIsStreaming(false);
          setStreamingMsgId(null);
          // Refresh conversation list for updated timestamp/title
          fetchConversations(token).then((list) => setConversations(list));
        },
        onError: (err: string) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? {
                    ...m,
                    content:
                      m.content +
                      `\n\n> [!WARNING]\n> **Generation Error**: ${err}`,
                  }
                : m
            )
          );
          setIsStreaming(false);
          setStreamingMsgId(null);
        },
      });
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        console.error('Streaming request error:', err);
        const detail = err instanceof Error ? err.message : String(err);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? {
                  ...m,
                  content:
                    m.content +
                    `\n\n> [!WARNING]\n> **Connection Error**: ${detail} \u2014 the response stream was interrupted before completing.`,
                }
              : m
          )
        );
      }
      setIsStreaming(false);
      setStreamingMsgId(null);
    }
  };

  // 9. Stop Streaming
  const handleStopStreaming = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
    setStreamingMsgId(null);
  };

  // 10. Retry Last Response
  const handleRetryLast = () => {
    if (messages.length < 2 || isStreaming) return;
    const lastUserIdx = [...messages].reverse().findIndex((m) => m.role === 'user');
    if (lastUserIdx === -1) return;

    const actualIdx = messages.length - 1 - lastUserIdx;
    const userMsg = messages[actualIdx];

    // Remove current assistant message
    setMessages((prev) => prev.slice(0, actualIdx + 1));
    handleSendMessage(userMsg.content);
  };

  // 11. Explain CLI Command Action (Sends follow-up query to conversation)
  const handleExplainCommand = (deviceId: string, command: string) => {
    const dev = devices.find((d) => d.id === deviceId);
    const devName = dev?.label || deviceId || 'the active device';
    const prompt = `Explain the following CLI command for ${devName}:\n\`\`\`cisco\n${command.trim()}\n\`\`\`\nDescribe the operational purpose, syntax flags, expected output, and safety considerations.`;
    handleSendMessage(prompt);
  };

  // 12. Drawer Resizing Drag Handlers
  const handleMouseDownResize = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    isDraggingRef.current = true;

    const handleMouseMove = (ev: MouseEvent) => {
      if (!isDraggingRef.current) return;
      const newWidth = window.innerWidth - ev.clientX;
      const clamped = Math.min(Math.max(newWidth, 360), Math.min(840, window.innerWidth * 0.6));
      setPanelWidth(clamped);
      if (onRefitTerminals) {
        onRefitTerminals();
      }
    };

    const handleMouseUp = () => {
      setIsDragging(false);
      isDraggingRef.current = false;
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
      if (onRefitTerminals) {
        onRefitTerminals();
      }
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
  };

  // Date-based conversation grouping for collapsible history sidebar
  const filteredConversations = conversations.filter((c) =>
    c.title.toLowerCase().includes(searchFilter.toLowerCase())
  );

  const groupedConversations = useMemo(() => {
    const groups: { [key: string]: Conversation[] } = {
      Today: [],
      Yesterday: [],
      'Previous 7 Days': [],
      Older: [],
    };

    const now = Date.now();
    const ONE_DAY = 24 * 60 * 60 * 1000;
    const startOfToday = new Date().setHours(0, 0, 0, 0);
    const startOfYesterday = startOfToday - ONE_DAY;
    const startOfSevenDaysAgo = startOfToday - 7 * ONE_DAY;

    for (const c of filteredConversations) {
      const timeMs = c.updated_at * 1000;
      if (timeMs >= startOfToday) {
        groups.Today.push(c);
      } else if (timeMs >= startOfYesterday) {
        groups.Yesterday.push(c);
      } else if (timeMs >= startOfSevenDaysAgo) {
        groups['Previous 7 Days'].push(c);
      } else {
        groups.Older.push(c);
      }
    }

    return groups;
  }, [filteredConversations]);

  const activeConv = conversations.find((c) => c.id === activeConvId);

  if (!isOpen) return null;

  return (
    <aside
      className="relative flex-shrink-0 flex flex-col h-full bg-neutral-950 border-l border-neutral-800 shadow-2xl z-30 select-none transition-[width] duration-75"
      style={{ width: `${panelWidth}px`, maxWidth: '100%' }}
    >
      {/* ── Drag Resize Rail on Left Border ───────────────────────────────── */}
      <div
        onMouseDown={handleMouseDownResize}
        className={`absolute left-0 top-0 bottom-0 w-1.5 -ml-1 cursor-col-resize hover:bg-cyan-500/60 transition-colors z-40 ${
          isDragging ? 'bg-cyan-500' : 'bg-transparent'
        }`}
        title="Drag to resize Copilot drawer"
      />

      {/* ── 1. Top Header Bar ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800 bg-neutral-900/80">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-5 h-5 rounded-md bg-cyan-500/20 text-cyan-400 flex items-center justify-center border border-cyan-500/30 shrink-0">
            <SparklesIcon size={12} />
          </div>
          <span className="font-semibold text-xs tracking-wide text-neutral-100 truncate">
            CLIQ Copilot
          </span>

          {/* Provider Status Indicator */}
          {(() => {
            const metaProv = providers.find((p) => p.id === 'meta') || providers[0];
            const isOnline = Boolean(metaProv?.is_configured) && models.some((m) => m.available !== false);
            return isOnline ? (
              <div className="flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-950/60 border border-emerald-800/60 text-[9.5px] font-mono text-emerald-300 shrink-0">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                <span>Online</span>
              </div>
            ) : (
              <div className="flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-neutral-900 border border-neutral-700/60 text-[9.5px] font-mono text-neutral-400 shrink-0">
                <span className="w-1.5 h-1.5 rounded-full bg-neutral-500" />
                <span>Offline</span>
              </div>
            );
          })()}

          {/* Model Selector */}
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            disabled={isStreaming}
            className="bg-neutral-900 border border-neutral-700/70 text-neutral-300 text-[10px] font-mono rounded px-1.5 py-0.5 focus:outline-none focus:border-cyan-500 cursor-pointer max-w-[125px] truncate"
            title="Active Meta Model API"
          >
            {models.map((m) => {
              const isUnavailable = m.available === false;
              return (
                <option
                  key={m.id}
                  value={m.id}
                  disabled={isUnavailable}
                  className={isUnavailable ? 'text-neutral-500 bg-neutral-950' : 'text-neutral-200 bg-neutral-900'}
                >
                  {m.name}{isUnavailable ? ' (Unavailable)' : ''}
                </option>
              );
            })}
          </select>
        </div>

        <div className="flex items-center gap-1 shrink-0 ml-1">
          {/* New Conversation Button */}
          <button
            onClick={handleNewConversation}
            className="p-1 rounded text-neutral-400 hover:text-cyan-300 hover:bg-neutral-800 transition"
            title="New Conversation (+)"
          >
            <PlusIcon size={14} />
          </button>

          {/* History Sidebar Toggle Button */}
          <button
            onClick={() => setIsHistoryOpen(!isHistoryOpen)}
            className={`flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium transition border ${
              isHistoryOpen
                ? 'bg-cyan-950/60 border-cyan-700 text-cyan-300'
                : 'bg-neutral-800 border-neutral-700 text-neutral-300 hover:bg-neutral-700'
            }`}
            title="Toggle Conversation History"
          >
            <HistoryIcon size={12} />
            <span>History</span>
          </button>

          {/* Close Panel Button */}
          <button
            onClick={onClose}
            className="p-1 rounded text-neutral-400 hover:text-neutral-100 hover:bg-neutral-800 transition ml-0.5"
            title="Close Copilot Panel"
          >
            <CloseIcon size={14} />
          </button>
        </div>
      </div>

      {/* ── 2. Main Body Container (Chat Area + Collapsible History Sidebar) ── */}
      <div className="flex flex-1 min-h-0 overflow-hidden relative">
        {/* Collapsible History Sidebar */}
        {isHistoryOpen && (
          <div className="w-56 shrink-0 border-r border-neutral-800 bg-neutral-950/95 flex flex-col h-full z-20 animate-in slide-in-from-left-2 duration-150">
            {/* History Header & Search */}
            <div className="p-2 border-b border-neutral-800 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
                  Conversations
                </span>
                <button
                  type="button"
                  onClick={handleNewConversation}
                  className="flex items-center gap-1 text-[10px] text-cyan-400 hover:underline"
                >
                  <PlusIcon size={11} />
                  <span>New</span>
                </button>
              </div>

              <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-neutral-900 border border-neutral-800">
                <SearchIcon size={11} className="text-neutral-500 shrink-0" />
                <input
                  type="text"
                  value={searchFilter}
                  onChange={(e) => setSearchFilter(e.target.value)}
                  placeholder="Filter chats..."
                  className="w-full bg-transparent text-[10.5px] text-neutral-200 placeholder-neutral-500 focus:outline-none"
                />
              </div>
            </div>

            {/* Date-Grouped Conversations */}
            <div className="flex-1 overflow-y-auto p-2 space-y-3">
              {Object.entries(groupedConversations).map(([groupTitle, convList]) => {
                if (convList.length === 0) return null;

                return (
                  <div key={groupTitle} className="space-y-1">
                    <div className="text-[9.5px] font-semibold uppercase tracking-wider text-neutral-400 px-1.5">
                      {groupTitle}
                    </div>

                    {convList.map((c) => {
                      const isActive = c.id === activeConvId;
                      const isEditing = c.id === editingConvId;

                      return (
                        <div
                          key={c.id}
                          onClick={() => {
                            if (!isEditing) {
                              setActiveConvId(c.id);
                            }
                          }}
                          className={`flex items-center justify-between p-1.5 rounded text-xs transition cursor-pointer group ${
                            isActive
                              ? 'bg-cyan-950/70 border border-cyan-800 text-cyan-200'
                              : 'hover:bg-neutral-900 text-neutral-300 border border-transparent'
                          }`}
                        >
                          {isEditing ? (
                            <div
                              className="flex items-center gap-1 w-full"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <input
                                type="text"
                                value={editingTitle}
                                onChange={(e) => setEditingTitle(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') handleSaveRename(c.id);
                                  if (e.key === 'Escape') setEditingConvId(null);
                                }}
                                className="bg-neutral-950 border border-neutral-700 px-1 py-0.5 rounded text-[10.5px] text-neutral-100 w-full focus:outline-none"
                                autoFocus
                              />
                              <button
                                type="button"
                                onClick={() => handleSaveRename(c.id)}
                                className="p-0.5 text-emerald-400 hover:bg-neutral-800 rounded"
                              >
                                <CheckIcon size={11} />
                              </button>
                            </div>
                          ) : (
                            <>
                              <span className="truncate text-[11px] leading-tight flex-1 mr-1" title={c.title}>
                                {c.title}
                              </span>

                              <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition shrink-0">
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setEditingConvId(c.id);
                                    setEditingTitle(c.title);
                                  }}
                                  className="p-1 hover:text-neutral-100 hover:bg-neutral-800 rounded text-neutral-400"
                                  title="Rename conversation"
                                >
                                  <EditIcon size={10} />
                                </button>
                                <button
                                  type="button"
                                  onClick={(e) => handleDeleteConversation(c.id, e)}
                                  className="p-1 hover:text-red-400 hover:bg-neutral-800 rounded text-neutral-400"
                                  title="Delete conversation"
                                >
                                  <TrashIcon size={10} />
                                </button>
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })}

              {filteredConversations.length === 0 && (
                <div className="text-[11px] text-neutral-500 text-center py-4">
                  No conversations found.
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Active Conversation Stream & Composer ──────────────────────── */}
        <div className="flex-1 flex flex-col min-w-0 h-full">
          {/* Subheader: Active Thread Title */}
          <div className="flex items-center justify-between px-3 py-1 border-b border-neutral-800/60 bg-neutral-900/30 text-[10.5px]">
            <span className="truncate text-neutral-400 font-medium" title={activeConv?.title}>
              {activeConv?.title || 'CLIQ Copilot'}
            </span>
          </div>

          {/* Message List */}
          <CopilotMessageList
            messages={messages}
            isStreaming={isStreaming}
            streamingMessageId={streamingMsgId}
            onSelectSuggestion={(prompt) => handleSendMessage(prompt)}
            onRetryLast={handleRetryLast}
            activeModelName={selectedModel}
            devices={devices}
            openDevices={openDevices}
            activeDeviceId={activeDeviceId}
            focusedDeviceId={focusedDeviceId}
            deviceMetrics={deviceMetrics}
            onInsertCommand={onInsertCommand}
            onExplainCommand={handleExplainCommand}
          />

          {/* Input Composer */}
          <CopilotComposer
            input={input}
            setInput={setInput}
            onSend={() => handleSendMessage()}
            onStop={handleStopStreaming}
            isStreaming={isStreaming}
            contextChips={contextChips}
            onRemoveChip={handleRemoveChip}
            attachments={attachments}
            onRemoveAttachment={handleRemoveAttachment}
            onFileUpload={handleFileUpload}
            isUploading={isUploading}
            onOpenContextModal={() => setIsContextModalOpen(true)}
            activeModelName={selectedModel}
          />
        </div>
      </div>

      {/* ── 3. Add Context Modal ─────────────────────────────────────────── */}
      <AddContextModal
        isOpen={isContextModalOpen}
        onClose={() => setIsContextModalOpen(false)}
        devices={devices}
        activeDeviceId={activeDeviceId}
        onAddChip={handleAddChip}
        onAddChips={handleAddChips}
        onClearChips={() => setContextChips([])}
        currentChips={contextChips}
      />
    </aside>
  );
}
