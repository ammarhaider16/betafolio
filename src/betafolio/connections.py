"""Global readiness probes contain synthetic public inputs only."""
import asyncio
import hashlib
import json
from .production import settings
from .service import ConnectionService, probe_model_readiness, build_research, SEARCH_PROVIDERS, MODEL_PROVIDER_SLOTS
from .database import now


def revision(service, kind):
    cfg = settings(service.store)
    if kind == 'model':
        values = [cfg.research.model.model_dump(),
                  service.secrets.get(MODEL_PROVIDER_SLOTS[cfg.research.model.provider])]
    elif kind == 'search':
        values = [cfg.research.search_provider, cfg.research.primary_feeds_enabled,
                  cfg.research.ticker_news_enabled, cfg.research.news_feed_sources if cfg.research.ticker_news_enabled else [],
                  cfg.research.max_data_age_hours, cfg.research.sec_company_filings_enabled,
                  cfg.research.sec_contact if cfg.research.sec_company_filings_enabled or cfg.research.primary_feeds_enabled else '',
                  service.secrets.get(SEARCH_PROVIDERS[cfg.research.search_provider][0])
                  if cfg.research.search_provider in SEARCH_PROVIDERS else None]
    else:
        email = cfg.email
        values = [email.transport, email.sender, email.sender_name]
        if email.transport == 'resend':
            values.append(service.secrets.get('email_api_key'))
        else:
            values.extend([email.smtp_host, email.smtp_port, email.smtp_username, email.smtp_tls,
                           service.secrets.get('smtp_password')])
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def connection_status(database):
    service = ConnectionService(database)
    outcomes = database.get('readiness', {})
    for kind, outcome in list(outcomes.items()):
        if kind in {'model', 'search', 'email'} and outcome.get('revision') != revision(service, kind):
            outcomes[kind] = {**outcome, 'ok':False,
                              'message':'Connection settings changed. Check the saved connections.'}
            # Old successful substeps must not appear current after a configuration change.
            outcomes[kind].pop('steps', None)
            outcomes[kind].pop('sources', None)
    return outcomes


async def check_all(database, recipient, *, force=()):
    from .mailer import send_mail
    from .providers import probe_research_sources
    service = ConnectionService(database)
    captured = {kind: revision(service, kind) for kind in ('model','search','email')}
    saved = database.get('readiness', {})
    outcomes = {}
    outcomes['model'] = await probe_model_readiness(service.settings, service.secrets)
    outcomes['model'].update(tested_at=now().isoformat(), revision=captured['model'])
    for kind in ('search', 'email'):
        previous = saved.get(kind, {})
        unchanged = previous.get('revision') == captured[kind]
        failed_sources = any(item.get('status') == 'failed' for item in previous.get('sources', {}).values())
        if kind not in force and previous.get('ok') and unchanged and not failed_sources:
            outcomes[kind] = dict(previous)
            continue
        if not outcomes['model']['ok']:
            # Preserve prior results and their real check times; a skipped probe is not a test.
            outcomes[kind] = dict(previous) if previous else {
                'ok':False, 'message':'Not checked because the model connection failed.'}
            continue
        if kind == 'search':
            try:
                reused = previous.get('sources', {}) if unchanged and kind not in force else {}
                skip = {key for key, value in reused.items() if value.get('status') in {'passed', 'empty'}}
                async with asyncio.timeout(45):
                    sources = {**reused, **await probe_research_sources(
                        build_research(service.settings, service.secrets), symbol='AAPL', name='Apple', skip=skip)}
                outcome = {'ok': any(v.get('status') in {'passed','empty'} for v in sources.values()), 'sources': sources}
            except Exception:
                outcome = {'ok':False, 'message':'Search check failed; review the selected source and credentials.'}
        else:
            try:
                await send_mail(database, recipient, 'Betafolio connection test',
                                'This test email was requested from your Betafolio administrator console.')
                outcome = {'ok':True, 'message':'Test email accepted; confirm receipt.'}
            except Exception:
                outcome = {'ok':False, 'message':'Email check failed; verify sender, credential, and domain.'}
        outcome.update(tested_at=now().isoformat(), revision=captured[kind])
        outcomes[kind] = outcome
    for kind, outcome in outcomes.items():
        if outcome.get('revision') != revision(service, kind) and outcome.get('ok'):
            outcome.update(ok=False, message='Settings changed; check this connection again.')
    database.set('readiness', outcomes)
    return connection_status(database)
