import { defineConfig } from "@playwright/test";

const python = process.env.WEB_TEST_PYTHON || (process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const port = process.env.WEB_TEST_PORT || "8765";
export default defineConfig({
  testDir: "tests/web/browser",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: {
    command: `"${python}" -m tests.web.server`,
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 90_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
});
