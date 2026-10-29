export type PlatformId = 'direct_ssh' | 'cml' | 'future_platforms';

export type PlatformOption = {
  id: PlatformId;
  name: string;
  tagline: string;
  description: string;
  badge: string;
  badgeColor: 'emerald' | 'sky' | 'amber' | 'slate';
  status: 'available' | 'coming_soon';
  icon: string;
  supportedDrivers: string[];
};

export const SANDBOX_PLATFORMS: PlatformOption[] = [
  {
    id: 'direct_ssh',
    name: 'Direct Sandbox Device',
    tagline: 'Direct SSH & Terminal Server',
    description:
      'Connect directly to physical or virtual switches, routers, or terminal server lines via SSH with optional privilege EXEC enable secret.',
    badge: 'Available',
    badgeColor: 'emerald',
    status: 'available',
    icon: '⚡',
    supportedDrivers: ['Cisco IOS / IOS-XE', 'Cisco NX-OS', 'Cisco IOS-XR'],
  },
  {
    id: 'cml',
    name: 'CML Sandbox Lab',
    tagline: 'Cisco Modeling Labs',
    description:
      'Authenticate to a CML controller to discover deployed network simulations, inspect live interactive ReactFlow topologies, and launch node consoles.',
    badge: 'Available',
    badgeColor: 'sky',
    status: 'available',
    icon: '🌐',
    supportedDrivers: ['CML Controller REST API', 'Interactive Topology', 'Direct Node Console'],
  },
  {
    id: 'future_platforms',
    name: 'Multi-Vendor Platforms',
    tagline: 'Palo Alto & Juniper Networks',
    description:
      'Extensible multi-vendor architecture for Palo Alto Networks (PAN-OS) and Juniper Networks (Junos OS) sandboxes and firewalls.',
    badge: 'Coming Soon',
    badgeColor: 'amber',
    status: 'coming_soon',
    icon: '🛡️',
    supportedDrivers: ['Palo Alto PAN-OS (Planned)', 'Juniper Junos OS (Planned)'],
  },
];

