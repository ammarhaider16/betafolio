# Betafolio architecture

## Processes and ownership

FastAPI serves authenticated user/admin APIs and a static browser client. A separate Python worker executes research, refreshes prices, schedules daily runs and delivers email. Postgres stores accounts, private portfolios/preferences, revocable sessions, one-time account tokens, encrypted service credentials, jobs and audit metadata. Alembic owns the schema; there is no SQLite compatibility layer or data migration.

All web portfolio/report/job access derives its user ID from a database-backed session. Admins are denied user workspace endpoints, even if they know a run ID. Admin usage APIs select an explicit metadata allowlist and never serialize private account or job records. The operator and trusted worker still have database access; this is not end-to-end encryption against infrastructure operators.

Signup is administrator-approved. Public email-only requests are stored separately from accounts. Approval queues an encrypted invitation token; only its hash and 24-hour expiry are kept on the request. Completing the single-use link with a password atomically verifies the email and creates the account. Reissuing a link invalidates the previous token. Opening a link does not consume it. Existing accounts remain unchanged. Passwords use Argon2; MFA is TOTP with encrypted enrollment secrets, replay protection and hashed single-use recovery codes. Admin MFA is required. First enrollment uses the password-authenticated session to show a stable pending QR automatically; only explicit regeneration rotates it. Confirmation is bound to the displayed setup ID. Setup and sign-in share a one-step clock tolerance and store the matched counter to prevent replay. Enrolled-authenticator replacement still requires password and existing MFA. Session cookies are HttpOnly, SameSite=Strict and Secure in production. Writes require session-bound CSRF tokens. Verification/reset tokens are hashed in the token table; the pending email outbox stores an encrypted copy until delivery.

## Configuration and secrets

ProductionSettings contains shared research settings, email transport, daily scheduling and usage allowances. User preferences hold report detail, investment profile, timezone and consent. Users cannot change model endpoints, provider keys, network destinations, request budgets or email senders.

Production research always constructs the configured model; there is no model-enable toggle. Retired saved toggle values are discarded. Missing models, initialization errors and empty agent output fail the run, with setup and execution events available in the owner's Activity trace. Failed runs cannot be served or emailed as briefings. Model-free arithmetic summaries require explicit `EngineDependencies(offline=True)`, selected by the CLI's offline/demo options.

Fernet encrypts provider credentials and MFA secrets using a stable operator-supplied key. Local configuration generates that key automatically. The admin UI returns credential presence and timestamps only. Connection revisions bind readiness to the selected settings and decrypted credential values through hashes, not plaintext records. Readiness probes use synthetic public inputs and never inspect a user's portfolio.

## Queue and scheduling

Admissions lock the user row to serialize one-active-run and daily-allowance reservations. Claims lock a global queue row, then use row locks to enforce global research concurrency. Account and signup-approval emails share a separate single delivery slot and priority over research so verification/reset links do not wait behind a long research queue. Jobs retain ownership; no process-global current user exists.

The worker heartbeats active jobs, propagates cancellation/suspension/deletion, and marks abandoned work incomplete after its lease expires. Interrupted paid research is not automatically repeated. Queued jobs persist across restarts. Email delivery is claimed before sending; uncertain sends are not blindly retried. Resend requests use job-specific idempotency keys; SMTP cannot promise exactly-once delivery.

The shared daily schedule uses a configured wall-clock time and IANA timezone, including weekends. A user/date unique key prevents duplicate scheduling across repeated ticks or DST transitions. Only today's due occurrence is queued; old queued schedules expire. Consent is rechecked before execution and email. Scheduled AnalysisRequest objects never inherit research_as_of, reporting_cutoff or an earlier user's question. They acquire a fresh research clock at execution. Historical dates apply only to on-demand runs.

## Research engine

AnalysisRequest and AnalysisResult remain the standalone engine contracts. EngineDependencies supplies model, research and market-data adapters. The reusable research_portfolio tool can be embedded as a subagent; it does not trade, change service settings or send email itself.

The analyst controls its research order and report headings. Tools are search_news, read_article and get_market_data. The selected search provider can operate without feeds. Provider-prepared text bypasses redundant retrieval; metadata-only articles use bounded public HTTPS retrieval. Guarded transport validates DNS/IPs, pins connections and revalidates redirects, blocking private/reserved destinations. External content is reference data, never application instructions.

Application-calculated financial facts distinguish share prices, holding values and valuation contributions, with explicit dollar/percentage scale and formatted amounts. The analyst receives readable labels. Exact known internal identifiers trigger one tool-disabled editorial rewrite; contaminated drafts are withheld if cleanup fails. This checks presentation, not factual truth.

Live quote selection compares regular, premarket, after-hours and stale-summary intraday observations without a US-calendar gate. Only public quote/identity information may be cached across users; quantities, profiles, questions, results and research context are private. Research valuation/session-return calculations remain separate from quote retrieval. Historical market-data adapters enforce the selected cutoff and do not mutate saved live prices.

## Presentation and operations

The user UI follows [the current UI guidance](ui-guidance.md): read-only saved holdings with an editable draft, allocation, research and report history in Portfolio, detailed traces in Activity, and schedule/delivery/security in Account. The admin UI provides connections, email, schedule/allowances, service health and usage. Markdown reports preserve list layout in HTML/plain text and escape raw HTML. Source IDs, URLs, technical field names and model counters do not belong in briefings.

Account deletion cascades to sessions, account tokens and owned jobs/results. Retention maintenance removes finished jobs and audits after the configured window, normally 90 days. Backups and encryption-key recovery are operator responsibilities. Web access logging is disabled; application errors use safe categories rather than provider payloads. Health endpoints expose only service availability; detailed synthetic checks require administrator MFA.

Docker Compose runs the same architecture locally. Railway IaC declares the hosted resources without a separate Redis or frontend framework. No cloud deployment is performed automatically by the repository. See deployment.md for the exact setup, backups and current runtime-verification limitations.

Connection checks always probe the model and reuse successful unchanged source/email checks by configuration fingerprint. Failed source adapters are retried without repeating successful adapters; explicit Retest bypasses reuse. Only active transport credentials affect the email fingerprint. Skipped probes preserve their actual check times. Research readiness depends on model and search; email readiness separately gates invitations and briefing delivery. Research requested with unavailable email still completes in the dashboard, with delivery marked unavailable.
