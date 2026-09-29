import { defineConfig } from '@playwright/test';
import { randomUUID } from 'node:crypto';

// Address edits from a previous run must not alter the next run's fixtures.
const storagePath = `../artifacts/e2e-${randomUUID()}.sqlite`;

export default defineConfig({
  testDir: './tests',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8001', channel: process.env.PLAYWRIGHT_CHANNEL || undefined },
  webServer: {
    command: `DISPATCH_ROUTING_MODE=synthetic DADATA_API_KEY="" DISPATCH_TILE_URL="" DISPATCH_STORAGE_PATH=${storagePath} ../.venv/bin/python -m uvicorn dispatch.api:app --app-dir .. --host 127.0.0.1 --port 8001`,
    url: 'http://127.0.0.1:8001/api/health',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
