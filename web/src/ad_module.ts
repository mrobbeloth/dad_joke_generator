// Flag-gated advertising banner module.
//
// Behaviour matrix (R8.1–R8.6):
//
//   adModuleEnabled = false  -> do nothing. No DOM mutation. No script
//                              tag. No third-party network request.
//                              The slot's `:empty` rule in styles.css
//                              collapses it to zero layout space.
//
//   adModuleEnabled = true,  -> lazy-load exactly one configured
//   adNetworkId allowlisted     ad-network loader script with a 3 s
//                              timeout. On script load, insert one
//                              ad-placeholder element into the slot.
//                              On script error or timeout, remove the
//                              script and leave the slot empty.
//
//   adModuleEnabled = true,  -> treat as if disabled. Never inject a
//   adNetworkId not in allow-    script. Logged once at debug level.
//   list / empty
//
// The module also enforces a single-injection invariant: across the
// page lifetime no more than one ad-loader script is appended,
// regardless of how many times `mountAdModule()` is invoked. Tests
// reset this with the `__resetForTests` symbol.
//
// Validates: R8.1, R8.2, R8.3, R8.4, R8.5, R8.6.

import { getConfig, type AppConfig, type GetConfigOptions } from "./config.js";

// ---------------------------------------------------------------------------
// Allowlist of ad networks
// ---------------------------------------------------------------------------

/** Per-network loader-script URL. R8.4 requires that, when enabled,
 *  the only third-party domain we touch is the configured ad network.
 *  Hardcoding the URL per identifier prevents a misconfigured
 *  `adNetworkId` from steering the loader to an arbitrary host. */
const NETWORK_LOADER_URLS: Readonly<Record<string, string>> = {
  adsense: "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
  // A deterministic test value used by 12.4 property tests.
  "fixed-test-network": "https://example.test/loader.js",
} as const;

/** Per-network "ad placeholder" markup inserted into the slot once the
 *  loader script reports onload. Networks not listed here render an
 *  empty slot even on success — defence-in-depth for R8.6. */
const NETWORK_PLACEHOLDERS: Readonly<
  Record<string, (slot: HTMLElement) => void>
> = {
  adsense: (slot) => {
    const ins = document.createElement("ins");
    ins.className = "adsbygoogle";
    ins.style.display = "block";
    ins.setAttribute("data-ad-format", "auto");
    ins.setAttribute("data-full-width-responsive", "true");
    slot.appendChild(ins);
  },
  "fixed-test-network": (slot) => {
    const div = document.createElement("div");
    div.className = "ad-placeholder";
    div.setAttribute("data-network", "fixed-test-network");
    slot.appendChild(div);
  },
} as const;

// ---------------------------------------------------------------------------
// Singleton state
// ---------------------------------------------------------------------------

interface ModuleState {
  /** True once a `<script>` element has been appended to the document
   *  during this page lifetime. Prevents duplicate injections (R8.4
   *  "exactly one configured ad-network script"). */
  scriptInjected: boolean;
  /** True once `mountAdModule` has begun work for this page lifetime,
   *  even if the flag was disabled. Prevents duplicate config fetches
   *  if the bootstrap accidentally calls the entry point twice. */
  mountStarted: boolean;
}

const state: ModuleState = {
  scriptInjected: false,
  mountStarted: false,
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Optional dependency injection for tests. */
export interface MountAdModuleOptions {
  /** DOM id of the ad slot. Defaults to `"ad-slot"` (matches index.html). */
  readonly slotId?: string;
  /** Hard timeout (ms) for the ad-loader script. Defaults to 3000 (R8.5). */
  readonly timeoutMs?: number;
  /** Override config fetch. Defaults to `getConfig()` from config.ts. */
  readonly configFetcher?: (
    options?: GetConfigOptions,
  ) => Promise<AppConfig>;
  /** Override script loader. Defaults to DOM `<script>` injection.
   *  Tests inject a synchronous stub. */
  readonly scriptLoader?: ScriptLoader;
}

/** Per-call result from a script loader. The loader resolves with
 *  `{ ok: true }` once the network returned a script, `{ ok: false }`
 *  on timeout or transport error. The loader is also responsible for
 *  cleaning up its own DOM artefacts on failure. */
export type ScriptLoadResult = { readonly ok: true } | { readonly ok: false };

export type ScriptLoader = (params: {
  readonly src: string;
  readonly timeoutMs: number;
}) => Promise<ScriptLoadResult>;

const DEFAULT_SLOT_ID = "ad-slot";
const DEFAULT_TIMEOUT_MS = 3000;

/**
 * Wire the ad module to the page. Call once at bootstrap.
 *
 * The function never throws upward: any failure leaves the slot empty
 * and the rest of the SPA unaffected (R8.6 defence-in-depth).
 */
export async function mountAdModule(
  options: MountAdModuleOptions = {},
): Promise<void> {
  if (state.mountStarted) {
    return;
  }
  state.mountStarted = true;

  const slotId = options.slotId ?? DEFAULT_SLOT_ID;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const configFetcher = options.configFetcher ?? getConfig;
  const scriptLoader = options.scriptLoader ?? defaultScriptLoader;

  let config: AppConfig;
  try {
    config = await configFetcher();
  } catch {
    // Config fetch failed: treat as disabled. No DOM mutation, no
    // third-party request (R8.3).
    return;
  }

  // R8.1, R8.3: when disabled, do absolutely nothing. The slot stays
  // empty and `.ad-slot:empty { display: none }` collapses it.
  if (!config.adModuleEnabled) {
    return;
  }

  const networkId = config.adNetworkId;
  // Guard against `Object.prototype` method-name lookups (`toString`,
  // `hasOwnProperty`, `valueOf`, etc.). Plain index access on a
  // `Record<string, ...>` returns the inherited prototype method when
  // the key matches a method name, which would bypass the allowlist
  // and let an arbitrary input drive `<script>` injection
  // (Property 22 / R8.4). `Object.hasOwn` is the structural guarantee.
  if (
    !Object.hasOwn(NETWORK_LOADER_URLS, networkId) ||
    !Object.hasOwn(NETWORK_PLACEHOLDERS, networkId)
  ) {
    return;
  }
  const loaderUrl = NETWORK_LOADER_URLS[networkId];
  const placeholderFn = NETWORK_PLACEHOLDERS[networkId];

  // Unknown / unconfigured network: don't load anything. Leaving the
  // slot empty satisfies R8.4 by NOT initiating a third-party request
  // to an unrecognised host.
  if (loaderUrl === undefined || placeholderFn === undefined) {
    return;
  }

  const slot = document.getElementById(slotId);
  if (slot === null) {
    return;
  }

  // Single-injection invariant (R8.4).
  if (state.scriptInjected) {
    return;
  }
  state.scriptInjected = true;

  let result: ScriptLoadResult;
  try {
    result = await scriptLoader({ src: loaderUrl, timeoutMs });
  } catch {
    // Loader threw despite the contract: treat as failure (R8.6).
    return;
  }

  if (!result.ok) {
    // Timeout or transport error. Slot stays empty (R8.5, R8.6).
    return;
  }

  // Success path: insert exactly one placeholder element. Only mutate
  // the slot if it is still empty so a second mount call cannot
  // accumulate placeholders.
  if (slot.childElementCount === 0) {
    placeholderFn(slot);
  }
}

// ---------------------------------------------------------------------------
// Default script loader (DOM `<script>` injection with 3 s timeout)
// ---------------------------------------------------------------------------

const defaultScriptLoader: ScriptLoader = ({ src, timeoutMs }) =>
  new Promise<ScriptLoadResult>((resolve) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.referrerPolicy = "no-referrer-when-downgrade";

    let settled = false;
    const cleanup = (removeScript: boolean): void => {
      if (settled) {
        return;
      }
      settled = true;
      window.clearTimeout(timer);
      if (removeScript && script.parentNode !== null) {
        script.parentNode.removeChild(script);
      }
    };

    const timer = window.setTimeout(() => {
      // R8.5: deliver-by-3 s; on miss remove the script and leave slot
      // empty.
      cleanup(true);
      resolve({ ok: false });
    }, timeoutMs);

    script.onload = (): void => {
      cleanup(false);
      resolve({ ok: true });
    };
    script.onerror = (): void => {
      // R8.6: transport error -> empty slot, no error message anywhere.
      cleanup(true);
      resolve({ ok: false });
    };

    document.head.appendChild(script);
  });

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/** Reset the module-level singleton state. ONLY for use by tests. */
export function __resetForTests(): void {
  state.scriptInjected = false;
  state.mountStarted = false;
}

/** Read-only view of the singleton state. Tests assert against this
 *  to confirm the disabled path made no mutation. */
export function __getStateForTests(): Readonly<ModuleState> {
  return { ...state };
}
