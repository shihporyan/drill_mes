import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:8080',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  outputDir: './screenshot',
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  webServer: {
    command: 'DRILL_DEV_CONFIG=config/settings.dev.json python3 main.py --server-only',
    url: 'http://127.0.0.1:8080/api/drilling/overview',
    reuseExistingServer: !process.env.CI,
    timeout: 15_000,
    stdout: 'pipe',
  },
});
