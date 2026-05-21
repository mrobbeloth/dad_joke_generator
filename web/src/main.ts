// Entry point for the Dad Joke Generator SPA.
//
// Task 12.1: Implement the primary view. Wires the seed-word form to the
// Joke_API client (api.ts), handles the response/error render paths, and
// reserves the ad slot for ad_module.ts (12.3) without touching its
// implementation.
//
// Validates: R7.1, R7.2, R7.3, R7.4, R7.7, R7.8, R2.5, R2.7.

import { mountAdModule } from "./ad_module.js";
import { requestJoke, type JokeApiError, type JokeApiResponse } from "./api.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Delay before the progress indicator becomes visible (R7.2 lower bound:
 *  "within 200 ms"). Showing the spinner immediately produces a flash on
 *  fast responses; waiting until 200 ms have elapsed avoids that without
 *  violating the requirement (the indicator is shown by 200 ms exactly). */
const PROGRESS_VISIBLE_AFTER_MS = 200;

/** Seed input bounds per R7.1. */
const SEED_MIN = 1;
const SEED_MAX = 50;

/** Allowed characters in a seed word per R1.7. Spaces are added so the
 *  visitor can type multiple words in one input field. */
const SEED_CHARSET = /^[A-Za-z0-9' \-]+$/;

// ---------------------------------------------------------------------------
// DOM element lookup
// ---------------------------------------------------------------------------

interface PrimaryViewElements {
  form: HTMLFormElement;
  seedInput: HTMLInputElement;
  seedError: HTMLElement;
  generateBtn: HTMLButtonElement;
  progress: HTMLElement;
  remainingBadge: HTMLElement;
  remainingCount: HTMLElement;
  errorMessage: HTMLElement;
  jokeDisplay: HTMLElement;
  jokeText: HTMLElement;
  audioControls: HTMLElement;
  jokeAudio: HTMLAudioElement;
  replayBtn: HTMLButtonElement;
}

function getRequired<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (el === null) {
    throw new Error(`Required element #${id} is missing from the DOM`);
  }
  return el as T;
}

function lookupElements(): PrimaryViewElements {
  return {
    form: getRequired<HTMLFormElement>("joke-form"),
    seedInput: getRequired<HTMLInputElement>("seed-input"),
    seedError: getRequired<HTMLElement>("seed-error"),
    generateBtn: getRequired<HTMLButtonElement>("generate-btn"),
    progress: getRequired<HTMLElement>("progress"),
    remainingBadge: getRequired<HTMLElement>("remaining-badge"),
    remainingCount: getRequired<HTMLElement>("remaining-count"),
    errorMessage: getRequired<HTMLElement>("error-message"),
    jokeDisplay: getRequired<HTMLElement>("joke-display"),
    jokeText: getRequired<HTMLElement>("joke-text"),
    audioControls: getRequired<HTMLElement>("audio-controls"),
    jokeAudio: getRequired<HTMLAudioElement>("joke-audio"),
    replayBtn: getRequired<HTMLButtonElement>("replay-btn"),
  };
}

// ---------------------------------------------------------------------------
// Input parsing & client-side validation
// ---------------------------------------------------------------------------

interface ValidationOk {
  readonly ok: true;
  readonly seedWords: readonly string[];
}

interface ValidationErr {
  readonly ok: false;
  readonly message: string;
}

type ValidationResult = ValidationOk | ValidationErr;

/** Parse the raw seed-input string into an array of seed words and apply
 *  the client-side rules from R7.5 and R1.7. The backend re-validates
 *  every rule; this function only catches obvious mistakes early so the
 *  visitor gets a fast inline message instead of waiting for a round
 *  trip. */
function parseSeedInput(raw: string): ValidationResult {
  const trimmed = raw.trim();

  // Empty input is allowed: R1.1 covers zero seed words.
  if (trimmed.length === 0) {
    return { ok: true, seedWords: [] };
  }

  if (trimmed.length > SEED_MAX) {
    return {
      ok: false,
      message: `Seed words must be ${SEED_MAX} characters or fewer.`,
    };
  }

  if (trimmed.length < SEED_MIN) {
    return {
      ok: false,
      message: `Seed words must be at least ${SEED_MIN} character.`,
    };
  }

  if (!SEED_CHARSET.test(trimmed)) {
    return {
      ok: false,
      message:
        "Seed words may only contain letters, digits, hyphens, apostrophes, and spaces.",
    };
  }

  const seedWords = trimmed
    .split(/\s+/)
    .map((word) => word.trim())
    .filter((word) => word.length > 0);

  if (seedWords.length > 5) {
    return {
      ok: false,
      message: "Please use at most 5 seed words.",
    };
  }

  for (const word of seedWords) {
    if (word.length > 30) {
      return {
        ok: false,
        message: "Each seed word must be 30 characters or fewer.",
      };
    }
  }

  return { ok: true, seedWords };
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

/** Show a validation/error message inline above the joke display. All
 *  text is rendered via `textContent` so untrusted strings cannot inject
 *  HTML. */
function renderError(elements: PrimaryViewElements, message: string): void {
  elements.errorMessage.textContent = message;
  elements.errorMessage.hidden = false;
  elements.jokeDisplay.hidden = true;
}

function clearError(elements: PrimaryViewElements): void {
  elements.errorMessage.textContent = "";
  elements.errorMessage.hidden = true;
}

function setSeedFieldError(
  elements: PrimaryViewElements,
  message: string | null,
): void {
  if (message === null) {
    elements.seedError.textContent = "";
    elements.seedError.hidden = true;
    elements.seedInput.removeAttribute("aria-invalid");
    return;
  }
  elements.seedError.textContent = message;
  elements.seedError.hidden = false;
  elements.seedInput.setAttribute("aria-invalid", "true");
}

function setGenerateDisabled(
  elements: PrimaryViewElements,
  disabled: boolean,
): void {
  elements.generateBtn.disabled = disabled;
  if (disabled) {
    elements.generateBtn.setAttribute("aria-disabled", "true");
  } else {
    elements.generateBtn.removeAttribute("aria-disabled");
  }
}

function renderRemaining(
  elements: PrimaryViewElements,
  remaining: number | null,
): void {
  if (remaining === null) {
    elements.remainingBadge.hidden = true;
    return;
  }

  elements.remainingCount.textContent = String(remaining);
  elements.remainingBadge.hidden = false;

  if (remaining <= 0) {
    setGenerateDisabled(elements, true);
  } else {
    setGenerateDisabled(elements, false);
  }
}

function renderJoke(
  elements: PrimaryViewElements,
  response: Extract<JokeApiResponse, { kind: "success" }>,
): void {
  // Always use textContent — never innerHTML — to defeat any injection
  // attempt that survived backend moderation.
  elements.jokeText.textContent = response.text;
  elements.jokeDisplay.hidden = false;

  if (response.audioAvailable && response.audioUrl !== null) {
    elements.jokeAudio.src = response.audioUrl;
    elements.audioControls.hidden = false; // R2.5
  } else {
    // R2.7: hide audio controls completely when audio is not available.
    elements.jokeAudio.removeAttribute("src");
    elements.jokeAudio.load();
    elements.audioControls.hidden = true;
  }

  renderRemaining(elements, response.remaining);
}

/** Build a human-readable error message from a sanitized backend error.
 *  The backend already supplies a sanitized `message`; this function
 *  layers in any structured fields (e.g. `resetAtUtc`) that are useful
 *  to the visitor without ever revealing internal details. */
function formatErrorMessage(error: JokeApiError): string {
  switch (error.category) {
    case "rate_limited": {
      const reset = error.resetAtUtc;
      if (reset !== undefined && reset.length > 0) {
        return `${error.message} Counters reset at ${reset}.`;
      }
      return `${error.message} Counters reset at 00:00 UTC.`;
    }
    case "validation":
      return error.message;
    default:
      return error.message;
  }
}

// ---------------------------------------------------------------------------
// Submit handler
// ---------------------------------------------------------------------------

let inFlight = false;

async function handleSubmit(
  elements: PrimaryViewElements,
  event: Event,
): Promise<void> {
  event.preventDefault();
  if (inFlight) {
    return;
  }

  setSeedFieldError(elements, null);
  clearError(elements);

  const validation = parseSeedInput(elements.seedInput.value);
  if (!validation.ok) {
    setSeedFieldError(elements, validation.message);
    return;
  }

  inFlight = true;
  setGenerateDisabled(elements, true);

  // R7.2: progress indicator becomes visible by 200 ms after submit.
  // Cancel the timer if the response arrives first to avoid a flash.
  let progressTimer: ReturnType<typeof setTimeout> | null = setTimeout(() => {
    elements.progress.hidden = false;
    progressTimer = null;
  }, PROGRESS_VISIBLE_AFTER_MS);

  const cancelProgress = (): void => {
    if (progressTimer !== null) {
      clearTimeout(progressTimer);
      progressTimer = null;
    }
    elements.progress.hidden = true;
  };

  try {
    const response = await requestJoke({ seedWords: validation.seedWords });
    cancelProgress();

    if (response.kind === "success") {
      renderJoke(elements, response);
    } else {
      // R7.5: the backend already sanitized this message; we trust it
      // and never display anything else.
      renderError(elements, formatErrorMessage(response));
      // Rate-limit responses also trigger the "remaining = 0" UI per R7.8.
      if (response.category === "rate_limited") {
        renderRemaining(elements, 0);
      }
    }
  } catch (err) {
    cancelProgress();
    // The api client is the chokepoint that maps every exception into a
    // JokeApiError; reaching this branch means an unexpected client
    // bug. Render a generic message — never the underlying error
    // (R7.5).
    void err;
    renderError(
      elements,
      "Something went wrong. Please try again in a moment.",
    );
  } finally {
    inFlight = false;
    // Re-enable Generate unless we just learned remaining is 0.
    if (elements.remainingBadge.hidden) {
      setGenerateDisabled(elements, false);
    } else {
      const remainingText = elements.remainingCount.textContent ?? "";
      const remaining = Number.parseInt(remainingText, 10);
      setGenerateDisabled(
        elements,
        Number.isFinite(remaining) && remaining <= 0,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

function bootstrap(): void {
  const elements = lookupElements();

  elements.form.addEventListener("submit", (event) => {
    void handleSubmit(elements, event);
  });

  elements.seedInput.addEventListener("input", () => {
    if (!elements.seedError.hidden) {
      setSeedFieldError(elements, null);
    }
  });

  elements.replayBtn.addEventListener("click", () => {
    // R2.5 explicit replay control: rewind to start and play.
    if (elements.jokeAudio.src.length === 0) {
      return;
    }
    elements.jokeAudio.currentTime = 0;
    void elements.jokeAudio.play();
  });

  // Mount the flag-gated ad module (12.3). The module owns the
  // feature-flag check, the network allowlist, and the 3 s loader
  // timeout (R8.1–R8.6). Wrapped in try/catch so any unexpected
  // failure inside the module never blocks the joke UI — the module
  // is also internally fail-safe but the extra guard is defence in
  // depth for R8.6.
  try {
    void mountAdModule();
  } catch {
    // Swallow: the slot stays empty and `.ad-slot:empty` collapses it.
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap, { once: true });
} else {
  bootstrap();
}

// Export bootstrap for tests (12.5) without changing runtime behaviour.
export { bootstrap, parseSeedInput, formatErrorMessage };
