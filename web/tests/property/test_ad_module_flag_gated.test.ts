// Property tests for the ad module (task 12.4).
//
// Property 22: Ad_Module rendering and network access are flag-gated.
//
//   ∀ AppConfig with adModuleEnabled === false:
//     scriptLoader is NEVER invoked.
//     The slot stays empty (slot.children.length === 0).
//     state.scriptInjected stays false.
//
//   ∀ AppConfig with adModuleEnabled === true and adNetworkId
//     outside the allowlist:
//       scriptLoader is NEVER invoked.
//       The slot stays empty.
//
//   Plus singleton, missing-slot, and config-failure invariants
//   covered by example tests.
//
// Validates: Requirements 8.1, 8.3, 8.4.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as fc from "fast-check";

import {
  __getStateForTests,
  __resetForTests,
  mountAdModule,
  type ScriptLoadResult,
} from "../../src/ad_module.js";
import type { AppConfig } from "../../src/config.js";

const SLOT_ID = "ad-slot";

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

/** Build a fresh ad slot in document.body. Each test invokes this in
 *  its setup so the prior test's mutations cannot leak. */
function buildSlot(): HTMLElement {
  document.body.innerHTML = "";
  const slot = document.createElement("div");
  slot.id = SLOT_ID;
  document.body.appendChild(slot);
  return slot;
}

function configFetcherFor(config: AppConfig): () => Promise<AppConfig> {
  return () => Promise.resolve(config);
}

function rejectingConfigFetcher(): () => Promise<AppConfig> {
  return () => Promise.reject(new Error("config unreachable"));
}

afterEach(() => {
  __resetForTests();
  document.body.innerHTML = "";
});

// ===========================================================================
// Property: disabled flag fully prevents loading and DOM mutation.
// ===========================================================================

describe("Property 22 — disabled flag is fully gated", () => {
  it("∀ disabled config: scriptLoader is never called and slot stays empty", async () => {
    await fc.assert(
      fc.asyncProperty(
        // The adNetworkId is irrelevant when the flag is off; generate
        // any string (including "" and allowlisted ids) to assert the
        // disabled flag wins.
        fc.string(),
        async (adNetworkId: string) => {
          __resetForTests();
          const slot = buildSlot();

          const scriptLoader = vi
            .fn<
              (params: {
                src: string;
                timeoutMs: number;
              }) => Promise<ScriptLoadResult>
            >()
            .mockResolvedValue({ ok: true });

          await mountAdModule({
            slotId: SLOT_ID,
            timeoutMs: 3000,
            configFetcher: configFetcherFor({
              adModuleEnabled: false,
              adNetworkId,
            }),
            scriptLoader,
          });

          expect(scriptLoader).not.toHaveBeenCalled();
          expect(slot.children.length).toBe(0);
          expect(__getStateForTests().scriptInjected).toBe(false);
        },
      ),
      { numRuns: 100 },
    );
  });
});

// ===========================================================================
// Property: enabled but unknown adNetworkId is fully gated.
// ===========================================================================

describe("Property 22 — unknown adNetworkId is fully gated", () => {
  const ALLOWLIST = new Set<string>(["adsense", "fixed-test-network"]);

  it("∀ enabled config with unknown network: scriptLoader is never called", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc
          .string({ minLength: 0, maxLength: 32 })
          .filter((s) => !ALLOWLIST.has(s)),
        async (adNetworkId: string) => {
          __resetForTests();
          const slot = buildSlot();

          const scriptLoader = vi
            .fn<
              (params: {
                src: string;
                timeoutMs: number;
              }) => Promise<ScriptLoadResult>
            >()
            .mockResolvedValue({ ok: true });

          await mountAdModule({
            slotId: SLOT_ID,
            timeoutMs: 3000,
            configFetcher: configFetcherFor({
              adModuleEnabled: true,
              adNetworkId,
            }),
            scriptLoader,
          });

          expect(scriptLoader).not.toHaveBeenCalled();
          expect(slot.children.length).toBe(0);
          // scriptInjected only flips inside the allowlisted branch;
          // unknown network must leave it false.
          expect(__getStateForTests().scriptInjected).toBe(false);
        },
      ),
      { numRuns: 100 },
    );
  });
});

// ===========================================================================
// Example tests: enabled + allowlisted network paths.
// ===========================================================================

describe("Property 22 — enabled + allowlisted network", () => {
  it("scriptLoader resolves ok=true → exactly one .ad-placeholder child", async () => {
    __resetForTests();
    const slot = buildSlot();

    const scriptLoader = vi
      .fn<
        (params: {
          src: string;
          timeoutMs: number;
        }) => Promise<ScriptLoadResult>
      >()
      .mockResolvedValue({ ok: true });

    await mountAdModule({
      slotId: SLOT_ID,
      timeoutMs: 3000,
      configFetcher: configFetcherFor({
        adModuleEnabled: true,
        adNetworkId: "fixed-test-network",
      }),
      scriptLoader,
    });

    expect(scriptLoader).toHaveBeenCalledTimes(1);
    expect(slot.children.length).toBe(1);
    const child = slot.children[0] as HTMLElement;
    expect(child.classList.contains("ad-placeholder")).toBe(true);
    expect(__getStateForTests().scriptInjected).toBe(true);
  });

  it("scriptLoader resolves ok=false → slot stays empty, scriptInjected still flips on attempt", async () => {
    // ad_module.ts flips state.scriptInjected = true BEFORE awaiting
    // the loader (single-injection invariant), so a failed load still
    // shows scriptInjected === true. Slot must remain empty (R8.5/8.6).
    __resetForTests();
    const slot = buildSlot();

    const scriptLoader = vi
      .fn<
        (params: {
          src: string;
          timeoutMs: number;
        }) => Promise<ScriptLoadResult>
      >()
      .mockResolvedValue({ ok: false });

    await mountAdModule({
      slotId: SLOT_ID,
      timeoutMs: 3000,
      configFetcher: configFetcherFor({
        adModuleEnabled: true,
        adNetworkId: "fixed-test-network",
      }),
      scriptLoader,
    });

    expect(scriptLoader).toHaveBeenCalledTimes(1);
    expect(slot.children.length).toBe(0);
    expect(__getStateForTests().scriptInjected).toBe(true);
  });

  it("singleton invariant: 3 mountAdModule calls invoke scriptLoader exactly once", async () => {
    __resetForTests();
    buildSlot();

    const scriptLoader = vi
      .fn<
        (params: {
          src: string;
          timeoutMs: number;
        }) => Promise<ScriptLoadResult>
      >()
      .mockResolvedValue({ ok: true });

    const opts = {
      slotId: SLOT_ID,
      timeoutMs: 3000,
      configFetcher: configFetcherFor({
        adModuleEnabled: true,
        adNetworkId: "fixed-test-network",
      }),
      scriptLoader,
    };

    await mountAdModule(opts);
    await mountAdModule(opts);
    await mountAdModule(opts);

    expect(scriptLoader).toHaveBeenCalledTimes(1);
  });
});

// ===========================================================================
// Example tests: fail-safe paths (R8.3, R8.6 defence-in-depth).
// ===========================================================================

describe("Property 22 — fail-safe paths", () => {
  it("missing slot DOM → scriptLoader never called, returns without throwing", async () => {
    __resetForTests();
    document.body.innerHTML = ""; // no #ad-slot present

    const scriptLoader = vi
      .fn<
        (params: {
          src: string;
          timeoutMs: number;
        }) => Promise<ScriptLoadResult>
      >()
      .mockResolvedValue({ ok: true });

    await expect(
      mountAdModule({
        slotId: SLOT_ID,
        timeoutMs: 3000,
        configFetcher: configFetcherFor({
          adModuleEnabled: true,
          adNetworkId: "fixed-test-network",
        }),
        scriptLoader,
      }),
    ).resolves.toBeUndefined();

    expect(scriptLoader).not.toHaveBeenCalled();
    expect(__getStateForTests().scriptInjected).toBe(false);
  });

  it("configFetcher rejects → no DOM mutation, no scriptLoader call", async () => {
    __resetForTests();
    const slot = buildSlot();

    const scriptLoader = vi
      .fn<
        (params: {
          src: string;
          timeoutMs: number;
        }) => Promise<ScriptLoadResult>
      >()
      .mockResolvedValue({ ok: true });

    await expect(
      mountAdModule({
        slotId: SLOT_ID,
        timeoutMs: 3000,
        configFetcher: rejectingConfigFetcher(),
        scriptLoader,
      }),
    ).resolves.toBeUndefined();

    expect(scriptLoader).not.toHaveBeenCalled();
    expect(slot.children.length).toBe(0);
    expect(__getStateForTests().scriptInjected).toBe(false);
  });
});
