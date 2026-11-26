import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// Dev: Vite on 127.0.0.1:VITE_DEV_PORT proxies /api -> FastAPI on 127.0.0.1:PORT (both from the repo-root .env).
// Prod: `npm run build` -> dist/, served by FastAPI itself (single origin behind tailscale serve).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', '') // repo root .env, all keys
  const apiPort = env.PORT || '8620'
  const devPort = Number(env.VITE_DEV_PORT || 5620)
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: '127.0.0.1',
      port: devPort,
      strictPort: true,
      proxy: {
        '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
      },
    },
    build: {
      outDir: 'dist',
      chunkSizeWarningLimit: 2500,
    },
  }
})
