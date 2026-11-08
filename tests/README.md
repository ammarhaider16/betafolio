# Test scope

Keep tests for failures that change a user decision, lose portfolio edits, expose secrets, break provider interoperability, misdate returns or duplicate delivery. Use the narrowest meaningful boundary.

- Arithmetic and pricing: exact quantities/cash, finite prices, real quote timestamps and session-calendar behavior. Retrieval time must never masquerade as observation time.
- Model integration: the actual ChatOpenAI SDK sends a tool request, receives its result and finalizes the current report schema. Readiness uses that same schema. Mocking only the narrative function cannot establish provider compatibility.
- News integration: mocked HTTP checks exercise real adapter payloads, extraction, publication dates and failures. Runtime checks prove prepared provider content avoids page refetches, while explicit reads and metadata-only retrieval still work. These establish adapter contracts, not live provider availability or news quality.
- Portfolio and operations: editable imports, save boundaries, latest saved positions at schedule time, cancellation, optional delivery and idempotency.
- Security: authorization/CSRF, safe HTML, source redirects and private-address blocking, credential isolation and email recipient restrictions.
- Browser smoke: core editing/report behavior and small-screen layout; no snapshots of generated prose.

Removed suites enforced obsolete report templates, event grades, fixed search queues, publisher ranking and formatting internals. They were protecting constraints the product no longer wants. Do not replace them with assertions about exact analyst headings, prose length or call counts. Live configured-provider checks are manual workspace probes, not credential-dependent unit tests.

The targeted cleanup removes duplicate invalid-quantity cases and folds blank-price imports into the price-ignore contract. A purported partial-feed-failure test only exercised success and duplicated parsing coverage; it was removed. The independent-probes test now combines a stalled source with a healthy one and requires the healthy source to run before the stalled source exits. SDK round-trip and redirect tests assert behavior rather than incidental model/DNS call counts. Invalid market-data fallback must prove refresh was attempted, so simply skipping it cannot pass.

Keep lower-level credential/transport tests and HTTP authorization/persistence tests when they protect distinct boundaries. Keep arithmetic, quote-session freshness, cancellation, saved drafts, scheduling/delivery idempotency and security checks. Do not delete safeguards to reach a test-count target or merge unrelated scenarios into one opaque test.
