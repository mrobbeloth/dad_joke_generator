// Vitest configuration for the SPA frontend.
//
// Both the property tests (12.4, in tests/property) and the component
// tests (12.5, in tests/component) run under the same vitest instance.
// They share a happy-dom environment so component tests can render the
// real index.html template against `document` while the property tests
// can also exercise DOM-touching helpers when needed.
//
// Keep the `include` glob in sync if a new top-level test directory is
// added.

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["tests/property/**/*.test.ts", "tests/component/**/*.test.ts"],
    globals: false,
    // The component tests load index.html from disk; vitest defaults
    // to a fresh module graph per test file already, but we make the
    // intent explicit so a future user enabling test isolation does
    // not surprise this suite.
    isolate: true,
    // Component tests use vi.useFakeTimers + module imports; keep
    // pool=forks so worker threads don't share state between files.
    pool: "forks",
  },
});
