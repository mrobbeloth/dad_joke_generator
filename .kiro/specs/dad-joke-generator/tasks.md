# Implementation Plan: Dad Joke Generator

## Overview

This plan converts the design into incremental, code-focused steps. Each task produces a working slice that builds on the previous ones; each property-based sub-task implements exactly one Correctness Property from `design.md` and is placed next to the implementation it verifies. The Phase 1 MVP runs on AWS Lambda + API Gateway + DynamoDB + S3 + Bedrock + Polly + Comprehend, with a static TypeScript SPA fronted by CloudFront. Backend code is Python 3.12; frontend code is TypeScript.

> Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Set up project structure and tooling
  - [x] 1.1 Create Python backend skeleton at `src/joke_api/`
    - Create empty modules: `handler.py`, `request_validator.py`, `client_ip.py`, `rate_limiter.py`, `input_moderator.py`, `output_moderator.py`, `joke_generator.py`, `voice_synthesizer.py`, `joke_store.py`, `response_builder.py`, `ip_hashing.py`, `training_corpus.py`, `observability.py`, `fallback_jokes.py`, `config.py`
    - Author `pyproject.toml` pinning Python 3.12 and dependencies (`boto3`, `pytest`, `hypothesis`, `moto`)
    - Create `tests/unit/`, `tests/property/`, `tests/integration/`, `tests/smoke/` directories with `__init__.py`
    - _Requirements: 1.1, 16.1_

  - [x] 1.2 Create TypeScript SPA skeleton at `web/`
    - Author `package.json` pinning TypeScript, `vitest`, `fast-check`, `@testing-library/dom`, `@axe-core/playwright`, `playwright`
    - Create `web/src/` with `main.ts`, `api.ts`, `config.ts`, `ad_module.ts`, `index.html`, and CSS scaffold
    - _Requirements: 7.1, 7.4_

  - [x] 1.3 Create `docs/` skeleton and config files
    - Create empty `docs/PLAN.md`, `docs/TEST_PLAN.md`, `docs/COST_REPORT.md`, `docs/architecture/component.puml`, `docs/architecture/deployment.puml`, `docs/architecture/sequence.puml`
    - Add a `scripts/` directory for governance/gate scripts
    - _Requirements: 10.1, 10.2, 11.1, 11.2_

- [x] 2. Implement core stateless utilities
  - [x] 2.1 Implement `request_validator.validate(event)`
    - Enforce 0–5 seed words, 1–30 chars each, charset `[A-Za-z0-9'-]`, aggregate length 0–100
    - Raise typed `ValidationError(rule, field)` short-circuiting before any AWS call
    - _Requirements: 1.7, 3.4, 3.5, 7.5_

  - [x]* 2.2 Write property test for request_validator
    - **Property 5: Input validation rejection short-circuits the pipeline**
    - **Validates: Requirements 1.7, 3.4, 3.5**
    - Use `hypothesis` strategies to generate invalid inputs and assert no moderator/Bedrock/Polly/rate-limiter call occurs

  - [x] 2.3 Implement `client_ip.resolve(event)`
    - Parse leftmost address from `X-Forwarded-For`, trim whitespace, validate IPv4/IPv6
    - Raise `ClientIpUnresolvable` on missing/empty/malformed XFF
    - _Requirements: 5.8, 5.9_

  - [x]* 2.4 Write property test for client_ip
    - **Property 18: Forwarded-For resolution uses the leftmost address**
    - **Validates: Requirements 5.8, 5.9**

  - [x] 2.5 Implement `response_builder.sanitize_error(category, message)` and `build_success(...)`
    - Single chokepoint that emits sanitized error bodies; never accepts free-form internal text
    - _Requirements: 7.5, 7.6_

  - [x]* 2.6 Write property test for response_builder
    - **Property 20: Error responses are sanitized and logged in full**
    - **Validates: Requirements 7.5, 7.6**
    - Assert response body never contains `Traceback`, `arn:aws:`, file paths, or AWS account IDs

  - [x] 2.7 Implement `ip_hashing.hash_ip(ip)`
    - Salted SHA-256 using salt loaded from SSM SecureString; 64-char lowercase hex
    - _Requirements: 16.7_

  - [x]* 2.8 Write property test for ip_hashing
    - **Property 34: IP addresses are never logged in raw form**
    - **Validates: Requirements 16.7**
    - Generate IPs, run through hashing + log capture, assert raw IP never appears in any captured artifact

- [x] 3. Implement Rate_Limiter on DynamoDB
  - [x] 3.1 Implement `config.load()` for SSM Parameter Store values
    - Load `daily_limit` (int, validated 5–10), `bedrock_model_id`, `polly_voice_id`, `ad_module_enabled`, `ad_network_id`, `ip_hash_salt`, `cost_alarm_threshold_usd`
    - _Requirements: 5.7, 8.1, 8.4, 16.3_

  - [x]* 3.2 Write property test for config bounds
    - **Property 17: Daily_Limit configuration is bounded**
    - **Validates: Requirements 5.7**

  - [x] 3.3 Implement `rate_limiter.check(ip_hash, day, limit)` and `increment(ip_hash, day)`
    - DynamoDB `UpdateItem` with `ADD #count :one` and conditional expression
    - TTL set to "next UTC midnight + 60 s"; treat prior-day rows as zero on read
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x]* 3.4 Write property test for rate_limiter
    - **Property 14: Rate-limit counters increment atomically and only on success**
    - **Property 15: Limit-reached requests are rejected with HTTP 429**
    - **Property 16: Counters reset across UTC-day boundaries**
    - **Validates: Requirements 5.3, 5.4, 5.5, 5.6**
    - Use `moto` for DynamoDB; concurrent increment harness with N parallel workers

- [x] 4. Implement Input/Output moderation
  - [x] 4.1 Implement denylist matcher
    - Word-boundary aware case-insensitive substring match over a curated denylist file
    - _Requirements: 3.3_

  - [x] 4.2 Implement `input_moderator.classify(text)`
    - Combine denylist + Comprehend `DetectToxicContent` (3 s timeout)
    - Raise `ModerationTimeout`, `ModerationUnavailable` per design
    - _Requirements: 3.1, 3.2, 3.3, 3.6, 3.7_

  - [x] 4.3 Implement `output_moderator.classify(text)` reusing the same classifier with 500 ms budget
    - _Requirements: 4.1, 4.4, 4.5_

  - [x]* 4.4 Write property tests for moderators
    - **Property 9: Family-friendliness is the logical OR of denylist and classifier flags**
    - **Property 11: Output moderator and Input moderator are equivalent**
    - **Validates: Requirements 3.3, 4.4**

  - [x] 4.5 Implement `fallback_jokes.FALLBACK_JOKES` curated list with at least 20 entries
    - _Requirements: 4.3_

  - [x]* 4.6 Write unit test asserting `len(FALLBACK_JOKES) >= 20`
    - _Requirements: 4.3_

- [x] 5. Checkpoint - core utilities and rate limiting
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement Training_Corpus loader and Joke_Generator
  - [x] 6.1 Implement `training_corpus.load_few_shot()`
    - Read examples from private S3 bucket; truncate each to ≤ 500 chars; cap pool to 3–10 entries; combined section ≤ 5000 chars
    - Skip binary files unless an extractor produces text; record extraction failures
    - Honor a rights-confirmation flag from PLAN.md (empty pool when false)
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.7_

  - [x]* 6.2 Write property tests for training_corpus
    - **Property 36: Few-shot prompt construction respects size bounds**
    - **Property 37: Training_Corpus contents never reach clients**
    - **Property 38: Binary corpus assets never reach Bedrock**
    - **Property 39: Rights-flag gates corpus inclusion**
    - **Validates: Requirements 17.1, 17.2, 17.3, 17.4, 17.5, 17.7**

  - [x] 6.3 Implement `joke_generator.generate(seed_words, few_shot, refine=False)`
    - Bedrock Converse with model id from SSM, 15 s hard timeout, 10–80 word length guard
    - Up to 3 attempts shared across length-rejection and output-moderation rejection; refined prompt on attempts 2 and 3
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.8, 4.2_

  - [x]* 6.4 Write property tests for joke_generator
    - **Property 1: Seed-word containment when seeds are supplied**
    - **Property 2: Joke length is within 10..80 words inclusive**
    - **Property 3: Bedrock failure produces 503 with no partial content**
    - **Property 4: Generation IDs are unique UUID v4s**
    - **Property 12: Output rejection retries up to three attempts with refined prompts**
    - **Property 13: All-rejected outputs fall back to a curated safe joke**
    - **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.8, 4.2, 4.3, 4.5, 18.1**

- [x] 7. Implement Voice_Synthesizer
  - [x] 7.1 Implement `voice_synthesizer.synthesize(joke_text)`
    - Skip Polly when `len(joke_text)` outside `[1, 1500]`
    - `polly.synthesize_speech(OutputFormat='mp3', SampleRate='22050', VoiceId=<ssm>, Engine='standard')`
    - 10 s synthesis budget, 64 kbps cap, write to S3, return 15-minute presigned GET URL; soft-fail returning `audio_available=false`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.8, 2.9_

  - [x]* 7.2 Write property tests for voice_synthesizer
    - **Property 6: Audio availability mirrors Polly outcome**
    - **Property 7: Presigned audio URLs are valid for at least 15 minutes**
    - **Validates: Requirements 2.1, 2.3, 2.4, 2.6, 2.7, 2.9**

  - [x]* 7.3 Write unit test asserting Polly is called with `OutputFormat='mp3'`, `SampleRate='22050'`, `Engine='standard'`, configured `VoiceId`
    - _Requirements: 2.2, 2.8_

- [x] 8. Implement Joke_Store
  - [x] 8.1 Implement `joke_store.persist(record)` and `get(id)`
    - DynamoDB single-table write with `pk=JOKE#<uuid>`, `sk=META`, `expires_at = created_at + 90 days`
    - Reject records with `joke_text > 2000` or `audio_ref > 2048` chars; soft-fail on persist errors
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

  - [x]* 8.2 Write property tests for joke_store
    - **Property 40: Joke persistence round-trip is byte-exact**
    - **Property 41: Unknown ids do not mutate the store**
    - **Property 42: TTL retention rule is enforced by `expires_at`**
    - **Property 43: Persistence failures do not affect the visitor response**
    - **Property 44: Persistence input-size validation**
    - **Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5, 18.6**

- [x] 9. Implement Observability layer
  - [x] 9.1 Implement `observability.emit_log(record)` and `emit_metric(...)`
    - Structured JSON log with `request_id`, `ip_hash`, `decision`, `model_id`, `voice_id`, `latency_ms`, `estimated_cost_usd`, `ts`; emitted within 2 s of completion
    - CloudWatch metrics for jokes-per-hour, moderation-rejections-per-hour, rate-limit-rejections-per-hour, observability-failure counter
    - Soft-fail emission errors (increment internal counter, never fail the request)
    - _Requirements: 16.1, 16.2, 16.7, 16.8_

  - [x]* 9.2 Write property tests for observability
    - **Property 30: Per-request structured log schema**
    - **Property 35: Observability emission failures are soft-failures**
    - **Validates: Requirements 16.1, 16.8**

  - [x] 9.3 Implement `observability.alerts` cost-alert and ops-alert dispatchers
    - Cost alert: subject `[COST-ALERT]` + threshold value, sent only on `OK→ALARM`, on cost SNS topic; up to 1+3 retries spaced 60 s
    - Ops alert: subject `[OPS-ALERT]` (must NOT contain `cost`), separate SNS topic, threshold-driven
    - _Requirements: 16.3, 16.4, 16.5, 16.6_

  - [x]* 9.4 Write property tests for alerting
    - **Property 31: Cost-alert email subject and gating**
    - **Property 32: Cost-email retry caps at three attempts**
    - **Property 33: Ops-alert email subject, channel, and trigger thresholds**
    - **Validates: Requirements 16.4, 16.5, 16.6**

- [x] 10. Wire the Lambda handler pipeline
  - [x] 10.1 Implement `handler.py` orchestration for `POST /v1/jokes`
    - Sequence: validate → resolve_ip → rate_limit_check → input_moderate → generate (with output_moderate retry loop) → synthesize → persist → rate_limit_increment → build response
    - Guarantee fail-closed for moderation errors; fail-soft for Polly and persistence; rate-limit increment only after every other stage succeeds
    - _Requirements: 1.1, 1.3, 2.7, 3.1, 3.2, 4.1, 5.1, 5.4, 5.5, 18.5_

  - [x] 10.2 Implement `GET /v1/jokes/{id}`, `GET /v1/config`, `GET /v1/health`
    - `/v1/config` returns `{adModuleEnabled, adNetworkId, dailyLimit}` from SSM
    - `/v1/health` is the Production_Gate self-health probe
    - _Requirements: 8.1, 12.2, 18.2, 18.3_

  - [x]* 10.3 Write property tests for handler-level pipeline ordering
    - **Property 8: Moderation gate precedes Bedrock for all accepted inputs**
    - **Property 10: Moderator unavailability fails closed**
    - **Validates: Requirements 3.1, 3.2, 3.6, 3.7**

  - [x]* 10.4 Write unit tests for handler error paths
    - Cover validation, moderation rejection/timeout, rate-limit, Bedrock failure, Polly soft-fail, persistence soft-fail, unexpected exception
    - _Requirements: 1.5, 1.7, 3.2, 3.6, 3.7, 4.5, 5.3, 7.5, 18.5_

- [x] 11. Checkpoint - backend pipeline complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement Frontend SPA
  - [x] 12.1 Implement primary view in `web/src/main.ts` and `index.html`
    - Seed input (1–50 chars), Generate button with 200 ms progress indicator, joke display (≤1000 chars), audio play/pause/replay controls, remaining-count badge
    - Hide audio controls when `audioAvailable=false`; disable Generate when `remaining=0`
    - WCAG 2.1 AA contrast tokens, keyboard operability, visible focus indicators
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.7, 7.8, 2.5, 2.7_

  - [x] 12.2 Implement `api.ts` client and sanitized error rendering
    - 30 s timeout; map JSON error categories to human-readable messages; never display stack traces or AWS identifiers
    - _Requirements: 7.5, 7.6_

  - [x] 12.3 Implement `ad_module.ts` flag-gated banner
    - When disabled: render nothing, reserve no space, issue no third-party requests
    - When enabled: lazy-load exactly one configured ad-network script with 3 s timeout; on failure leave slot empty
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x]* 12.4 Write frontend property tests with `fast-check`
    - **Property 21: Generate button state mirrors remaining count**
    - **Property 22: Ad_Module rendering and network access are flag-gated**
    - **Validates: Requirements 7.8, 8.1, 8.3, 8.4**

  - [x]* 12.5 Write component tests with `vitest` + `@testing-library/dom`
    - Render against mocked API; assert visible elements, control states, error rendering
    - _Requirements: 7.1, 7.2, 7.5, 7.7_

  - [x]* 12.6 Write accessibility tests with `@axe-core/playwright`
    - Zero serious or critical violations on the primary view at 320, 768, 1280, 1920 px
    - _Requirements: 7.3, 7.4_

- [x] 13. Implement governance and Production_Gate scripts
  - [x] 13.1 Implement `scripts/plan_parser.py` to parse PLAN.md and TEST_PLAN.md
    - Extract requirement IDs, phase assignments, manual-setup items, model/voice selections
    - _Requirements: 11.1, 11.2, 14.2, 15.1_

  - [x] 13.2 Implement freshness check (`scripts/check_doc_freshness.py`)
    - Fail when PR touches `src/` and PLAN.md or TEST_PLAN.md last commit is older than 90 whole UTC days
    - _Requirements: 11.6_

  - [x] 13.3 Implement cross-reference checker (`scripts/check_plan_xref.py`)
    - Pass iff every requirement ID in PLAN.md appears in TEST_PLAN.md
    - _Requirements: 12.5_

  - [x] 13.4 Implement cost-report consistency check (`scripts/check_cost_report.py`)
    - Block when `cost_report_model_id != runtime_model_id` or `cost_report_voice_id != runtime_voice_id`
    - _Requirements: 12.6_

  - [x] 13.5 Implement feature-branch validator (`scripts/check_branch_name.py`)
    - Accept iff name matches `^feature/[a-z0-9-]{3,50}$`
    - _Requirements: 13.2_

  - [x] 13.6 Implement phase-assignment and phase-scope checks (`scripts/check_phases.py`)
    - Every requirement assigned to exactly one phase; deployment allowed iff requirement phase ≤ current phase
    - _Requirements: 14.2, 14.3_

  - [x] 13.7 Implement manual-setup completion validator (`scripts/check_manual_setup.py`)
    - Reject completion edits without ISO 8601 date; on build start fail if any item incomplete
    - _Requirements: 15.4, 15.5, 15.6_

  - [x]* 13.8 Write property tests for governance scripts
    - **Property 23: Plan/Test-Plan freshness check on src/ changes** (Validates 11.6)
    - **Property 24: Plan/Test-Plan cross-reference completeness** (Validates 12.5)
    - **Property 25: Cost_Report ↔ runtime configuration consistency** (Validates 12.6)
    - **Property 26: Feature branch name validator** (Validates 13.2)
    - **Property 27: Each requirement assigned to exactly one phase** (Validates 14.2)
    - **Property 28: Phase scope rule for deployments** (Validates 14.3)
    - **Property 29: Manual-setup completion requires an ISO 8601 date** (Validates 15.4, 15.5)

- [x] 14. Author Architecture_Document and render pipeline step
  - [x] 14.1 Author the three required PlantUML diagrams
    - `docs/architecture/component.puml` (component view)
    - `docs/architecture/deployment.puml` (AWS deployment view)
    - `docs/architecture/sequence.puml` (end-to-end generate flow)
    - _Requirements: 10.1, 10.2_

  - [x] 14.2 Implement `scripts/render_plantuml.sh` invoking the official `plantuml/plantuml` Docker image
    - Render every `.puml` under `docs/architecture/` to PNG and SVG with a 120 s per-file timeout
    - Fail the build with a per-diagram error message on missing/timeout/syntax-error
    - _Requirements: 10.3, 10.4, 10.5, 10.6_

  - [x]* 14.3 Write smoke test asserting all three diagrams render successfully
    - _Requirements: 10.2, 10.5_

- [x] 15. Author governance documents required by the Build_Pipeline
  - [x] 15.1 Populate `docs/PLAN.md` per R11.1, R14, R15
    - Requirement table with `R[0-9]+` IDs, phase, selected Bedrock and Polly options
    - Three phase sections with entry/exit checklists and unique requirement-to-phase assignment
    - "AWS Manual Setup" section with required items, completion checkboxes, ISO 8601 date fields
    - Training_Corpus rights-confirmation entry per R17.6
    - _Requirements: 11.1, 14.1, 14.2, 15.1, 15.2, 17.6_

  - [x] 15.2 Populate `docs/TEST_PLAN.md` per R11.2
    - One row per test type (unit, integration, end-to-end, accessibility, performance) with coverage target and pass criterion
    - Cross-reference every R-id from PLAN.md
    - _Requirements: 11.2, 12.5_

  - [x] 15.3 Populate `docs/COST_REPORT.md` per R9
    - Compare ≥3 Bedrock models (Anthropic + Amazon + Meta/Mistral) on input/output token price; compare Polly standard vs neural per million chars
    - Project monthly cost at 100, 500, 1000 jokes/day on a 30-day month with documented token/character assumptions
    - Recommend exactly one model and one voice; include the ad-revenue-vs-hosting evaluation per R8.7
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 8.7_

- [x] 16. Implement Infrastructure as Code
  - [x] 16.1 Implement IaC for DynamoDB single-table `dadjokes`
    - On-demand billing, TTL on `expires_at`, GSIs as needed
    - _Requirements: 5.4, 5.6, 18.4_

  - [x] 16.2 Implement IaC for S3 buckets
    - `spa-assets` (CloudFront OAC), `audio` (BPA all, 30-day lifecycle), `training-corpus` (BPA all, Lambda-only read)
    - _Requirements: 2.4, 17.2_

  - [x] 16.3 Implement IaC for SSM Parameter Store entries
    - All parameters listed in design (`daily_limit`, model id, voice id, ad flags, ip hash salt as SecureString, cost threshold)
    - _Requirements: 5.7, 8.1, 8.4, 16.3, 16.7_

  - [x] 16.4 Implement IaC for Lambda + API Gateway HTTP API + IAM execution role
    - Routes for `POST /v1/jokes`, `GET /v1/jokes/{id}`, `GET /v1/config`, `GET /v1/health`
    - Least-privilege IAM permissions for Bedrock, Polly, Comprehend, DynamoDB, S3, SSM, CloudWatch
    - _Requirements: 12.2_

  - [x] 16.5 Implement IaC for CloudFront + ACM + Route 53
    - SPA origin (S3 OAC) and `/api/*` origin (API Gateway custom domain); ACM cert with SAN matching Custom_Domain; HTTP→HTTPS 301 with path/query preservation; reject TLS for non-matching SNI
    - _Requirements: 6.1, 6.2, 6.3, 6.5_

  - [x]* 16.6 Write integration test for HTTPS redirect
    - **Property 19: HTTP requests redirect to HTTPS preserving path and query**
    - **Validates: Requirements 6.3**

  - [x] 16.7 Implement IaC for CloudWatch alarms, SNS topics, and metric filters
    - Cost alarm on daily-cost metric (5-min eval, configurable threshold), separate cost and ops SNS topics, ops alarms for moderation/rate-limit/Bedrock-Polly error spikes
    - _Requirements: 16.2, 16.3, 16.4, 16.6_

  - [x]* 16.8 Write smoke tests for IaC configuration
    - Assert SSM parameters exist, ACM cert SANs match Custom_Domain, DynamoDB TTL configured, S3 BPA enabled on `audio` and `training-corpus`
    - _Requirements: 6.2, 17.2_

- [x] 17. Implement Build_Pipeline and Production_Gate orchestration
  - [x] 17.1 Author `.github/workflows/ci.yml`
    - Steps: verify PLAN/TEST_PLAN present (10 s), log commit hash + last-modified ISO 8601 UTC for both, run all governance scripts, run unit + property + integration tests, render PlantUML, build SPA, deploy on `main` after all gates pass
    - _Requirements: 10.4, 10.5, 11.3, 11.4, 11.5, 11.6, 12.1, 12.4, 12.5, 12.6, 13.3, 13.7_

  - [x] 17.2 Implement `scripts/production_gate.py`
    - Aggregate gate results; on block emit a report within 30 s naming the failed gate, failing items, and run timestamp; emit self-health signal within 60 s of run start
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

- [x] 18. Final checkpoint - end-to-end verification
  - Ensure all tests pass, ask the user if questions arise.

- [x] 19. Downloadable joke audio (R2.10, R2.11) — post-Phase-1 enhancement
  - [x] 19.1 Add a download-variant presigned URL to `voice_synthesizer`
    - `synthesize` mints a second presigned GET URL with `ResponseContentDisposition = attachment; filename="dad-joke-<id>.mp3"`; add `audio_download_url` to `SynthesisResult`; degrade to `None` if only the download presign fails
    - Extend `presign_audio_url` with a `download_generation_id` param for the `GET /v1/jokes/{id}` audit path
    - _Requirements: 2.10_

  - [x] 19.2 Thread `audioDownloadUrl` through `response_builder` + `handler`
    - `build_success` gains an `audio_download_url` param (forced to `null` when `audio_available` is `false`); POST and GET-by-id paths pass it through; `_re_presign_audio_ref` returns the download URL too
    - _Requirements: 2.10_

  - [x] 19.3 Add the frontend download control
    - `api.ts` `JokeApiSuccess.audioDownloadUrl`; `index.html` download link; `main.ts` `renderDownloadLink` (sets href + `download` filename, hides when null or audio unavailable); styles for `.audio-actions` + `.btn[hidden]`
    - _Requirements: 2.11_

  - [x]* 19.4 Tests for the download feature
    - **Property 45: Audio download URL carries an attachment disposition**
    - voice_synthesizer unit + property (disposition, degradation), response_builder/handler flow-through, frontend component (visible/hidden)
    - **Validates: Requirements 2.10, 2.11**

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP, but every Correctness Property test (1..45) must be implemented before a production deployment per the Test_Plan.
- Each task references specific granular requirement clauses (e.g., 5.4) for traceability rather than just user-story numbers.
- Property test sub-tasks are placed adjacent to the implementation they verify so failures are caught at the earliest possible point.
- Checkpoints provide explicit pause points for human review before crossing major architectural boundaries (utilities → backend pipeline → frontend → IaC).
- Documentation tasks (15.x, 14.1) author files that the Build_Pipeline scripts validate; they are required code artifacts, not user-facing prose.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["2.1", "2.3", "2.5", "2.7", "3.1", "4.1", "4.5", "14.1", "15.3"] },
    { "id": 2, "tasks": ["2.2", "2.4", "2.6", "2.8", "3.2", "3.3", "4.2", "4.3", "4.6", "6.1", "8.1", "13.1", "13.2", "13.4", "13.5", "13.6", "13.7", "14.2", "16.1", "16.2", "16.3"] },
    { "id": 3, "tasks": ["3.4", "4.4", "6.2", "6.3", "7.1", "8.2", "9.1", "9.3", "13.3", "14.3", "15.1", "16.4", "16.7"] },
    { "id": 4, "tasks": ["6.4", "7.2", "7.3", "9.2", "9.4", "10.1", "13.8", "15.2", "16.5", "16.8"] },
    { "id": 5, "tasks": ["10.2", "10.3", "10.4", "12.1", "16.6", "17.2"] },
    { "id": 6, "tasks": ["12.2", "12.3", "17.1"] },
    { "id": 7, "tasks": ["12.4", "12.5", "12.6"] }
  ]
}
```
