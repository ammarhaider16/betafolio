# Betafolio UI guidance

Apply these decisions to new screens as well as existing ones. The multi-user architecture does not reset the product's visual or interaction design. UI-only agents must ask for permission before changing non-UI functionality, business rules, API contracts, or persistence; do not assume the backend has stayed unchanged.

## Visual system

- Use DM Sans throughout the dashboard and HTML briefings, with a sans-serif fallback. Load Google Fonts with `display=swap`.
- Palette: background `#F5F7FC`, surface `#FFFFFF`, text `#14213D`, muted `#64748B`, brand `#2563EB`, positive `#15803D`, negative `#DC2626`, border `#E2E8F0`.
- Status and saved-key labels use blue or neutral surfaces. Green is for meaningful positive financial changes, not decorative labels.
- Keep the wordmark plain. The application icon is a simple white B on blue. The GitHub social preview is the same simple B treatment at 1280×640.
- Use useful icons, aligned numeric columns, consistent select chevrons, and native keyboard-accessible disclosures. Use structured rows, badges, and expandable details for dense information; `|` is only for short secondary facts, not walls of text.
- Support desktop, tablet, and mobile without page overflow. Contain long messages, email addresses, and instrument names.

## Portfolio

- Show compact portfolio-value and cash cards; portfolio value includes cash. An empty portfolio shows $0.00, not Unavailable. Keep genuinely missing quotes unavailable. Omit holdings-priced counts and cash-percentage captions; keep the empty-state instruction concise. Identify holdings omitted because prices are unavailable only where material.
- Place the latest briefing beneath these cards, collapsed on load and after research completes. Explicit report actions expand the intended report. Polling must preserve user choices.
- Stack Allocation and Holdings & cash in one column, with Research alongside on wider screens. Avoid stretching cards or leaving a gap beneath Allocation.
- Saved holdings are read-only. Edit mode exposes ticker, quantity, and a separate cash input; Save and Discard return to read-only mode. Cash has ordinary table-sized, regular-weight text; spacing distinguishes it.
- Keep sample holdings out of the app; supply the [sample CSV](sample-portfolio.csv) in repository documentation instead.
- Imports support CSV, XLSX, and XLS through an accessible drop zone. Use the simple description Required columns: `ticker`, `quantity`. Required columns are ticker and quantity; uploaded prices are ignored. Never infer missing quantities. Imports remain editable drafts until saved.
- Instrument metadata and available prices are looked up automatically for imports and changed tickers. Metadata is read-only; there is no Fill missing details button. Failures do not block saving valid quantities and cash.
- Debounce and batch lookups, and reject stale results after ticker changes, removal, discard, or account changes. Never silently save a draft. Dashboard polling reads cached data and preserves edits.
- Prices come from the system, with accurate observation dates. A previous close is not today's observation. Add holding goes at the bottom with a plus icon; removal uses a delete icon.
- Brief/Detailed belongs in Research, defaults to Detailed, and is remembered for scheduled research. Failed saves offer retry and prevent running with an ambiguous detail choice.
- Keep the optional focus textarea. Do not restore the investment-profile form or adjustment-ideas checkbox. Preserve backend profile data and APIs.
- Research date defaults to today and rejects future dates. Do not restore the removed historical-date helper text or add methodological commentary to the briefing.

## Navigation and new account/admin screens

- User navigation is Portfolio, Activity, and Account. Research history stays inside Portfolio; there is no Preferences or separate Reports/Runs page.
- First-time authenticator setup shows its QR automatically after sign-in and reuses the pending setup across refreshes. Regeneration is explicit and warns users to replace the old authenticator entry. Replacing an enrolled authenticator requires password and existing MFA confirmation.
- Account holds timezone, optional schedule/delivery consent, and security. Account verification and administrator MFA remain required where the multi-user architecture requires them; optional briefing delivery is a separate choice.
- Administrators manage shared connections, sender email, scheduling policy, and user usage. The approved admin deletion action belongs inside each ordinary user's expanded Account & usage details, with an account-specific confirmation; keep suspension and allowance controls outside. Do not expose administrator credentials or diagnostics to regular users.
- Name providers directly: DDGS web search, Tavily, Exa, Perplexity, and Custom OpenAI-compatible endpoint. Show only credentials relevant to the selected provider. Each paid search provider works independently.
- Saved secrets show saved status and time, with Change, Cancel, and Remove controls; their values remain hidden. Do not represent saved keys as empty password inputs.
- Keep one connection check covering model connection, tools, and briefing output on every check invocation; stop further checks when the model connection fails. Reuse unchanged successful email/source checks, retry failures and changed settings, and offer an explicit Retest action without a cached-status label. Keep technical limits and usage counters in advanced/admin disclosures.
- Connection checks use accessible icons and recognizable provider labels, with explanations available in expandable details. Users & usage and research history use structured rows; keep their information and existing controls intact.
- Notices auto-dismiss after six seconds (ten for errors), pause while hovered or focused, and align their close control with the text.
- Present one agent-trace row per action by pairing stored start/completion events in the renderer. Show only the step name: no state icons, queries, timing, or diagnostic disclosures in trace rows. Keep the underlying logging unchanged.
- Use one inline research-progress presentation with a shortcut to the intended agent trace. Successful completion removes stale progress banners. Detailed observed activity belongs in Activity; never show private model reasoning.

## Signup and footer

- Signup requests ask for email only. Admins approve requests in Users & usage; approval sends a single-use account-creation link. Users verify their email and choose a password together, after approval. Show invitation delivery and expiry information to admins and allow sending a replacement link.
- The footer on signup and user pages reads exactly: "Betafolio does not offer financial advice, and cannot make trading decisions on your behalf."

## Briefings and delivery

- Preserve agent-controlled structure, useful emphasis, lists, and tables. Reports describe portfolio implications, not provider performance, search failures, methodology, or software activity.
- Keep operational diagnostics in Connections/Activity. Research date and execution time are distinct. Do not restore meaningless placeholder summaries.
- Scheduled research uses the latest saved portfolio and saved briefing detail; historical on-demand dates do not carry into scheduling.

Before committing UI work, review these decisions against the final diff. Follow the session's current verification constraints; static checks do not establish browser or database correctness. Do not commit or amend without approval.

Access requests have a dedicated admin-only page, separate from Users & usage. Use a compact table with one row per request. Pending requests offer Approve and Deny; denied rows show no approval or resend control; denial must never queue or send email. Keep denied decisions visible to administrators.
