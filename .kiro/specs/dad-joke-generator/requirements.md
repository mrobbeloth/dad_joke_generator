# Requirements Document

## Introduction

The Dad Joke Generator is a web application that produces corny, family-friendly dad jokes on demand using Amazon Bedrock as the underlying large language model (LLM) provider. Users visit a public website, optionally enter one or more seed words, and receive a generated dad joke as both readable text and synthesized audio. The system is anonymous (no login), rate-limited per source IP address to control cost, fronted by a custom domain, and designed for low operating cost. The project is delivered in phases under a documented Plan Document and Test Plan that gate every build, with architecture maintained in PlantUML and feature work tracked on a GitHub project board.

## Glossary

- **Web_App**: The end-to-end Dad Joke Generator system, including frontend, backend, and AWS-hosted services.
- **Frontend**: The browser-facing single-page web application that collects user input and renders jokes and audio.
- **Joke_API**: The backend HTTP service that orchestrates input validation, generation, synthesis, and rate limiting.
- **Joke_Generator**: The component that calls Amazon Bedrock to produce dad joke text from seed words and prompt context.
- **Voice_Synthesizer**: The component that converts joke text into audio using Amazon Polly.
- **Input_Moderator**: The component that classifies user-supplied seed words as family-friendly (G/PG) or not.
- **Output_Moderator**: The component that verifies generated joke text is family-friendly before returning it.
- **Rate_Limiter**: The component that tracks and enforces per-IP daily generation limits.
- **Joke_Store**: The persistent store of generated jokes, audio references, and rate-limit counters.
- **Custom_Domain**: The DNS-registered domain name attached to the Web_App via Route 53 and AWS Certificate Manager.
- **Ad_Module**: The optional component that renders a single advertising banner on the Frontend.
- **Cost_Report**: The written deliverable that compares Bedrock model and Polly voice cost options and projects monthly spend.
- **Architecture_Document**: The PlantUML-based document describing system components, interactions, and deployments.
- **Plan_Document**: The authoritative project plan that captures requirements, phases, architecture references, and acceptance gates.
- **Test_Plan**: The authoritative test strategy document that defines required test types, coverage targets, and pass criteria.
- **Build_Pipeline**: The CI/CD pipeline that builds, tests, and deploys the Web_App.
- **Production_Gate**: The set of automated and documented checks that must pass before a Build_Pipeline run is allowed to deploy to production.
- **Training_Corpus**: The user-supplied collection of dad joke source material located at `D:\Dad Jokes-3-001`, used as style reference and few-shot examples (not for model fine-tuning in Phase 1).
- **Project_Board**: The GitHub project board used to plan and track features.
- **Feature_Branch**: A Git branch dedicated to a single feature, merged to `main` only after a successful production deployment.
- **Family_Friendly**: Content rated G or PG: no profanity, sexual content, graphic violence, drugs, slurs, or targeted harassment.
- **Daily_Limit**: The maximum number of jokes a single source IP may generate within a 24-hour UTC window. Configurable between 5 and 10; default 5.

## Assumptions (to confirm during review)

- A1. Programming language for backend is **Python** (strong Bedrock SDK support via `boto3`); Frontend is a static SPA (HTML/CSS/JS or a lightweight framework). Reviewer may swap to TypeScript/Node.js.
- A2. Daily_Limit defaults to **5 per IP per UTC day**, configurable up to 10 without code change.
- A3. Training_Corpus is used as **few-shot prompt examples and tone/style reference** in Phase 1 (no Bedrock fine-tuning), with an optional Phase 3 evaluation of fine-tuning or knowledge bases.
- A4. The Web_App is **anonymous** (no user accounts or authentication) in all phases.
- A5. Ad_Module is **optional** and gated behind a feature flag; inclusion is decided after the Cost_Report review.
- A6. Target audience is **English-speaking, global, all ages** (G/PG content). Region: AWS `us-east-1` primary.
- A7. Accessibility target is **WCAG 2.1 AA** for the Frontend.

## Requirements

### Requirement 1: Joke Generation from Seed Words

**User Story:** As a visitor, I want to enter optional seed words and receive a generated dad joke, so that I can get a personalized corny joke.

#### Acceptance Criteria

1. WHEN a visitor submits a generation request with zero seed words, THE Joke_Generator SHALL produce one dad joke using Amazon Bedrock without seed-word constraints.
2. WHEN a visitor submits a generation request with one to five seed words where each seed word is between 1 and 30 characters and contains only letters, digits, hyphens, or apostrophes, THE Joke_Generator SHALL produce one dad joke whose text contains at least one of the supplied seed words as a case-insensitive substring match.
3. WHEN the Joke_Generator produces a joke, THE Joke_API SHALL return the joke text and a generation identifier that is unique per generation in the HTTP response within 10 seconds at the 95th percentile.
4. THE Joke_Generator SHALL constrain returned joke length to between 10 and 80 words inclusive, regenerating up to 2 additional times if a Bedrock response falls outside this range before returning the joke.
5. IF Amazon Bedrock returns an error or does not respond within 15 seconds, THEN THE Joke_API SHALL return HTTP 503 with a user-facing message indicating temporary unavailability and SHALL NOT return partial or out-of-range joke text.
6. THE Joke_Generator SHALL use a Bedrock model selected by the Cost_Report and recorded in the Plan_Document.
7. IF a visitor submits a generation request with more than five seed words, with any seed word exceeding 30 characters, or with any seed word containing characters other than letters, digits, hyphens, or apostrophes, THEN THE Joke_API SHALL reject the request with a validation error response identifying the violated rule and SHALL NOT invoke Amazon Bedrock.
8. IF the Joke_Generator cannot produce a joke within the 10-to-80 word range after 3 generation attempts within a single request, THEN THE Joke_API SHALL return HTTP 503 with a user-facing message indicating temporary unavailability.

### Requirement 2: Voice Output of Generated Jokes

**User Story:** As a visitor, I want to hear the joke read aloud and save the audio, so that I can enjoy the delivery, share it audibly, and keep a copy to replay offline.

#### Acceptance Criteria

1. WHEN a joke is successfully generated, THE Voice_Synthesizer SHALL submit the joke text to Amazon Polly for audio synthesis within 1 second of joke generation completion, provided the joke text is between 1 and 1500 characters.
2. THE Voice_Synthesizer SHALL produce audio in MP3 format at a bitrate of 64 kbps or lower to control bandwidth and Polly cost.
3. WHEN audio synthesis completes within 10 seconds, THE Joke_API SHALL return a playable audio URL alongside the joke text and SHALL set an `audio_available` flag to `true` in the response.
4. THE Joke_API SHALL ensure each returned audio URL remains playable for at least 15 minutes from the time of response.
5. WHERE the response contains a playable audio URL, THE Frontend SHALL provide a play control, a pause control, and a replay control for the returned audio.
6. IF Polly synthesis fails, returns an error, or does not complete within 10 seconds, THEN THE Joke_API SHALL return the joke text, SHALL set an `audio_available` flag to `false` in the response, and SHALL include an error indication identifying that audio synthesis was unavailable, without rolling back or discarding the joke text.
7. WHERE `audio_available` is `false`, THE Frontend SHALL hide the play, pause, and replay controls and SHALL display the joke as text only.
8. WHEN Polly synthesis is invoked, THE Voice_Synthesizer SHALL use the specific Polly voice identifier recorded in the Plan_Document as selected by the Cost_Report, and in Phase 1 this identifier SHALL reference a standard (non-neural) Polly voice.
9. IF the joke text exceeds 1500 characters, THEN THE Voice_Synthesizer SHALL skip Polly synthesis, and THE Joke_API SHALL return the joke text with `audio_available` set to `false`.
10. WHERE the response contains a playable audio URL, THE Joke_API SHALL also return a distinct download URL that, when retrieved, delivers the synthesized MP3 as a file attachment via a `Content-Disposition: attachment` response header with a filename of the form `dad-joke-<id>.mp3`, AND THE download URL SHALL remain valid for at least 15 minutes from the time of response.
11. WHERE the response contains a download URL, THE Frontend SHALL provide a download control that saves the audio file to the visitor's device, AND WHERE `audio_available` is `false`, THE Frontend SHALL hide the download control.

### Requirement 3: Family-Friendly Input Moderation

**User Story:** As the site owner, I want user-supplied seed words filtered for inappropriate content, so that the Web_App stays family-friendly.

#### Acceptance Criteria

1. WHEN a visitor submits seed words, THE Input_Moderator SHALL classify the submitted input as either Family_Friendly or not Family_Friendly before the Joke_API initiates any Bedrock call for that request.
2. IF the Input_Moderator classifies any portion of the submitted seed words as not Family_Friendly, THEN THE Joke_API SHALL reject the request with HTTP 400 and an error message indicating that input must be G or PG rated, AND SHALL NOT initiate any Bedrock call for that request.
3. WHEN classifying submitted seed words, THE Input_Moderator SHALL evaluate the input against a denylist covering profanity, sexual content, slurs, drug references, graphic violence, and targeted harassment, AND SHALL evaluate the input using a Bedrock-based or AWS Comprehend-based classifier, AND SHALL treat the input as not Family_Friendly if either the denylist or the classifier flags any portion of the input.
4. THE Input_Moderator SHALL accept seed word inputs with a length from 0 to 100 characters inclusive, where length is the count of characters in the submitted text.
5. IF a visitor submits seed words with a length greater than 100 characters, THEN THE Joke_API SHALL reject the request with HTTP 400 and an error message indicating the 100-character maximum, AND SHALL NOT invoke the Input_Moderator or initiate any Bedrock call for that request.
6. IF the Input_Moderator service is unreachable or returns an error response, THEN THE Joke_API SHALL fail closed by rejecting the request with HTTP 503 and an error message indicating that the moderation service is unavailable, AND SHALL NOT initiate any Bedrock call for that request.
7. IF the Input_Moderator does not return a classification within 3 seconds of receiving the input, THEN THE Joke_API SHALL fail closed by rejecting the request with HTTP 504 and an error message indicating a moderation timeout, AND SHALL NOT initiate any Bedrock call for that request.

### Requirement 4: Family-Friendly Output Moderation

**User Story:** As the site owner, I want generated jokes screened before display, so that the Web_App never serves inappropriate content even if a model produces it.

#### Acceptance Criteria

1. WHEN the Joke_Generator returns text, THE Output_Moderator SHALL classify the text as Family_Friendly or not_Family_Friendly within 500 milliseconds before the Joke_API responds to the visitor.
2. IF the Output_Moderator classifies the generated text as not_Family_Friendly, THEN THE Joke_Generator SHALL retry generation up to two additional times (maximum three attempts total) with a refined prompt that explicitly excludes profanity, sexual content, graphic violence, drugs, slurs, and targeted harassment.
3. IF all three generation attempts produce text classified as not_Family_Friendly, THEN THE Joke_API SHALL return a fallback joke randomly selected from a curated safe-joke list containing at least 20 entries and SHALL record a moderation failure entry containing timestamp, attempt count, and rejection category.
4. THE Output_Moderator SHALL apply the same content classification rules as the Input_Moderator, rejecting any text containing profanity, sexual content, graphic violence, drug references, slurs, or targeted harassment as defined by the Family_Friendly (G/PG) standard.
5. IF the Output_Moderator is unavailable or fails to return a classification within 500 milliseconds, THEN THE Joke_API SHALL return a fallback joke from the curated safe-joke list and SHALL record a moderation failure entry indicating moderator unavailability.

### Requirement 5: IP-Based Rate Limiting

**User Story:** As the site owner, I want each visitor IP limited to a small number of jokes per day, so that operating costs stay predictable.

#### Acceptance Criteria

1. WHEN a generation request is received, THE Rate_Limiter SHALL identify the source IP of the request before any joke generation work begins.
2. WHEN the source IP for a generation request has been identified, THE Rate_Limiter SHALL retrieve the count of successful generations recorded for that IP within the current UTC day, where the current UTC day is the interval from 00:00:00.000 UTC up to but not including the next 00:00:00.000 UTC.
3. IF the IP's daily count is greater than or equal to the Daily_Limit, THEN THE Joke_API SHALL reject the request with HTTP 429 and a response message indicating that the daily limit has been reached and that counters reset at 00:00 UTC.
4. WHEN a generation request completes successfully, THE Rate_Limiter SHALL atomically increment the IP's daily counter by exactly 1, such that concurrent requests from the same IP cannot produce a recorded count that diverges from the actual number of successful generations for that IP in the current UTC day.
5. IF a generation request does not complete successfully for any reason (input validation failure, timeout, upstream failure, or server error), THEN THE Rate_Limiter SHALL NOT increment the IP's daily counter.
6. WHEN UTC time crosses the 00:00:00 boundary of a new day, THE Rate_Limiter SHALL reset every IP daily counter to 0 within 60 seconds of that boundary.
7. THE Daily_Limit SHALL be configurable to any integer value from 5 to 10 inclusive through external configuration without modifying or redeploying source code, with a default value of 5.
8. WHERE a generation request arrives with a forwarded-for header populated by a trusted proxy or CDN, THE Rate_Limiter SHALL treat the leftmost address in that header as the originating client IP and apply rate-limiting against that address.
9. IF the source IP of a generation request cannot be determined (forwarded-for header missing when expected, header malformed, or request originates from an untrusted proxy), THEN THE Joke_API SHALL reject the request with an error response indicating that the client IP could not be identified, and THE Rate_Limiter SHALL NOT increment any counter for that request.

### Requirement 6: Custom Domain and TLS

**User Story:** As a visitor, I want to access the Web_App through a memorable custom domain over HTTPS, so that the site looks professional and is trustworthy.

#### Acceptance Criteria

1. THE Web_App SHALL be reachable over HTTPS at the Custom_Domain whose DNS records are managed in an AWS Route 53 hosted zone, such that a DNS query for the Custom_Domain resolves to the Web_App's public endpoint.
2. THE Web_App SHALL present a TLS certificate issued by AWS Certificate Manager that is unexpired at the time of access and whose Subject Common Name or Subject Alternative Name exactly matches the requested Custom_Domain hostname.
3. WHEN a visitor accesses the Web_App over plain HTTP, THE Web_App SHALL respond with HTTP status 301 redirecting to the equivalent HTTPS URL on the same Custom_Domain while preserving the original request path and query string.
4. THE Architecture_Document SHALL document the Custom_Domain name, the AWS Route 53 hosted zone and DNS record configuration, and the AWS Certificate Manager certificate provisioning and renewal process for the Custom_Domain.
5. IF a visitor requests the Web_App at a hostname not matching the TLS certificate's Subject Common Name or any Subject Alternative Name, THEN THE Web_App SHALL terminate the TLS handshake without delivering Web_App content.

### Requirement 7: Professional and Simple Frontend

**User Story:** As a visitor, I want a clean, easy-to-use interface, so that I can generate and hear a joke in seconds.

#### Acceptance Criteria

1. THE Frontend SHALL display, on a single primary view, a seed-word input field accepting 1 to 50 characters, a generate button, a joke display area supporting up to 1000 characters, and an audio control with play, pause, and replay actions.
2. WHEN the visitor presses the generate button, THE Frontend SHALL display a visible progress indicator within 200 milliseconds and SHALL keep it visible until either a joke is rendered or an error message is shown.
3. THE Frontend SHALL render the primary view without horizontal scrolling and without overlapping or clipped controls on viewports from 320 pixels to 1920 pixels wide.
4. THE Frontend SHALL meet WCAG 2.1 Level AA conformance for the primary view, including a minimum text contrast ratio of 4.5 to 1, full keyboard operability of all interactive controls, and visible focus indicators on every focusable element.
5. IF the Frontend encounters an error from the Joke_API, a network timeout exceeding 30 seconds, or a client-side validation failure such as empty input, input exceeding 50 characters, or unsupported characters, THEN THE Frontend SHALL display a human-readable error message identifying the error category and a suggested next action, and SHALL NOT display stack traces, cloud provider identifiers, or other internal system details.
6. WHEN the Joke_API or Frontend handles an error, THE Web_App SHALL record full technical error details in server-side or browser-side telemetry within 5 seconds of the error occurring, while displaying only the sanitized message defined in criterion 5 to the visitor.
7. WHEN the Frontend receives a successful joke response, THE Frontend SHALL display the visitor's remaining daily generation count as a non-negative integer within 500 milliseconds of rendering the joke.
8. IF the visitor's remaining daily generation count is 0, THEN THE Frontend SHALL disable the generate button and display a message indicating the daily limit has been reached and when it will reset.

### Requirement 8: Optional Advertising Banner

**User Story:** As the site owner, I want the option to display a single advertising banner, so that ad revenue can offset hosting costs if it is worth the effort.

#### Acceptance Criteria

1. THE Ad_Module SHALL be controlled by a boolean feature flag stored in configuration, SHALL default to disabled (false), and SHALL be re-readable without redeploying the Frontend.
2. WHERE the Ad_Module feature flag is enabled, THE Frontend SHALL render exactly one advertising banner positioned directly above the joke display area, occupying the full width of the joke display area, with a maximum rendered height of 250 pixels.
3. WHERE the Ad_Module feature flag is disabled, THE Frontend SHALL NOT render any advertising banner, SHALL NOT reserve layout space for the banner, and SHALL NOT issue any network requests to ad networks or trackers.
4. WHERE the Ad_Module is enabled, THE Ad_Module SHALL load advertisements from exactly one configured ad network identifier and SHALL NOT initiate network requests to any third-party domain other than that configured ad network.
5. WHERE the Ad_Module is enabled, IF the configured ad network does not deliver a banner within 3 seconds of page load, THEN THE Frontend SHALL leave the banner area visually empty, SHALL NOT display any error message or placeholder text in the banner area, and SHALL continue to render the joke display area without degradation.
6. WHERE the Ad_Module is enabled, IF the configured ad network returns an error response or is unreachable, THEN THE Frontend SHALL leave the banner area visually empty, SHALL NOT display any error message in the banner area, and SHALL NOT block or delay rendering of the joke display area beyond the 3 second timeout.
7. THE Cost_Report SHALL include a quantitative evaluation comparing projected monthly ad revenue at the Daily_Limit traffic level against monthly hosting costs, expressed in the same currency, and SHALL state a recommendation of either "enable" or "do not enable" the Ad_Module based on whether projected revenue meets or exceeds hosting costs.

### Requirement 9: Cost Evaluation and Model Selection

**User Story:** As the site owner, I want a documented comparison of Bedrock and Polly cost options, so that I can choose an affordable configuration.

#### Acceptance Criteria

1. THE Cost_Report SHALL compare at least three Bedrock text models suitable for short creative generation of up to 280 characters per joke, including at minimum one Anthropic model, one Amazon model, and one Meta or Mistral model, on price per 1,000 input tokens and price per 1,000 output tokens, with prices expressed in USD and sourced from the official AWS pricing page on a date recorded in the Cost_Report.
2. THE Cost_Report SHALL compare Amazon Polly standard voices and neural voices on price per one million characters in USD, sourced from the official AWS pricing page on a date recorded in the Cost_Report.
3. THE Cost_Report SHALL project monthly cost in USD for each compared Bedrock model and Polly voice tier at three traffic levels: low at 100 jokes per day, medium at 500 jokes per day, and high at 1,000 jokes per day, assuming a 30-day month and a documented average input token count, output token count, and character count per joke.
4. THE Cost_Report SHALL recommend exactly one default Bedrock model and exactly one default Polly voice, each with a written justification referencing the projected monthly cost figures from criterion 3, and THE Plan_Document SHALL record both recommendations.
5. WHEN a delivery phase begins, THE Cost_Report SHALL be reviewed and updated with current AWS pricing and a new review date recorded in the Cost_Report.
6. IF official AWS pricing for a compared model or voice cannot be retrieved during a review, THEN THE Cost_Report SHALL retain the prior price, flag the entry as unverified with the date of the failed retrieval, and exclude the unverified entry from the recommendation in criterion 4.

### Requirement 10: Architecture Documentation in PlantUML

**User Story:** As an engineer, I want the architecture maintained in PlantUML, so that diagrams are versioned alongside code and reviewed in pull requests.

#### Acceptance Criteria

1. THE Architecture_Document SHALL be authored in PlantUML source files with the `.puml` extension stored in the repository under `docs/architecture/`.
2. THE Architecture_Document SHALL include exactly three required diagrams: a component diagram showing all system components and their relationships, a deployment diagram showing AWS services and their connections, and a sequence diagram showing the end-to-end joke generation flow from user request to response delivery.
3. WHEN a pull request modifies any component, AWS service, or data flow in the implementation, THE Architecture_Document SHALL be updated within the same pull request, and the pull request SHALL NOT be merged until the corresponding PlantUML source files reflect the implementation change.
4. WHEN the Build_Pipeline runs, THE Build_Pipeline SHALL render each PlantUML source file in `docs/architecture/` to PNG or SVG format within 120 seconds per file.
5. WHEN all three required diagrams listed in criterion 2 are present and render successfully, THE Build_Pipeline SHALL publish the rendered diagrams as build artifacts retained for at least 30 days.
6. IF any of the three required diagrams listed in criterion 2 is missing from `docs/architecture/`, fails to render within the 120-second per-file timeout, or produces a PlantUML syntax error, THEN THE Build_Pipeline SHALL fail with an error message indicating which diagram failed and the failure reason, and SHALL NOT publish any rendered diagram artifacts from that build.

### Requirement 11: Plan Document and Test Plan Governance

**User Story:** As the site owner, I want the Plan Document and Test Plan referenced on every build, so that delivery stays aligned with documented requirements and tests.

#### Acceptance Criteria

1. THE Plan_Document SHALL be stored at `docs/PLAN.md` and SHALL contain, for every requirement, a unique requirement identifier matching the pattern `R[0-9]+`, the current phase selected from the set {Planned, In-Progress, Completed, Deferred}, and the selected Bedrock and Polly options identified by name.
2. THE Test_Plan SHALL be stored at `docs/TEST_PLAN.md` and SHALL list, for each required test type from the set {unit, integration, end-to-end, accessibility, performance}, a numeric coverage target between 0 and 100 percent inclusive and a pass criterion stated as an observable, measurable condition.
3. WHEN the Build_Pipeline starts a build, THE Build_Pipeline SHALL verify the presence of `docs/PLAN.md` and `docs/TEST_PLAN.md` within 10 seconds, and IF either file is missing, THEN THE Build_Pipeline SHALL terminate the build with a non-zero exit status and emit an error indicating which file is missing, without executing any subsequent build steps.
4. WHEN the Build_Pipeline starts a build, THE Build_Pipeline SHALL print, in the build log as a single atomic step, the commit hash and last-modified date in ISO 8601 UTC format for each of `docs/PLAN.md` and `docs/TEST_PLAN.md`.
5. IF the Build_Pipeline cannot obtain the commit hash or last-modified date for `docs/PLAN.md` or `docs/TEST_PLAN.md` during the logging step, THEN THE Build_Pipeline SHALL terminate the build with a non-zero exit status and emit an error indicating which value could not be obtained, with no partial values written to the build log.
6. WHEN a pull request modifies any file under `src/`, THE Build_Pipeline SHALL compute the number of whole days between the current build time in UTC and the timestamp of the most recent commit touching `docs/PLAN.md` and the most recent commit touching `docs/TEST_PLAN.md`, and IF either computed value exceeds 90 days, THEN THE Build_Pipeline SHALL terminate the build with a non-zero exit status and emit an error indicating which document is overdue for review.

### Requirement 12: Production Deployment Gate

**User Story:** As the site owner, I want production deployments blocked until tests pass and architecture and requirements are honored, so that production stays trustworthy.

#### Acceptance Criteria

1. IF any unit, integration, or end-to-end test defined in the Test_Plan has not completed with a passing result in the same Build_Pipeline run, or any such test was skipped without an explicit recorded waiver, THEN THE Production_Gate SHALL block deployment to the production environment.
2. WHEN a Build_Pipeline run begins, THE Production_Gate SHALL emit a self-health signal within 60 seconds of run start.
3. IF the self-health signal is not received within 60 seconds of run start, or the signal reports failure, THEN THE Build_Pipeline SHALL block deployment to production and SHALL terminate the run with a non-success result.
4. IF any PlantUML source under `docs/architecture/` fails to render, THEN THE Production_Gate SHALL block deployment to the production environment.
5. IF any requirement identifier listed in `docs/PLAN.md` lacks a corresponding test reference in `docs/TEST_PLAN.md`, or if either file is missing or unreadable, THEN THE Production_Gate SHALL block deployment to the production environment.
6. IF the Cost_Report is missing, or if the Cost_Report's recorded model selection or voice selection does not match the configured runtime values, THEN THE Production_Gate SHALL block deployment to the production environment.
7. WHEN the Production_Gate blocks a deployment, THE Build_Pipeline SHALL produce a report within 30 seconds of the block that identifies the failed gate name, the specific failing item or items, and the run timestamp.

### Requirement 13: GitHub Project Board and Branching Workflow

**User Story:** As the site owner, I want features planned on a GitHub project board and delivered on dedicated branches, so that work is visible and reversible.

#### Acceptance Criteria

1. THE Project_Board SHALL contain exactly five columns labeled Backlog, In Progress, In Review, In Production, and Done.
2. WHEN an engineer begins work on a Project_Board card, THE engineer SHALL create a Feature_Branch from `main` named `feature/<short-description>`, where `<short-description>` consists only of lowercase letters, digits, and hyphens and is between 3 and 50 characters in length.
3. WHEN a Feature_Branch is opened as a pull request targeting `main`, THE Build_Pipeline SHALL run all checks defined by the Production_Gate against the Feature_Branch and SHALL report a pass or fail result on the pull request.
4. WHEN a Feature_Branch is successfully deployed to production, THE engineer SHALL merge the Feature_Branch into `main`.
5. THE Plan_Document SHALL list the current set of Project_Board feature cards by title and identifier, and SHALL be updated within 1 business day of any card being added, removed, or moved between columns.
6. WHEN a Feature_Branch is created for a Project_Board card, THE engineer SHALL move the corresponding card from Backlog to In Progress.
7. IF any Production_Gate check fails on a Feature_Branch pull request, THEN THE Build_Pipeline SHALL block the pull request from being merged into `main` until all failing checks pass.
8. WHEN a Feature_Branch has been merged into `main`, THE engineer SHALL move the corresponding Project_Board card to Done within 1 business day.

### Requirement 14: Phased Delivery

**User Story:** As the site owner, I want the system delivered in phases, so that early value is shipped and risk is managed.

#### Acceptance Criteria

1. THE Plan_Document SHALL define exactly three delivery phases named Phase 1 Minimum Viable Product, Phase 2 Hardening and Cost Optimization, and Phase 3 Optional Enhancements, with each phase containing a written entry condition and a written exit condition expressed as a checklist of measurable items.
2. THE Plan_Document SHALL assign every requirement identifier from the requirements document to exactly one of the three phases such that no identifier appears in more than one phase and no identifier is left unassigned.
3. WHILE a phase is in progress, THE Build_Pipeline SHALL allow deployment of features whose requirement identifiers belong to that phase or any earlier phase, and SHALL reject deployment of features whose requirement identifiers belong to later phases with an error indicating phase scope violation.
4. WHEN every exit-condition checklist item for a phase is marked complete in the Plan_Document, THE engineer SHALL publish a phase summary within 5 business days that lists every requirement identifier delivered in the phase, the observed monthly running cost in United States dollars, and at least three lessons learned.

### Requirement 15: AWS Manual Setup Identification

**User Story:** As the site owner, I want a clear list of AWS actions that must be done manually, so that I know exactly what to do outside of code.

#### Acceptance Criteria

1. THE Plan_Document SHALL contain a section titled "AWS Manual Setup" listing every action that cannot be performed by the Build_Pipeline, where each item includes a unique identifier, a description, a checkbox indicating completion status, and a field for the completion date in ISO 8601 format (YYYY-MM-DD).
2. THE "AWS Manual Setup" section SHALL include, at minimum, the following items: AWS account creation, billing alert configuration, requesting Bedrock model access, domain registration or delegation to Route 53, and creation of the deployment IAM user or role.
3. WHEN an engineer marks a manual setup item as complete in the Plan_Document, THE Plan_Document SHALL record the completion status as checked and SHALL record the completion date in ISO 8601 format (YYYY-MM-DD).
4. IF an engineer marks a manual setup item as complete without providing a completion date in ISO 8601 format (YYYY-MM-DD), THEN THE Plan_Document SHALL reject the change and SHALL display an error indicating that a valid completion date is required.
5. WHEN the Build_Pipeline starts, THE Build_Pipeline SHALL verify that every item in the "AWS Manual Setup" section is recorded as complete with a valid completion date.
6. IF any item in the "AWS Manual Setup" section is not recorded as complete or is missing a valid completion date when the Build_Pipeline starts, THEN THE Build_Pipeline SHALL terminate within 5 seconds of startup with a non-zero exit status and SHALL emit an error message identifying each missing or incomplete item by its unique identifier and description.

### Requirement 16: Observability and Cost Monitoring

**User Story:** As the site owner, I want runtime metrics and cost alerts, so that I can detect abuse or runaway spend early.

#### Acceptance Criteria

1. THE Joke_API SHALL emit, for every request, a structured log record containing request identifier (UUID v4), salted SHA-256 source IP hash, decision outcome (one of: accepted, moderation_rejected, rate_limited, error), Bedrock model identifier, Polly voice identifier, end-to-end latency in milliseconds (integer, 0 to 60000), and estimated request cost in USD (decimal, 0.000000 to 1.000000, six decimal places), within 2 seconds of request completion.
2. THE Web_App SHALL emit CloudWatch metrics at one-minute resolution for jokes generated per hour (integer count, 0 to 1,000,000), moderation rejections per hour (integer count, 0 to 1,000,000), and rate-limit rejections per hour (integer count, 0 to 1,000,000).
3. WHEN total daily AWS cost for the Web_App exceeds a site-owner-configurable threshold (USD, 1.00 to 10000.00, default 10.00) as measured by a CloudWatch alarm evaluating the daily cost metric every 5 minutes over a 1-period window, THE Web_App SHALL trigger the cost alarm and send an email notification to the configured site-owner email address within 5 minutes of threshold breach.
4. THE Web_App SHALL send cost-related email notifications to the site owner only when the CloudWatch cost alarm defined in criterion 3 transitions to the ALARM state, and SHALL include in the email subject line the literal token "[COST-ALERT]" and the breached threshold value.
5. IF the cost alarm email delivery fails, THEN THE Web_App SHALL retry delivery up to 3 times with 60-second intervals and record each delivery attempt outcome in the structured log.
6. WHERE operational conditions warrant notification, including moderation-failure spikes (more than 50 rejections in any 5-minute window), rate-limit spikes (more than 100 rejections in any 5-minute window), or Bedrock or Polly error spikes (more than 10 errors in any 5-minute window), THE Web_App SHALL emit those notifications through a notification channel separate from the cost alarm channel and SHALL include in the email subject line the literal token "[OPS-ALERT]" with no reference to cost.
7. THE Joke_API SHALL hash source IP addresses using SHA-256 with a server-side secret salt of at least 32 bytes before any logging or persistence, and SHALL never log, persist, or transmit raw IP addresses.
8. IF emission of a structured log record or CloudWatch metric fails, THEN THE Web_App SHALL continue processing the originating request without failing it and SHALL increment an internal observability-failure counter exposed as a CloudWatch metric.

### Requirement 17: Training Corpus Handling

**User Story:** As the site owner, I want my dad joke corpus used to shape style without raising privacy or compliance issues, so that jokes feel authentic.

#### Acceptance Criteria

1. WHEN the Joke_Generator constructs a Bedrock prompt, THE Joke_Generator SHALL include between 3 and 10 textual examples derived from the Training_Corpus as few-shot examples, each example SHALL be at most 500 characters in length, and the combined few-shot section SHALL not exceed 5000 characters.
2. THE Training_Corpus SHALL be stored in a private S3 bucket configured to block all public access, and THE Frontend SHALL not expose, link to, or return any Training_Corpus file contents or signed URLs to end users.
3. IF a request from the Frontend attempts to retrieve a raw Training_Corpus file, THEN THE Web_App SHALL reject the request with an authorization error response and SHALL not return the file contents.
4. WHERE the Training_Corpus contains video or image files, THE Web_App SHALL extract only text or caption content from those files for prompt use, SHALL truncate each extracted text item to at most 500 characters, and SHALL not transmit raw video or image bytes to Bedrock.
5. IF text or caption extraction from a Training_Corpus video or image file fails, THEN THE Web_App SHALL skip that file, exclude it from the few-shot example pool, and record an extraction-failure indication in the Plan_Document or processing log.
6. THE Plan_Document SHALL record, for the Training_Corpus, the source location, the date of acquisition, the owner or licensor, and a written confirmation that the site owner has rights to use the contents as style reference and few-shot examples.
7. IF the Plan_Document does not contain a rights confirmation for the Training_Corpus, THEN THE Web_App SHALL not send Training_Corpus-derived content to Bedrock as few-shot examples.
8. WHERE a Phase 3 evaluation of Bedrock fine-tuning or knowledge bases is approved, THE Cost_Report SHALL be updated within 1 business day of the approval to include estimated fine-tuning costs, and no fine-tuning work SHALL begin until the updated Cost_Report is recorded in the Plan_Document.

### Requirement 18: Joke Output Round-Trip and Logging Integrity

**User Story:** As an engineer, I want generated jokes and their audio references stored consistently, so that we can replay or audit any joke shown to a visitor.

#### Acceptance Criteria

1. WHEN the Joke_API returns a joke to a visitor, THE Joke_Store SHALL persist a record containing a unique generation identifier (UUID v4), joke text (1 to 2000 characters), audio reference (URI string up to 2048 characters), model identifier (string up to 128 characters), voice identifier (string up to 128 characters), and creation timestamp (ISO 8601 UTC) within 2 seconds of returning the joke to the visitor.
2. WHEN a generation identifier is submitted to the Joke_Store for retrieval, THE Joke_Store SHALL return a record whose joke text and audio reference are byte-for-byte identical to the values originally returned to the visitor.
3. IF a generation identifier submitted for retrieval does not exist in the Joke_Store, THEN THE Joke_Store SHALL return a not-found response indicating the identifier was not located, without modifying any stored records.
4. THE Joke_Store SHALL retain every persisted record for a minimum of 30 days from its creation timestamp and SHALL permanently delete records whose creation timestamp is older than 90 days within 24 hours of crossing that threshold.
5. IF persistence to the Joke_Store fails for any reason, THEN THE Joke_API SHALL still return the joke and audio reference to the visitor within the standard response time, SHALL record a persistence-failure log entry containing the generation identifier, failure reason, and timestamp, and SHALL NOT expose the persistence failure in the visitor-facing response.
6. IF the joke text exceeds 2000 characters or the audio reference exceeds 2048 characters at persistence time, THEN THE Joke_Store SHALL reject the record, SHALL log a validation-failure entry identifying the offending field and the generation identifier, and SHALL leave existing records unchanged.
