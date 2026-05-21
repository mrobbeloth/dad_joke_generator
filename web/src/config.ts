// Frontend configuration loader.
//
// Task 12.2 introduces the API base URL helper used by api.ts. In
// production the SPA is served from the same CloudFront distribution
// that fronts the Joke_API, so the default base URL is the empty
// string (relative requests). For local development the
// `VITE_API_BASE_URL` build-time env variable can point the SPA at a
// remote/local backend without touching source.
//
// 12.3 owns the ad-feature-flag and ad-network-id parts of this
// module; the `AppConfig` / `GetConfigOptions` / `getConfig`
// declarations below are minimal forward-compatible stubs that exist
// only so `ad_module.ts` (also part of 12.3) compiles in isolation.
// Replacing the stub body with a real loader is 12.3's responsibility.

// ---------------------------------------------------------------------------
// API base URL (owned by 12.2)
// ---------------------------------------------------------------------------

/**
 * Narrowly-typed view of the Vite-style import.meta.env object.
 *
 * The SPA build pipeline is bundler-agnostic at the TypeScript layer:
 * if no bundler is configured, ``import.meta.env`` is ``undefined`` and
 * the helper falls back to the empty default. The cast through
 * ``unknown`` is required because the TypeScript dom lib does not
 * declare an ``env`` member on ``ImportMeta``.
 */
interface ApiBaseUrlEnv {
  readonly VITE_API_BASE_URL?: string;
}

/**
 * Return the Joke_API base URL.
 *
 * Defaults to ``""`` so requests resolve relative to the current
 * document origin (same-origin behind CloudFront). A trailing slash
 * on the override is stripped so the caller can compose
 * ``${base}${path}`` without doubling separators.
 */
export function getApiBaseUrl(): string {
  const env = (import.meta as unknown as { env?: ApiBaseUrlEnv }).env;
  const raw = env?.VITE_API_BASE_URL;
  if (typeof raw !== "string") {
    return "";
  }
  return raw.replace(/\/+$/, "");
}

// ---------------------------------------------------------------------------
// Ad-module configuration shape (forward-compatible stubs for 12.3)
// ---------------------------------------------------------------------------

/** Minimum fields ad_module.ts (task 12.3) reads from the config
 *  document. The real loader will populate these from a static JSON
 *  asset; the stub below returns the disabled defaults so the SPA
 *  cannot accidentally render ads before 12.3 lands. */
export interface AppConfig {
  readonly adModuleEnabled: boolean;
  readonly adNetworkId: string;
}

/** Options accepted by `getConfig`. Empty for now — 12.3 may add
 *  cache-busting or fetch-overrides as the implementation grows. */
export interface GetConfigOptions {
  readonly _reserved?: never;
}

/** Stub config loader. Returns the disabled defaults so the ad module
 *  is a no-op until 12.3 wires in the real loader. */
export function getConfig(_options?: GetConfigOptions): Promise<AppConfig> {
  void _options;
  return Promise.resolve({
    adModuleEnabled: false,
    adNetworkId: "",
  });
}
