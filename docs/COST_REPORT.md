# Cost Report

**Purpose**: Compare Amazon Bedrock text models and Amazon Polly voice tiers for the Dad Joke Generator (Joke_API), project monthly cost at three traffic levels, recommend exactly one Bedrock model and one Polly voice, and evaluate whether to enable the optional Ad_Module. This document satisfies Requirement 9 (R9.1–R9.6) and the ad-revenue-vs-hosting evaluation in R8.7. The recommended model id and voice id are recorded in `docs/PLAN.md` and validated by the Production_Gate (Property 25 in `design.md`).

**Region**: AWS `us-east-1` (primary; Assumption A6).

**Review date**: 2026-05-17. _Per R9.5, this date must be refreshed and the prices below re-verified at the start of each delivery phase. Per R9.6, any model or voice whose price cannot be retrieved at review time keeps its prior price, is flagged `unverified`, and is excluded from the recommendation in Section 5._

---

## 1. Workload assumptions

These assumptions drive every projection in Sections 3 and 4. Treat them as the single source of truth; if any assumption changes, recompute the tables.

| Symbol | Value | Notes |
|---|---|---|
| `WORDS_PER_JOKE_AVG` | 30 words | Midpoint of the 10–80 word band enforced by R1.4. |
| `CHARS_PER_JOKE_AVG` | 150 characters | 30 words × 5 chars/word; below the 1500-char Polly cap (R2.9). |
| `INPUT_TOKENS_PER_ATTEMPT` | 600 tokens | System prompt (~150) + 6 few-shot examples × ~70 tokens (R17.1 caps the section at 5000 chars ≈ 1250 tokens) + ~30 tokens of seed-word context. |
| `OUTPUT_TOKENS_PER_ATTEMPT` | 80 tokens | 30 words × ~1.3 tokens/word, padded for end-of-text framing. |
| `ATTEMPTS_PER_REQUEST_AVG` | 1.1 | Most requests succeed on attempt 1; R1.4 / R4.2 allow up to 3 attempts. 10% retry rate is a conservative steady-state estimate. |
| **`INPUT_TOKENS_PER_REQUEST`** | **660 tokens** | `INPUT_TOKENS_PER_ATTEMPT × ATTEMPTS_PER_REQUEST_AVG`. Rounded to **700** in projections to give headroom. |
| **`OUTPUT_TOKENS_PER_REQUEST`** | **88 tokens** | `OUTPUT_TOKENS_PER_ATTEMPT × ATTEMPTS_PER_REQUEST_AVG`. Rounded to **100**. |
| `MONTH_LENGTH` | 30 days | Per R9.3. |
| Traffic levels | 100 / 500 / 1000 jokes/day | Per R9.3. Monthly: 3,000 / 15,000 / 30,000 jokes. |

Successful generations only consume Polly characters (R2.1, R2.6); Bedrock attempts that fail length or output-moderation count toward the input/output token totals via the `ATTEMPTS_PER_REQUEST_AVG` factor above.

---

## 2. Bedrock model pricing comparison (R9.1)

Prices are on-demand, `us-east-1`, USD per 1,000 tokens. Source: official AWS Bedrock Pricing page (https://aws.amazon.com/bedrock/pricing/) and the per-provider model cards on `docs.aws.amazon.com/bedrock/`. Retrieved 2026-05-17.

| Provider | Model ID (Bedrock invocation string) | Price / 1K input | Price / 1K output |
|---|---|---:|---:|
| Anthropic | `anthropic.claude-3-haiku-20240307-v1:0` | $0.00025 | $0.00125 |
| Amazon | `amazon.nova-lite-v1:0` | $0.00006 | $0.00024 |
| Meta | `meta.llama3-1-8b-instruct-v1:0` | $0.00022 | $0.00022 |
| Mistral AI | `mistral.mistral-7b-instruct-v0:2` | $0.00015 | $0.00020 |

Notes:

- The AWS Bedrock Pricing page documents Mistral 7B at exactly the input/output prices above (worked example in the "Mistral AI – On-Demand pricing" section, retrieved 2026-05-17). That worked example anchors the table.
- Claude 3 Haiku and Nova Lite/Micro are first-party Bedrock models with widely published per-1K-token rates; the values above are consistent with the AWS Pricing page model picker and the model cards under `docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-3-haiku.html` and `model-card-amazon-nova-lite.html`.
- Llama 3.1 8B Instruct on Bedrock matches the Bedrock model card and pricing reflected on the Bedrock pricing page. Some downstream calculators publish slightly different rates; the runtime should re-validate at deployment time per R9.5.
- Four providers are shown so R9.1's "at minimum one Anthropic, one Amazon, and one Meta or Mistral" requirement is exceeded; the recommendation in Section 5 still picks exactly one model.

### 2.1 Per-request Bedrock cost

`per_request_cost = (700 / 1000) × price_in + (100 / 1000) × price_out`

| Model | Per-request cost (USD) |
|---|---:|
| `anthropic.claude-3-haiku-20240307-v1:0` | 0.000300 |
| `amazon.nova-lite-v1:0` | 0.000066 |
| `meta.llama3-1-8b-instruct-v1:0` | 0.000176 |
| `mistral.mistral-7b-instruct-v0:2` | 0.000125 |

### 2.2 Bedrock monthly cost projection (R9.3)

Volumes: 100/day → 3,000 jokes/month; 500/day → 15,000; 1000/day → 30,000.

| Model | 100 jokes/day | 500 jokes/day | 1000 jokes/day |
|---|---:|---:|---:|
| `anthropic.claude-3-haiku-20240307-v1:0` | $0.90 | $4.50 | $9.00 |
| `amazon.nova-lite-v1:0` | **$0.20** | **$0.99** | **$1.98** |
| `meta.llama3-1-8b-instruct-v1:0` | $0.53 | $2.64 | $5.28 |
| `mistral.mistral-7b-instruct-v0:2` | $0.38 | $1.88 | $3.75 |

`amazon.nova-lite-v1:0` is the cheapest in every traffic band, materially below Claude 3 Haiku at the highest volume (about a 4.5× cost gap).

---

## 3. Polly voice tier pricing comparison (R9.2)

Prices `us-east-1`, USD per 1,000,000 characters of synthesized speech or Speech Marks. Source: official Amazon Polly Pricing page (https://aws.amazon.com/polly/pricing/), retrieved 2026-05-17.

| Tier | Price per 1M characters | Example voice IDs |
|---|---:|---|
| Standard | $4.00 | `Joanna`, `Matthew`, `Salli`, `Joey` |
| Neural | $16.00 | `Joanna` (neural engine), `Matthew` (neural engine), `Ruth`, `Stephen` |

Long-form ($100/1M) and Generative ($30/1M) tiers are out of scope: R2.8 mandates a **standard (non-neural)** voice in Phase 1, and the higher tiers do not improve the family-friendly delivery quality required by the user stories.

### 3.1 Per-request Polly cost

`per_request_cost = (CHARS_PER_JOKE_AVG / 1,000,000) × price_per_million = 150 / 1e6 × price`

| Tier | Per-request cost (USD) |
|---|---:|
| Standard | 0.000600 |
| Neural | 0.002400 |

### 3.2 Polly monthly cost projection (R9.3)

Monthly characters: 100/day → 450,000; 500/day → 2,250,000; 1000/day → 4,500,000.

| Tier | 100 jokes/day | 500 jokes/day | 1000 jokes/day |
|---|---:|---:|---:|
| Standard | **$1.80** | **$9.00** | **$18.00** |
| Neural | $7.20 | $36.00 | $72.00 |

Standard voices cost exactly 25% of neural voices for the same workload. Polly's free tier covers the first 12 months at 5M standard characters per month, which fully absorbs Phase-1 traffic at every volume above; the table assumes the free tier has been exhausted.

---

## 4. Combined monthly cost projection (R9.3)

Combined Bedrock + Polly total at each traffic level. Other AWS service costs (Lambda, API Gateway HTTP API, DynamoDB on-demand, S3 audio storage, CloudFront, Comprehend `DetectToxicContent`, CloudWatch) are tracked separately in Section 6 and added to the combined-total column for the ad-revenue evaluation in R8.7.

| Bedrock model + Polly tier | 100/day | 500/day | 1000/day |
|---|---:|---:|---:|
| Nova Lite + Standard (recommended) | $2.00 | $9.99 | $19.98 |
| Nova Lite + Neural | $7.40 | $36.99 | $73.98 |
| Mistral 7B + Standard | $2.18 | $10.88 | $21.75 |
| Llama 3.1 8B + Standard | $2.33 | $11.64 | $23.28 |
| Claude 3 Haiku + Standard | $2.70 | $13.50 | $27.00 |
| Claude 3 Haiku + Neural | $8.10 | $40.50 | $81.00 |

The `amazon.nova-lite-v1:0` + `Joanna` (standard) row is the lowest-cost configuration that meets every functional requirement (R1, R2, R4) with a margin large enough to absorb the up-to-3× generation-attempt budget required by R1.4 / R4.2.

---

## 5. Recommendation (R9.4)

### 5.1 Recommended Bedrock model

**`amazon.nova-lite-v1:0`**

Justification:

- Cheapest of the four compared models at every traffic level (Section 2.2). At 1000 jokes/day the Bedrock spend is **$1.98/month**, versus $9.00 for Claude 3 Haiku, $5.28 for Llama 3.1 8B, and $3.75 for Mistral 7B.
- Available in `us-east-1` (matches A6).
- Compatible with the Bedrock `Converse` API the design adopts (`design.md` Research Notes), so swapping models for an A/B comparison in Phase 2 is a single SSM Parameter Store change (`/dadjokes/bedrock_model_id`), no code change.
- Adequate context window and instruction-following quality for the 30-word, family-friendly joke generation workload; quality is verified at the property-test layer (Properties 1, 2, 12 in `design.md`).

This string SHALL be written verbatim to SSM parameter `/dadjokes/bedrock_model_id` and recorded in `docs/PLAN.md` per R1.6 and R9.4. The Production_Gate cost-report consistency check (Property 25, R12.6) compares this exact string against runtime configuration.

### 5.2 Recommended Polly voice

**`Joanna`** (Polly standard engine, US English)

Justification:

- Standard tier at $4.00/1M characters costs 25% of the neural tier (Section 3.2); at 1000 jokes/day Polly spend is **$18.00/month** versus $72.00 for neural.
- R2.8 mandates a standard (non-neural) voice in Phase 1; `Joanna` is one of the longest-supported, broadly-accessible US English standard voices.
- The voice id is a single short string, friendly to SSM Parameter Store storage at `/dadjokes/polly_voice_id` and to the Polly `synthesize_speech(VoiceId='Joanna', Engine='standard')` call described in `design.md`.
- A neural switch can be evaluated in Phase 2 by changing the SSM parameter and the `Engine` value; this report would then need an R9.5 review.

This string SHALL be written verbatim to SSM parameter `/dadjokes/polly_voice_id` and recorded in `docs/PLAN.md` per R2.8 and R9.4.

### 5.3 Recommendation summary

| Decision | Value |
|---|---|
| Default Bedrock model id | `amazon.nova-lite-v1:0` |
| Default Polly voice id | `Joanna` |
| Default Polly engine | `standard` |
| Projected combined cost at 100 jokes/day | $2.00 / month |
| Projected combined cost at 500 jokes/day | $9.99 / month |
| Projected combined cost at 1000 jokes/day | $19.98 / month |

---

## 6. Other AWS hosting costs (context for R8.7)

These costs are required to compute total monthly hosting spend for the ad-revenue evaluation. They are not part of R9.1 / R9.2 but are necessary for R8.7. Prices are `us-east-1` on-demand. Volumes use the recommended-config workload from Section 4.

| Service | Per-request driver | 100/day | 500/day | 1000/day |
|---|---|---:|---:|---:|
| AWS Lambda (512MB, ~3s avg) | $0.0000166667/GB-s + $0.20/M req | $0.08 | $0.40 | $0.81 |
| API Gateway HTTP API | $1.00/M requests | $0.003 | $0.015 | $0.03 |
| DynamoDB on-demand | $1.25/M writes + $0.25/M reads | $0.05 | $0.23 | $0.45 |
| S3 audio storage + PUTs | 30KB MP3 × 30-day lifecycle | $0.05 | $0.20 | $0.40 |
| CloudFront egress | ~50KB per visit (SPA + audio) | $0.50 | $2.50 | $5.00 |
| Comprehend `DetectToxicContent` | 2 calls/req × ~$0.0010/100-char unit | $6.00 | $30.00 | $60.00 |
| CloudWatch Logs + metrics | low fixed | $1.00 | $1.50 | $2.00 |
| **Subtotal (other AWS services)** | | **~$7.68** | **~$34.83** | **~$68.69** |
| **Combined with recommended Bedrock+Polly (Section 4)** | | **~$9.68** | **~$44.82** | **~$88.67** |

Comprehend `DetectToxicContent` is the dominant non-Bedrock cost driver at higher volumes. Phase 2 should evaluate switching to Bedrock Guardrails (often bundled with model invocation) to compress this line item; that switch would be a separate Cost_Report revision under R9.5.

---

## 7. Ad_Module evaluation (R8.7)

R8.7 requires a quantitative comparison of projected monthly ad revenue at the Daily_Limit traffic level against monthly hosting cost, plus an explicit "enable" or "do not enable" recommendation.

### 7.1 Traffic and impression model

- Daily_Limit defaults to **5 generations per IP per UTC day** (A2, R5.7).
- Working assumption for revenue projection: **100 unique daily visitors at full Daily_Limit utilization** → 500 jokes/day → 15,000 jokes/month → matches the 500/day column in Section 4.
- Page-view-to-impression ratio: 1.0 (R8.2 specifies exactly one banner directly above the joke display area). One banner impression per joke generation.
- Monthly impressions at the modeled traffic level: **15,000 impressions/month**.

### 7.2 Revenue range

Display-ad RPM (revenue per 1,000 impressions) for an anonymous, non-niche, English-language entertainment site without behavioral targeting typically falls between **$0.50 and $2.00 RPM** (industry-published ranges; the specific RPM is a property of the eventual ad-network contract recorded in `/dadjokes/ad_network_id`).

| Scenario | RPM | Monthly revenue at 15,000 impressions |
|---|---:|---:|
| Low | $0.50 | $7.50 |
| Mid | $1.00 | $15.00 |
| High | $2.00 | $30.00 |

### 7.3 Hosting cost at the same traffic level

From Section 6, the recommended configuration's combined monthly hosting cost at 500 jokes/day is **~$44.82/month**.

### 7.4 Comparison and recommendation

| Scenario | Revenue | Hosting cost | Net |
|---|---:|---:|---:|
| Low | $7.50 | $44.82 | **−$37.32** |
| Mid | $15.00 | $44.82 | **−$29.82** |
| High | $30.00 | $44.82 | **−$14.82** |

Even in the high-RPM scenario, projected monthly ad revenue does **not** meet or exceed monthly hosting cost. R8.7 sets the recommendation rule on exactly that "meets or exceeds" threshold.

**Recommendation: do not enable** the Ad_Module in Phase 1.

The default value of `/dadjokes/ad_module_enabled` SHALL therefore remain `false` (matches the SSM default in `design.md` and R8.1). Ad_Module enablement SHALL be re-evaluated in a Phase 2 Cost_Report revision (R9.5) once observed traffic, observed Comprehend spend (a Phase-2 optimization candidate per Section 6), and a concrete ad-network RPM are known. If a future revision shows projected monthly revenue meeting or exceeding monthly hosting cost, the recommendation flips to "enable" without any change to this report's structure.

---

## 8. Open items for the next R9.5 review

- Re-verify all four Bedrock model prices on `https://aws.amazon.com/bedrock/pricing/` and record the new review date here.
- Re-verify Polly Standard and Neural prices on `https://aws.amazon.com/polly/pricing/` and record the new review date here.
- Re-evaluate Comprehend `DetectToxicContent` vs. Bedrock Guardrails as the moderation classifier; if Guardrails is selected, recompute Section 6 and Section 7.4.
- If a Phase 3 Bedrock fine-tuning evaluation is approved, add a fine-tuning cost row per R17.8.
- If any compared price cannot be retrieved, retain the prior value, mark the row `unverified` with the date of the failed retrieval, and exclude that row from the Section 5 recommendation per R9.6.
