import React, { useState, useMemo, useEffect } from 'react';
import type { ContextChip, ContextChipType } from '../../types/copilot';
import type { Device } from '../../types/api';
import { CloseIcon, SearchIcon, CheckIcon, AtSignIcon, TerminalIcon } from './CopilotIcons';

interface AddContextModalProps {
  isOpen: boolean;
  onClose: () => void;
  devices: Device[];
  activeDeviceId?: string;
  onAddChip: (chip: ContextChip) => void;
  onAddChips?: (chips: ContextChip[]) => void;
  onClearChips?: () => void;
  currentChips: ContextChip[];
}

interface ContextCategory {
  type: ContextChipType;
  label: string;
  command: string;
  description: string;
}

const CONTEXT_CATEGORIES: ContextCategory[] = [
  {
    type: 'interfaces',
    label: 'Interfaces',
    command: 'show ip interface brief',
    description: 'Interface status, IP addresses & protocol states',
  },
  {
    type: 'routes',
    label: 'Routing Table',
    command: 'show ip route',
    description: 'Active IP prefixes, next hops & routing protocols',
  },
  {
    type: 'config',
    label: 'Running Config',
    command: 'show running-config',
    description: 'Device configuration (passwords & secrets auto-redacted)',
  },
  {
    type: 'neighbors',
    label: 'Neighbors',
    command: 'show cdp/lldp neighbors',
    description: 'Directly connected adjacent devices & link topology',
  },
  {
    type: 'terminal_history',
    label: 'Command History',
    command: 'recent session commands',
    description: 'Recent CLI commands entered in this terminal session',
  },
  {
    type: 'device',
    label: 'Device Info',
    command: 'status & summary',
    description: 'Hardware type, operational mode & reachability',
  },
];

export default function AddContextModal({
  isOpen,
  onClose,
  devices = [],
  activeDeviceId,
  onAddChip,
  onAddChips,
  onClearChips,
  currentChips = [],
}: AddContextModalProps) {
  // Mode selection state: 'quick' (modes A, B, D) | 'custom' (mode C)
  const [activeTab, setActiveTab] = useState<'quick' | 'custom'>('quick');

  // Multi-device selection for custom mode
  const [selectedDeviceIds, setSelectedDeviceIds] = useState<Set<string>>(new Set());
  // Category selection for custom mode
  const [selectedCategories, setSelectedCategories] = useState<Set<ContextChipType>>(
    new Set(['interfaces', 'routes'])
  );

  const [searchQuery, setSearchQuery] = useState('');
  const [feedback, setFeedback] = useState<string | null>(null);

  const activeDevice = useMemo(
    () => (activeDeviceId ? devices.find((d) => d.id === activeDeviceId) || null : null),
    [devices, activeDeviceId]
  );

  // Initialize selected device for custom mode
  useEffect(() => {
    if (isOpen) {
      if (activeDeviceId && devices.some((d) => d.id === activeDeviceId)) {
        setSelectedDeviceIds(new Set([activeDeviceId]));
      } else {
        setSelectedDeviceIds(new Set());
      }
    }
  }, [isOpen, activeDeviceId, devices]);

  if (!isOpen) return null;

  const showFeedback = (msg: string) => {
    setFeedback(msg);
    setTimeout(() => setFeedback(null), 2500);
  };

  // ── Mode A: Clear Device Context (General questions) ────────────────────────
  const handleNoDeviceContext = () => {
    if (onClearChips) {
      onClearChips();
    }
    showFeedback('Device context cleared for general conceptual questions.');
    setTimeout(() => onClose(), 600);
  };

  // ── Mode B: Active Device (Recommended default) ─────────────────────────────
  const handleAddActiveDevice = () => {
    if (!activeDevice) {
      showFeedback('No connected device is currently active.');
      return;
    }

    const chipId = `active_${activeDevice.id}`;
    const alreadyExists = currentChips.some((c) => c.id === chipId || (c.deviceId === activeDevice.id && c.type === 'device'));

    if (alreadyExists) {
      showFeedback(`${activeDevice.label} is already attached to context.`);
      return;
    }

    const chip: ContextChip = {
      id: chipId,
      type: 'device',
      label: `@${activeDevice.label} · Active Device`,
      deviceId: activeDevice.id,
      detail: `Active terminal device (${activeDevice.label}) live telemetry`,
    };

    onAddChip(chip);
    showFeedback(`Added @${activeDevice.label} · Active Device`);
    setTimeout(() => onClose(), 600);
  };

  // ── Mode D: All Connected Devices ───────────────────────────────────────────
  const handleAddAllDevices = () => {
    if (devices.length === 0) {
      showFeedback('No connected devices available.');
      return;
    }

    const chipId = 'all_devices';
    const alreadyExists = currentChips.some((c) => c.id === chipId || c.type === 'all_devices');

    if (alreadyExists) {
      showFeedback('All Connected Devices is already attached.');
      return;
    }

    const chip: ContextChip = {
      id: chipId,
      type: 'all_devices',
      label: '@All Devices',
      detail: `All ${devices.length} connected network devices`,
    };

    onAddChip(chip);
    showFeedback(`Added @All Devices (${devices.length} nodes)`);
    setTimeout(() => onClose(), 600);
  };

  // ── Mode C: Custom Devices & Categories ──────────────────────────────────────
  const toggleDevice = (devId: string) => {
    setSelectedDeviceIds((prev) => {
      const next = new Set(prev);
      if (next.has(devId)) next.delete(devId);
      else next.add(devId);
      return next;
    });
  };

  const toggleCategory = (catType: ContextChipType) => {
    setSelectedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(catType)) next.delete(catType);
      else next.add(catType);
      return next;
    });
  };

  const handleAddCustomSelection = () => {
    const targetDevs = devices.filter((d) => selectedDeviceIds.has(d.id));
    if (targetDevs.length === 0) {
      showFeedback('Please select at least one device.');
      return;
    }
    if (selectedCategories.size === 0) {
      showFeedback('Please select at least one information category.');
      return;
    }

    const newChips: ContextChip[] = [];
    targetDevs.forEach((dev) => {
      selectedCategories.forEach((catType) => {
        const cat = CONTEXT_CATEGORIES.find((c) => c.type === catType);
        const chipId = `${catType}_${dev.id}`;
        const alreadyExists = currentChips.some((c) => c.id === chipId);

        if (!alreadyExists && cat) {
          newChips.push({
            id: chipId,
            type: catType,
            label: `@${dev.label} · ${cat.label}`,
            deviceId: dev.id,
            detail: `${cat.command} on ${dev.label}`,
          });
        }
      });
    });

    if (newChips.length === 0) {
      showFeedback('Selected context items are already added.');
      return;
    }

    if (onAddChips) {
      onAddChips(newChips);
    } else {
      newChips.forEach(onAddChip);
    }

    showFeedback(`Added ${newChips.length} context item(s).`);
    setTimeout(() => onClose(), 700);
  };

  const filteredCategories = CONTEXT_CATEGORIES.filter((c) =>
    c.label.toLowerCase().includes(searchQuery.toLowerCase()) ||
    c.description.toLowerCase().includes(searchQuery.toLowerCase()) ||
    c.command.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 animate-in fade-in duration-150">
      <div className="flex flex-col w-full max-w-lg rounded-xl border border-neutral-700 bg-neutral-900 shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800 bg-neutral-950/70">
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 rounded bg-cyan-500/20 text-cyan-400 flex items-center justify-center border border-cyan-500/30">
              <AtSignIcon size={12} />
            </div>
            <div>
              <h3 className="text-xs font-semibold text-neutral-100 tracking-wide">
                Add Network Context
              </h3>
              <p className="text-[10px] text-neutral-400">
                Inject live device telemetry and configuration into AI reasoning
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded text-neutral-400 hover:text-white hover:bg-neutral-800 transition"
          >
            <CloseIcon size={14} />
          </button>
        </div>

        {/* Tab Navigation */}
        <div className="flex items-center border-b border-neutral-800 bg-neutral-950/40 px-3">
          <button
            type="button"
            onClick={() => setActiveTab('quick')}
            className={`px-3 py-2 text-xs font-medium border-b-2 transition ${
              activeTab === 'quick'
                ? 'border-cyan-500 text-cyan-300'
                : 'border-transparent text-neutral-400 hover:text-neutral-200'
            }`}
          >
            Quick Presets
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('custom')}
            className={`px-3 py-2 text-xs font-medium border-b-2 transition ${
              activeTab === 'custom'
                ? 'border-cyan-500 text-cyan-300'
                : 'border-transparent text-neutral-400 hover:text-neutral-200'
            }`}
          >
            Custom Device & Category
          </button>
        </div>

        {/* Feedback Alert */}
        {feedback && (
          <div className="px-4 py-1.5 bg-cyan-950/60 border-b border-cyan-800/60 text-[11px] text-cyan-200 flex items-center gap-2">
            <CheckIcon size={12} className="text-cyan-400" />
            <span>{feedback}</span>
          </div>
        )}

        {/* Content Area */}
        <div className="p-4 max-h-[420px] overflow-y-auto space-y-4">
          {activeTab === 'quick' ? (
            <div className="space-y-3">
              {/* Option B: Active Device */}
              <div className="p-3 rounded-lg border border-neutral-800 bg-neutral-950/50 hover:border-cyan-500/40 transition">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5 p-1 rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                      <TerminalIcon size={14} />
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-neutral-100 flex items-center gap-1.5">
                        <span>Active Terminal Device</span>
                        {activeDevice && (
                          <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-cyan-950 text-cyan-300 border border-cyan-800">
                            {activeDevice.label}
                          </span>
                        )}
                      </div>
                      <p className="text-[11px] text-neutral-400 mt-0.5">
                        Injects current status, interfaces, and routes for the currently focused terminal.
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={handleAddActiveDevice}
                    disabled={!activeDevice}
                    className="shrink-0 px-2.5 py-1 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium transition shadow disabled:opacity-50"
                  >
                    Add Active
                  </button>
                </div>
              </div>

              {/* Option D: All Connected Devices */}
              <div className="p-3 rounded-lg border border-neutral-800 bg-neutral-950/50 hover:border-cyan-500/40 transition">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5 p-1 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                      <AtSignIcon size={14} />
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-neutral-100 flex items-center gap-1.5">
                        <span>All Connected Devices</span>
                        <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-neutral-800 text-neutral-300 border border-neutral-700">
                          {devices.length} nodes
                        </span>
                      </div>
                      <p className="text-[11px] text-neutral-400 mt-0.5">
                        Injects topology-wide interface overview across all connected lab nodes.
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={handleAddAllDevices}
                    disabled={devices.length === 0}
                    className="shrink-0 px-2.5 py-1 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-200 hover:text-white text-xs font-medium transition border border-neutral-700 disabled:opacity-50"
                  >
                    Add All
                  </button>
                </div>
              </div>

              {/* Option A: No Device Context */}
              <div className="p-3 rounded-lg border border-neutral-800 bg-neutral-950/50 hover:border-neutral-700 transition">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold text-neutral-200">
                      General Question (No Device Context)
                    </div>
                    <p className="text-[11px] text-neutral-400 mt-0.5">
                      Clear device telemetry to ask conceptual protocol or configuration questions.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={handleNoDeviceContext}
                    className="shrink-0 px-2.5 py-1 rounded hover:bg-neutral-800 text-neutral-400 hover:text-neutral-200 text-xs font-medium transition border border-neutral-800"
                  >
                    Clear Context
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-3.5">
              {/* Step 1: Target Devices */}
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[11px] font-semibold text-neutral-300 uppercase tracking-wider">
                    1. Target Devices
                  </span>
                  <div className="flex items-center gap-2 text-[10.5px]">
                    <button
                      type="button"
                      onClick={() => setSelectedDeviceIds(new Set(devices.map((d) => d.id)))}
                      className="text-cyan-400 hover:underline"
                    >
                      Select All
                    </button>
                    <span className="text-neutral-600">·</span>
                    <button
                      type="button"
                      onClick={() => setSelectedDeviceIds(new Set())}
                      className="text-neutral-400 hover:underline"
                    >
                      Clear
                    </button>
                  </div>
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {devices.map((dev) => {
                    const isSelected = selectedDeviceIds.has(dev.id);
                    const isActive = dev.id === activeDeviceId;

                    return (
                      <button
                        key={dev.id}
                        type="button"
                        onClick={() => toggleDevice(dev.id)}
                        className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-mono transition border ${
                          isSelected
                            ? 'bg-cyan-950/80 border-cyan-700 text-cyan-200 font-semibold'
                            : 'bg-neutral-800/60 border-neutral-700/80 text-neutral-300 hover:bg-neutral-800'
                        }`}
                      >
                        <span className={`w-1.5 h-1.5 rounded-full ${isActive ? 'bg-cyan-400' : 'bg-neutral-500'}`} />
                        <span>{dev.label}</span>
                        {isActive && <span className="text-[9px] text-cyan-400 font-sans">(Active)</span>}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Step 2: Telemetry Categories */}
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[11px] font-semibold text-neutral-300 uppercase tracking-wider">
                    2. Telemetry Categories
                  </span>
                  <div className="flex items-center gap-2 text-[10.5px]">
                    <button
                      type="button"
                      onClick={() => setSelectedCategories(new Set(CONTEXT_CATEGORIES.map((c) => c.type)))}
                      className="text-cyan-400 hover:underline"
                    >
                      Select All
                    </button>
                    <span className="text-neutral-600">·</span>
                    <button
                      type="button"
                      onClick={() => setSelectedCategories(new Set())}
                      className="text-neutral-400 hover:underline"
                    >
                      Clear
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2">
                  {filteredCategories.map((cat) => {
                    const isSelected = selectedCategories.has(cat.type);

                    return (
                      <button
                        key={cat.type}
                        type="button"
                        onClick={() => toggleCategory(cat.type)}
                        className={`flex flex-col items-start p-2 rounded text-left transition border ${
                          isSelected
                            ? 'bg-cyan-950/60 border-cyan-700 text-cyan-200'
                            : 'bg-neutral-950 border-neutral-800 text-neutral-300 hover:bg-neutral-850'
                        }`}
                      >
                        <div className="flex items-center justify-between w-full">
                          <span className="text-xs font-medium text-neutral-100">
                            {cat.label}
                          </span>
                          {isSelected && <CheckIcon size={12} className="text-cyan-400" />}
                        </div>
                        <span className="text-[10px] text-neutral-500 font-mono mt-0.5">
                          {cat.command}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Add Custom Button */}
              <div className="pt-2 border-t border-neutral-800 flex justify-end">
                <button
                  type="button"
                  onClick={handleAddCustomSelection}
                  disabled={selectedDeviceIds.size === 0 || selectedCategories.size === 0}
                  className="px-4 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium transition shadow disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Add Selected Context ({selectedDeviceIds.size * selectedCategories.size} items)
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
