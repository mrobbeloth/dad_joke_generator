// Build helper used by the @axe-core/playwright accessibility suite.
//
// The SPA source ships with `<script type="module" src="./main.ts">`
// so the development workflow can import TypeScript directly through a
// bundler. The Playwright accessibility suite needs a shippable build
// served over HTTP so the browser can render the same DOM a visitor
// sees, without TypeScript-aware tooling. This helper:
//
//   1. Compiles `src/*.ts` to `dist/*.js` via `tsc`. The existing
//      `tsconfig.json` already pins `outDir` to `./dist`, so this
//      script invokes `tsc` with no arguments.
//   2. Copies `src/index.html` to `dist/index.html`, rewriting the
//      script reference from `./main.ts` to `./main.js` so the
//      browser loads the compiled bundle.
//   3. Copies `src/styles.css` to `dist/styles.css` verbatim.
//
// Run with `node build_a11y.mjs` (or via `npm run build:a11y`). The
// Playwright `webServer` command runs this before starting the
// static server.

import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "src");
const DIST = resolve(HERE, "dist");

function log(message) {
  process.stdout.write(`[build:a11y] ${message}\n`);
}

function runTsc() {
  log("compiling TypeScript with `tsc`");
  // No arguments: rely on tsconfig.json (outDir ./dist, rootDir ./src).
  // Use shell:true so the platform-appropriate `npx` shim resolves on
  // both Windows and POSIX runners.
  const result = spawnSync("npx", ["--no-install", "tsc"], {
    cwd: HERE,
    stdio: "inherit",
    shell: true,
  });
  if (result.status !== 0) {
    process.exitCode = result.status ?? 1;
    throw new Error(`tsc exited with status ${result.status}`);
  }
}

function ensureDist() {
  mkdirSync(DIST, { recursive: true });
}

function copyHtml() {
  const htmlSrc = resolve(SRC, "index.html");
  const htmlDst = resolve(DIST, "index.html");
  log(`rewriting ${htmlSrc} -> ${htmlDst}`);
  const original = readFileSync(htmlSrc, "utf8");
  // Match the exact attribute sequence shipped in src/index.html and
  // swap the .ts extension for .js. The regex is intentionally strict
  // so a malformed source file fails loudly rather than silently
  // shipping a broken script tag.
  const rewritten = original.replace(
    /<script\s+type="module"\s+src="\.\/main\.ts"\s*><\/script>/,
    '<script type="module" src="./main.js"></script>',
  );
  if (rewritten === original) {
    throw new Error(
      'index.html did not contain the expected `<script src="./main.ts">` reference; ' +
        "build_a11y.mjs needs an update.",
    );
  }
  writeFileSync(htmlDst, rewritten, "utf8");
}

function copyStyles() {
  const cssSrc = resolve(SRC, "styles.css");
  const cssDst = resolve(DIST, "styles.css");
  log(`copying ${cssSrc} -> ${cssDst}`);
  copyFileSync(cssSrc, cssDst);
}

runTsc();
ensureDist();
copyHtml();
copyStyles();
log("done");
