import React, { useState } from 'react';
import { SANDBOX_PLATFORMS, type PlatformId, type PlatformOption } from '../types/platform';

type SandboxHubProps = {
  onSelectPlatform: (platformId: PlatformId) => void;
};

export default function SandboxHub({ onSelectPlatform }: SandboxHubProps) {
  const [modalPlatform, setModalPlatform] = useState<PlatformOption | null>(null);

  const getBadgeClass = (color: PlatformOption['badgeColor']) => {
    switch (color) {
      case 'emerald':
        return 'bg-emerald-50 border-emerald-200 text-emerald-700';
      case 'sky':
        return 'bg-sky-50 border-sky-200 text-sky-700';
      case 'amber':
        return 'bg-amber-50 border-amber-200 text-amber-700';
      case 'slate':
      default:
        return 'bg-slate-50 border-slate-200 text-slate-700';
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
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-5xl">
        {/* Hub Header */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center gap-2 rounded-full border border-sky-200 bg-sky-50 px-4 py-1.5 text-xs font-semibold text-sky-800 mb-4 shadow-sm">
            <span className="inline-block h-2 w-2 rounded-full bg-sky-500 animate-pulse" />
            CLIQ Sandbox Hub
          </div>
          <h1 className="font-display text-4xl font-extrabold text-slate-900 tracking-tight sm:text-5xl">
            CLIQ Sandbox Hub
          </h1>
          <p className="mt-3 text-lg text-slate-600 font-medium">
            Your network lab, one terminal away
          </p>
          <p className="mt-1 text-sm text-slate-500 max-w-2xl mx-auto">
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
                className={`flex flex-col justify-between rounded-3xl border bg-white p-7 shadow-sm transition-all duration-200 text-left cursor-pointer outline-none ${
                  isAvailable
                    ? 'border-slate-200 hover:border-sky-300 hover:shadow-xl hover:-translate-y-1 focus:ring-4 focus:ring-sky-100'
                    : 'border-slate-200/80 hover:border-amber-300 hover:shadow-md focus:ring-4 focus:ring-amber-100'
                }`}
              >
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <span className="inline-flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100 text-2xl shadow-inner">
                      {platform.icon}
                    </span>
                    <span
                      className={`rounded-full border px-3 py-1 text-xs font-bold tracking-wide uppercase ${getBadgeClass(
                        platform.badgeColor,
                      )}`}
                    >
                      {platform.badge}
                    </span>
                  </div>

                  <h2 className="text-xl font-bold text-slate-900 group-hover:text-sky-600">
                    {platform.name}
                  </h2>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mt-0.5 mb-3">
                    {platform.tagline}
                  </div>

                  <p className="text-sm text-slate-600 leading-relaxed mb-6">
                    {platform.description}
                  </p>
                </div>

                <div>
                  <div className="border-t border-slate-100 pt-4 mb-5">
                    <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">
                      Capabilities & Drivers
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {platform.supportedDrivers.map((driver) => (
                        <span
                          key={driver}
                          className="rounded-lg bg-slate-100/90 border border-slate-200/60 px-2 py-0.5 text-xs font-medium text-slate-700"
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
                        ? 'bg-sky-500 text-white shadow hover:bg-sky-400'
                        : 'bg-amber-500/10 text-amber-900 border border-amber-300/60 hover:bg-amber-500/20'
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
        <div className="mt-10 rounded-2xl border border-sky-100 bg-sky-50/70 p-4 text-center text-xs text-slate-600 flex items-center justify-center gap-3">
          <span className="text-sky-600 font-bold">🔒 Secure In-Memory Architecture:</span>
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4 backdrop-blur-sm"
        >
          <div className="w-full max-w-md rounded-3xl border border-amber-200 bg-white p-7 shadow-2xl">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2.5">
                <span className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-amber-100 text-lg">
                  {modalPlatform.icon}
                </span>
                <h3 className="font-display text-lg font-bold text-slate-900">
                  {modalPlatform.name}
                </h3>
              </div>
              <span className="rounded-full bg-amber-50 border border-amber-200 px-2.5 py-0.5 text-xs font-bold text-amber-700">
                Coming Soon
              </span>
            </div>

            <p className="text-sm text-slate-600 mb-5 leading-relaxed">
              Multi-vendor platform integration is currently in active development. CLIQ will support direct device CLI streaming, command intelligence, and topology discovery for these ecosystems in upcoming phases:
            </p>

            <div className="space-y-3 mb-6">
              <div className="rounded-xl border border-slate-200 bg-slate-50 p-3.5">
                <div className="font-semibold text-sm text-slate-900 flex items-center justify-between">
                  <span>Palo Alto Networks (PAN-OS)</span>
                  <span className="text-xs text-amber-700 font-medium">In Development</span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Next-generation firewall terminal access, security policy commands, and operational diagnostics.
                </div>
              </div>

              <div className="rounded-xl border border-slate-200 bg-slate-50 p-3.5">
                <div className="font-semibold text-sm text-slate-900 flex items-center justify-between">
                  <span>Juniper Networks (Junos OS)</span>
                  <span className="text-xs text-amber-700 font-medium">In Development</span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Junos CLI hierarchy parsing, routing engine operational modes, and commit pipeline validation.
                </div>
              </div>
            </div>

            <button
              type="button"
              onClick={() => setModalPlatform(null)}
              className="w-full rounded-xl bg-slate-900 py-2.5 text-sm font-semibold text-white shadow hover:bg-slate-800 transition"
            >
              Understood — Return to Hub
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

