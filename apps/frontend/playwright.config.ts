import { defineConfig, devices } from "@playwright/test";

// E2E runs against the **prod-local** stack, through nginx — the same profile
// CI runs, for the same reason: "works in dev" must predict "works in prod",
// and the two differ exactly where it is cheap to be wrong. Hitting the Next
// dev server directly would skip nginx, the BFF's real routing, and uWSGI.
//
// BASE_URL is injected by the Makefile: `http://nginx` when this container is
// on the compose network, `http://localhost:8080` when a developer runs it
// against a published port.
const baseURL = process.env.BASE_URL ?? "http://localhost:8080";

export default defineConfig({
  testDir: "./e2e",
  // The flow is inherently sequential — generate, reject, regenerate, edit,
  // approve — and it shares one seeded project. Parallel workers would race
  // each other through the same artifact's state machine and fail in ways
  // that tell you nothing about the application.
  workers: 1,
  fullyParallel: false,
  // No retries. A flaky exit criterion is not an exit criterion, and a retry
  // would hide exactly the timing bugs this suite exists to surface.
  retries: 0,
  // Generous: a real Celery round-trip through a real broker is seconds, and
  // the failure this guards against (a job that never completes) is worth
  // waiting for rather than declaring early.
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
