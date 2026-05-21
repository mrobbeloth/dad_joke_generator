// Playwright configuration for the @axe-core/playwright accessibility
// suite (task 12.6).
//
// The four viewport widths come straight from R7.3 (mobile 320 px,
// tablet 768 px, desktop 1280 px, wide 1920 px). Each width gets its
// own Playwright project so a regression at one breakpoint shows up
// as one isolated failing test instead of polluting every other
// width's report. Heights are pinned to 800 px so the only variable
// across projects is the breakpoint under test.
//
// The `webServer` block compiles the SPA into `dist/` (via
// `build_a11y.mjs`) and serves the static output on
// `http://localhost:8080`. Running the build inside `webServer.command`
// keeps the suite self-contained: a fresh clone can run
// `npm run test:a11y` and pick up source changes without a separate
// build step. `http-server -c-1` disables HTTP caching so a re-run
// always reflects the latest bundle.
//
// Browsers are NOT installed by `npm install`. Run
// `npx playwright install chromium` once before the first invocation;
// see `tests/a11y/README.md` for full setup notes.

import { defineConfig, devices } from "playwright/test";

const PORT = 8080;
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./tests/a11y",
  // Accessibility checks are independent at each viewport so the
  // suite parallelizes cleanly. Cap workers in CI to keep the
  // browser memory footprint predictable.
  fullyParallel: true,
  workers: process.env.CI ? 2 : undefined,
  // No retries: an axe violation either reproduces or it doesn't, and
  // retrying hides flake instead of fixing it.
  retries: 0,
  // Default per-test timeout (30 s). Axe analysis on a page this size
  // finishes well under that ceiling; the headroom is for slow CI
  // hardware and the cold-start of the static server.
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: BASE_URL,
    // Headless is the only mode CI can run; using it locally too keeps
    // results consistent.
    headless: true,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
  },
  // One project per WCAG viewport breakpoint (R7.3). Heights are
  // identical so width is the single independent variable across the
  // four scans.
  projects: [
    {
      name: "mobile-320",
      use: { ...devices["Desktop Chrome"], viewport: { width: 320, height: 800 } },
    },
    {
      name: "tablet-768",
      use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 800 } },
    },
    {
      name: "desktop-1280",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      name: "wide-1920",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1920, height: 800 } },
    },
  ],
  webServer: {
    // Build the SPA into `dist/` then serve the static files. `-c-1`
    // disables HTTP caching so a re-run picks up rebuilt assets.
    command: "node build_a11y.mjs && npx http-server dist -p 8080 -c-1 --silent",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
