import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      // ws: true is required or the WebSocket proxy (/ws) silently fails and the app
      // looks broken in dev and fine in production - the worst possible split.
      '/api': { target: 'http://127.0.0.1:8766', changeOrigin: false },
      '/ws': { target: 'ws://127.0.0.1:8766', ws: true, changeOrigin: false },
    },
  },
  // ES2022, as tsconfig: noVNC awaits at the top level of a module
  optimizeDeps: { esbuildOptions: { target: 'es2022' } },
  build: {
    outDir: '../server/celine/static',
    emptyOutDir: true,
    target: 'es2022',
  },
})
