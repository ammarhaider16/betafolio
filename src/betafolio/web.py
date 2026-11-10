"""Authenticated user workspace and a separate, metadata-only administration API."""
import asyncio
import hmac
import io
import json
import os
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from email_validator import validate_email, EmailNotValidError
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .accounts import (Credentials, DUMMY_HASH, PASSWORDS, approved_signup, check_password, consume_token, csrf_token,
    digest, encryption_key, issue_token, match_totp, new_session, rate_limit, revoke_sessions, verify_mfa)
from .config import data_directory
from .database import AppState, Audit, Database, Job, Session, SignupRequest, User, now
from .delivery import render_report
from .imports import ColumnMappingRequired, DraftPosition, parse_holdings_file, validate_portfolio_snapshot
from .models import InvestmentProfile, PortfolioSnapshot, Position
from .production import ProductionSettings, email_ready, ready, settings


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EmailInput(Input):
    email: str = Field(max_length=254)

    @field_validator('email')
    @classmethod
    def address(cls, value):
        try:
            return validate_email(value, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            raise ValueError('Enter a valid email address') from None


class Login(EmailInput):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(default='', max_length=128)


class SignupLink(Input):
    token: str = Field(min_length=1, max_length=200)


class CompleteSignup(SignupLink):
    password: str = Field(min_length=12, max_length=128)


class MfaSetup(Input):
    password: str = Field(default='', max_length=128)
    code: str = Field(default='', max_length=128)
    regenerate: bool = False
    setup_id: str = Field(default='', max_length=64, pattern=r'^(?:[a-f0-9]{64})?$')


class MfaConfirmation(Input):
    setup_id: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')
    code: str = Field(min_length=1, max_length=128)


class ConnectionCheck(Input):
    force: list[Literal['search', 'email']] = Field(default_factory=list, max_length=2)


class RunSubmission(Input):
    question: str = Field(default='', max_length=4000)
    research_date: date | None = None
    send_email: bool = False


class Preferences(Input):
    timezone: str = 'America/New_York'
    schedule_opt_in: bool = False
    email_opt_in: bool = False
    report_length: Literal['brief', 'detailed'] = 'detailed'
    profile: InvestmentProfile | None = None

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError):
            raise ValueError('Choose a valid IANA timezone') from None
        return value


class Holding(Input):
    symbol: str = Field(min_length=1, max_length=40)
    quantity: str = Field(max_length=80)


class Holdings(Input):
    positions: list[Holding] = Field(max_length=1000)
    cash: str = Field(default='0', max_length=80)


def open_database(directory=None, server=False):
    db = Database()
    db.credentials = Credentials(db, encryption_key(directory or data_directory(), server))
    return db


def create_app(directory: Path | None = None, server: bool | None = None, *, database=None):
    server = os.environ.get('PORTFOLIO_SERVER_MODE') == '1' if server is None else server
    database = database or open_database(directory, server)
    public_url = os.environ.get('PORTFOLIO_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
    origin = urlsplit(public_url)
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/') or not origin.hostname:
        raise ValueError('PORTFOLIO_PUBLIC_URL must be an origin without a path')
    if server and origin.scheme != 'https':
        raise ValueError('Production requires an HTTPS PORTFOLIO_PUBLIC_URL')
    if not server and origin.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Local mode accepts loopback origins only')
    app = FastAPI(title='Betafolio', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = database
    package = Path(__file__).parent
    app.mount('/static', StaticFiles(directory=package/'static'), name='static')
    hosts = [origin.hostname,'127.0.0.1','localhost','[::1]']
    if os.environ.get('RAILWAY_PROJECT_ID'):
        hosts.append('healthcheck.railway.app')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    templates = Jinja2Templates(directory=package/'templates')

    def set_session(response, token):
        response.set_cookie('betafolio_session', token, httponly=True, secure=server,
                            samesite='strict', max_age=43200, path='/')

    def identity(request, role=None, *, allow_setup=False):
        token = request.cookies.get('betafolio_session', '')
        with database.transaction() as db:
            session = db.scalar(select(Session).where(Session.token_hash == digest(token), Session.expires > now()))
            user = db.get(User, session.user_id) if session else None
            if not user or user.suspended:
                raise HTTPException(401, 'Please sign in')
            if role and user.role != role:
                raise HTTPException(403, 'This area is not available for your account')
            if not allow_setup and (user.role == 'admin' or user.mfa_secret) and not session.mfa:
                raise HTTPException(403, 'Complete authenticator setup or sign in with your code')
            user.last_active = now()
            return user

    def limited(request, action, limit=10, seconds=300, owner=None):
        peer = owner or (request.client.host if request.client else 'unknown')
        # The supplied Railway deployment exposes only its HTTPS edge, which replaces
        # X-Real-IP. Never trust this header on ordinary/local/direct deployments.
        if not owner and server and os.environ.get('RAILWAY_PROJECT_ID') and request.url.hostname == origin.hostname:
            import ipaddress
            try:
                peer = str(ipaddress.ip_address(request.headers.get('x-real-ip','')))
            except ValueError:
                pass
        if not rate_limit(database, f'{action}:{peer}', limit, seconds):
            raise HTTPException(429, 'Too many requests. Please try again later.')

    async def body(request, model=None):
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError()
            return model.model_validate(value) if model else value
        except (ValueError, ValidationError):
            raise HTTPException(422, 'Check the submitted fields') from None

    @app.middleware('http')
    async def security(request, call_next):
        cookie = request.cookies.get('betafolio_session') or secrets.token_urlsafe(32)
        request.state.csrf = csrf_token(cookie, database.credentials)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if request.headers.get('origin') not in (None, public_url) or not hmac.compare_digest(
                request.headers.get('x-csrf-token', ''), request.state.csrf):
                return JSONResponse({'detail': 'Reload the page before submitting'}, status_code=403)
            limit = 5_500_000 if request.url.path == '/api/import/file' else 1_000_000
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > limit:
                    return JSONResponse({'detail': 'File or request is too large'}, status_code=413)
            request._body = bytes(content)
        try:
            response = await call_next(request)
        except Exception:
            # Never return provider/DB errors containing queries, content, keys or addresses.
            response = JSONResponse({'detail': 'The service could not complete this request. Please try again.'}, status_code=503)
        if not request.cookies.get('betafolio_session') and 'set-cookie' not in response.headers:
            set_session(response, cookie)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'",
        })
        if server:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': 'Check the submitted fields'}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)[:250]}, status_code=400)

    @app.get('/', response_class=HTMLResponse)
    @app.get('/admin', response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name='production.html', context={'csrf':request.state.csrf})

    @app.get('/health')
    async def health():
        with database.transaction() as db:
            db.execute(text('SELECT 1'))
        return {'status':'ok'}

    @app.get('/ready')
    async def readiness():
        worker_health = database.get('worker_health', {})
        from datetime import datetime
        alive = bool(worker_health.get('at') and (now()-datetime.fromisoformat(worker_health['at'])).total_seconds() < 60)
        return JSONResponse({'ready':alive and ready(database)}, status_code=200 if alive and ready(database) else 503)

    @app.get('/api/session')
    async def current(request: Request):
        result = {'csrf':request.state.csrf,'signed_in':False}
        try:
            user = identity(request, allow_setup=True)
        except HTTPException:
            result['signup_available'] = settings(database).signup_enabled
            return result
        result.update(signed_in=True, email=user.email, role=user.role, verified=user.verified,
                      mfa_enrolled=bool(user.mfa_secret), admin_setup_required=user.role == 'admin' and not user.mfa_secret)
        return result

    @app.post('/api/auth/register')
    async def register(request: Request):
        limited(request, 'register', 5, 3600)
        value = await body(request, EmailInput)
        if not settings(database).signup_enabled:
            raise HTTPException(503, 'Access requests are currently closed')
        try:
            with database.transaction() as db:
                invitation = db.scalar(select(SignupRequest).where(
                    SignupRequest.email == value.email).with_for_update())
                if not db.scalar(select(User.id).where(User.email == value.email)):
                    if invitation is None:
                        db.add(SignupRequest(email=value.email))
                    elif invitation.status == 'completed' or (
                        invitation.status == 'approved' and invitation.expires and invitation.expires <= now()
                    ):
                        invitation.status, invitation.created_at = 'pending', now()
                        invitation.token_hash, invitation.expires, invitation.approved_at = None, None, None
        except IntegrityError:
            pass  # Concurrent duplicate requests return the same non-enumerating response.
        return {'message':'Your access request has been received. If approved, an email will invite you to create your account.'}

    @app.post('/api/auth/signup-info')
    async def signup_info(request: Request):
        limited(request, 'signup-link', 20)
        value = await body(request, SignupLink)
        with database.transaction() as db:
            invitation = approved_signup(db, value.token)
            return {'email':invitation.email}

    @app.post('/api/auth/complete-signup')
    async def complete_signup(request: Request):
        limited(request, 'complete-signup', 10)
        value = await body(request, CompleteSignup)
        hashed = await asyncio.to_thread(check_password, value.password)
        try:
            with database.transaction() as db:
                invitation = approved_signup(db, value.token)
                if db.scalar(select(User.id).where(User.email == invitation.email)):
                    raise ValueError('This account already exists. Sign in or reset your password.')
                user = User(email=invitation.email, password_hash=hashed, verified=True)
                db.add(user)
                db.flush()
                invitation.status, invitation.token_hash, invitation.expires = 'completed', None, None
                db.execute(delete(Session).where(Session.token_hash == digest(request.cookies.get('betafolio_session',''))))
                token = new_session(db, user)
        except IntegrityError:
            raise ValueError('This account already exists. Sign in or reset your password.') from None
        response = JSONResponse({'message':'Your email is verified and your account is ready.'})
        set_session(response, token)
        return response

    @app.post('/api/auth/login')
    async def login(request: Request):
        limited(request, 'login', 15, 300)
        value = await body(request, Login)
        limited(request, 'login-account', 15, 300, owner=digest(value.email))
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.email == value.email))
            hashed = user.password_hash if user else DUMMY_HASH
        valid = await asyncio.to_thread(PASSWORDS.verify, value.password, hashed)
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.email == value.email).with_for_update())
            if not user or not valid or user.password_hash != hashed or user.suspended:
                raise HTTPException(401, 'Email, password, or authentication code is incorrect')
            if user.mfa_secret and not verify_mfa(user, value.code, database.credentials):
                raise HTTPException(401, 'Email, password, or authentication code is incorrect')
            token = new_session(db, user, mfa=bool(user.mfa_secret))
        response = JSONResponse({'ok':True})
        set_session(response, token)
        return response

    @app.post('/api/auth/logout')
    async def logout(request: Request):
        with database.transaction() as db:
            db.execute(delete(Session).where(Session.token_hash == digest(request.cookies.get('betafolio_session',''))))
        response = JSONResponse({'ok':True})
        response.delete_cookie('betafolio_session', path='/')
        return response

    @app.post('/api/auth/request-link')
    async def request_link(request: Request):
        limited(request, 'account-links', 5, 3600)
        value = await body(request)
        email = str(value.get('email',''))[:254].strip().lower()
        purpose = 'reset' if value.get('purpose') == 'reset' else 'verify'
        limited(request, 'account-links-address', 3, 3600, owner=digest(email))
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.email == email).with_for_update())
            if user and not user.suspended and (purpose == 'reset' or not user.verified):
                token = issue_token(db, user, purpose)
                db.add(Job(user_id=user.id, kind='account_email', payload={'purpose':purpose,'token':database.credentials.encrypt(token)}))
        return {'message':'If this account exists, an email will arrive shortly.'}

    @app.post('/api/auth/verify')
    async def verify(request: Request):
        limited(request, 'verify', 20)
        value = await body(request)
        with database.transaction() as db:
            user = consume_token(db, str(value.get('token',''))[:200], 'verify')
            user.verified = True
        return {'message':'Email verified. You can sign in.'}

    @app.post('/api/auth/reset')
    async def reset(request: Request):
        limited(request, 'reset', 10)
        value = await body(request)
        hashed = await asyncio.to_thread(check_password, str(value.get('password','')))
        with database.transaction() as db:
            user = consume_token(db, str(value.get('token',''))[:200], 'reset')
            user.password_hash = hashed
            revoke_sessions(db, user.id)
        return {'message':'Password updated. Sign in again; your authenticator is still required.'}

    @app.post('/api/account/mfa/setup')
    async def mfa_setup(request: Request):
        import pyotp
        import qrcode
        import qrcode.image.svg
        import base64
        user = identity(request, allow_setup=True)
        if user.role != 'admin':
            limited(request, 'mfa-setup', 5, owner=user.id)
        value = await body(request, MfaSetup)
        password_valid = bool(user.mfa_secret) and await asyncio.to_thread(
            PASSWORDS.verify, value.password, user.password_hash)
        with database.transaction() as db:
            current = db.scalar(select(User).where(User.id == user.id).with_for_update())
            if not current or current.suspended or current.password_hash != user.password_hash:
                raise HTTPException(401, 'Sign in again before setting up your authenticator')
            if current.mfa_secret:
                if not password_valid:
                    raise HTTPException(403, 'Enter your current password')
                if not verify_mfa(current, value.code, database.credentials):
                    raise HTTPException(403, 'Enter a current authenticator or unused recovery code to replace it')
            secret = database.credentials.decrypt(current.pending_mfa) if current.pending_mfa else None
            if value.regenerate:
                if not secret or not hmac.compare_digest(value.setup_id, digest(secret)):
                    raise HTTPException(409, 'This setup code has changed. Reload setup before generating a new code.')
                secret = None
            if not secret:
                secret = pyotp.random_base32()
                current.pending_mfa = database.credentials.encrypt(secret)
        uri = pyotp.TOTP(secret).provisioning_uri(user.email, issuer_name='Betafolio')
        image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        return {'qr':'data:image/svg+xml;base64,'+base64.b64encode(buffer.getvalue()).decode(),
                'manual_key':secret, 'setup_id':digest(secret)}

    @app.post('/api/account/mfa/confirm')
    async def mfa_confirm(request: Request):
        user = identity(request, allow_setup=True)
        if user.role != 'admin':
            limited(request, 'mfa-confirm', 8, owner=user.id)
        value = await body(request, MfaConfirmation)
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.id == user.id).with_for_update())
            if not user.pending_mfa:
                raise ValueError('Start authenticator setup again')
            secret = database.credentials.decrypt(user.pending_mfa)
            if not hmac.compare_digest(value.setup_id, digest(secret)):
                raise HTTPException(409, 'This QR code was replaced. Reload setup and scan the current QR code.')
            counter = match_totp(secret, value.code)
            if counter is None:
                raise ValueError('Enter the current six-digit code from the displayed authenticator entry. If needed, wait for the next code and check your phone uses automatic time.')
            codes = [secrets.token_hex(8) for _ in range(8)]
            user.mfa_secret, user.pending_mfa = user.pending_mfa, None
            user.recovery_hashes = [digest(code) for code in codes]
            user.mfa_last_counter = counter
            revoke_sessions(db,user.id)
            token = new_session(db,user,mfa=True)
        response = JSONResponse({'recovery_codes':codes})
        set_session(response,token)
        return response

    @app.get('/api/workspace')
    async def workspace(request: Request):
        user = identity(request,'user')
        with database.transaction() as db:
            jobs = list(db.scalars(select(Job).where(Job.user_id == user.id, Job.kind == 'research').order_by(Job.created_at.desc()).limit(90)))
        return {'portfolio':user.portfolio,'profile':user.profile,'preferences':{
            key:getattr(user,key) for key in ('timezone','schedule_opt_in','email_opt_in','report_length')},
            'service_available':ready(database), 'verified':user.verified,
            'schedule':settings(database).schedule.model_dump(mode='json'),
            'history':[{'id':j.id,'status':j.status,'created_at':j.created_at.isoformat(),
                        'scheduled':bool(j.schedule_key),'delivery':j.delivery,'error_code':j.error_code,
                        'research_as_of':(j.result or {}).get('research_as_of')} for j in jobs]}

    @app.put('/api/portfolio')
    async def portfolio(request: Request):
        user = identity(request,'user')
        value = await body(request,Holdings)
        from .imports import parse_amount
        snapshot = validate_portfolio_snapshot(PortfolioSnapshot(as_of=now(),source='manual',
            positions=[Position(symbol=p.symbol,quantity=parse_amount(p.quantity)) for p in value.positions],cash=parse_amount(value.cash)))
        with database.transaction() as db:
            current = db.scalar(select(User).where(User.id == user.id).with_for_update())
            previous = {p['symbol']:p for p in (current.portfolio or {}).get('positions',[])}
            for position in snapshot.positions:
                cached = db.get(AppState,'instrument:'+position.symbol)
                saved = previous.get(position.symbol,{})
                if saved:
                    prior = Position.model_validate({**saved,'quantity':str(position.quantity)})
                    for key in ('asset_type','instrument_name','exchange','sector','price','previous_close','price_session','price_as_of','price_source'):
                        setattr(position,key,getattr(prior,key))
                if cached:
                    details = cached.value
                    for key in ('asset_type','instrument_name','exchange','sector'):
                        if details.get(key) is not None:
                            setattr(position,key,details[key])
                    from .providers import quote_is_newer
                    quote = {'trading_date':details.get('price_session'),'quote_as_of':details.get('price_as_of')}
                    if details.get('price') is not None and quote_is_newer(quote,position):
                        validated = Position.model_validate({**position.model_dump(mode='json'),
                            **{key:details[key] for key in ('price','previous_close','price_session','price_as_of','price_source') if key in details}})
                        for key in ('price','previous_close','price_session','price_as_of','price_source'):
                            setattr(position,key,getattr(validated,key))
            current.portfolio = snapshot.model_dump(mode='json')
        return {'portfolio':snapshot.model_dump(mode='json')}

    @app.get('/api/demo')
    async def demo(request: Request):
        identity(request,'user')
        from .demo import demo_holdings
        return demo_holdings().model_dump(mode='json')

    @app.post('/api/import/file')
    async def upload(request: Request):
        user = identity(request,'user')
        limited(request,'import',15,owner=user.id)
        form = await request.form(max_files=1,max_fields=2,max_part_size=5_500_000)
        upload = form.get('file')
        if not upload or not hasattr(upload,'read'):
            raise ValueError('Choose a CSV, XLSX or XLS file')
        content = await upload.read(5*1024*1024+1)
        try:
            mapping = json.loads(str(form['mapping'])) if form.get('mapping') else None
            draft = await asyncio.to_thread(parse_holdings_file,content,upload.filename,now(),mapping)
        except ColumnMappingRequired as exc:
            return {'needs_mapping':True,'columns':exc.columns,'suggestions':exc.suggestions}
        return {'draft':draft.model_dump(mode='json')}

    @app.post('/api/portfolio/enrich')
    async def enrich(request: Request):
        user = identity(request,'user')
        if not user.verified:
            raise HTTPException(403,'Verify your email before looking up instrument details')
        limited(request,'enrichment',10,owner=user.id)
        value = await body(request)
        positions = value.get('positions',[])
        if not 1 <= len(positions) <= 30:
            raise ValueError('Fill details supports 1 to 30 holdings')
        from .enrichment import PortfolioEnrichmentAgent
        trusted = [DraftPosition(symbol=Position.normalize_symbol(p['symbol']),quantity=p.get('quantity')) for p in positions]
        result = await PortfolioEnrichmentAgent().enrich(trusted)
        # Cache public instrument facts only, never quantities or private draft contents.
        for item in result['positions']:
            facts = {key:value for key,value in item.items() if key not in ('symbol','quantity') and value is not None}
            if facts.get('asset_type') == 'unknown':
                facts.pop('asset_type')
            if facts:
                key = 'instrument:'+item['symbol']
                database.set(key,{**database.get(key,{}),**facts})
        return result

    @app.put('/api/account/preferences')
    async def preferences(request: Request):
        user = identity(request,'user')
        value = await body(request,Preferences)
        with database.transaction() as db:
            current = db.scalar(select(User).where(User.id == user.id).with_for_update())
            for key,item in value.model_dump(mode='json').items():
                setattr(current,key,item)
        return {'ok':True}

    @app.post('/api/runs')
    async def run(request: Request):
        user = identity(request,'user')
        limited(request,'run',10,owner=user.id)
        value = await body(request,RunSubmission)
        from .research_clock import research_timestamp
        research_timestamp(value.research_date,user.timezone)
        if not ready(database):
            raise HTTPException(503,'Research is temporarily unavailable. The administrator has been notified through service status.')
        from .jobs import enqueue
        return {'id':enqueue(database,user.id,value.model_dump(mode='json'))}

    @app.get('/api/runs/{run_id}')
    async def run_detail(run_id: str, request: Request):
        user = identity(request,'user')
        job = database.owned_job(user.id,run_id)
        if not job or job.kind != 'research':
            raise HTTPException(404,'Research run not found')
        return {'id':job.id,'status':job.status,'result':job.result,'activity':job.activity,
                'error_code':job.error_code,'delivery':job.delivery}

    @app.post('/api/runs/{run_id}/cancel')
    async def cancel(run_id: str, request: Request):
        user = identity(request,'user')
        with database.transaction() as db:
            job = db.scalar(select(Job).where(Job.user_id == user.id,Job.id == run_id,Job.kind == 'research').with_for_update())
            if not job:
                raise HTTPException(404,'Research run not found')
            job.cancel_requested = True
            if job.status == 'queued':
                job.status,job.finished_at = 'cancelled',now()
        return {'ok':True}

    @app.get('/reports/{run_id}',response_class=HTMLResponse)
    async def report(run_id: str,request: Request):
        user = identity(request,'user')
        job = database.owned_job(user.id,run_id)
        if not job or not job.result or job.kind != 'research' or job.status not in {'success', 'partial'}:
            raise HTTPException(404,'Report not found')
        return HTMLResponse(render_report(job.result,timezone=user.timezone)[0])

    @app.delete('/api/account')
    async def delete_account(request: Request):
        user = identity(request,'user')
        value = await body(request)
        if not await asyncio.to_thread(PASSWORDS.verify,str(value.get('password',''))[:128],user.password_hash):
            raise HTTPException(403,'Enter your password to delete your account')
        with database.transaction() as db:
            current = db.scalar(select(User).where(User.id == user.id).with_for_update())
            db.execute(delete(SignupRequest).where(SignupRequest.email == user.email))
            db.delete(current)
        response = JSONResponse({'ok':True})
        response.delete_cookie('betafolio_session',path='/')
        return response

    @app.get('/api/admin/settings')
    async def admin_settings(request: Request):
        from .connections import connection_status
        identity(request,'admin')
        with database.transaction() as db:
            mail_queue = {status:db.scalar(select(func.count()).select_from(Job).where(
                Job.kind.in_(['account_email','signup_email']),Job.status == status)) for status in ('queued','running','success','failed')}
        return {'settings':settings(database).model_dump(mode='json'), 'secrets':database.credentials.status(),
                'readiness':connection_status(database),'worker':database.get('worker_health',{}),'account_mail':mail_queue}

    @app.put('/api/admin/settings')
    async def save_admin_settings(request: Request):
        user = identity(request,'admin')
        value = await body(request,ProductionSettings)
        database.set('settings',value.model_dump(mode='json'))
        database.audit(user.id,'settings_updated')
        return {'ok':True}

    @app.put('/api/admin/secrets/{name}')
    async def save_secret(name: str,request: Request):
        user = identity(request,'admin')
        value = await body(request)
        database.credentials.set(name,value.get('value'))
        database.audit(user.id,'credential_updated:'+name)
        return {'ok':True}

    @app.delete('/api/admin/secrets/{name}')
    async def delete_secret(name: str,request: Request):
        user = identity(request,'admin')
        database.credentials.delete(name)
        database.audit(user.id,'credential_deleted:'+name)
        return {'ok':True}

    @app.post('/api/admin/check')
    async def check(request: Request):
        user = identity(request,'admin')
        from .connections import check_all
        value = await body(request, ConnectionCheck)
        return await check_all(database, user.email, force=value.force)

    @app.get('/api/admin/signup-requests')
    async def signup_requests(request: Request):
        identity(request, 'admin')
        with database.transaction() as db:
            requests = db.scalars(select(SignupRequest).where(SignupRequest.status != 'completed')
                                  .order_by(SignupRequest.created_at.desc()).limit(200)).all()
            result = []
            for invitation in requests:
                mail = db.scalar(select(Job).where(Job.signup_request_id == invitation.id)
                                 .order_by(Job.created_at.desc()).limit(1))
                result.append({'id':invitation.id, 'email':invitation.email, 'status':invitation.status,
                    'created_at':invitation.created_at.isoformat(),
                    'expires':invitation.expires.isoformat() if invitation.expires else None,
                    'delivery':mail.delivery if mail else None, 'mail_status':mail.status if mail else None})
        return {'requests':result}

    @app.post('/api/admin/signup-requests/{request_id}/deny')
    async def deny_signup(request_id: str, request: Request):
        actor = identity(request, 'admin')
        with database.transaction() as db:
            invitation = db.scalar(select(SignupRequest).where(
                SignupRequest.id == request_id).with_for_update())
            if not invitation or invitation.status == 'completed':
                raise HTTPException(404, 'Access request not found')
            if invitation.status == 'denied':
                return {'message': 'Access request denied. No email was sent.'}
            if invitation.status != 'pending' or db.scalar(select(Job.id).where(
                Job.signup_request_id == request_id, Job.status.in_(['queued', 'running']))):
                raise HTTPException(409, 'Only pending requests without an invitation in progress can be denied')
            invitation.status = 'denied'
            invitation.token_hash, invitation.expires, invitation.approved_at = None, None, None
            db.add(Audit(actor_id=actor.id, action='signup_denied:' + invitation.id))
        # Denial does not invoke delivery or create any email job.
        return {'message': 'Access request denied. No email was sent.'}

    @app.post('/api/admin/signup-requests/{request_id}/approve')
    async def approve_signup(request_id: str, request: Request):
        actor = identity(request, 'admin')
        if not email_ready(database):
            raise HTTPException(503, 'Check the saved email connection before sending account invitations')
        with database.transaction() as db:
            invitation = db.scalar(select(SignupRequest).where(SignupRequest.id == request_id).with_for_update())
            if not invitation or invitation.status == 'completed':
                raise HTTPException(404, 'Access request not found')
            if db.scalar(select(User.id).where(User.email == invitation.email)):
                raise ValueError('This account already exists')
            if db.scalar(select(Job.id).where(Job.signup_request_id == invitation.id,
                                             Job.status.in_(['queued','running']))):
                raise HTTPException(409, 'The invitation email is already queued or being sent')
            token = secrets.token_urlsafe(32)
            invitation.status, invitation.approved_at = 'approved', now()
            invitation.token_hash, invitation.expires = digest(token), now()+timedelta(hours=24)
            db.add(Job(kind='signup_email', signup_request_id=invitation.id,
                       payload={'token':database.credentials.encrypt(token)}))
            db.add(Audit(actor_id=actor.id, action='signup_approved:'+invitation.id))
        return {'message':'Access approved. The account-creation email is queued.'}

    @app.get('/api/admin/users')
    async def admin_users(request: Request):
        identity(request,'admin')
        # Explicit columns: never query or serialize portfolio/profile/result/activity/payload here.
        with database.transaction() as db:
            users = db.execute(select(User.id,User.email,User.verified,User.suspended,User.created_at,User.last_active,
                User.schedule_opt_in,User.email_opt_in,User.allowance).where(User.role == 'user')).mappings().all()
            result = []
            for user in users:
                jobs = db.execute(select(Job.status,Job.created_at,Job.started_at,Job.finished_at,Job.delivery,
                    Job.model_calls,Job.searches,Job.input_tokens,Job.output_tokens,Job.estimated_cost)
                    .where(Job.user_id == user['id'],Job.kind == 'research')).mappings().all()
                outcomes = {status:sum(j['status']==status for j in jobs) for status in ('queued','running','success','partial','failed','cancelled')}
                def seconds(a,b):
                    return (b-a).total_seconds() if a and b else None
                waits = [v for j in jobs if (v:=seconds(j['created_at'],j['started_at'])) is not None]
                durations = [v for j in jobs if (v:=seconds(j['started_at'],j['finished_at'])) is not None]
                result.append({**dict(user),'runs':len(jobs),'outcomes':outcomes,
                    'mean_queue_seconds':sum(waits)/len(waits) if waits else None,
                    'mean_run_seconds':sum(durations)/len(durations) if durations else None,
                    'model_calls':sum(j['model_calls'] for j in jobs),'searches':sum(j['searches'] for j in jobs),
                    'input_tokens':sum(j['input_tokens'] or 0 for j in jobs) if any(j['input_tokens'] is not None for j in jobs) else None,
                    'output_tokens':sum(j['output_tokens'] or 0 for j in jobs) if any(j['output_tokens'] is not None for j in jobs) else None,
                    'estimated_cost':sum(j['estimated_cost'] or 0 for j in jobs) if any(j['estimated_cost'] is not None for j in jobs) else None,
                    'email_sent':sum(j['delivery']=='sent' for j in jobs),'email_uncertain':sum(j['delivery']=='uncertain' for j in jobs)})
        return {'users':result,'retention_days':settings(database).research.retention_days}

    @app.put('/api/admin/users/{user_id}')
    async def manage_user(user_id: str,request: Request):
        actor = identity(request,'admin')
        value = await body(request)
        if set(value)-{'suspended','allowance'}:
            raise ValueError('Only suspension and research allowance can be changed here')
        with database.transaction() as db:
            user = db.scalar(select(User).where(User.id == user_id,User.role == 'user').with_for_update())
            if not user:
                raise HTTPException(404,'User not found')
            if 'suspended' in value:
                if not isinstance(value['suspended'],bool):
                    raise ValueError('Suspension must be true or false')
                user.suspended = value['suspended']
                if user.suspended:
                    revoke_sessions(db,user.id)
            if 'allowance' in value:
                allowance = value['allowance']
                if allowance is not None and (not isinstance(allowance,int) or isinstance(allowance,bool) or not 0 <= allowance <= 100):
                    raise ValueError('Allowance must be between 0 and 100')
                user.allowance = allowance
        database.audit(actor.id,'user_access_updated')
        return {'ok':True}

    @app.delete('/api/admin/users/{user_id}')
    async def delete_user(user_id: str, request: Request):
        actor = identity(request, 'admin')
        from .database import Audit

        with database.transaction() as db:
            user = db.scalar(select(User).where(
                User.id == user_id, User.role == 'user').with_for_update())
            if not user:
                raise HTTPException(404, 'User not found')
            # Existing foreign-key cascades remove sessions, auth tokens, and jobs.
            # Keep the audit event atomic with deletion and omit private account data.
            db.add(Audit(actor_id=actor.id, action='user_deleted:' + user.id))
            db.execute(delete(SignupRequest).where(SignupRequest.email == user.email))
            db.delete(user)
        return {'ok': True}

    return app
