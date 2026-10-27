import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        shell: {
          bg: '#0f1729',
          panel: '#17223b',
          edge: '#2f3f66',
          text: '#dce3f3',
          muted: '#8b98b8',
          accent: '#22c55e',
        },
      },
      fontFamily: {
        display: ['Space Grotesk', 'ui-sans-serif', 'system-ui'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular'],
      },
      keyframes: {
        pulseNode: {
          '0%, 100%': { opacity: '0.55' },
          '50%': { opacity: '1' },
        },
      },
      animation: {
        pulseNode: 'pulseNode 1.2s ease-in-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config;
