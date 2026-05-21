// HTTP client for the Joke_API.
//
// Task 12.1 declared the public types and a stub `requestJoke` so the
// primary view in main.ts could compile and exercise its render paths.
// Task 12.2 (this file) replaces the stub with the real fetch-based
// client: 30 s timeout, error-category mapping, and message
// sanitization (R7.5, R7.6). The exported types and the `requestJoke`
// signature are unchanged so main.ts compiles without modification.

import { getApiBaseUrl } from "./config.js";

/**
 * Possible error categories returned by the backend's response_builder
 * (see src/joke_api/response_builder.py) plus three pseudo-categories
 * the network layer adds:
 *
 *   - "internal_error" — unexpected client bug or unparseable body.
 *   - "network"        — fetch itself failed (DNS, TLS, offline...).
 *   - "timeout"        — the 30 s AbortController fired before a
 *                        response arrived (R7.5 explicit ceiling).
 *
 * The frontend never inspects any other field for branching behaviour;
 * structured fields like `resetAtUtc` are passed through opaquely.
 */
export type JokeApiErrorCategory =
  | "validation"
  | "moderation"
  | "moderation_timeout"
  | "moderation_unavailable"
  | "rate_limited"
  | "client_ip_unresolvable"
  | "unavailable"
  | "internal_error"
  | "not_found"
  | "network"
  | "timeout";

/** Successful response shape (mirrors response_builder.build_success). */
export interface JokeApiSuccess {
  readonly kind: "success";
  readonly id: string;
  readonly text: string;
  readonly audioUrl: string | null;
  readonly audioAvailable: boolean;
  readonly remaining: number | null;
  readonly modelId: string;
  readonly voiceId: string;
}

/** Sanitized error response. `message` is the human-readable text to
 *  display to the visitor; it must never contain stack traces, ARNs,
 *  account ids, or other internal identifiers (R7.5). */
export interface JokeApiError {
  readonly kind: "error";
  readonly category: JokeApiErrorCategory;
  readonly message: string;
  readonly resetAtUtc?: string;
  readonly rule?: string;
}

export type JokeApiResponse = JokeApiSuccess | JokeApiError;

export interface JokeRequest {
  readonly seedWords: readonly string[];
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Hard ceiling on a single request, per R7.5 ("network timeout
 *  exceeding 30 seconds"). Any AbortError fired by this timer is
 *  surfaced as category="timeout". */
const REQUEST_TIMEOUT_MS = 30_000;

/** Path of the generation endpoint, joined to `getApiBaseUrl()`. */
const JOKES_PATH = "/v1/jokes";

/** Maximum length of any error message rendered to the visitor. The
 *  backend's static messages are well under this; the cap exists to
 *  defend against an unparseable body that smuggled through. */
const MAX_MESSAGE_LENGTH = 200;

// ---------------------------------------------------------------------------
// Sanitization (R7.5)
// ---------------------------------------------------------------------------

/**
 * Strip patterns that look like internal identifiers from a string.
 *
 * The backend already returns sanitized messages, but the network layer
 * is the last line of defence: if a misconfigured stage ever leaked a
 * stack trace or AWS identifier into the body, it must not be rendered
 * to the visitor. The following patterns are removed:
 *
 *   - "Traceback (most recent call last):"  Python stack-trace banner.
 *   - "arn:aws:..."                          AWS resource ARNs.
 *   - "/foo/bar.py", "module.py"            Python source paths.
 *   - 12-digit AWS account identifiers.
 *   - IPv4 addresses.
 *   - http(s):// URLs (presigned audio URLs especially must not echo
 *     into errors).
 *
 * Validates: Requirements 7.5, 7.6.
 */
export function sanitizeMessage(input: string): string {
  if (typeof input !== "string") {
    return "";
  }

  let out = input;

  // Strip Python stack-trace banners and any text that follows on the
  // same line. We don't try to perfectly re-format multi-line traces;
  // we just guarantee the marker word never reaches the visitor.
  out = out.replace(/Traceback[^\n]*/gi, "");
  out = out.replace(/\bat\s+[^\s,]+\.(py|ts|js)\b[^\n]*/gi, "");

  // AWS resource ARNs (arn:aws:service:region:account:resource).
  out = out.replace(/arn:aws:[A-Za-z0-9:_/.\-*]+/gi, "");

  // Python source paths (e.g. "src/joke_api/handler.py", "handler.py").
  out = out.replace(/\b[A-Za-z][A-Za-z_/]*\.py\b/g, "");

  // 12-digit AWS account identifiers (must run before generic numbers).
  out = out.replace(/\b\d{12}\b/g, "");

  // IPv4 addresses (R16.7 forbids logging raw IPs; defence-in-depth
  // here ensures none ever render either).
  out = out.replace(/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g, "");

  // Any http(s):// URL — presigned audio URLs include the bucket name
  // and signature query string and must not appear in error copy.
  out = out.replace(/https?:\/\/\S+/gi, "");

  // Collapse runs of whitespace produced by the removals.
  out = out.replace(/\s+/g, " ").trim();

  // Final length cap. Slice on a code-unit boundary; we do not need
  // grapheme-cluster awareness for static error copy.
  if (out.length > MAX_MESSAGE_LENGTH) {
    out = out.slice(0, MAX_MESSAGE_LENGTH).trimEnd();
  }

  return out;
}

// ---------------------------------------------------------------------------
// Category-specific human-readable copy (R7.5)
// ---------------------------------------------------------------------------

/** Visitor-facing copy keyed by error category. The backend supplies
 *  static, sanitized messages; we layer this map on top so every code
 *  path — including the synthetic "network"/"timeout"/"internal_error"
 *  categories — has a guaranteed fallback. */
const CATEGORY_COPY: Readonly<Record<JokeApiErrorCategory, string>> = {
  validation: "Please check your seed words and try again.",
  moderation: "That seed couldn't be used.",
  moderation_timeout: "The safety check timed out. Please try again.",
  moderation_unavailable: "Safety checks are temporarily down.",
  rate_limited: "You've reached today's limit.",
  client_ip_unresolvable: "Unable to verify your request origin.",
  unavailable: "The joke service is temporarily unavailable.",
  not_found: "That joke is no longer available.",
  network: "Could not reach the joke service.",
  timeout: "The request took too long.",
  internal_error: "Something went wrong on our end.",
};

/**
 * Resolve the user-facing message for an error category.
 *
 * If the (already-sanitized) backend message is non-empty and looks
 * like real visitor copy, prefer it; otherwise fall back to the static
 * per-category default. Either way the result is bounded by
 * MAX_MESSAGE_LENGTH.
 *
 * Validates: Requirements 7.5, 7.6.
 */
export function humanizeError(
  category: JokeApiErrorCategory,
  message: string,
): string {
  const sanitized = sanitizeMessage(message);
  const fallback = CATEGORY_COPY[category];
  if (sanitized.length === 0) {
    return fallback;
  }
  return sanitized;
}

// ---------------------------------------------------------------------------
// Body parsing helpers
// ---------------------------------------------------------------------------

/** Type guard for the success body shape returned by build_success. */
function isSuccessBody(value: unknown): value is {
  id: string;
  text: string;
  audioUrl: string | null;
  audioAvailable: boolean;
  remaining?: number | null;
  modelId: string;
  voiceId: string;
} {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const v = value as Record<string, unknown>;
  return (
    typeof v["id"] === "string" &&
    typeof v["text"] === "string" &&
    typeof v["audioAvailable"] === "boolean" &&
    typeof v["modelId"] === "string" &&
    typeof v["voiceId"] === "string" &&
    (v["audioUrl"] === null || typeof v["audioUrl"] === "string") &&
    (v["remaining"] === undefined ||
      v["remaining"] === null ||
      typeof v["remaining"] === "number")
  );
}

/** Coerce an unknown JSON value into the `JokeApiErrorCategory`
 *  enumeration. Anything outside the enum maps to "internal_error". */
function coerceCategory(value: unknown): JokeApiErrorCategory {
  if (typeof value !== "string") {
    return "internal_error";
  }
  switch (value) {
    case "validation":
    case "moderation":
    case "moderation_timeout":
    case "moderation_unavailable":
    case "rate_limited":
    case "client_ip_unresolvable":
    case "unavailable":
    case "not_found":
    case "internal_error":
      return value;
    default:
      return "internal_error";
  }
}

/** Build a sanitized JokeApiError envelope. */
function buildError(
  category: JokeApiErrorCategory,
  rawMessage: string,
  extras: { resetAtUtc?: unknown; rule?: unknown } = {},
): JokeApiError {
  const error: {
    kind: "error";
    category: JokeApiErrorCategory;
    message: string;
    resetAtUtc?: string;
    rule?: string;
  } = {
    kind: "error",
    category,
    message: humanizeError(category, rawMessage),
  };
  if (typeof extras.resetAtUtc === "string" && extras.resetAtUtc.length > 0) {
    error.resetAtUtc = sanitizeMessage(extras.resetAtUtc);
  }
  if (typeof extras.rule === "string" && extras.rule.length > 0) {
    error.rule = sanitizeMessage(extras.rule);
  }
  return error;
}

/**
 * Translate a parsed HTTP response body into a JokeApiResponse.
 *
 * - 2xx + valid success body → `kind: "success"`.
 * - 2xx + malformed body     → category="internal_error".
 * - 4xx with a parseable body → category from `body.error`.
 * - 4xx with no/bad body      → category="internal_error".
 * - 5xx                       → category from body when present, else
 *                               "unavailable".
 */
function bodyToResponse(status: number, body: unknown): JokeApiResponse {
  if (status >= 200 && status < 300) {
    if (!isSuccessBody(body)) {
      return buildError("internal_error", "");
    }
    const remaining =
      typeof body.remaining === "number" ? body.remaining : null;
    return {
      kind: "success",
      id: body.id,
      text: body.text,
      audioUrl: body.audioUrl,
      audioAvailable: body.audioAvailable,
      remaining,
      modelId: body.modelId,
      voiceId: body.voiceId,
    };
  }

  // Error path. Pull category + message + extras from the body if it
  // looks well-formed; otherwise fall back by HTTP class.
  if (body !== null && typeof body === "object") {
    const obj = body as Record<string, unknown>;
    const category = coerceCategory(obj["error"]);
    const message = typeof obj["message"] === "string" ? obj["message"] : "";
    return buildError(category, message, {
      resetAtUtc: obj["resetAtUtc"],
      rule: obj["rule"],
    });
  }

  if (status >= 500) {
    return buildError("unavailable", "");
  }
  return buildError("internal_error", "");
}

// ---------------------------------------------------------------------------
// Request entry point
// ---------------------------------------------------------------------------

/**
 * Issue a generation request to POST /v1/jokes.
 *
 * Enforces the R7.5 timeout ceiling (30 s) via AbortController, maps
 * every failure mode to a sanitized JokeApiError, and never throws to
 * the caller — main.ts treats a thrown exception as an unexpected
 * client bug, but the contract here is to return a `JokeApiResponse`
 * for every input.
 *
 * Validates: Requirements 7.5, 7.6.
 */
export async function requestJoke(
  request: JokeRequest,
): Promise<JokeApiResponse> {
  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => {
    controller.abort();
  }, REQUEST_TIMEOUT_MS);

  const url = `${getApiBaseUrl()}${JOKES_PATH}`;
  const payload = JSON.stringify({ seedWords: [...request.seedWords] });

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: payload,
      signal: controller.signal,
      // Same-origin requests behind CloudFront; an explicit value
      // documents the intent and avoids cross-origin cookie surprises.
      credentials: "same-origin",
    });
  } catch (err) {
    clearTimeout(timeoutHandle);
    if (isAbortError(err)) {
      return buildError("timeout", "");
    }
    return buildError("network", "");
  }

  clearTimeout(timeoutHandle);

  // Parse the body once; an unparseable body still flows through
  // bodyToResponse so the HTTP class drives the fallback category.
  let parsed: unknown = null;
  try {
    const text = await response.text();
    if (text.length > 0) {
      parsed = JSON.parse(text);
    }
  } catch {
    parsed = null;
  }

  return bodyToResponse(response.status, parsed);
}

/** Detect the AbortError that fires when the 30 s timer trips. The
 *  spec says fetch rejects with a DOMException whose name is
 *  "AbortError"; some runtimes use a plain Error subclass with the
 *  same name, so we match by the `name` field instead of the class. */
function isAbortError(err: unknown): boolean {
  if (err === null || typeof err !== "object") {
    return false;
  }
  const name = (err as { name?: unknown }).name;
  return name === "AbortError";
}
