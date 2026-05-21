# Test Plan — Dad Joke Generator

**Purpose**: Authoritative test plan for the Dad Joke Generator (Web_App). This document satisfies Requirement 11.2 (one row per test type with coverage target and pass criterion) and Requirement 12.5 (every requirement identifier in `docs/PLAN.md` is referenced here at least once). It is parsed by `scripts/plan_parser.py`, validated for cross-reference completeness by `scripts/check_plan_xref.py`, and validated for freshness by `scripts/check_doc_freshness.py`.

**Last reviewed (UTC)**: 2026-05-17
**Author**: Site owner
**Companion documents**: `docs/PLAN.md`, `docs/COST_REPORT.md`, `docs/architecture/component.puml`, `docs/architecture/deployment.puml`, `docs/architecture/sequence.puml`

---

## Test types

Exactly five rows, one per required test type. The Production_Gate inspects this table via `scripts.plan_parser.parse_test_plan` and treats any missing test-type row as a build failure.

| Test type     | Coverage target | Pass criterion |
| ------------- | --------------- | -------------- |
| unit          | 90%             | Every public function in `src/joke_api/` and `scripts/` is covered by at least one unit or property test; the full `pytest tests/unit tests/property tests/smoke` suite passes with no failures and no unexpected warnings other than pre-existing botocore deprecation warnings. |
| integration   | 80%             | Every cross-module invocation path (handler to moderator, moderator to generator, generator to synthesizer, synthesizer to store) is exercised by at least one moto-backed integration test in `tests/integration/`; the full integration suite passes with no failures. |
| end-to-end    | 50%             | Happy-path `POST /v1/jokes` against a deployed dev stack returns a 200 response with a 10..80 word joke, an audio URL, and a remaining count. Suite is run nightly and on every production deploy. |
| accessibility | 100%            | Every page of the SPA at viewport widths 320, 768, 1280, and 1920 px reports zero serious or critical violations from `@axe-core/playwright` (R7.3, R7.4). |
| performance   | 90%             | p95 of `POST /v1/jokes` under 10 seconds at 50 RPS sustained for 5 minutes (R1.3); zero 5xx responses during the same window. |

---

## Requirement cross-reference

Every requirement identifier `R1`..`R18` declared in `docs/PLAN.md` MUST appear at least once in this section. `scripts/check_plan_xref.py` enforces this property (R12.5, Property 24) by comparing the parsed PLAN.md identifier set with the set of `\bR\d+\b` tokens found anywhere in this document. The bullet list below maps each requirement to the test artefacts that cover it.

- R1 (Joke Generation from Seed Words): unit (`tests/property/test_joke_generator_property.py` Properties 1, 2, 12), integration (handler smoke against the joke-generator stage), end-to-end (`POST /v1/jokes` happy path).
- R2 (Voice Output of Generated Jokes): unit (`tests/property/test_voice_synthesizer_property.py` Properties 6, 7; `tests/unit/test_voice_synthesizer_polly_kwargs.py` for R2.2 / R2.8), integration (handler synthesize stage with moto-backed Polly stub).
- R3 (Family-Friendly Input Moderation): unit (`tests/property/test_moderators_property.py` Property 9; `tests/property/test_request_validator_property.py` for R3.4 / R3.5), integration (handler moderation gate against Comprehend stub).
- R4 (Family-Friendly Output Moderation): unit (`tests/property/test_moderators_property.py` Property 11; `tests/unit/test_fallback_jokes.py` for the R4 retry-then-fallback contract), integration (handler retry loop).
- R5 (IP-Based Rate Limiting): unit (`tests/property/test_rate_limiter_property.py` Properties 14-16; `tests/property/test_client_ip_property.py` Property 18; `tests/property/test_config_property.py` Property 17 for R5.7 daily-limit bounds), integration (handler rate-limit gate against moto-backed DynamoDB).
- R6 (Custom Domain and TLS): integration (smoke against deployed CloudFront distribution and Route 53 record set), end-to-end (HTTPS handshake check covering ACM certificate, redirect from `http://` to `https://`, and Custom_Domain `Host` header preservation).
- R7 (Professional and Simple Frontend): accessibility (`@axe-core/playwright` sweep at viewport widths 320, 768, 1280, 1920 px), unit (frontend component tests in `web/` covering R7.1 layout, R7.2 typography, R7.5 sanitized error display).
- R8 (Optional Advertising Banner): unit (frontend `web/src/ad_module.ts` flag tests covering R8.1 enable/disable, R8.2 single-banner rule, R8.4 CSP allow-list), accessibility (axe sweep with the flag both enabled and disabled).
- R9 (Cost Evaluation and Model Selection): unit (`tests/unit/test_check_cost_report.py` covering R9.1..R9.5 — model comparison count, projection table, recommendation singularity, freshness window).
- R10 (Architecture Documentation in PlantUML): unit (`tests/smoke/test_plantuml_render.py` smoke-renders all three diagrams under `docs/architecture/`).
- R11 (Plan Document and Test Plan Governance): unit (`tests/unit/test_plan_parser.py`, `tests/unit/test_check_doc_freshness.py` covering R11.1..R11.6).
- R12 (Production Deployment Gate): unit (`tests/unit/test_check_plan_xref.py`, `tests/unit/test_check_cost_report.py`, `tests/unit/test_check_phases.py`, `tests/unit/test_check_manual_setup.py` covering R12.1..R12.6).
- R13 (GitHub Project Board and Branching Workflow): unit (`tests/unit/test_check_branch_name.py` covering R13.2 feature-branch regex `^feature/[a-z0-9-]{3,50}$` and R13.3 / R13.7 protected-branch enforcement).
- R14 (Phased Delivery): unit (`tests/unit/test_check_phases.py` covering R14.1 phase count, R14.2 unique requirement-to-phase assignment, R14.3 entry/exit checklists, R14.4 phase summary).
- R15 (AWS Manual Setup Identification): unit (`tests/unit/test_check_manual_setup.py` covering R15.1..R15.6 — edit-mode and build-start modes, ISO 8601 date enforcement).
- R16 (Observability and Cost Monitoring): unit (`tests/property/test_observability_property.py` Properties 30, 35; `tests/property/test_alerting_property.py` Properties 31-33; `tests/property/test_ip_hashing_property.py` Property 34 for R16.7).
- R17 (Training Corpus Handling): unit (`tests/property/test_training_corpus_property.py` Properties 36-39 covering R17.1..R17.7 — manifest integrity, S3-private staging, rights-confirmed fail-closed default).
- R18 (Joke Output Round-Trip and Logging Integrity): unit (`tests/property/test_joke_store_property.py` Properties 40-44 covering R18.1..R18.5 — write-then-read invariance, monotonic timestamps, hash determinism, redaction).

---

## Test execution commands

Quick reference for running each tier locally and in CI. Every command is terminable on Windows (no watch flags).

- Unit and property: `python -m pytest tests/unit tests/property tests/smoke`
- Property only: `python -m pytest tests/property`
- Smoke (PlantUML render): `python -m pytest tests/smoke`
- Integration: `python -m pytest tests/integration` — currently a documented placeholder; the moto-backed handler integration suite lands in task 10.4.
- End-to-end: to be added — Playwright smoke against the deployed dev stack, gated on a successful `POST /v1/jokes` happy path response shape.
- Accessibility: to be added — `@axe-core/playwright` invoked from Playwright at the four viewport widths listed in the test-types table.
- Performance: to be added — `k6` or `locust` harness driving the dev API Gateway endpoint at 50 RPS for 5 minutes, asserting the p95-latency and 5xx-rate criteria above.

---

## Coverage measurement

How the coverage target column is measured for each tier. The Production_Gate consumes the numeric target from the test-types table and the corresponding measurement here.

- Unit: line coverage measured with `pytest-cov` over `src/joke_api/` and `scripts/`. The 90% target is the floor; the gate fails when measured coverage drops below the target on `main`.
- Integration: line coverage measured with `pytest-cov` over the handler pipeline modules (`handler`, `input_moderator`, `output_moderator`, `joke_generator`, `voice_synthesizer`, `joke_store`, `rate_limiter`).
- End-to-end: count of design-mandated user journeys covered, expressed as a percentage. Currently one journey (`POST /v1/jokes` happy path) is in scope; the future `GET /v1/jokes/{id}` retrieval journey will raise the denominator.
- Accessibility: percentage of viewport-page combinations sweeping clean of serious or critical violations. With four viewports and N pages, the denominator is `4 * N`.
- Performance: percentage of 5-minute load-test runs in the trailing 30 days that met the p95 under 10 s threshold with zero 5xx responses. Runs are recorded in CloudWatch.

---

## Document maintenance

- The "Last reviewed (UTC)" date at the top of this document MUST be refreshed within 90 whole UTC days of the most recent review whenever a pull request modifies any file under `src/` (R11.6); otherwise `scripts/check_doc_freshness.py` will fail the build.
- Every requirement identifier in the Requirements table of `docs/PLAN.md` MUST appear at least once in the "Requirement cross-reference" section above (R12.5, Property 24); otherwise `scripts/check_plan_xref.py` will fail the build.
- The five-row test-types table MUST remain present and parseable by `scripts.plan_parser.parse_test_plan`; removing or renaming a row causes the Production_Gate to block deployment (R11.2, R12.1).
