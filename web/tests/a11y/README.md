# Accessibility test suite (`@axe-core/playwright`)

Validates **R7.3** (WCAG 2.1 Level AA at 320, 768, 1280, and 1920 px viewports)
and **R7.4** (full keyboard operability of every interactive control on the
primary view). Implements task **12.6** from the spec.

## What it does

`test_primary_view.spec.ts` runs two tests:

1. **WCAG 2.1 AA scan.** Stubs `GET /v1/config` so `mountAdModule` short-circuits
   (no third-party network access during the run), navigates to `/`, waits for
   `#joke-form`, and runs
   `new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])`.
   The list of violations whose `impact` is `"serious"` or `"critical"` must be
   empty. On failure the full violation set is dumped to the build log via
   `JSON.stringify(violations, null, 2)`. This test runs once per Playwright
   project, i.e. once at each of the four viewport widths.
2. **Keyboard reachability.** Walks `Tab` through the page and asserts that
   `#seed-input` and `#generate-btn` are both reachable from `<body>`. The
   replay button is hidden until a successful joke renders, so it is excluded
   from the initial-load tab order. Runs only on the `desktop-1280` project
   (the assertion is viewport-independent).

Total: **5 test invocations** per run (4 axe scans + 1 keyboard scan).

## One-time setup

```sh
cd web
npm install
npx playwright install chromium
```

`npx playwright install chromium` downloads roughly 230 MB of browser binaries
into `%LOCALAPPDATA%\ms-playwright\` (Windows) or `~/.cache/ms-playwright/`
(macOS / Linux). The download is required because Playwright does not bundle
browsers inside the npm package.

## Running

```sh
cd web
npm run test:a11y
```

Under the hood this runs `playwright test --config=playwright.config.ts`.
Playwright's `webServer` block invokes `node build_a11y.mjs` to compile the
SPA into `dist/` and then serves the static output with
`npx http-server dist -p 8080 -c-1 --silent`. The build step is idempotent so
re-runs always pick up source changes.

## Build helper

`build_a11y.mjs` (in `web/`) is a small Node script that:

1. Spawns `tsc` with no arguments (the existing `tsconfig.json` already pins
   `outDir` to `./dist` and `rootDir` to `./src`).
2. Copies `src/index.html` to `dist/index.html`, rewriting
   `<script type="module" src="./main.ts">` to
   `<script type="module" src="./main.js">` so the browser loads the compiled
   bundle.
3. Copies `src/styles.css` to `dist/styles.css` verbatim.

## Known environment caveats

### Validation status

The suite was validated end-to-end on the development machine (Chromium
131.0.6778.33, Playwright 1.49.1, Windows 11) by binding the static server to
port 4173 instead of 8080. All 5 tests passed: four axe scans (one per
viewport, zero serious/critical violations on each) plus the keyboard
reachability test on `desktop-1280`. The committed `playwright.config.ts`
keeps port 8080 because that is what task 12.6 and the CI runner expect; the
validation port change was throwaway.

### Windows: port 8080 may be in a reserved range

On some Windows machines (especially after Hyper-V / WSL2 / Docker Desktop
have been installed) the kernel reserves port ranges that include 8080. The
exact ranges are visible via:

```sh
netsh interface ipv4 show excludedportrange protocol=tcp
```

If 8080 falls inside an excluded range, `http-server` fails with:

```
Error: listen EACCES: permission denied 0.0.0.0:8080
```

Two workarounds, in order of preference:

1. **Free the port range (admin privileges required).** Run, in an elevated
   shell, `net stop winnat` then `net start winnat`. Windows reshuffles the
   dynamic exclusions on every NAT restart and the new ranges typically
   leave 8080 free.
2. **Pick a different port locally.** Edit the `PORT` constant at the top of
   `playwright.config.ts` (and the `webServer.command` string) to a port
   outside every excluded range (e.g. 4173 or 5173). CI runs are unaffected
   because the GitHub Actions runners do not reserve 8080.

### Sandbox / restricted CI environments

Some hardened CI environments block raw browser launches. Playwright honours
the `PWDEBUG`, `DEBUG`, and `HEADED` env vars; the suite is configured for
fully-headless execution by default and does not require a display server.

## Files owned by task 12.6

- `web/playwright.config.ts`
- `web/build_a11y.mjs`
- `web/tests/a11y/test_primary_view.spec.ts`
- `web/tests/a11y/README.md` (this file)
- `http-server` devDependency and `build:a11y` / `test:a11y` scripts in
  `web/package.json`
