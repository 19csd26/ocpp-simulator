import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The UI always talks to relative URLs. In development Vite forwards them to the simulator backend.
const backend = process.env.SIM_BACKEND ?? 'http://127.0.0.1:8100'
export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 700 }, // the guide (markdown + React Flow) loads on demand as its own chunk
  server: {
    proxy: {
      '/api': backend,
      '/ws': { target: backend.replace('http', 'ws'), ws: true },
    },
  },
})
