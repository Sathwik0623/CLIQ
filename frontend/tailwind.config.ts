import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        /* ── Phase T: VS Code-inspired IDE palette ── */
        ide: {
          bg:       'var(--ide-bg)',
          sidebar:  'var(--ide-sidebar-bg)',
          panel:    'var(--ide-panel-bg)',
          header:   'var(--ide-header-bg)',
          tab:      'var(--ide-tab-bg)',
          'tab-active': 'var(--ide-tab-active)',
          border:   'var(--ide-border)',
          'border-light': 'var(--ide-border-light)',
          text:     'var(--ide-text)',
          muted:    'var(--ide-text-muted)',
          dim:      'var(--ide-text-dim)',
          accent:   'var(--ide-accent)',
          hover:    'var(--ide-hover)',
          selected: 'var(--ide-selected)',
          error:    'var(--ide-error)',
          warning:  'var(--ide-warning)',
          success:  'var(--ide-success)',
          info:     'var(--ide-info)',
          'status-bg':   'var(--ide-status-bg)',
          'status-text': 'var(--ide-status-text)',
        },
        /* ── Legacy shell palette (kept for backward compat) ── */
        shell: {
          bg:     '#0f1729',
          panel:  '#17223b',
          edge:   '#2f3f66',
          text:   '#dce3f3',
          muted:  '#8b98b8',
          accent: '#22c55e',
        },
      },
      fontFamily: {
        display: ['Space Grotesk', 'ui-sans-serif', 'system-ui'],
        mono:    ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular'],
      },
      keyframes: {
        pulseNode: {
          '0%, 100%': { opacity: '0.55' },
          '50%':      { opacity: '1' },
        },
      },
      animation: {
        pulseNode: 'pulseNode 1.2s ease-in-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config;
