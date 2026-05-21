// Component-test setup helper.
//
// Loads the production index.html template into the happy-dom document
// so the tests render against the same markup the SPA ships. Strips
// the inline <script type="module"> tag (we bootstrap manually) and
// removes stylesheet links (styles are not required for behavioural
// assertions).
//
// Used by web/tests/component/test_primary_view.test.ts (task 12.5).

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/** Absolute path to the SPA template. */
const INDEX_HTML_PATH = path.resolve(__dirname, "..", "..", "src", "index.html");

/** Read and cache index.html once per test process. */
let cachedTemplate: string | null = null;

function loadTemplate(): string {
  if (cachedTemplate === null) {
    cachedTemplate = fs.readFileSync(INDEX_HTML_PATH, "utf8");
  }
  return cachedTemplate;
}

/**
 * Strip pieces of the template the component tests don't need:
 *   - the entry-point <script type="module"> tag (we bootstrap
 *     manually so vi.mock() of the api module takes effect first)
 *   - stylesheet <link rel="stylesheet"> tags (no styles needed for
 *     behavioural assertions; happy-dom doesn't run a real stylesheet
 *     either way)
 */
function stripUnwantedTags(html: string): string {
  let out = html;
  out = out.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "");
  out = out.replace(/<link\b[^>]*rel=["']stylesheet["'][^>]*>/gi, "");
  return out;
}

/** Extract everything between <body ...> and </body>. */
function extractBody(html: string): string {
  const match = /<body[^>]*>([\s\S]*?)<\/body>/i.exec(html);
  if (match === null || match[1] === undefined) {
    throw new Error("index.html template missing a <body> element");
  }
  return match[1];
}

/**
 * Render the SPA template into the current happy-dom document and
 * return a function that calls main.ts's exported `bootstrap()`.
 *
 * Test files should:
 *   1. `vi.mock("../../src/api.js", ...)` at module scope.
 *   2. `await renderShell()` inside `beforeEach` to reset the DOM.
 *   3. Drive interactions via `@testing-library/dom`.
 */
export async function renderShell(): Promise<() => void> {
  const template = stripUnwantedTags(loadTemplate());
  const body = extractBody(template);

  // Reset both head and body so a previous test cannot leak state
  // (event listeners on the form, residual `<script>` tags from
  // ad_module's default loader, etc.).
  document.head.innerHTML = "";
  document.body.innerHTML = body;

  // Reset ad_module singleton so each test starts fresh. Imported
  // dynamically because the module is also imported transitively by
  // main.ts and we want to share the same instance.
  const adModule = await import("../../src/ad_module.js");
  adModule.__resetForTests();

  // Import main.ts AFTER the DOM exists and after vi.mock() has been
  // declared in the calling test module. The ESM cache shares the
  // same module across calls, so `bootstrap` is invoked multiple
  // times against fresh DOMs in different tests.
  const main = await import("../../src/main.js");

  return () => {
    main.bootstrap();
  };
}

/**
 * Reset the in-flight submission flag exported by main.ts. main.ts
 * stores the flag as a module-level singleton; without this helper a
 * pending submit from one test could block the next one.
 *
 * The flag is private to main.ts so we cannot mutate it directly.
 * Instead each test calls `renderShell()` which reloads the DOM; the
 * `inFlight` flag is reset to `false` at the end of `handleSubmit`'s
 * `finally` block on every test that exercises submit, so this
 * helper is currently a no-op placeholder reserved for future use.
 */
export function resetSubmitState(): void {
  // No-op for now — see docstring.
}
