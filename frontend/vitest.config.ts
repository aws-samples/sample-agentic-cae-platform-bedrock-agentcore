import { defineConfig } from 'vitest/config'

// Vitest runs the WebSocket client logic in a plain Node environment; the
// client is driven by an injected fake socket + clock, so no DOM is required.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
