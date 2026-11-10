# Deployment and operator setup

## Local installation

The README quickstart configures a loopback-only Compose deployment: web + worker + Postgres. Windows and macOS require Docker Desktop using Linux containers. The configuration script creates `.env` once with strong unique credentials. Never commit or publish it.

Only the web port is published, bound to `127.0.0.1:8000`. Postgres has no public port. Web and worker run as a non-root user with a read-only root filesystem, a writable temporary directory, and a private data volume. Migrations finish before the local worker starts.

### Local database password mismatch

Postgres initializes its password only when its data volume is first created. Changing or regenerating `.env` does not change the password in an existing volume. The database healthcheck now checks password authentication as well as connectivity.

If startup reports password authentication failure, restore the original `.env` or synchronize the local database role to the current configured password. For the default local database/user, run:

```sh
docker compose up -d db
docker compose exec db psql -X -U betafolio -d betafolio
```

At the `psql` prompt, enter these commands. They read the password from the container environment without displaying it:

```sql
\getenv configured_password POSTGRES_PASSWORD
SELECT format('ALTER ROLE %I PASSWORD %L', current_user, :'configured_password') \gexec
\q
```

Then run `docker compose up --build -d`. Use your configured database/user names if you changed the defaults. This procedure preserves database contents; do not remove the volume to fix a password mismatch.

## Railway: three services

Use an always-on web service and worker, plus Postgres in the same project/environment. Only web needs a public domain. Both application services build the root Dockerfile; no frontend build is required.

Railway's current configuration system is Infrastructure as Code. Legacy TOML/JSON configuration is deprecated; existing services can still read it until December 1, 2026. The supplied `.railway/railway.ts` declares the database, service commands, private database references, and shared environment references. [Migration guidance](https://docs.railway.com/infrastructure-as-code), [current IaC reference](https://docs.railway.com/infrastructure-as-code/reference).

### 1. Prepare your computer and link the blank project

Run these commands **on your computer, from the repository root**, not inside the Oracle VM. Install Node.js 22+, npm, and [uv](https://docs.astral.sh/uv/getting-started/installation/) first. You do not need Docker locally for this Railway deployment.

```sh
npm install -g @railway/cli@latest
npm --prefix .railway install
railway login
railway link
railway status
```

In the login/link prompts, select your Railway workspace, your **existing blank project**, and its `production` environment. A service selection is unnecessary while the project is blank. Verify the project/environment in `railway status`. Do not run `railway init` to create another project, or `railway config init` / `railway config pull` to overwrite the supplied definition.

In Railway's account/workspace integrations, authorize its GitHub application to access the repository. The services build the code on GitHub, not unpushed local edits. Ensure the intended release is pushed before deploying.

Set the GitHub repository in the same terminal you will use for plan/apply. Replace `YOUR_OWNER/YOUR_REPOSITORY` with the actual repository, without `https://github.com/`:

**PowerShell:**

```powershell
$env:BETAFOLIO_REPOSITORY = 'YOUR_OWNER/YOUR_REPOSITORY'
```

**Git Bash, Linux, or macOS:**

```sh
export BETAFOLIO_REPOSITORY='YOUR_OWNER/YOUR_REPOSITORY'
```

This is a local operator setting, not an application secret or Railway shared variable. Set it again when opening a new terminal. The script uses `github(repository)` without an explicit branch; verify the selected source branch in both services after creation.

### 2. Supply four shared values manually

Choose your final hostname now. For Cloudflare DNS, a subdomain such as `betafolio.example.com` avoids needing a Railway-generated hostname before services exist. You can set its HTTPS origin before configuring DNS.

Generate the encryption key once on your computer:

```sh
uv sync --frozen
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the key in your password manager and a separate protected backup. In Railway, open **Project Settings > Shared Variables**, select the **same production environment**, and add these four entries. Paste values without surrounding quotes:

| Shared variable | Manual input | Used by |
| --- | --- | --- |
| `BETAFOLIO_ENCRYPTION_KEY` | The generated Fernet key, unchanged | Web and worker |
| `PORTFOLIO_PUBLIC_URL` | Exact HTTPS origin, e.g. `https://betafolio.example.com`, without a path or trailing slash | Web and worker |
| `BETAFOLIO_BOOTSTRAP_ADMIN_EMAIL` | Your real administrator email address | Web only |
| `BETAFOLIO_BOOTSTRAP_ADMIN_PASSWORD` | A unique generated password of at least 12 characters; save it | Web only |

Save/apply any staged variable changes before planning. The script creates the service references to these shared values; it does **not** create their values. Do not manually share the bootstrap password with the worker. Do not upload a local `.env`, put secrets in `.railway/railway.ts`, or run the local Compose configuration script for this setup. [Railway shared variables](https://docs.railway.com/variables).

### 3. Preview and apply the script

From the repository root, in the terminal containing `BETAFOLIO_REPOSITORY`, run:

```sh
railway config plan
```

For a blank project, expect services named `postgres`, `web`, and `worker`, with database storage and references. Check the selected project/environment and ensure there are no unexpected deletions. Then run:

```sh
railway config apply
```

Review the fresh plan and confirm interactively. This provisions billable resources. No deployment or billing action is performed merely by installing the SDK or running `plan`. If the script cannot find a shared variable or GitHub repository, fix the corresponding input above and plan again. [IaC commands](https://docs.railway.com/infrastructure-as-code).

The script configures:

| Item | Script-managed configuration |
| --- | --- |
| Postgres | Database provisioning and its database storage |
| GitHub source | Same repository for web and worker; Railway builds the root Dockerfile |
| Database connection | `DATABASE_URL` on both app services from `db.env.DATABASE_URL`; no password copying |
| Shared values | References to the four values above, with admin credentials on web only |
| Runtime | Production mode, `/tmp` home/cache paths, correct start commands |
| Deployment | One replica each for web and worker; web `/health` check with a 120-second timeout |

It does not configure domains, DNS, provider credentials, billing controls, or backup schedules. Do not create duplicate services manually or attach app volumes to web/worker. After applying, inspect both services' source settings and deployments: confirm the intended branch, root Dockerfile build, and successful startup. If builds have not started, trigger a deployment for each app service from its connected GitHub source.

### 4. Connect your Cloudflare hostname manually

1. Open **web > Settings > Networking > Public Networking** in Railway and add your custom domain, such as `betafolio.example.com`.
2. If asked for a target port, use the port the app listens on: Railway's injected `PORT`, or `8000` if none is injected. The start command already honors `PORT`.
3. In **Cloudflare > DNS**, create the exact CNAME and any verification records Railway displays. Use **DNS only** (grey cloud) initially; do not point an A record at the old Oracle VM. Keep existing Resend DNS records unchanged.
4. Wait until Railway reports the domain/certificate ready, then open the HTTPS URL. Confirm it exactly matches `PORTFOLIO_PUBLIC_URL`.

Only web needs public HTTP networking. Do not enable a public domain for worker or a public TCP proxy for Postgres just for app connectivity. Leave the app hostname DNS-only for this deployment; proxying it through Cloudflare would need a separate review of client-IP handling. [Railway domains](https://docs.railway.com/networking/domains/working-with-domains).

**Alternative: Railway-generated hostname.** If you do not have a custom hostname, manually create an **empty** service named `web` first, generate its Railway domain under Networking, and use that exact HTTPS origin in step 2. Then plan/apply the script. Check that the plan updates the existing `web` rather than creating a duplicate; the script will create the remaining services. This extra manual service is unnecessary when using a known Cloudflare hostname.

### 5. Complete setup and control spending

1. Sign in with the bootstrap admin credentials and enroll your authenticator/MFA. Bootstrap creates the first admin only when none exists; changing the bootstrap variables later is not a password-reset mechanism.
2. In the app's **Connections**, configure and save the model endpoint/name/key and selected research provider. Run the connection checks.
3. Configure your existing **Resend HTTPS API** key and verified sender in the app's email settings, save, and test delivery to your administrator address. These provider values are entered in the application, not as extra IaC shared variables. Email is required for this hosted access-approval flow, while briefing emails remain optional for users.
4. Confirm the worker heartbeat is current. Test access approval with a separate user address, then a small research run and report delivery if enabled. Administrators do not have a portfolio workspace.
5. Configure database backups and save the encryption key separately. The script does not schedule backups; see **Backups and recovery** below.
6. Set Railway usage alerts/budget controls and external-provider spending limits. Keep one production environment and one replica of each app service. Observe per-service RAM/CPU and projected spend over representative days before changing resource limits. Hobby's subscription is a minimum usage commitment, not a fixed total bill; hard usage limits may stop services. [Railway cost controls](https://docs.railway.com/pricing/cost-control).

For later releases, verify the GitHub deployment branch and deploy the new code to **both** web and worker. For infrastructure changes, repeat `railway config plan` and `railway config apply` with the local repository variable set. Keep the original shared encryption key and database; do not regenerate them during routine updates.

Never rotate the encryption key by simply replacing it. It protects provider keys, account-email tokens, authenticator secrets, and session-CSRF derivation. A future key rotation must re-encrypt stored values and revoke sessions.

### Runtime settings reference

| Setting | Web | Worker |
| --- | --- | --- |
| Builder | Root Dockerfile | Root Dockerfile |
| Start command | `/app/.venv/bin/betafolio serve --server --host 0.0.0.0` | `/app/.venv/bin/betafolio worker` |
| Replicas | 1 | 1 |
| Healthcheck | `/health` | None; inspect admin worker heartbeat |
| Public domain | Required | None |
| Persistent app volume | Not needed when encryption key is supplied | Not needed |

The web command honors Railway's `PORT`. Both processes apply migrations under a Postgres advisory lock, so simultaneous startup does not race schema creation. The first admin is created only if no admin exists; bootstrap never overwrites a password or promotes an existing user. No admin credentials are shipped in source.

`/health` checks database connectivity and is suitable for deployment startup. Railway checks it during deployment, not continuously after deployment. Railway's healthcheck hostname is accepted only when running in Railway. `/ready` additionally requires a recent worker heartbeat and passing saved connection checks; do not use it to block the initial admin setup. [Railway healthchecks](https://docs.railway.com/deployments/healthchecks).

Use Resend HTTPS delivery on Railway Hobby; SMTP requires Pro or above, and services must be redeployed after upgrading to enable SMTP access. Sender-domain verification remains an operator task, never a user task. [Outbound email restrictions](https://docs.railway.com/networking/outbound-networking).

Budget for all running services and external APIs. Set provider spending caps and hosting alerts before accepting access requests. Default application allowances help control per-account research, but do not replace administrator review of access requests. For a wider public launch, add an edge bot challenge/WAF if signup abuse appears. Do not use uptime pings to disguise a sleeping free tier as reliable scheduling.

## Other container hosts

Use the same image and Postgres database with the two start commands above. Set `PORTFOLIO_SERVER_MODE=1`, the public HTTPS origin, and a stable encryption key. Terminate TLS at a trusted reverse proxy and publish only web. The optional `compose.production.yaml` overlay enables production mode for a self-hosted Compose installation; put its loopback port behind the HTTPS proxy.

The application ignores forwarded IP headers on local and generic deployments. On Railway, for requests to the configured public hostname, it uses the platform's validated `X-Real-IP` value so all users do not share the proxy's rate-limit bucket. This relies on Railway's HTTPS edge overwriting that header: expose web only through that edge, never through a TCP proxy or an untrusted private-network peer. Other hosts need explicitly trusted ingress handling before changing this policy. Account-bound login/reset limits and authenticated research allowances apply separately. [Railway ingress headers](https://docs.railway.com/networking/public-networking/specs-and-limits).

## Backups and recovery

Enable daily encrypted database backups in the hosting account and document their expiration. Keep a separate protected copy of the encryption key. Application deletion cannot erase already-created backups; restore procedures must respect subsequent account deletions before reopening access.

For local Compose, use the binary-safe helper so Windows shell redirection cannot corrupt a dump:

```sh
uv run python scripts/backup.py betafolio-backup.dump
```

Restore only into the intended installation, with web and worker stopped:

```sh
docker compose stop web worker
uv run python scripts/backup.py --restore betafolio-backup.dump
docker compose up -d
```

The helper asks for an explicit restore confirmation and uses the database credentials already in the container. It does not start or stop services. Store dumps outside the repository with restricted access. On cloud hosts, use their supported snapshot/restore procedure against a separate recovery database before switching production over.

## Release boundary

No cloud project was provisioned and no application, browser flow, integration suite, or live research was run during this implementation, by request. Added regression scenarios are available for an operator to run when desired. Static review cannot establish runtime security or provider availability.

Before public use, confirm market-data permissions and coverage for your intended distribution. The Yahoo/yfinance adapter does not provide a guaranteed real-time service-level agreement. Replace it through the existing market-data interface if your production requirements require licensed or guaranteed data.
