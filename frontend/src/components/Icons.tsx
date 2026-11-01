/**
 * Icons.tsx — Centralized, consistent SVG icon set for the CLIQ IDE.
 *
 * Enterprise-grade, stroke-based minimalist iconography.
 * Replaces all emojis, arbitrary Unicode glyphs, and mixed icon sizes.
 */
import React from 'react';

type IconProps = {
  className?: string;
  size?: number;
  style?: React.CSSProperties;
};

export function RouterIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {/* Network router cylinder/circle with 4 directional arrows */}
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v10" />
      <path d="M7 12h10" />
      <path d="m9 9 3-2 3 2" />
      <path d="m9 15 3 2 3-2" />
      <path d="m9 9-2 3 2 3" />
      <path d="m15 9 2 3-2 3" />
    </svg>
  );
}

export function SwitchIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {/* Network switch box with opposing directional arrows */}
      <rect width="20" height="12" x="2" y="6" rx="2" />
      <path d="M7 10h8m-3-3 3 3-3 3" />
      <path d="M17 14H9m3-3-3 3 3 3" />
    </svg>
  );
}

export function FirewallIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {/* Firewall shield with masonry brick grid */}
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      <line x1="7" y1="10" x2="17" y2="10" />
      <line x1="7" y1="14" x2="17" y2="14" />
      <line x1="12" y1="6" x2="12" y2="10" />
      <line x1="9.5" y1="10" x2="9.5" y2="14" />
      <line x1="14.5" y1="10" x2="14.5" y2="14" />
    </svg>
  );
}

export function ServerIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {/* Server rack with 2 blade units, drive slots and status LEDs */}
      <rect width="20" height="8" x="2" y="3" rx="2" />
      <rect width="20" height="8" x="2" y="13" rx="2" />
      <circle cx="6" cy="7" r="1" fill="currentColor" />
      <circle cx="6" cy="17" r="1" fill="currentColor" />
      <line x1="10" y1="7" x2="18" y2="7" />
      <line x1="10" y1="17" x2="18" y2="17" />
    </svg>
  );
}

export function PcIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {/* Endpoint/PC desktop monitor with stand */}
      <rect width="20" height="14" x="2" y="3" rx="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  );
}

export function TerminalIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" x2="20" y1="19" y2="19" />
    </svg>
  );
}

export function TopologyIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <circle cx="6" cy="6" r="3" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="12" cy="18" r="3" />
      <line x1="8.5" x2="15.5" y1="6" y2="6" />
      <line x1="7.5" x2="10.5" y1="8.5" y2="15.5" />
      <line x1="16.5" x2="13.5" y1="8.5" y2="15.5" />
    </svg>
  );
}

export function AdvisoryIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M12 2v4" />
      <path d="M12 18v4" />
      <path d="m4.93 4.93 2.83 2.83" />
      <path d="m16.24 16.24 2.83 2.83" />
      <path d="M2 12h4" />
      <path d="M18 12h4" />
      <path d="m4.93 19.07 2.83-2.83" />
      <path d="m16.24 7.76 2.83-2.83" />
    </svg>
  );
}

export function SettingsIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

export function SearchIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" x2="16.65" y1="21" y2="16.65" />
    </svg>
  );
}

export function SplitSingleIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <rect width="18" height="18" x="3" y="3" rx="2" />
    </svg>
  );
}

export function SplitHorizontalIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <line x1="12" x2="12" y1="3" y2="21" />
    </svg>
  );
}

export function SplitVerticalIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <line x1="3" x2="21" y1="12" y2="12" />
    </svg>
  );
}

export function ChevronLeftIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

export function ChevronRightIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

export function ChevronDownIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

export function CloseIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <line x1="18" x2="6" y1="6" y2="18" />
      <line x1="6" x2="18" y1="6" y2="18" />
    </svg>
  );
}

export function KeyIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="m15.5 8.5 2 2" />
      <path d="m18 6 3 3" />
      <circle cx="7.5" cy="15.5" r="5.5" />
      <path d="m11.5 11.5 9-9" />
    </svg>
  );
}

export function CheckIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function AlertTriangleIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
      <line x1="12" x2="12" y1="9" y2="13" />
      <line x1="12" x2="12.01" y1="17" y2="17" />
    </svg>
  );
}

export function LockIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  );
}

export function GlobeIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <circle cx="12" cy="12" r="10" />
      <line x1="2" x2="22" y1="12" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

export function CliqLogoIcon({ size = 16, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

export function CopyIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <rect width="14" height="14" x="8" y="8" rx="2" ry="2" />
      <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
    </svg>
  );
}

export function InsertIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="9 10 4 15 9 20" />
      <path d="M20 4v7a4 4 0 0 1-4 4H4" />
    </svg>
  );
}

export function LightbulbIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" />
      <path d="M9 18h6" />
      <path d="M10 22h4" />
    </svg>
  );
}

export function WrenchIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

export function BookOpenIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  );
}

export function ActivityIcon({ size = 14, className = '', style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

/**
 * getDeviceIcon — Deterministic device icon mapper for CLIQ IDE.
 *
 * Uses the actual device type metadata returned by CML (e.g. iosv, iosvl2,
 * csr1000v, cat8000v, asav, ftdv, ubuntu, desktop, ostinato).
 * Falls back to label classification only when type is missing or unknown.
 */
export function getDeviceIcon(type?: string, label?: string, size = 14, className = '') {
  const t = (type || '').toLowerCase().trim();
  const l = (label || '').toLowerCase().trim();

  // 1. Switch check (CML node_definitions: iosvl2, cat9000v, nxosv9000, nxosv, etc.)
  if (
    t === 'iosvl2' ||
    t === 'iosv-l2' ||
    t === 'cat9000v' ||
    t.includes('switch') ||
    t.includes('l2') ||
    t.includes('nxos') ||
    (!t || t === 'unknown' ? l.startsWith('sw') || l.includes('switch') : false)
  ) {
    return <SwitchIcon size={size} className={className} />;
  }

  // 2. Firewall check (CML node_definitions: asav, ftdv, pan-os, etc.)
  if (
    t === 'asav' ||
    t === 'ftdv' ||
    t.includes('firewall') ||
    t.includes('asa') ||
    t.includes('ftd') ||
    t.includes('pan') ||
    (!t || t === 'unknown' ? l.startsWith('fw') || l.includes('firewall') : false)
  ) {
    return <FirewallIcon size={size} className={className} />;
  }

  // 3. Endpoint / PC check (CML node_definitions: desktop, ostinato, traffic_generator, workstation)
  if (
    t === 'desktop' ||
    t === 'pc' ||
    t === 'ostinato' ||
    t.includes('desktop') ||
    t.includes('workstation') ||
    t.includes('endpoint') ||
    t.includes('client') ||
    (!t || t === 'unknown' ? l.startsWith('pc') || l.includes('host') || l.includes('client') : false)
  ) {
    return <PcIcon size={size} className={className} />;
  }

  // 4. Server check (CML node_definitions: server, ubuntu, centos, debian, alpine, linux)
  if (
    t === 'server' ||
    t === 'ubuntu' ||
    t === 'centos' ||
    t === 'debian' ||
    t === 'alpine' ||
    t.includes('linux') ||
    t.includes('server') ||
    (!t || t === 'unknown' ? l.startsWith('srv') || l.includes('server') : false)
  ) {
    return <ServerIcon size={size} className={className} />;
  }

  // 5. Router (CML node_definitions: iosv, csr1000v, cat8000v, c8000v, xrv, xrv9000, router, default)
  return <RouterIcon size={size} className={className} />;
}
