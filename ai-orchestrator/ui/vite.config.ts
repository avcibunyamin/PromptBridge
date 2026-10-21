import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // In dev mode, proxy /api and /health to the FastAPI backend
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8765', changeOrigin: true },
    },
  },
  build: {
    // Output to app/static/ so FastAPI can serve it directly
    outDir: resolve(__dirname, '../app/static'),
    emptyOutDir: true,
  },
})
