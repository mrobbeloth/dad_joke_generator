# Plan Document — Dad Joke Generator

**Purpose**: Authoritative project plan for the Dad Joke Generator (Web_App). This document satisfies Requirement 11 (Plan Document and Test Plan governance), Requirement 14 (Phased Delivery), Requirement 15 (AWS Manual Setup Identification), and Requirement 17.6 (Training_Corpus rights confirmation). It is parsed by `scripts/plan_parser.py` and validated by `scripts/check_phases.py`, `scripts/check_manual_setup.py`, `scripts/check_doc_freshness.py`, `scripts/check_plan_xref.py`, and `scripts/check_cost_report.py`. Per R11.1, every requirement identifier from `.kiro/specs/dad-joke-generator/requirements.md` is recorded below with its phase assignment and status, and the recommended Bedrock model and Polly voice are named.

**Last reviewed (UTC)**: 2026-05-17
**Author**: Site owner
**Companion documents**: `docs/TEST_PLAN.md`, `docs/COST_REPORT.md`, `docs/architecture/component.puml`, `docs/architecture/deployment.puml`, `docs/architecture/sequence.puml`

---

## Configuration

These values are also written to AWS Systems Manager Parameter Store at deploy time and are validated against the runtime configuration by the Production_Gate (R12.6, Property 25).

- Bedrock model: amazon.nova-lite-v1:0
- Polly voice: Joanna
- Polly engine: standard
- training_corpus_rights_confirmed: false

The `training_corpus_rights_confirmed` flag defaults to `false` per R17.7's fail-closed rule. **Until this flag is flipped to `true` by the operator following the runbook in Section "Training_Corpus rights confirmation" below, the Joke_Generator SHALL NOT include any Training_Corpus-derived content as few-shot examples.**

The Bedrock model id and Polly voice id mirror the recommendation in Section 5 of `docs/COST_REPORT.md`. Any change here MUST be matched by a Cost_Report revision under R9.5 and the runtime SSM values; otherwise the Production_Gate consistency check (R12.6) will block deployment.

---

## Requirements

Every requirement identifier below comes from `.kiro/specs/dad-joke-generator/requirements.md`. Each row records the phase assignment and current delivery status. Status vocabulary is `{Planned, In-Progress, Completed, Deferred}` per R11.1.

| ID | Title | Phase | Status |
| -- | ----- | ----- | ------ |
| R1 | Joke Generation from Seed Words | Phase 1 Minimum Viable Product | Completed |
| R2 | Voice Output of Generated Jokes | Phase 1 Minimum Viable Product | Completed |
| R3 | Family-Friendly Input Moderation | Phase 1 Minimum Viable Product | Completed |
| R4 | Family-Friendly Output Moderation | Phase 1 Minimum Viable Product | Completed |
| R5 | IP-Based Rate Limiting | Phase 1 Minimum Viable Product | Completed |
| R6 | Custom Domain and TLS | Phase 1 Minimum Viable Product | In-Progress |
| R7 | Professional and Simple Frontend | Phase 1 Minimum Viable Product | In-Progress |
| R8 | Optional Advertising Banner | Phase 3 Optional Enhancements | Completed |
| R9 | Cost Evaluation and Model Selection | Phase 2 Hardening and Cost Optimization | Completed |
| R10 | Architecture Documentation in PlantUML | Phase 1 Minimum Viable Product | Completed |
| R11 | Plan Document and Test Plan Governance | Phase 1 Minimum Viable Product | In-Progress |
| R12 | Production Deployment Gate | Phase 1 Minimum Viable Product | In-Progress |
| R13 | GitHub Project Board and Branching Workflow | Phase 2 Hardening and Cost Optimization | In-Progress |
| R14 | Phased Delivery | Phase 1 Minimum Viable Product | In-Progress |
| R15 | AWS Manual Setup Identification | Phase 1 Minimum Viable Product | In-Progress |
| R16 | Observability and Cost Monitoring | Phase 1 Minimum Viable Product | Completed |
| R17 | Training Corpus Handling | Phase 1 Minimum Viable Product | Completed |
| R18 | Joke Output Round-Trip and Logging Integrity | Phase 1 Minimum Viable Product | Completed |

The "Bedrock model" and "Polly voice" selections recorded in the Configuration section above and in the row for R1, R2, and R9 above are the values audited by Property 25 (R12.6).


---

## Phase 1 Minimum Viable Product

**Summary**: Ship a working, family-friendly Dad Joke Generator on a custom domain with rate limiting, observability, governance, and a curated Training_Corpus. This is the bulk of the system: every requirement that must be true for a publicly reachable launch.

**Entry checklist** (must hold before Phase 1 work begins):

- [x] AWS account created and billing alert configured (see "AWS Manual Setup" below).
- [x] `docs/COST_REPORT.md` recommends exactly one Bedrock model and one Polly voice (Requirement 9.4).
- [x] `docs/architecture/*.puml` skeletons exist (Requirement 10.1, Requirement 10.2).
- [x] `docs/PLAN.md` and `docs/TEST_PLAN.md` exist and are committed (Requirement 11.3).
- [x] Repository pinned to Python 3.12 backend and TypeScript SPA (Assumption A1).

**Exit checklist** (must hold to consider Phase 1 complete):

- [ ] Every Phase 1 requirement listed below is marked `Completed` in the table above.
- [ ] All five test types in `docs/TEST_PLAN.md` meet their coverage targets and pass criteria (Requirement 11.2, Requirement 12.1).
- [ ] Production_Gate self-health probe responds within 60 s of pipeline start (Requirement 12.2, Requirement 12.3).
- [ ] All three PlantUML diagrams render successfully in CI within 120 s per file (Requirement 10.4, Requirement 10.5).
- [ ] Phase 1 phase summary published within 5 business days of exit-checklist completion, listing every delivered requirement identifier, observed monthly cost in USD, and at least three lessons learned (Requirement 14.4).

**Requirements assigned to Phase 1**:

- R1
- R2
- R3
- R4
- R5
- R6
- R7
- R10
- R11
- R12
- R14
- R15
- R16
- R17
- R18

---

## Phase 2 Hardening and Cost Optimization

**Summary**: After Phase 1 is publicly reachable, harden the operational discipline (project-board hygiene, branching workflow, CI/Production_Gate maturity) and re-run the Cost_Report against observed traffic to lock in the cheapest viable Bedrock model and Polly tier.

**Entry checklist** (must hold before Phase 2 work begins):

- [ ] Phase 1 exit checklist fully satisfied.
- [ ] At least 30 calendar days of observed traffic and cost data captured in CloudWatch.
- [ ] Phase 1 phase summary published per Requirement 14.4.

**Exit checklist** (must hold to consider Phase 2 complete):

- [ ] `docs/COST_REPORT.md` re-reviewed with current AWS pricing and a fresh review date recorded (Requirement 9.5).
- [ ] Project_Board contains exactly five columns labeled Backlog, In Progress, In Review, In Production, Done (Requirement 13.1).
- [ ] Every active Project_Board card is reflected in the "GitHub Project Board" section of this document within 1 business day (Requirement 13.5).
- [ ] Branch protection on `main` enforces the Production_Gate suite on every PR (Requirement 13.3, Requirement 13.7).
- [ ] Phase 2 phase summary published within 5 business days of exit-checklist completion (Requirement 14.4).

**Requirements assigned to Phase 2**:

- R9
- R13

---

## Phase 3 Optional Enhancements

**Summary**: Enhancements considered only if a future Cost_Report review (Requirement 9.5) or operator decision warrants them. The advertising banner is the only requirement assigned here; per Requirement 8.7 and the analysis in `docs/COST_REPORT.md` Section 7.4, the current recommendation is **do not enable**, and the feature flag remains `false` until projected revenue meets or exceeds hosting cost.

**Entry checklist** (must hold before Phase 3 work begins):

- [ ] Phase 2 exit checklist fully satisfied.
- [ ] A future Cost_Report revision shows projected monthly Ad_Module revenue meets or exceeds projected monthly hosting cost at the modeled traffic level (Requirement 8.7).
- [ ] An ad-network identifier has been selected and recorded as the value of `/dadjokes/ad_network_id` (Requirement 8.4).

**Exit checklist** (must hold to consider Phase 3 complete):

- [ ] `/dadjokes/ad_module_enabled` flipped to `true` in SSM Parameter Store (Requirement 8.1).
- [ ] Frontend renders exactly one banner above the joke display area, full-width, max 250 px tall (Requirement 8.2).
- [ ] CSP allow-list contains exactly the configured ad-network domain and no other third-party domains (Requirement 8.4).
- [ ] Phase 3 phase summary published within 5 business days of exit-checklist completion (Requirement 14.4).

**Requirements assigned to Phase 3**:

- R8


---

## AWS Manual Setup

This section satisfies R15.1 and R15.2. Each item is an action that **cannot** be performed by the Build_Pipeline and must be carried out by an operator outside of code. Every item carries a unique identifier, a description, a completion checkbox, and a completion-date field in ISO 8601 (`YYYY-MM-DD`) form.

**Required format** for completed items, validated by `scripts/check_manual_setup.py`: each line is a markdown checkbox followed by a unique identifier, a colon, the item description, and a completion date in parentheses written as `YYYY-MM-DD`. A checked item without a valid `YYYY-MM-DD` date in the same line will fail the manual-setup validator (R15.4, R15.5, Property 29). When the Build_Pipeline starts, every item below must be checked and dated; otherwise the build terminates within 5 s with a non-zero exit code (R15.6).

Items:

- [x] MS01: Repository initialized and committed to GitHub (2026-05-17)
- [x] MS02: AWS account created and root MFA enabled (2026-05-17)
- [ ] MS03: Billing alert configured at the cost threshold recorded in `/dadjokes/cost_alarm_threshold_usd` _(deferred 2026-05-21: account 455110962976 is shared in OSU AWS Organization `o-w9mnpf422e`; whole-account budget produces noise from non-dadjokes spend (~$200/month). Tag-scoped budget requires `Project` cost-allocation tag activation at the org level (only the master account `683792142612` can do this). OSU IT emailed 2026-05-21. HCL retained in `infra/terraform-bootstrap/budgets.tf` behind `var.budget_enabled` flag — set to `true` and re-apply once tag is activated.)_
- [x] MS04: Bedrock model access requested and approved for `amazon.nova-lite-v1:0` in `us-east-1` (2026-05-21)
- [ ] MS05: Custom domain registered or delegated to AWS Route 53 hosted zone
- [ ] MS06: ACM certificate requested for the Custom_Domain and DNS validation completed
- [x] MS07: SSM SecureString parameter `/dadjokes/ip_hash_salt` set to 32 or more random bytes via `aws ssm put-parameter --type SecureString` (2026-05-21)
- [ ] MS08: SES domain verification completed for the cost-alert and ops-alert sender address
- [ ] MS09: Cost SNS topic subscription confirmed by the site-owner email recipient
- [ ] MS10: Ops SNS topic subscription confirmed by the site-owner email recipient
- [ ] MS11: Training_Corpus S3 bucket populated with curated source files (only after `training_corpus_rights_confirmed` is flipped to `true` in this document)
- [ ] MS12: Deployment IAM role or user created with least-privilege policies for Bedrock, Polly, Comprehend, DynamoDB, S3, SSM, CloudWatch _(deferred 2026-05-21: the GitHub Actions deploy role `dadjokes-github-deploy` currently has `AdministratorAccess` from bootstrap. The Lambda execution role created by `infra/terraform/iam.tf` is **already** least-privilege (10 documented Sids, all services scoped). The deploy role narrow-down is intentionally deferred to **after the first end-to-end production apply**, when IAM Access Analyzer can generate a least-privilege policy from the real CloudTrail events of that deploy. See `infra/terraform-bootstrap/README.md` "Narrowing the deploy role after first apply" for the runbook.)_
- [x] MS13: GitHub project board created with exactly five columns: Backlog, In Progress, In Review, In Production, Done (R13.1) (2026-05-21)
- [x] MS14: GitHub branch protection enabled on `main` to require Production_Gate checks on every pull request (R13.3, R13.7) (2026-05-21)
- [x] MS15: GitHub Actions repository secrets configured (deployment role ARN, Bedrock-region, custom-domain hosted-zone id) (2026-05-21)

Items MS01 and MS02 are recorded as complete with real ISO 8601 dates so the validator's date-extraction path is exercised on the document. Every other item must be ticked and dated by the operator before the first production deployment.

---

## GitHub Project Board

Per R13.5, this section lists the current set of Project_Board feature cards by title and identifier. The operator MUST update this section within 1 business day of any card being added, removed, or moved between columns. The Project_Board itself is an operator-managed artifact at `https://github.com/<owner>/<repo>/projects/<n>`; the snapshot below is the source of truth for the Build_Pipeline.

Columns (R13.1): Backlog, In Progress, In Review, In Production, Done.

Card list (initial — replace as cards are created):

- _(Backlog)_ — no cards recorded yet. Add cards here as `<card-id>: <title>`.
- _(In Progress)_ — no cards recorded yet.
- _(In Review)_ — no cards recorded yet.
- _(In Production)_ — no cards recorded yet.
- _(Done)_ — no cards recorded yet.

Branching rule for every card (R13.2): the Feature_Branch name SHALL match `^feature/[a-z0-9-]{3,50}$` and is validated by `scripts/check_branch_name.py`.

---

## Training_Corpus rights confirmation

Per R17.6, this section records the provenance of the Training_Corpus and an explicit rights confirmation. Per R17.7, the Joke_Generator SHALL NOT include any Training_Corpus-derived content as Bedrock few-shot examples until the `training_corpus_rights_confirmed` flag is set to `true`.

| Field | Value |
| ----- | ----- |
| Source location | `D:\Dad Jokes-3-001` (operator local), staged to private S3 bucket `training-corpus` |
| Date of acquisition | _to be recorded by operator_ |
| Owner or licensor | _to be recorded by operator_ |
| Rights confirmation written? | No |
| `training_corpus_rights_confirmed` | false |

**Operator runbook to flip the flag to `true`** (every step required, in order):

1. Confirm in writing that the owner or licensor of every file under `D:\Dad Jokes-3-001` has granted rights to use the contents as a style reference and as Bedrock few-shot examples for an anonymous, family-friendly public web service.
2. Record the source location, the date of acquisition, the owner or licensor, and the rights-confirmation statement in the table above.
3. For every video or image file in the corpus, confirm that text or caption extraction succeeded (R17.4, R17.5); record any extraction failures in this section so they are excluded from the few-shot pool.
4. Stage the curated, text-only corpus into the private `training-corpus` S3 bucket configured to block all public access (R17.2). Do not publish presigned URLs (R17.2, R17.3).
5. Run a legal-review checklist covering: source attribution, derivative-work rights, family-friendly content (G/PG), absence of identifiable third-party private data.
6. Update the table above so `Rights confirmation written?` reads `Yes`, then change the configuration line at the top of this document from `training_corpus_rights_confirmed: false` to `training_corpus_rights_confirmed: true`.
7. Tick MS11 in the AWS Manual Setup section with the same ISO 8601 date the flag was flipped.

Until step 6 is complete, `scripts/plan_parser.py` will report `rights_confirmed=false` and the Joke_Generator will fall back to an empty few-shot pool, producing jokes from the prompt context alone.

---

## Document maintenance

- The "Last reviewed (UTC)" date at the top of this document MUST be refreshed within 90 whole UTC days of the most recent review whenever a pull request modifies any file under `src/` (R11.6); otherwise `scripts/check_doc_freshness.py` will fail the build.
- Every requirement identifier in the Requirements table above MUST appear at least once in `docs/TEST_PLAN.md` (R12.5); otherwise `scripts/check_plan_xref.py` will fail the build.
- Every requirement identifier above MUST appear in exactly one of the three phase sections (R14.2, Property 27); duplicates or omissions are rejected by `scripts/check_phases.py`.
