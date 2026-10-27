import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev-only: forward the same-origin relative `/api` and `/ws` calls to the
    // backend so `npm run dev` keeps working after the frontend switched to
    // origin-relative URLs. Production serves everything from one port, so no
    // proxy is involved there.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
});
