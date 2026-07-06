// Property tests for the Generate button (task 12.4).
//
// Property 21: Generate button state mirrors remaining count.
//
//   ∀ JokeApiSuccess response with remaining ∈ [0, 100]:
//     after the submit pipeline settles,
//       button.disabled === (remaining === 0)
//       aria-disabled is set ⇔ button is disabled
//
//   ∀ remaining = null: badge stays hidden and button stays enabled.
//   ∀ rate_limited error: forces remaining=0 and disables Generate.
//
// Validates: Requirements 7.7, 7.8.

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockedFunction,
} from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/dom";
import * as fc from "fast-check";

import type {
  JokeApiResponse,
  JokeApiSuccess,
  JokeApiError,
} from "../../src/api.js";

// ---------------------------------------------------------------------------
// Mock the api module. Keep the rest of api.ts real (sanitization etc.).
// ---------------------------------------------------------------------------

vi.mock("../../src/api.js", async () => {
  const actual = await vi.importActual<typeof import("../../src/api.js")>(
    "../../src/api.js",
  );
  return {
    ...actual,
    requestJoke: vi.fn(),
  };
});

// ---------------------------------------------------------------------------
// DOM-loading helper (duplicated from tests/component/setup.ts on purpose so
// the property suite has no cross-suite dependency).
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const INDEX_HTML_PATH = path.resolve(
  __dirname,
  "..",
  "..",
  "src",
  "index.html",
);

let cachedTemplate: string | null = null;

function loadTemplate(): string {
  if (cachedTemplate === null) {
    cachedTemplate = fs.readFileSync(INDEX_HTML_PATH, "utf8");
  }
  return cachedTemplate;
}

/** Strip the entry-point <script> and stylesheet <link> tags from
 *  index.html so we can render the body markup directly into happy-dom
 *  without triggering the production bootstrap or external network
 *  requests. */
function stripUnwantedTags(html: string): string {
  let out = html;
  out = out.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "");
  out = out.replace(/<link\b[^>]*rel=["']stylesheet["'][^>]*>/gi, "");
  return out;
}

function extractBody(html: string): string {
  const match = /<body[^>]*>([\s\S]*?)<\/body>/i.exec(html);
  if (match === null || match[1] === undefined) {
    throw new Error("index.html template missing a <body> element");
  }
  return match[1];
}

const BODY_HTML = extractBody(stripUnwantedTags(loadTemplate()));

type RequestJokeFn = (...args: unknown[]) => Promise<JokeApiResponse>;

/**
 * Reset the DOM, reset module singletons, and run main.ts's
 * auto-bootstrap against a fresh module graph. Returns the freshly
 * bound `requestJoke` mock.
 *
 * vi.resetModules() guarantees that:
 *   - main.ts's top-level `if (document.readyState ...) bootstrap()`
 *     fires against the new DOM (readyState is "complete" in
 *     happy-dom by the time the import resolves).
 *   - main.ts's module-level `inFlight` flag starts at `false`.
 *   - The vi.mock declaration above re-applies on the next import,
 *     so the mock function is re-created and must be re-bound.
 */
async function setupShell(): Promise<MockedFunction<RequestJokeFn>> {
  vi.resetModules();
  document.head.innerHTML = "";
  document.body.innerHTML = BODY_HTML;

  const adModule = await import("../../src/ad_module.js");
  adModule.__resetForTests();

  const apiModule = await import("../../src/api.js");
  const mock = vi.mocked(apiModule.requestJoke) as MockedFunction<
    RequestJokeFn
  >;
  mock.mockReset();

  // Importing main.ts runs its top-level bootstrap call, which wires
  // up the submit handler against the freshly-rendered DOM.
  await import("../../src/main.js");

  return mock;
}

// ---------------------------------------------------------------------------
// DOM lookup helpers
// ---------------------------------------------------------------------------

function getGenerateButton(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: /^generate$/i,
  }) as HTMLButtonElement;
}

function getById<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (el === null) {
    throw new Error(`#${id} missing from rendered DOM`);
  }
  return el as T;
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

function buildSuccess(overrides: Partial<JokeApiSuccess> = {}): JokeApiSuccess {
  return {
    kind: "success",
    id: "joke-prop-21",
    text: "Property 21 joke text.",
    audioUrl: null,
    audioDownloadUrl: null,
    audioAvailable: false,
    remaining: 4,
    modelId: "anthropic.claude-3-haiku",
    voiceId: "Joanna",
    ...overrides,
  };
}

function buildError(overrides: Partial<JokeApiError> = {}): JokeApiError {
  return {
    kind: "error",
    category: "internal_error",
    message: "Something went wrong.",
    ...overrides,
  };
}

async function submit(): Promise<void> {
  fireEvent.click(getGenerateButton());
  // Yield once so handleSubmit's first `await` runs.
  await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Lifecycle: each example test starts fresh.
// ---------------------------------------------------------------------------

afterEach(() => {
  vi.useRealTimers();
});

// ===========================================================================
// Example tests (specific values that anchor the property)
// ===========================================================================

describe("Property 21 — example anchors", () => {
  it("remaining=0 disables the button and sets aria-disabled='true'", async () => {
    const mock = await setupShell();
    mock.mockResolvedValueOnce(buildSuccess({ remaining: 0 }));

    await submit();

    const btn = getGenerateButton();
    await waitFor(() => {
      expect(btn.disabled).toBe(true);
    });
    expect(btn.getAttribute("aria-disabled")).toBe("true");
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("0");
  });

  it("remaining=1 leaves the button enabled with no aria-disabled", async () => {
    const mock = await setupShell();
    mock.mockResolvedValueOnce(buildSuccess({ remaining: 1 }));

    await submit();

    const btn = getGenerateButton();
    await waitFor(() => {
      expect(getById<HTMLElement>("remaining-count").textContent).toBe("1");
    });
    expect(btn.disabled).toBe(false);
    expect(btn.hasAttribute("aria-disabled")).toBe(false);
  });

  it("remaining=null keeps the badge hidden and the button enabled", async () => {
    const mock = await setupShell();
    mock.mockResolvedValueOnce(buildSuccess({ remaining: null }));

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("joke-display").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("remaining-badge").hidden).toBe(true);
    const btn = getGenerateButton();
    expect(btn.disabled).toBe(false);
    expect(btn.hasAttribute("aria-disabled")).toBe(false);
  });

  it("rate_limited error forces remaining=0 and disables the button", async () => {
    const mock = await setupShell();
    mock.mockResolvedValueOnce(
      buildError({
        category: "rate_limited",
        message: "You've reached today's limit.",
        resetAtUtc: "2025-01-15T00:00:00Z",
      }),
    );

    await submit();

    const btn = getGenerateButton();
    await waitFor(() => {
      expect(btn.disabled).toBe(true);
    });
    expect(getById<HTMLElement>("remaining-badge").hidden).toBe(false);
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("0");
    expect(btn.getAttribute("aria-disabled")).toBe("true");
  });
});

// ===========================================================================
// fast-check property: button.disabled === (remaining === 0)
// ===========================================================================

describe("Property 21 — universal property", () => {
  it("∀ remaining ∈ [0, 100]: button.disabled ⇔ remaining === 0", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 0, max: 100 }),
        async (remaining: number) => {
          const mock = await setupShell();
          mock.mockResolvedValueOnce(buildSuccess({ remaining }));

          await submit();

          const btn = getGenerateButton();
          await waitFor(() => {
            expect(getById<HTMLElement>("remaining-count").textContent).toBe(
              String(remaining),
            );
          });

          expect(btn.disabled).toBe(remaining === 0);
        },
      ),
      { numRuns: 100 },
    );
  });

  it("∀ remaining ∈ [1, 100]: aria-disabled is NOT set after success", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 1, max: 100 }),
        async (remaining: number) => {
          const mock = await setupShell();
          mock.mockResolvedValueOnce(buildSuccess({ remaining }));

          await submit();

          const btn = getGenerateButton();
          await waitFor(() => {
            expect(getById<HTMLElement>("remaining-count").textContent).toBe(
              String(remaining),
            );
          });

          expect(btn.hasAttribute("aria-disabled")).toBe(false);
        },
      ),
      { numRuns: 100 },
    );
  });
});
