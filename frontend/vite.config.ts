import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import wasm from 'vite-plugin-wasm'

export default defineConfig({
  plugins: [react(), tailwindcss(), wasm()],
  server: {
    // 监听 0.0.0.0 让 LAN / Tailscale 内（如 100.124.149.118）能直访；HMR 用 host 自适应。
    host: true,
    // 端口固定 9999 — cloudflared / 各处链接对齐这个端口
    port: 9999,
    strictPort: true,
    // 允许任意 host 头（cloudflare tunnel / nginx 反代时 Host 不是 localhost）。
    // Vite 5+ 默认收紧到 'localhost'，这里放开为内网 + 自定义域名。
    allowedHosts: true,
    proxy: {
      '/api/': 'http://localhost:8000',
      '/v1': 'http://localhost:8000',
      // /sys/admin/* (login / passkey / totp) lives on the same backend (:8000)
      // since PR #33. The :8001 target was for a legacy "system" service that
      // no longer exists; pointing it there used to return 500 on every login.
      '/sys': 'http://localhost:8000',
      // Signed-URL static route for image_generate outputs. In prod the
      // backend serves the SPA dist/ AND /files/* in one process (no proxy
      // needed); in dev vite (:9999) and backend (:8000) are split, so we
      // forward the path explicitly.
      '/files/': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy node_modules into focused vendor chunks so the
        // initial app-only chunk stays small. Keeps the workflow canvas
        // (xyflow) and usage charts (recharts) out of the critical path
        // for users who just hit /services or /dashboard.
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/react-router-dom/') ||
            id.includes('/react-router/') ||
            id.includes('/scheduler/')
          ) {
            return 'vendor-react'
          }
          if (id.includes('@tanstack')) return 'vendor-query'
          if (id.includes('@xyflow')) return 'vendor-xyflow'
          // d3-* is shared by both recharts and @xyflow's zoom/selection
          // helpers — pull it into its own chunk so the two consumers
          // don't form a circular dependency through the shared code.
          if (id.includes('/d3-')) return 'vendor-d3'
          if (id.includes('recharts') || id.includes('/victory-')) return 'vendor-charts'
          if (id.includes('lucide-react')) return 'vendor-icons'
          if (id.includes('zustand')) return 'vendor-state'
        },
      },
    },
  },
})
