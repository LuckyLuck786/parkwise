import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const proxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

/**
 * Dev proxies /api to the local FastAPI backend so the browser only ever talks
 * to one origin. In production the same relative `/api/v1` URLs are rewritten
 * by vercel.json to the single Python function, so the deployed app is
 * same-origin too and needs no CORS configuration at all.
 */
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    port: 5173,
    proxy: { '/api': { target: proxyTarget, changeOrigin: true } },
  },
  build: { outDir: 'dist', assetsDir: 'assets', sourcemap: false },
  base: '/',
})
