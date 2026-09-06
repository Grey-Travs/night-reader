import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The Python backend runs on :8000. In dev, Vite proxies /api there so the
// frontend and backend feel like one origin (no CORS fuss).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
  // `npm run build` cannot catch a runtime fault: a bad dependency array still
  // compiles. That is exactly how a crash-on-render shipped in ChapterReader and made
  // the reader unreachable. These tests render the real components in jsdom, so that
  // class of bug fails here instead of in front of the user.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
    // The default `forks` pool times out starting a worker on this Windows setup
    // (60s, no response). Threads start reliably here. `fileParallelism: false`
    // keeps the run deterministic, which matters more than speed at this size —
    // it replaces the poolOptions.threads.singleThread that Vitest 4 removed.
    pool: 'threads',
    fileParallelism: false,
  },
})
