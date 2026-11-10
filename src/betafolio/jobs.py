"""Database-backed queue, live daily scheduling, and isolated research execution."""
import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import delete, func, select
from .database import AppState, Audit, AuthToken, Job, RateBucket, Session, SignupRequest, User, now
from .models import AnalysisRequest, PortfolioSnapshot
from .production import email_ready, ready, settings


ACTIVE = ('queued', 'running')
ACCOUNT_MAIL = ('account_email', 'signup_email')


def enqueue(database, user_id, payload, *, schedule_key=None):
    cfg = settings(database)
    with database.transaction() as db:
        # The user row serializes admissions, reservations and portfolio changes.
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if not user or user.role != 'user' or user.suspended or not user.verified or not user.portfolio:
            raise ValueError('Verify your account and save holdings before running research')
        if schedule_key and db.scalar(select(Job.id).where(Job.user_id == user.id, Job.schedule_key == schedule_key)):
            return None
        if db.scalar(select(Job.id).where(Job.user_id == user.id, Job.kind == 'research', Job.status.in_(ACTIVE))):
            raise ValueError('Your research is already queued or running')
        day = now().astimezone(ZoneInfo(user.timezone)).date().isoformat()
        if schedule_key is None:
            count = db.scalar(select(func.count()).select_from(Job).where(
                Job.user_id == user.id, Job.allowance_day == day, Job.schedule_key.is_(None),
                (Job.consumed.is_(True) | Job.status.in_(ACTIVE))))
            allowance = user.allowance if user.allowance is not None else cfg.daily_allowance
            if count >= allowance:
                raise ValueError('You have used today\'s research allowance. Try again tomorrow.')
        job = Job(user_id=user.id, payload=payload, schedule_key=schedule_key, allowance_day=day)
        db.add(job)
        db.flush()
        return job.id


def claim(database, worker_id):
    cfg = settings(database)
    with database.transaction() as db:
        # One global row serializes capacity checks even with multiple workers.
        db.scalar(select(AppState).where(AppState.key == 'queue_lock').with_for_update())
        # Account verification/reset mail must not wait behind a full research queue.
        mail_running = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'running',Job.kind.in_(ACCOUNT_MAIL)))
        job = None
        if mail_running < 1:
            job = db.scalar(select(Job).where(Job.status == 'queued',Job.kind.in_(ACCOUNT_MAIL))
                            .order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if job:
            job.status, job.started_at, job.heartbeat, job.worker_id = 'running', now(), now(), worker_id
            return job.id
        if db.scalar(select(func.count()).select_from(Job).where(Job.status == 'running',Job.kind == 'research')) >= cfg.concurrency:
            return None
        job = db.scalar(select(Job).where(Job.status == 'queued',Job.kind == 'research').order_by(Job.created_at)
                        .with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.status, job.started_at, job.heartbeat, job.worker_id = 'running', now(), now(), worker_id
        return job.id


def schedule_tick(database, timestamp=None):
    timestamp = timestamp or now()
    cfg = settings(database)
    if not cfg.schedule.enabled or not ready(database):
        return
    local = timestamp.astimezone(ZoneInfo(cfg.schedule.timezone))
    if local.time().replace(tzinfo=None) < cfg.schedule.at:
        return
    key = f'{cfg.schedule.timezone}:{local.date().isoformat()}'
    with database.transaction() as db:
        users = list(db.scalars(select(User.id).where(User.role == 'user', User.schedule_opt_in.is_(True),
                          User.verified.is_(True), User.suspended.is_(False), User.portfolio.is_not(None))))
    for user_id in users:
        try:
            enqueue(database, user_id, {}, schedule_key=key)
        except ValueError:
            pass  # Active runs are reconsidered on the next tick, never duplicated.


async def execute(database, job_id):
    stage = 'run_setup'

    def activity(event, budget=None):
        nonlocal stage
        if event.get('status') == 'started':
            stage = event['tool']
        with database.transaction() as db:
            row = db.get(Job, job_id)
            if row:
                row.activity = [*row.activity[-499:], event]
                if budget is not None:
                    row.budget = dict(budget)

    try:
        with database.transaction() as db:
            initial = db.get(Job, job_id)
            kind = initial.kind if initial else None
        if kind == 'account_email':
            await account_email(database, job_id)
            return
        if kind == 'signup_email':
            await signup_email(database, job_id)
            return
        if kind is None:
            return
        activity({'tool': 'run_setup', 'status': 'started', 'at': now().isoformat()})
        await execute_research(database, job_id, activity)
    except asyncio.CancelledError:
        activity({'tool': stage, 'status': 'cancelled', 'at': now().isoformat()})
        raise
    except Exception:
        # Never persist exception payloads: they may contain credentials or portfolio content.
        reason = ('The research model could not be initialized. Ask the administrator to check the model connection.'
                  if stage == 'model_setup' else
                  'Research could not complete this stage. Review the connection settings and try again.')
        activity({'tool': stage, 'status': 'failed', 'reason': reason, 'at': now().isoformat()})
        with database.transaction() as db:
            row = db.get(Job, job_id)
            if row:
                row.status, row.finished_at, row.error_code = 'failed', now(), 'research_failed'


async def execute_research(database, job_id, activity):
    from .engine import analyze_portfolio
    from .service import build_dependencies
    from .research_clock import research_timestamp
    from .delivery import render_report
    from .mailer import send_mail
    cfg = settings(database)
    with database.transaction() as db:
        job = db.get(Job, job_id)
        user = db.get(User, job.user_id) if job else None
        if not job or not user:
            return
        if user.suspended or job.cancel_requested or (job.schedule_key and (not user.schedule_opt_in or not cfg.schedule.enabled)):
            job.status, job.finished_at, job.error_code = 'cancelled', now(), 'cancelled_before_start'
            return
        if job.schedule_key and not job.schedule_key.endswith(now().astimezone(ZoneInfo(cfg.schedule.timezone)).date().isoformat()):
            job.status, job.finished_at, job.error_code = 'failed', now(), 'schedule_expired'
            job.activity = [*job.activity, {'tool':'run_setup', 'status':'failed', 'at':now().isoformat(),
                'reason':'This scheduled run expired before it could start.'}]
            return
        if not ready(database):
            job.status, job.finished_at, job.error_code = 'failed', now(), 'service_unavailable'
            job.activity = [*job.activity, {'tool':'run_setup', 'status':'failed', 'at':now().isoformat(),
                'reason':'Research connections need administrator attention. No agent was started.'}]
            return
        snapshot = PortfolioSnapshot.model_validate(user.portfolio)
        research_as_of = None if job.schedule_key else research_timestamp(job.payload.get('research_date'), user.timezone)
        policy = cfg.research
        request = AnalysisRequest(snapshot=snapshot, question=job.payload.get('question', '') if not job.schedule_key else '',
            report_length=user.report_length, investment_profile=user.profile, research_as_of=research_as_of,
            **{key: getattr(policy, key) for key in ('max_searches', 'max_model_calls', 'max_tool_calls', 'max_seconds',
                'max_data_age_hours', 'benchmark', 'position_review_threshold_pct', 'sector_review_threshold_pct')})
        recipient, user_zone = user.email, user.timezone
        send_email = user.email_opt_in and (bool(job.schedule_key) or job.payload.get('send_email', False))
        owner = user.id
    activity({'tool':'run_setup', 'status':'passed', 'at':now().isoformat()})
    activity({'tool':'model_setup', 'status':'started', 'at':now().isoformat()})
    deps = build_dependencies(cfg.research, database.credentials)
    if deps.model is None:
        raise ValueError('A production research model is required')
    deps.offline = False
    deps.activity_sink = activity
    activity({'tool':'model_setup', 'status':'passed', 'at':now().isoformat()})
    with database.transaction() as db:
        row = db.get(Job, job_id)
        if not row or row.cancel_requested:
            if row:
                row.status, row.finished_at = 'cancelled', now()
            return
        row.consumed = True
    activity({'tool':'research', 'status':'started', 'at':now().isoformat()})
    result = await analyze_portfolio(request, deps)
    if not (result.research_budget.get('analysis_written') and result.research_budget.get('model_calls', 0) > 0
            and (result.executive_summary or result.sections)):
        result.status = 'failed'
        result.error = 'The research agent did not produce a briefing. Review the agent trace before trying again.'
    activity({'tool':'research', 'status':'failed' if result.status == 'failed' else 'completed',
              'at':now().isoformat(), **({'reason':result.error} if result.error else {})})
    result.run_id = job_id
    email_available = email_ready(database) if send_email and result.status != 'failed' else False
    with database.transaction() as db:
        row = db.get(Job, job_id)
        user = db.get(User, owner)
        if not row or not user:
            return
        if row.cancel_requested or user.suspended:
            row.status, row.finished_at = 'cancelled', now()
            return
        row.status, row.result, row.finished_at = result.status, result.model_dump(mode='json'), now()
        if result.status == 'failed':
            row.error_code = 'research_failed'
        row.budget = result.research_budget
        row.model_calls = result.research_budget.get('model_calls', 0)
        row.searches = result.research_budget.get('queries', 0)
        row.input_tokens = result.research_budget.get('input_tokens')
        row.output_tokens = result.research_budget.get('output_tokens')
        usage_complete = result.research_budget.get('usage_reports',0) == row.model_calls and row.model_calls > 0
        if usage_complete and row.input_tokens is not None and row.output_tokens is not None and cfg.input_cost_per_million is not None and cfg.output_cost_per_million is not None:
            row.estimated_cost = (row.input_tokens*cfg.input_cost_per_million + row.output_tokens*cfg.output_cost_per_million)/1_000_000
        # Recheck consent immediately before claiming delivery.
        send_email = send_email and user.email_opt_in and user.verified and not user.suspended and user.email == recipient
        row.delivery = ('sending' if email_available else 'unavailable') if send_email and result.status != 'failed' else None
    if send_email and email_available and result.status != 'failed':
        html, text = render_report(result, timezone=user_zone)
        try:
            await send_mail(database, recipient, 'Your Betafolio briefing', text, html, idempotency_key='briefing-'+job_id)
            delivery = 'sent'
        except Exception:
            delivery = 'uncertain'
        with database.transaction() as db:
            row = db.get(Job, job_id)
            if row:
                row.delivery = delivery


async def account_email(database, job_id):
    from .mailer import send_mail
    with database.transaction() as db:
        job = db.get(Job, job_id)
        user = db.get(User, job.user_id) if job else None
        if not job or not user or user.suspended:
            if job:
                job.status, job.finished_at = 'cancelled', now()
            return
        purpose, recipient = job.payload['purpose'], user.email
        token = database.credentials.decrypt(job.payload['token'])
    origin = os.environ.get('PORTFOLIO_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
    label = 'Verify your email' if purpose == 'verify' else 'Reset your password'
    link = origin+'/#'+purpose+'='+token
    explanation = ('A request was made to verify the email address for your Betafolio account.'
                   if purpose == 'verify' else 'A request was made to reset your Betafolio password.')
    message = (explanation+'\n\n'+label+':\n'+link+
               '\n\nIf you did not make this request or were not expecting this email, you can ignore it. '
               'No action will be taken unless you use the link.')
    try:
        await send_mail(database, recipient, 'Betafolio: '+label, message,
                        idempotency_key='account-'+job_id)
        status, delivery = 'success', 'sent'
    except Exception:
        status, delivery = 'failed', 'uncertain'
    with database.transaction() as db:
        job = db.get(Job, job_id)
        if job:
            job.status, job.delivery, job.finished_at, job.payload = status, delivery, now(), {}


async def signup_email(database, job_id):
    from .accounts import digest
    from .mailer import send_mail
    with database.transaction() as db:
        job = db.get(Job, job_id)
        invitation = db.get(SignupRequest, job.signup_request_id) if job else None
        token = database.credentials.decrypt(job.payload['token']) if job and job.payload.get('token') else ''
        if (not job or not invitation or invitation.status != 'approved' or not invitation.expires
                or invitation.expires <= now() or invitation.token_hash != digest(token)):
            if job:
                job.status, job.finished_at, job.payload = 'cancelled', now(), {}
            return
        recipient = invitation.email
    origin = os.environ.get('PORTFOLIO_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
    message = (
        'A request was submitted to create a Betafolio account using this email address. '
        'A Betafolio administrator has approved that request.\n\n'
        'If you made this request, use the link below to verify your email address and choose a password. '
        'Your account will be created only after you complete that step.\n\n'
        +origin+'/#signup='+token+'\n\n'
        'This link expires in 24 hours and can be used once.\n\n'
        'If you did not request an account or were not expecting this email, you can safely ignore it. '
        'No account will be created and no action is required.'
    )
    try:
        await send_mail(database, recipient, 'Your Betafolio access request was approved', message,
                        idempotency_key='signup-'+job_id)
        status, delivery = 'success', 'sent'
    except Exception:
        status, delivery = 'failed', 'uncertain'
    with database.transaction() as db:
        job = db.get(Job, job_id)
        if job:
            job.status, job.delivery, job.finished_at, job.payload = status, delivery, now(), {}


async def refresh_prices(database):
    from .providers import YFinanceMarketDataProvider, quote_is_newer
    from datetime import date
    with database.transaction() as db:
        portfolios = [(u.id, u.portfolio) for u in db.scalars(select(User).where(
            User.role == 'user', User.verified.is_(True), User.suspended.is_(False), User.portfolio.is_not(None))) if u.portfolio]
    symbols = list(dict.fromkeys(p['symbol'] for _, portfolio in portfolios for p in portfolio['positions']))
    provider = YFinanceMarketDataProvider()
    quotes = {}
    for start in range(0, len(symbols), 30):
        quotes.update(await provider.quotes(symbols[start:start+30]))
    for user_id, _ in portfolios:
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            if not user or not user.portfolio:
                continue
            snapshot = PortfolioSnapshot.model_validate(user.portfolio)
            for position in snapshot.positions:
                quote = quotes.get(position.symbol)
                if quote and quote_is_newer(quote, position):
                    position.price, position.previous_close = quote['price'], quote.get('previous_close')
                    position.price_session = date.fromisoformat(quote['trading_date'])
                    position.price_as_of = datetime.fromisoformat(quote['quote_as_of']) if quote.get('quote_as_of') else None
                    position.price_source = quote['source']
            user.portfolio = snapshot.model_dump(mode='json')


def maintenance(database):
    cutoff = now()-timedelta(days=settings(database).research.retention_days)
    with database.transaction() as db:
        for table, column in ((AuthToken, AuthToken.expires), (Session, Session.expires), (RateBucket, RateBucket.expires)):
            db.execute(delete(table).where(column < now()))
        db.execute(delete(Job).where(Job.created_at < cutoff, Job.status.not_in(ACTIVE)))
        db.execute(delete(Audit).where(Audit.created_at < cutoff))
        db.execute(delete(SignupRequest).where(SignupRequest.created_at < cutoff,
            (SignupRequest.expires.is_(None) | (SignupRequest.expires < now()))))
        for job in db.scalars(select(Job).where(Job.status == 'running', Job.heartbeat < now()-timedelta(minutes=2)).with_for_update()):
            job.status, job.finished_at, job.error_code = 'failed', now(), 'worker_interrupted'
        for job in db.scalars(select(Job).where(Job.delivery == 'sending', Job.finished_at < now()-timedelta(minutes=2))):
            job.delivery = 'uncertain'


async def worker(database):
    worker_id = str(uuid4())
    tasks = {}
    last_prices = last_maintenance = 0
    price_task = None
    try:
        while True:
            timestamp = now()
            # Cancellation and account deletion propagate to the active research task.
            for job_id, task in list(tasks.items()):
                if task.done():
                    task.exception() if not task.cancelled() else None
                    tasks.pop(job_id)
                    continue
                with database.transaction() as db:
                    row = db.get(Job, job_id)
                    user = db.get(User, row.user_id) if row and row.user_id else None
                    invitation = db.get(SignupRequest, row.signup_request_id) if row and row.kind == 'signup_email' else None
                    available = bool(invitation and invitation.status == 'approved') if row and row.kind == 'signup_email' else bool(user and not user.suspended)
                    if not row or not available or row.cancel_requested or row.status != 'running':
                        task.cancel()
                        if row and row.status == 'running':
                            row.status, row.finished_at = 'cancelled', timestamp
                    else:
                        row.heartbeat = timestamp
            schedule_tick(database, timestamp)
            if timestamp.timestamp()-last_maintenance > 60:
                maintenance(database)
                last_maintenance = timestamp.timestamp()
            if timestamp.timestamp()-last_prices >= settings(database).research.price_refresh_seconds and (not price_task or price_task.done()):
                if price_task and not price_task.cancelled():
                    price_task.exception()
                price_task = asyncio.create_task(refresh_prices(database))
                last_prices = timestamp.timestamp()
            job_id = claim(database, worker_id)
            if job_id:
                tasks[job_id] = asyncio.create_task(execute(database, job_id))
            database.set('worker_health', {'at': timestamp.isoformat()})
            await asyncio.sleep(2)
    finally:
        pending = [*tasks.values(), *([price_task] if price_task else [])]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
