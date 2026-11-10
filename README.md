# Betafolio

Betafolio is a portfolio research agent with a browser command center for holdings, briefings, research history, and agent activity. The analyst uses news and market-data tools to explain developments affecting your positions. Administrators manage the shared connections; users only enter their portfolio and preferences.

Run the same application locally or on a cloud host. The web service, background worker, and Postgres database are separate processes. This is a fresh multi-user version: previous single-owner settings and SQLite data are not imported.

## Quickstart: local deployment

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Docker Desktop](https://www.docker.com/products/docker-desktop/). Start Docker Desktop with Linux containers enabled. From the repository root:

```sh
uv run --python 3.12 python scripts/configure.py
docker compose up --build -d
```

The configuration script asks for an administrator email and generates unique database, encryption, and initial administrator credentials in an ignored `.env` file. Save the displayed password. It does not start services or overwrite an existing `.env`.

Open **http://127.0.0.1:8000** using that exact address. The first startup installs the database schema and creates the first administrator. Sign in using the generated credentials, scan the authenticator QR code, and save the recovery codes.

Complete setup in the admin view:

1. **Connections:** select a tool-calling model, enter its name and API URL, enable it, and save the matching provider key. Select DDGS, Tavily, Exa, or Perplexity; save a search key if required. Feeds are optional.
2. **Email:** enter a verified sender and its delivery credential. Resend's HTTPS API is recommended; verify your domain in Resend first. SMTP with TLS is also supported for self-hosted installations.
3. **Test saved connections:** the model is checked on every connection-check invocation. Sources and email are checked when untested, failed, changed, or explicitly retested; the test email goes to your signed-in administrator address. Resend needs sending enabled, not inbound receiving. A successful unchanged email check is reused without sending another test message.
4. **Schedule & limits:** optionally enable a daily briefing time and timezone, then enable access requests and save.

Administrators do not have a portfolio workspace. To use Betafolio yourself, sign out and request access with a separate user email address. In the admin console, open **Access requests** and approve that address. Administrators can also deny pending requests; denial records the decision without queuing or sending any email. The recipient of an approval receives a single-use link, valid for 24 hours, to verify their email and choose a password. Only then is the account created. The admin can send a new link if delivery fails or the link expires; this invalidates the previous link. Users can then add holdings or import the [sample CSV](docs/sample-portfolio.csv), without configuring provider keys or sender email.

The worker must remain running for research, verification emails, price refresh, and scheduling. A local computer must remain awake for unattended work. Use `docker compose stop` to stop services; `docker compose up -d` resumes them. Do not delete database volumes unless you intend to erase the installation.

## Using your workspace

**Portfolio:** enter tickers, quantities, and cash. [Download a sample CSV](docs/sample-portfolio.csv) with fictional quantities; its `CASH` row is a monetary balance. Required columns: `ticker`, `quantity`. CSV, XLSX, and XLS imports open an editable draft. Common brokerage headers such as `Security Symbol`, `Qty`, and `Number of Shares` are recognized. Ambiguous files ask you to choose columns. Prices in uploads are ignored; quantities are never guessed from price or value fields. Excel imports use the first visible worksheet. Review and save the draft before research.

**Prices:** the worker refreshes saved prices every 60 seconds by default, including weekends. It selects the newest timestamped regular or extended-session observation, with intraday and closing-data fallbacks. Provider availability and delay still apply. Hover a price to inspect its observation time. Instrument details and available prices are looked up automatically when importing or editing tickers. Draft lookups never save changes or infer quantities.

**Research:** choose Brief or Detailed (Detailed by default), optionally ask a question, and select Run research. The detail choice is saved for future and scheduled briefings; save or discard portfolio edits first. The latest briefing starts collapsed beneath the value cards, and research history stays in Portfolio. The default allowance is three on-demand runs per user per day, plus scheduled research. Administrators can adjust allowances. Only one research job per user can be queued or running at once. Activity shows observed tool calls and outcomes, not private model reasoning.

**Research date:** today uses live research. Earlier dates use your saved quantities and cash with historical prices and date-filtered news. This is best-effort historical research, not a hindsight-free backtest: source revisions and model knowledge may contain later information. Saved live prices remain unchanged.

**Account:** set your timezone, opt into the shared daily schedule, and separately allow briefing emails. Account security remains here; briefing detail is configured directly in Portfolio. Email goes only to your verified account address. Scheduled research runs every calendar day, including weekends, from the perspective of its actual execution day. Missed previous days are skipped, not replayed. Historical on-demand dates never carry into scheduled jobs.

An optional authenticator is available for users. Password reset does not remove MFA. Deleting an account removes its private data and revokes sessions; copies in operator backups expire according to the backup policy.

## Administrator visibility

Admins see account/verification status, last activity, scheduling consent, run outcomes, model/search call counts, average queue/run duration, email delivery outcomes, and allowances. Token counts appear when the model provider reports them. Cost estimates appear only when the operator supplies model pricing; they are not provider invoices. Counts cover retained jobs, normally 90 days.

Administrators can suspend accounts or permanently delete ordinary user accounts from **Users & usage > Account & usage details**. Deletion removes the account, portfolio, sessions, tokens, and retained jobs/reports, and records the administrator action. Administrator accounts cannot be deleted through this control. Operator backup retention still applies.

Admin APIs do **not** expose holdings, quantities, cash, portfolio values, questions, investment profiles, reports, research queries, or traces. There is no impersonation feature. This is an application access boundary: infrastructure/database operators technically retain access to stored information, and model/research providers receive the inputs needed for research.

Saved service credentials are encrypted and shown as saved rather than blank. Updating a model, source, or sender invalidates the corresponding connection check. Keep the encryption key stable and back it up separately from the database; changing it without re-encrypting stored credentials makes those credentials and MFA enrollments unreadable.

If the administrator loses all authenticator and recovery codes, an operator with container access can run:

```sh
docker compose exec web /app/.venv/bin/betafolio recover-admin --email admin@example.com
```

This explicitly revokes that administrator's MFA and sessions. Sign in with the account password and enroll a new authenticator. It does not grant access to user portfolios.

## Hosted deployment

See [deployment instructions](docs/deployment.md) for Railway, a generic container host, HTTPS setup, backups, and the environment-variable checklist. Railway is the recommended small hosted deployment; the repository includes `.railway/railway.ts` for its current Infrastructure as Code workflow. The older Railway TOML deployment format is not used. [Railway IaC documentation](https://docs.railway.com/infrastructure-as-code).

An always-on web service, worker, persistent Postgres, paid research APIs, and email delivery may incur separate costs. Railway Hobby has a $5 minimum rather than a fixed total bill. Render's free web services sleep and are unsuitable for reliable unattended production scheduling; free tiers are useful for previews. [Railway pricing](https://railway.com/pricing), [Render free-service limitations](https://render.com/docs/free).

Railway Free/Trial/Hobby blocks outbound SMTP. Use Resend's HTTPS API there. [Railway email delivery](https://docs.railway.com/networking/outbound-networking).

## UI guidance

Use [the current UI guidance](docs/ui-guidance.md) when adding or changing screens, including account and administrator pages.

## Developer entry points

The core research engine remains independently callable and can be used as a subagent:

```python
from betafolio import AnalysisRequest, EngineDependencies, analyze_portfolio
from betafolio.agent import create_research_tool

request = AnalysisRequest.model_validate(your_request_dict)
result = await analyze_portfolio(request, EngineDependencies(...))
tool = create_research_tool(EngineDependencies(...))
```

`agent.py` and `prompts.py` control the analyst; `providers.py` and `research_runtime.py` provide its tools. `database.py`, `accounts.py`, `jobs.py`, and `web.py` contain hosted ownership, authentication, durable execution, and API boundaries. Browser assets are `production.html`, `production.js`, and `production.css`. [Architecture](docs/architecture.md) describes the data flow.

For an offline standalone demonstration, without Postgres or API configuration:

```sh
uv sync --frozen --python 3.12
uv run betafolio analyze --demo --offline --format html
uv run betafolio schema request
```

Online CLI research accepts `--request path/to/request.json` and uses the operator's database configuration. It does not bypass user authentication to read a web user's portfolio. Environment variables are not automatically loaded from `.env` by the Python CLI; Compose loads them, while native development requires exporting them explicitly.

To develop the web application natively, provide a Postgres `DATABASE_URL`, export the variables described in the deployment guide, then run `uv run betafolio serve` and `uv run betafolio worker` in separate terminals. Use a separate database for development. The serve and worker commands apply Alembic migrations with a database lock to prevent overlapping upgrades.

Focused regression scenarios are described in [tests/README.md](tests/README.md). Production-boundary tests require an explicitly disposable Postgres database. No app startup, integration tests, browser runs, or deployment were performed as part of this migration at the user's request; static syntax/lint review is separate from runtime verification.
