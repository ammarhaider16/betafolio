# Focused regression checks

The suite covers arithmetic and date boundaries, prepared source transport, provider wire contracts, safe report rendering, imports and the autonomous research tool loop. Single-owner browser/keyring/SQLite/scheduler tests were removed with those retired features rather than preserved as compatibility requirements.

`test_production.py` adds tenant/admin separation, encrypted credentials, account/session deletion, concurrent job admission/capacity, live weekend scheduling and import-column ambiguity. Its database cases are opt-in and require **BETAFOLIO_TEST_DATABASE_URL** pointing to a disposable Postgres database whose name ends in `_test`. Each case uses a generated schema and removes that schema afterward. Never point it at production.

When explicitly choosing to run checks:

```sh
uv sync --frozen --group dev --python 3.12
uv run pytest -q
```

Without the dedicated Postgres URL, database tests skip; that does not establish tenant or queue correctness. CI includes a separate Linux/Postgres job for those cases. No paid provider calls are required by the suite.

For a manual browser acceptance pass, cover administrator bootstrap/MFA, connection setup, verified signup, editable import, research/status/report/activity, consent changes, account deletion and admin metrics without portfolio contents. Verify startup, email delivery, DST handling and a database restore in your intended deployment environment.

During this migration the user requested **no application startup or test execution**. These scenarios were written but not executed by the implementation agent; only source parsing, JavaScript syntax checking and lint/static review were performed. Security-review working notes belong under ignored `.tools/`, never tracked review documents.

For the restored multi-user UI, when runtime verification is permitted, also cover:

- Import and ticker-change lookups preserve edited quantities, ignore stale responses after discard/removal/account changes, and batch more than 30 holdings. Lookup failure still permits saving valid holdings and cash.
- Saved holdings return to read-only after save/discard; polling preserves drafts, and research cannot start while editing or while briefing detail is unsaved.
- Brief/Detailed persists without changing schedule/email consent or stored profile data; save failure offers retry and blocks research.
- Latest briefing starts collapsed, stays collapsed after completion, expands through report actions, and opens the selected run from Activity. Polling preserves disclosures and trace selection.
- Administrator keys show saved state and provider-specific Change/Cancel/Remove controls; saving a key preserves unsaved provider settings. Connection checks remain model-first.
- Account, MFA, administration, portfolio tables, and long trace messages fit desktop/tablet/mobile layouts; disclosures work by keyboard, and account changes clear private content.
