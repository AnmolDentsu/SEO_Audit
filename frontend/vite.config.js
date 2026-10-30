import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 5173,
    host: '0.0.0.0',

    proxy: {
      '/auth': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },

      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },

      '/healthz': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },

      '/readyz': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})