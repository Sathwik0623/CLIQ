import React, { useState } from 'react';
import { SANDBOX_PLATFORMS, type PlatformId, type PlatformOption } from '../types/platform';
import { CliqLogoIcon, GlobeIcon, FirewallIcon, LockIcon } from './Icons';

function getPlatformIcon(id: PlatformId, size = 22) {
  switch (id) {
    case 'direct_ssh':
      return <CliqLogoIcon size={size} className="text-amber-400" />;
    case 'cml':
      return <GlobeIcon size={size} className="text-sky-400" />;
    case 'future_platforms':
      return <FirewallIcon size={size} className="text-violet-400" />;
  }
}

type SandboxHubProps = {
  onSelectPlatform: (platformId: PlatformId) => void;
};

export default function SandboxHub({ onSelectPlatform }: SandboxHubProps) {
  const [modalPlatform, setModalPlatform] = useState<PlatformOption | null>(null);

  const getBadgeClass = (color: PlatformOption['badgeColor']) => {
    switch (color) {
      case 'emerald':
        return 'bg-emerald-950/60 border-emerald-800 text-emerald-400';
      case 'sky':
        return 'bg-sky-950/60 border-sky-800 text-sky-400';
      case 'amber':
        return 'bg-amber-950/60 border-amber-800 text-amber-400';
      case 'slate':
      default:
        return 'bg-slate-800 border-slate-700 text-slate-300';
    }
  };

  const handleCardClick = (platform: PlatformOption) => {
    if (platform.status === 'coming_soon') {
      setModalPlatform(platform);
    } else {
      onSelectPlatform(platform.id);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-12" style={{ background: 'var(--ide-bg)', color: 'var(--ide-text)' }}>
      <div className="w-full max-w-5xl">
        {/* Hub Header */}
        <div className="text-center mb-10">
          <div
            className="inline-flex items-center gap-2 rounded-full border px-4 py-1.5 text-xs font-semibold mb-4 shadow-sm"
            style={{ background: 'var(--ide-sidebar-bg)', borderColor: 'var(--ide-border)', color: '#75beff' }}
          >
            <span className="inline-block h-2 w-2 rounded-full bg-sky-400 animate-pulse" />
            CLIQ Sandbox Hub
          </div>
          <h1 className="font-display text-4xl font-extrabold text-slate-100 tracking-tight sm:text-5xl">
            CLIQ Sandbox Hub
          </h1>
          <p className="mt-3 text-lg text-slate-300 font-medium">
            Your network lab, one terminal away
          </p>
          <p className="mt-1 text-sm text-slate-400 max-w-2xl mx-auto">
            Choose how you want to connect to your network sandbox environment: direct hardware console, multi-node CML topology simulation, or next-generation multi-vendor lab.
          </p>
        </div>

        {/* 3 Selectable Platform Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {SANDBOX_PLATFORMS.map((platform) => {
            const isAvailable = platform.status === 'available';
            return (
              <div
                key={platform.id}
                role="button"
                tabIndex={0}
                aria-label={`Select ${platform.name}`}
                onClick={() => handleCardClick(platform)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    handleCardClick(platform);
                  }
                }}
                className={`flex flex-col justify-between rounded-2xl border p-7 shadow-sm transition-all duration-200 text-left cursor-pointer outline-none ${
                  isAvailable
                    ? 'hover:border-sky-500 hover:shadow-xl hover:-translate-y-1 focus:ring-2 focus:ring-sky-500'
                    : 'hover:border-amber-500 hover:shadow-md focus:ring-2 focus:ring-amber-500'
                }`}
                style={{
                  background: 'var(--ide-sidebar-bg)',
                  borderColor: isAvailable ? 'var(--ide-border)' : 'rgba(217,119,6,0.3)',
                }}
              >
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <span
                      className="inline-flex h-12 w-12 items-center justify-center rounded-xl text-2xl shadow-inner border"
                      style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}
                    >
                      {getPlatformIcon(platform.id)}
                    </span>
                    <span
                      className={`rounded-full border px-3 py-1 text-xs font-bold tracking-wide uppercase ${getBadgeClass(
                        platform.badgeColor,
                      )}`}
                    >
                      {platform.badge}
                    </span>
                  </div>

                  <h2 className="text-xl font-bold text-slate-100 group-hover:text-sky-400">
                    {platform.name}
                  </h2>
                  <div className="text-xs font-semibold text-sky-400 uppercase tracking-wider mt-0.5 mb-3">
                    {platform.tagline}
                  </div>

                  <p className="text-sm leading-relaxed mb-6" style={{ color: 'var(--ide-text-muted)' }}>
                    {platform.description}
                  </p>
                </div>

                <div>
                  <div className="border-t pt-4 mb-5" style={{ borderColor: 'var(--ide-border)' }}>
                    <div className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--ide-text-muted)' }}>
                      Capabilities & Drivers
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {platform.supportedDrivers.map((driver) => (
                        <span
                          key={driver}
                          className="rounded-lg border px-2 py-0.5 text-xs font-medium"
                          style={{
                            background: 'var(--ide-bg)',
                            borderColor: 'var(--ide-border)',
                            color: 'var(--ide-text)',
                          }}
                        >
                          {driver}
                        </span>
                      ))}
                    </div>
                  </div>

                  <button
                    type="button"
                    tabIndex={-1}
                    className={`w-full rounded-xl py-2.5 px-4 text-sm font-semibold transition flex items-center justify-center gap-2 ${
                      isAvailable
                        ? 'bg-sky-600 hover:bg-sky-500 text-white shadow'
                        : 'bg-amber-950/30 text-amber-300 border border-amber-800 hover:bg-amber-900/40'
                    }`}
                  >
                    {isAvailable ? (
                      <>
                        <span>Select Platform</span>
                        <span aria-hidden>→</span>
                      </>
                    ) : (
                      <>
                        <span>Preview Platform</span>
                        <span aria-hidden>ℹ</span>
                      </>
                    )}
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {/* Footer info banner */}
        <div
          className="mt-10 rounded-xl border p-4 text-center text-xs flex items-center justify-center gap-3"
          style={{
            background: 'var(--ide-sidebar-bg)',
            borderColor: 'var(--ide-border)',
            color: 'var(--ide-text-muted)',
          }}
        >
          <span className="flex items-center gap-1.5 text-sky-400 font-bold"><LockIcon size={14} /> Secure In-Memory Architecture:</span>
          <span>
            Passwords, tokens, and enable secrets are handled strictly in transient memory. Credentials are never written to disk, SQLite, browser storage, or Git repositories.
          </span>
        </div>
      </div>

      {/* Modal for Future Platforms (Coming Soon) */}
      {modalPlatform && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs"
        >
          <div
            className="w-full max-w-md rounded-2xl border p-7 shadow-2xl"
            style={{ background: 'var(--ide-panel-bg)', borderColor: 'var(--ide-border)', color: 'var(--ide-text)' }}
          >
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2.5">
                <span
                  className="inline-flex h-9 w-9 items-center justify-center rounded-xl text-lg border"
                  style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}
                >
                  {getPlatformIcon(modalPlatform.id, 18)}
                </span>
                <h3 className="font-display text-lg font-bold text-slate-100">
                  {modalPlatform.name}
                </h3>
              </div>
              <span className="rounded-full px-2.5 py-0.5 text-xs font-bold" style={{ background: 'rgba(217,119,6,0.15)', color: '#fbbf24', border: '1px solid #b45309' }}>
                Coming Soon
              </span>
            </div>

            <p className="text-sm mb-5 leading-relaxed" style={{ color: 'var(--ide-text-muted)' }}>
              Multi-vendor platform integration is currently in active development. CLIQ will support direct device CLI streaming, command intelligence, and topology discovery for these ecosystems in upcoming phases:
            </p>

            <div className="space-y-3 mb-6">
              <div className="rounded-xl border p-3.5" style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}>
                <div className="font-semibold text-sm text-slate-200 flex items-center justify-between">
                  <span>Palo Alto Networks (PAN-OS)</span>
                  <span className="text-xs text-amber-400 font-medium">In Development</span>
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--ide-text-muted)' }}>
                  Next-generation firewall terminal access, security policy commands, and operational diagnostics.
                </div>
              </div>

              <div className="rounded-xl border p-3.5" style={{ background: 'var(--ide-bg)', borderColor: 'var(--ide-border)' }}>
                <div className="font-semibold text-sm text-slate-200 flex items-center justify-between">
                  <span>Juniper Networks (Junos OS)</span>
                  <span className="text-xs text-amber-400 font-medium">In Development</span>
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--ide-text-muted)' }}>
                  Junos CLI hierarchy parsing, routing engine operational modes, and commit pipeline validation.
                </div>
              </div>
            </div>

            <button
              type="button"
              onClick={() => setModalPlatform(null)}
              className="w-full rounded-xl py-2.5 text-sm font-semibold text-white shadow transition cursor-pointer"
              style={{ background: 'var(--ide-accent)' }}
            >
              Understood — Return to Hub
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
