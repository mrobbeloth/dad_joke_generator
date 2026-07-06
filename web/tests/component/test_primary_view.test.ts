// Component tests for the primary view (task 12.5).
//
// Renders the SPA template against a mocked Joke_API and asserts the
// visible elements, control states, and error rendering listed in
// design.md (R7.1, R7.2, R7.5, R7.6, R7.7, R7.8) and R2.5/R2.7.
//
// Validates: Requirements 7.1, 7.2, 7.5, 7.6, 7.7, 7.8, 2.5, 2.7.

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

import { renderShell } from "./setup.js";
import type {
  JokeApiResponse,
  JokeApiSuccess,
  JokeApiError,
} from "../../src/api.js";

// Replace `requestJoke` with a vi.fn(), but keep the rest of api.ts
// (especially `humanizeError` and `sanitizeMessage`) real so the
// sanitization regression test can exercise the chokepoint.
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
// Test helpers
// ---------------------------------------------------------------------------

type RequestJokeFn = (...args: unknown[]) => Promise<JokeApiResponse>;

let mockRequestJoke: MockedFunction<RequestJokeFn>;
let humanizeError: typeof import("../../src/api.js").humanizeError;

/** Build a minimal `JokeApiSuccess` response with sensible defaults so
 *  individual tests can override only the fields they care about. */
function buildSuccess(overrides: Partial<JokeApiSuccess> = {}): JokeApiSuccess {
  return {
    kind: "success",
    id: "joke-123",
    text: "Why did the chicken join a band? Because it had the drumsticks.",
    audioUrl: "https://example.test/audio.mp3",
    audioDownloadUrl: "https://example.test/audio.mp3?download=1",
    audioAvailable: true,
    remaining: 4,
    modelId: "amazon.nova-lite-v1:0",
    voiceId: "Matthew",
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

function getSeedInput(): HTMLInputElement {
  return screen.getByLabelText(/seed words/i) as HTMLInputElement;
}

function getGenerateButton(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: /^generate$/i,
  }) as HTMLButtonElement;
}

function getReplayButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /^replay$/i }) as HTMLButtonElement;
}

function getById<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (el === null) {
    throw new Error(`#${id} missing from rendered DOM`);
  }
  return el as T;
}

async function submit(): Promise<void> {
  fireEvent.click(getGenerateButton());
  // Yield to the microtask queue so handleSubmit's first `await` runs.
  await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

beforeEach(async () => {
  vi.resetModules();

  // Re-bind mock references after the module reset.
  const apiModule = await import("../../src/api.js");
  mockRequestJoke = vi.mocked(apiModule.requestJoke) as MockedFunction<
    RequestJokeFn
  >;
  mockRequestJoke.mockReset();
  humanizeError = apiModule.humanizeError;

  await renderShell();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ===========================================================================
// TestSeedInputValidation (R7.1, R7.5)
// ===========================================================================

describe("TestSeedInputValidation", () => {
  it("submits with seedWords=[] when input is empty", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 4 }));

    await submit();

    await waitFor(() => {
      expect(mockRequestJoke).toHaveBeenCalledTimes(1);
    });
    expect(mockRequestJoke).toHaveBeenCalledWith({ seedWords: [] });
  });

  it("blocks submit and shows inline error when input has invalid characters", async () => {
    const input = getSeedInput();
    fireEvent.input(input, { target: { value: "bad@word" } });

    await submit();

    const seedError = getById<HTMLElement>("seed-error");
    expect(seedError.hidden).toBe(false);
    expect(seedError.textContent ?? "").toMatch(/letters|hyphens|apostrophes/i);
    expect(mockRequestJoke).not.toHaveBeenCalled();
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  it("blocks submit and shows inline error when input exceeds 50 characters", async () => {
    const input = getSeedInput();
    fireEvent.input(input, { target: { value: "a".repeat(51) } });

    await submit();

    const seedError = getById<HTMLElement>("seed-error");
    expect(seedError.hidden).toBe(false);
    expect(seedError.textContent ?? "").toMatch(/50 characters/i);
    expect(mockRequestJoke).not.toHaveBeenCalled();
  });

  it("blocks submit and shows inline error when input has 6 or more words", async () => {
    const input = getSeedInput();
    fireEvent.input(input, { target: { value: "one two three four five six" } });

    await submit();

    const seedError = getById<HTMLElement>("seed-error");
    expect(seedError.hidden).toBe(false);
    expect(seedError.textContent ?? "").toMatch(/at most 5/i);
    expect(mockRequestJoke).not.toHaveBeenCalled();
  });

  it("clears the inline error on the next keystroke", async () => {
    const input = getSeedInput();
    fireEvent.input(input, { target: { value: "bad@word" } });
    await submit();

    const seedError = getById<HTMLElement>("seed-error");
    expect(seedError.hidden).toBe(false);

    // Simulate the next keystroke.
    fireEvent.input(input, { target: { value: "good" } });

    expect(seedError.hidden).toBe(true);
    expect(seedError.textContent ?? "").toBe("");
    expect(input.hasAttribute("aria-invalid")).toBe(false);
  });
});

// ===========================================================================
// TestProgressIndicator (R7.2)
// ===========================================================================

describe("TestProgressIndicator", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("keeps the progress indicator hidden during the first 200 ms", async () => {
    // Slow response: never resolves within the timeline this test
    // explores.
    mockRequestJoke.mockImplementationOnce(
      () =>
        new Promise<JokeApiResponse>((resolve) => {
          setTimeout(() => resolve(buildSuccess()), 5_000);
        }),
    );

    fireEvent.click(getGenerateButton());
    // Flush the microtask that calls into handleSubmit.
    await vi.advanceTimersByTimeAsync(0);

    const progress = getById<HTMLElement>("progress");
    expect(progress.hidden).toBe(true);

    // 199 ms — still hidden.
    await vi.advanceTimersByTimeAsync(199);
    expect(progress.hidden).toBe(true);
  });

  it("becomes visible after the 200 ms boundary on a slow response", async () => {
    mockRequestJoke.mockImplementationOnce(
      () =>
        new Promise<JokeApiResponse>((resolve) => {
          setTimeout(() => resolve(buildSuccess()), 5_000);
        }),
    );

    fireEvent.click(getGenerateButton());
    await vi.advanceTimersByTimeAsync(0);

    // Cross the boundary (199 + 1 = 200 ms).
    await vi.advanceTimersByTimeAsync(200);
    const progress = getById<HTMLElement>("progress");
    expect(progress.hidden).toBe(false);

    // Once the response arrives the progress hides again.
    await vi.advanceTimersByTimeAsync(5_000);
    await Promise.resolve();
    expect(progress.hidden).toBe(true);
  });

  it("never shows the indicator when the response resolves quickly", async () => {
    // Fast response: synchronous resolve via Promise.resolve so the
    // microtask completes before the 200 ms timer fires.
    mockRequestJoke.mockResolvedValueOnce(buildSuccess());

    fireEvent.click(getGenerateButton());
    // Flush microtasks (mock resolution + handleSubmit's await).
    await vi.advanceTimersByTimeAsync(0);

    const progress = getById<HTMLElement>("progress");
    expect(progress.hidden).toBe(true);

    // Even if we now advance past 200 ms the timer was cleared.
    await vi.advanceTimersByTimeAsync(500);
    expect(progress.hidden).toBe(true);
  });
});

// ===========================================================================
// TestJokeRender (R7.1, R2.5, R2.7)
// ===========================================================================

describe("TestJokeRender", () => {
  it("renders joke text and audio controls when audio is available", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        text: "Audio joke text.",
        audioAvailable: true,
        audioUrl: "https://example.test/joke.mp3",
      }),
    );

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("joke-display").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("joke-text").textContent).toBe(
      "Audio joke text.",
    );
    expect(getById<HTMLElement>("audio-controls").hidden).toBe(false);
    const audio = getById<HTMLAudioElement>("joke-audio");
    expect(audio.src).toContain("https://example.test/joke.mp3");
  });

  it("hides audio controls when audioAvailable is false", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        text: "Text-only joke.",
        audioAvailable: false,
        audioUrl: null,
      }),
    );

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("joke-display").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("joke-text").textContent).toBe(
      "Text-only joke.",
    );
    // R2.7: hidden via the `hidden` attribute, not display:none.
    expect(getById<HTMLElement>("audio-controls").hidden).toBe(true);
  });

  it("displays the remaining count when remaining > 0", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 5 }));

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("remaining-badge").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("5");
  });

  it("disables the Generate button when remaining is 0", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 0 }));

    await submit();

    await waitFor(() => {
      expect(getGenerateButton().disabled).toBe(true);
    });
    expect(getGenerateButton().getAttribute("aria-disabled")).toBe("true");
  });
});

// ===========================================================================
// TestDownloadControl (R2.10, R2.11)
// ===========================================================================

describe("TestDownloadControl", () => {
  function getDownloadLink(): HTMLAnchorElement {
    return getById<HTMLAnchorElement>("download-link");
  }

  it("shows the download link with href + filename when a download URL is present", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        id: "abc-123",
        audioAvailable: true,
        audioUrl: "https://example.test/joke.mp3",
        audioDownloadUrl: "https://example.test/joke.mp3?download=1",
      }),
    );

    await submit();

    const link = getDownloadLink();
    await waitFor(() => {
      expect(link.hidden).toBe(false);
    });
    expect(link.getAttribute("href")).toBe(
      "https://example.test/joke.mp3?download=1",
    );
    // R2.11: friendly filename derived from the joke id.
    expect(link.getAttribute("download")).toBe("dad-joke-abc-123.mp3");
  });

  it("hides the download link when audio is available but no download URL is present", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        audioAvailable: true,
        audioUrl: "https://example.test/joke.mp3",
        audioDownloadUrl: null,
      }),
    );

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("audio-controls").hidden).toBe(false);
    });
    const link = getDownloadLink();
    expect(link.hidden).toBe(true);
    expect(link.hasAttribute("href")).toBe(false);
  });

  it("hides the download link when audio is unavailable", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        audioAvailable: false,
        audioUrl: null,
        audioDownloadUrl: null,
      }),
    );

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("joke-display").hidden).toBe(false);
    });
    expect(getDownloadLink().hidden).toBe(true);
    expect(getById<HTMLElement>("audio-controls").hidden).toBe(true);
  });
});

// ===========================================================================
// TestErrorRender (R7.5, R7.6)
// ===========================================================================

describe("TestErrorRender", () => {
  it("renders the sanitized backend message for a validation error", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildError({
        category: "validation",
        message: "Seed words must be 1 to 30 characters each.",
      }),
    );

    await submit();

    const errorEl = getById<HTMLElement>("error-message");
    await waitFor(() => {
      expect(errorEl.hidden).toBe(false);
    });
    expect(errorEl.textContent).toBe(
      "Seed words must be 1 to 30 characters each.",
    );
    expect(getById<HTMLElement>("joke-display").hidden).toBe(true);
  });

  it("includes the reset time and forces remaining=0 for a rate_limited error", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildError({
        category: "rate_limited",
        message: "You've reached today's limit.",
        resetAtUtc: "2025-01-15T00:00:00Z",
      }),
    );

    await submit();

    const errorEl = getById<HTMLElement>("error-message");
    await waitFor(() => {
      expect(errorEl.hidden).toBe(false);
    });
    expect(errorEl.textContent ?? "").toContain("2025-01-15T00:00:00Z");

    // R7.8: rate-limit response forces the remaining badge to 0 and
    // disables the Generate button.
    expect(getById<HTMLElement>("remaining-badge").hidden).toBe(false);
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("0");
    expect(getGenerateButton().disabled).toBe(true);
  });

  it("hides the joke display when a moderation error is returned", async () => {
    // First render a successful joke so the display is visible.
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({ text: "Old joke." }),
    );
    await submit();
    await waitFor(() => {
      expect(getById<HTMLElement>("joke-display").hidden).toBe(false);
    });

    // Now a moderation error: the display must hide and the error
    // message must show.
    mockRequestJoke.mockResolvedValueOnce(
      buildError({
        category: "moderation",
        message: "That seed couldn't be used.",
      }),
    );
    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("error-message").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("joke-display").hidden).toBe(true);
  });

  it("renders the generic message for an unavailable error", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildError({
        category: "unavailable",
        message: "The joke service is temporarily unavailable.",
      }),
    );

    await submit();

    const errorEl = getById<HTMLElement>("error-message");
    await waitFor(() => {
      expect(errorEl.hidden).toBe(false);
    });
    expect(errorEl.textContent ?? "").toMatch(/temporarily unavailable/i);
  });

  it("sanitization invariant: ARN and account id never reach the DOM", async () => {
    // Drive the chokepoint: humanizeError() is the real api.ts
    // implementation (kept via vi.importActual). It must strip the
    // ARN before the rendered message is constructed.
    const dirty =
      "Bedrock failed at arn:aws:bedrock:us-east-1:123456789012:model/foo";
    const message = humanizeError("internal_error", dirty);

    // Defensive double-check: humanizeError must have stripped both
    // markers. If this regresses, the failure mode of the assertion
    // below would be unclear, so we surface it explicitly here.
    expect(message).not.toMatch(/arn:aws:/);
    expect(message).not.toContain("123456789012");

    mockRequestJoke.mockResolvedValueOnce(
      buildError({ category: "internal_error", message }),
    );

    await submit();

    const errorEl = getById<HTMLElement>("error-message");
    await waitFor(() => {
      expect(errorEl.hidden).toBe(false);
    });

    // The visitor never sees the ARN or AWS account id anywhere on
    // the page.
    const rendered = document.body.textContent ?? "";
    expect(rendered).not.toMatch(/arn:aws:/);
    expect(rendered).not.toContain("123456789012");
  });
});

// ===========================================================================
// TestRemainingBadge (R7.7, R7.8)
// ===========================================================================

describe("TestRemainingBadge", () => {
  it("is hidden on first render", () => {
    expect(getById<HTMLElement>("remaining-badge").hidden).toBe(true);
  });

  it("shows the count after a successful response with remaining=3", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 3 }));

    await submit();

    await waitFor(() => {
      expect(getById<HTMLElement>("remaining-badge").hidden).toBe(false);
    });
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("3");
  });

  it("disables Generate when a response reports remaining=0", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 0 }));

    await submit();

    await waitFor(() => {
      expect(getGenerateButton().disabled).toBe(true);
    });
    expect(getById<HTMLElement>("remaining-count").textContent).toBe("0");
  });

  it("re-enables Generate when a subsequent response reports remaining=1", async () => {
    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 0 }));
    await submit();
    await waitFor(() => {
      expect(getGenerateButton().disabled).toBe(true);
    });

    mockRequestJoke.mockResolvedValueOnce(buildSuccess({ remaining: 1 }));
    // Force-enable the button so the click registers — main.ts will
    // re-render it disabled once the response arrives if needed. We
    // simulate the user-triggered submit via the form because the
    // button is currently disabled.
    const form = getById<HTMLFormElement>("joke-form");
    fireEvent.submit(form);
    await Promise.resolve();

    await waitFor(() => {
      expect(getById<HTMLElement>("remaining-count").textContent).toBe("1");
    });
    expect(getGenerateButton().disabled).toBe(false);
  });
});

// ===========================================================================
// TestReplayButton (R2.5)
// ===========================================================================

describe("TestReplayButton", () => {
  it("rewinds and plays the audio after a successful joke", async () => {
    mockRequestJoke.mockResolvedValueOnce(
      buildSuccess({
        text: "Joke with audio.",
        audioAvailable: true,
        audioUrl: "https://example.test/joke.mp3",
      }),
    );
    await submit();
    await waitFor(() => {
      expect(getById<HTMLElement>("audio-controls").hidden).toBe(false);
    });

    const audio = getById<HTMLAudioElement>("joke-audio");
    // happy-dom may not implement HTMLMediaElement.play(); stub it.
    const playSpy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(audio, "play", {
      configurable: true,
      writable: true,
      value: playSpy,
    });
    audio.currentTime = 12; // simulate prior playback

    fireEvent.click(getReplayButton());

    expect(audio.currentTime).toBe(0);
    expect(playSpy).toHaveBeenCalledTimes(1);
  });

  it("is a no-op when no audio has been loaded yet", () => {
    const audio = getById<HTMLAudioElement>("joke-audio");
    const playSpy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(audio, "play", {
      configurable: true,
      writable: true,
      value: playSpy,
    });

    // The replay button is inside joke-display which is hidden until
    // a joke has rendered, so testing-library's accessible queries
    // skip it. Reach for the element directly to exercise the click
    // handler's "no audio src" guard.
    const replayBtn = getById<HTMLButtonElement>("replay-btn");
    expect(() => fireEvent.click(replayBtn)).not.toThrow();
    expect(playSpy).not.toHaveBeenCalled();
  });
});
