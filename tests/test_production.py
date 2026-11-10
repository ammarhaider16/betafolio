"""Consequential hosted boundaries. Requires an explicitly disposable Postgres database."""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from betafolio.accounts import Credentials, check_password, issue_token, new_session, consume_token
from betafolio.cli import migrate
from betafolio.database import Credential, Database, Job, Session, SignupRequest, User, now
from betafolio.imports import ColumnMappingRequired, parse_holdings_csv
from betafolio.jobs import claim, enqueue, execute, schedule_tick
from betafolio.models import AnalysisResult
from betafolio.production import ProductionSettings
from betafolio.web import create_app


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get('BETAFOLIO_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set BETAFOLIO_TEST_DATABASE_URL to an isolated Postgres *_test database')
    parsed = make_url(url.replace('postgresql://','postgresql+psycopg://',1))
    if not (parsed.database or '').endswith('_test'):
        pytest.fail('Tests require a dedicated database ending in _test')
    schema = 'test_'+uuid4().hex
    operator = create_engine(parsed)
    with operator.begin() as db:
        db.execute(text('CREATE SCHEMA '+schema))
    scoped = parsed.update_query_dict({'options':'-csearch_path='+schema})
    database = Database(scoped.render_as_string(hide_password=False))
    database.credentials = Credentials(database,Fernet.generate_key())
    migrate(database)
    monkeypatch.setenv('PORTFOLIO_PUBLIC_URL','http://127.0.0.1:8000')
    monkeypatch.setenv('PORTFOLIO_SERVER_MODE','0')
    try:
        yield database
    finally:
        database.engine.dispose()
        with operator.begin() as db:
            db.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        operator.dispose()


def account(database,email='user@example.com',role='user'):
    with database.transaction() as db:
        user = User(email=email,password_hash=check_password('A strong sample password'),role=role,verified=True,
            portfolio=None if role=='admin' else {'as_of':now().isoformat(),'positions':[{'symbol':'PRIVATE','quantity':'7'}],'cash':'9876'})
        db.add(user)
        db.flush()
        return user.id


def browser(database,user_id):
    with database.transaction() as db:
        token = new_session(db,db.get(User,user_id),mfa=True)
    client = TestClient(create_app(database=database),base_url='http://127.0.0.1:8000')
    client.cookies.set('betafolio_session',token,domain='127.0.0.1',path='/')
    client.headers['x-csrf-token'] = client.get('/api/session').json()['csrf']
    return client


def test_tenant_and_admin_boundaries(database):
    first,second,admin = account(database),account(database,'second@example.com'),account(database,'admin@example.com','admin')
    with database.transaction() as db:
        job = Job(user_id=first,status='success',result={'executive_summary':'PRIVATE REPORT'},activity=[{'query':'PRIVATE QUERY'}])
        db.add(job)
        db.flush()
        job_id=job.id
    for owner in (second,admin):
        client = browser(database,owner)
        expected = 403 if owner==admin else 404
        assert client.get('/api/runs/'+job_id).status_code==expected
        assert client.get('/reports/'+job_id).status_code==expected
        assert client.post('/api/runs/'+job_id+'/cancel',json={}).status_code==expected
    client = browser(database,first)
    assert client.get('/api/admin/users').status_code==403
    assert client.get('/api/admin/settings').status_code==403
    assert client.post('/api/portfolio',json={}).status_code==405
    admin_browser=browser(database,admin)
    report=admin_browser.get('/api/admin/users')
    assert report.status_code==200
    assert not any(token in report.text for token in ('PRIVATE','9876','portfolio','activity','profile','result'))
    assert browser(database,first).get('/api/runs/'+job_id).json()['result']['executive_summary']=='PRIVATE REPORT'


def test_admin_deletion_is_role_scoped_and_csrf_protected(database):
    target = account(database)
    other = account(database, 'other@example.com')
    admin = account(database, 'admin@example.com', 'admin')
    client = browser(database, other)
    assert client.delete('/api/admin/users/' + target).status_code == 403

    client = browser(database, admin)
    token = client.headers.pop('x-csrf-token')
    assert client.delete('/api/admin/users/' + target).status_code == 403
    client.headers['x-csrf-token'] = token
    assert client.delete('/api/admin/users/' + admin).status_code == 404
    assert client.delete('/api/admin/users/' + str(uuid4())).status_code == 404
    with database.transaction() as db:
        assert db.get(User, target) is not None
        assert db.get(User, admin) is not None


def test_admin_deletion_cascades_private_data_and_records_actor(database):
    from betafolio.database import Audit, AuthToken

    target = account(database)
    other = account(database, 'other@example.com')
    admin = account(database, 'admin@example.com', 'admin')
    target_client = browser(database, target)
    with database.transaction() as db:
        issue_token(db, db.get(User, target), 'reset')
        db.add_all([
            Job(user_id=target, status='queued'),
            Job(user_id=target, status='running'),
            Job(user_id=target, status='success', result={'executive_summary': 'PRIVATE REPORT'}),
            Job(user_id=other, status='success'),
        ])
    client = browser(database, admin)
    response = client.delete('/api/admin/users/' + target)
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert target_client.get('/api/workspace').status_code == 401
    with database.transaction() as db:
        assert db.get(User, target) is None
        assert db.scalar(select(Session).where(Session.user_id == target)) is None
        assert db.scalar(select(AuthToken).where(AuthToken.user_id == target)) is None
        assert db.scalar(select(Job).where(Job.user_id == target)) is None
        assert db.get(User, other) is not None
        assert db.scalar(select(Job).where(Job.user_id == other)) is not None
        audit = db.scalar(select(Audit).where(Audit.action == 'user_deleted:' + target))
        assert audit is not None and audit.actor_id == admin
    assert client.delete('/api/admin/users/' + target).status_code == 404


def test_queue_admission_and_capacity_are_atomic(database):
    first,second=account(database),account(database,'second@example.com')
    def submit(_):
        try:
            return enqueue(database,first,{})
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted=[job for job in pool.map(submit,range(4)) if job]
    assert len(admitted)==1
    enqueue(database,second,{})
    cfg=ProductionSettings(concurrency=1)
    database.set('settings',cfg.model_dump(mode='json'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed=list(pool.map(lambda _:claim(database,'worker'),range(2)))
    assert len([job for job in claimed if job])==1


def test_account_tokens_sessions_and_encrypted_credentials(database):
    owner=account(database)
    with database.transaction() as db:
        user=db.get(User,owner)
        token=issue_token(db,user,'reset')
    with database.transaction() as db:
        assert consume_token(db,token,'reset').id==owner
    with pytest.raises(ValueError):
        with database.transaction() as db:
            consume_token(db,token,'reset')
    database.credentials.set('email_api_key','synthetic-test-secret')
    with database.transaction() as db:
        assert 'synthetic-test-secret' not in db.get(Credential,'email_api_key').ciphertext
    assert database.credentials.get('email_api_key')=='synthetic-test-secret'
    client=browser(database,owner)
    assert client.request('DELETE','/api/account',json={'password':'A strong sample password'}).status_code==200
    assert client.get('/api/workspace').status_code==401
    with database.transaction() as db:
        assert not db.scalar(select(Session).where(Session.user_id==owner))


def test_daily_schedule_is_live_deduplicated_and_weekend_capable(database,monkeypatch):
    owner=account(database)
    cfg=ProductionSettings(schedule={'enabled':True,'at':'17:00:00','timezone':'America/New_York'})
    database.set('settings',cfg.model_dump(mode='json'))
    with database.transaction() as db:
        user=db.get(User,owner)
        user.schedule_opt_in=True
    timestamp=datetime(2026,11,8,23,tzinfo=timezone.utc)
    monkeypatch.setattr('betafolio.jobs.ready',lambda _:True)
    monkeypatch.setattr('betafolio.jobs.now',lambda:timestamp)
    schedule_tick(database,timestamp)
    schedule_tick(database,timestamp)
    with database.transaction() as db:
        jobs=list(db.scalars(select(Job)))
        assert len(jobs)==1 and jobs[0].payload=={}
        job_id=jobs[0].id
        jobs[0].payload={'research_date':'2020-01-03','question':'Ignored for scheduled execution'}
    seen=[]
    async def analyze(request,deps):
        seen.append(request)
        return AnalysisResult(snapshot_as_of=request.snapshot.as_of,executive_summary='Briefing',
                              research_budget={'analysis_written':True,'model_calls':1})
    from betafolio.engine import EngineDependencies
    monkeypatch.setattr('betafolio.engine.analyze_portfolio',analyze)
    monkeypatch.setattr('betafolio.service.build_dependencies',lambda *a:EngineDependencies(model=object()))
    asyncio.run(execute(database,job_id))
    assert seen[0].research_as_of is None and seen[0].reporting_cutoff is None and seen[0].question==''
    # Yesterday is not backfilled after a restart before today's scheduled time.
    schedule_tick(database,timestamp+timedelta(hours=15))
    with database.transaction() as db:
        assert len(list(db.scalars(select(Job))))==1


def test_import_matching_requires_review_instead_of_price_guessing():
    stamp=datetime.now(timezone.utc)
    draft=parse_holdings_csv('Security Symbol,Number of Shares,Current Price\nVTI,40.5,999',stamp)
    assert draft.positions[0].quantity==40.5 and draft.positions[0].price is None
    with pytest.raises(ColumnMappingRequired):
        parse_holdings_csv('Security,Amount\nVTI,40.5',stamp)
    draft=parse_holdings_csv('Security,Amount\nVTI,40.5',stamp,{'symbol':0,'quantity':1})
    assert draft.positions[0].symbol=='VTI'
    draft=parse_holdings_csv('Ticker,Symbol,Qty\nVTI,VXUS,40.5',stamp,{'symbol':0,'quantity':2})
    assert draft.positions[0].symbol=='VTI'
    with pytest.raises(ValueError):
        parse_holdings_csv('Ticker,Price\nVTI,40.5',stamp,{'symbol':0,'quantity':1})


def test_signup_requires_admin_approval_and_creates_verified_account_only_on_completion(database,monkeypatch):
    monkeypatch.setattr('betafolio.web.ready',lambda _:True)
    monkeypatch.setattr('betafolio.web.email_ready',lambda _:True)
    database.set('settings',ProductionSettings(signup_enabled=True).model_dump(mode='json'))
    client=TestClient(create_app(database=database),base_url='http://127.0.0.1:8000')
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    password='A strong registration password'
    assert client.post('/api/auth/register',json={'email':'new@example.com'}).status_code==200
    assert client.post('/api/auth/register',json={'email':'new@example.com','password':password}).status_code==422
    assert client.post('/api/auth/complete-signup',json={'token':'not-approved','password':password}).status_code==400
    with database.transaction() as db:
        assert not db.scalar(select(User).where(User.email=='new@example.com'))
        invitation=db.scalar(select(SignupRequest).where(SignupRequest.email=='new@example.com'))
        assert invitation.status=='pending' and not invitation.token_hash
        request_id=invitation.id
        assert not db.scalar(select(Job).where(Job.kind=='signup_email'))
    assert client.get('/api/admin/signup-requests').status_code==401
    owner=account(database)
    assert browser(database,owner).post('/api/admin/signup-requests/'+request_id+'/approve',json={}).status_code==403
    admin=browser(database,account(database,'admin@example.com','admin'))
    path='/api/admin/signup-requests/'+request_id+'/approve'
    assert admin.post(path,json={}).status_code==200
    assert admin.post(path,json={}).status_code==409
    with database.transaction() as db:
        mail=db.scalar(select(Job).where(Job.signup_request_id==request_id))
        token=database.credentials.decrypt(mail.payload['token'])
        mail_id=mail.id
        assert mail.user_id is None
        assert not db.scalar(select(User).where(User.email=='new@example.com'))
    sent=[]
    async def send(*args,**kwargs):
        sent.append(args)
    monkeypatch.setattr('betafolio.mailer.send_mail',send)
    asyncio.run(execute(database,mail_id))
    assert sent[0][1]=='new@example.com'
    assert 'request was submitted' in sent[0][3] and 'ignore' in sent[0][3]
    assert '/#signup='+token in sent[0][3] and '24 hours' in sent[0][3]
    assert client.post('/api/auth/signup-info',json={'token':token}).json()['email']=='new@example.com'
    assert client.post('/api/auth/complete-signup',json={'token':token,'password':password}).status_code==200
    assert client.get('/api/session').json()['verified'] is True
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.post('/api/auth/complete-signup',json={'token':token,'password':password}).status_code==400
    with database.transaction() as db:
        user=db.scalar(select(User).where(User.email=='new@example.com'))
        assert user.verified and user.role=='user' and user.password_hash!=password
        assert db.get(SignupRequest,request_id).token_hash is None
    assert not admin.get('/api/admin/signup-requests').json()['requests']


def test_signup_denial_is_admin_only_and_never_queues_email(database, monkeypatch):
    from betafolio.database import Audit

    sent = []
    async def send(*args, **kwargs):
        sent.append(args)
    monkeypatch.setattr('betafolio.mailer.send_mail', send)
    # Rejecting requests must work even when delivery is unavailable.
    monkeypatch.setattr('betafolio.web.email_ready', lambda _: False)
    admin_id = account(database, 'admin@example.com', 'admin')
    user_id = account(database)
    with database.transaction() as db:
        invitation = SignupRequest(email='denied@example.com')
        db.add(invitation)
        db.flush()
        request_id = invitation.id
    path = '/api/admin/signup-requests/' + request_id + '/deny'
    assert browser(database, user_id).post(path, json={}).status_code == 403
    admin = browser(database, admin_id)
    csrf = admin.headers.pop('x-csrf-token')
    assert admin.post(path, json={}).status_code == 403
    admin.headers['x-csrf-token'] = csrf
    assert admin.post(path, json={}).status_code == 200
    assert admin.post(path, json={}).status_code == 200
    with database.transaction() as db:
        invitation = db.get(SignupRequest, request_id)
        assert invitation.status == 'denied'
        assert invitation.token_hash is None and invitation.expires is None
        assert invitation.approved_at is None
        assert db.scalar(select(Job).where(Job.signup_request_id == request_id)) is None
        assert db.scalar(select(User).where(User.email == 'denied@example.com')) is None
        audits = list(db.scalars(select(Audit).where(Audit.action == 'signup_denied:' + request_id)))
        assert len(audits) == 1 and audits[0].actor_id == admin_id
    assert sent == []
    listed = admin.get('/api/admin/signup-requests').json()['requests']
    assert next(row for row in listed if row['id'] == request_id)['status'] == 'denied'


def test_signup_denial_cannot_reverse_approval_or_an_email_in_progress(database):
    admin = browser(database, account(database, 'admin@example.com', 'admin'))
    with database.transaction() as db:
        approved = SignupRequest(email='approved@example.com', status='approved')
        pending = SignupRequest(email='pending@example.com')
        completed = SignupRequest(email='completed@example.com', status='completed')
        db.add_all([approved, pending, completed])
        db.flush()
        ids = approved.id, pending.id, completed.id
        db.add(Job(kind='signup_email', signup_request_id=pending.id, status='queued'))
    for request_id in ids[:2]:
        assert admin.post('/api/admin/signup-requests/' + request_id + '/deny', json={}).status_code == 409
    for request_id in (ids[2], str(uuid4())):
        assert admin.post('/api/admin/signup-requests/' + request_id + '/deny', json={}).status_code == 404
    with database.transaction() as db:
        assert db.get(SignupRequest, ids[0]).status == 'approved'
        assert db.get(SignupRequest, ids[1]).status == 'pending'


def test_expired_and_replaced_signup_links_cannot_create_accounts(database,monkeypatch):
    monkeypatch.setattr('betafolio.web.ready',lambda _:True)
    monkeypatch.setattr('betafolio.web.email_ready',lambda _:True)
    database.set('settings',ProductionSettings(signup_enabled=True).model_dump(mode='json'))
    admin=browser(database,account(database,'admin@example.com','admin'))
    with database.transaction() as db:
        invitation=SignupRequest(email='waiting@example.com')
        db.add(invitation)
        db.flush()
        request_id=invitation.id
    path='/api/admin/signup-requests/'+request_id+'/approve'
    assert admin.post(path,json={}).status_code==200
    with database.transaction() as db:
        first=db.scalar(select(Job).where(Job.signup_request_id==request_id))
        old_token=database.credentials.decrypt(first.payload['token'])
        first.status,first.payload='failed',{}
        db.get(SignupRequest,request_id).expires=now()-timedelta(minutes=1)
    client=TestClient(create_app(database=database),base_url='http://127.0.0.1:8000')
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.post('/api/auth/signup-info',json={'token':old_token}).status_code==400
    assert admin.post(path,json={}).status_code==200
    assert client.post('/api/auth/complete-signup',json={'token':old_token,'password':'A strong registration password'}).status_code==400
    with database.transaction() as db:
        assert not db.scalar(select(User).where(User.email=='waiting@example.com'))


def test_admin_mfa_enrollment_recovery_replay_and_reset_do_not_bypass_it(database,monkeypatch):
    clock_offset=30
    import pyotp
    owner=account(database,'admin@example.com','admin')
    with database.transaction() as db:
        token=new_session(db,db.get(User,owner),mfa=False)
    client=TestClient(create_app(database=database),base_url='http://127.0.0.1:8000')
    client.cookies.set('betafolio_session',token,domain='127.0.0.1',path='/')
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.get('/api/admin/settings').status_code==403
    stamp=datetime(2026,11,10,17,0,15,tzinfo=timezone.utc)
    monkeypatch.setattr('betafolio.accounts.now',lambda:stamp)
    setup=client.post('/api/account/mfa/setup',json={})
    assert setup.status_code==200
    assert setup.json()['qr'].startswith('data:image/svg+xml;base64,')
    resumed=client.post('/api/account/mfa/setup',json={})
    assert resumed.json()['setup_id']==setup.json()['setup_id']
    assert resumed.json()['manual_key']==setup.json()['manual_key']
    regenerated=client.post('/api/account/mfa/setup',json={'regenerate':True,'setup_id':setup.json()['setup_id']})
    assert regenerated.status_code==200 and regenerated.json()['setup_id']!=setup.json()['setup_id']
    old_code=pyotp.TOTP(setup.json()['manual_key']).at(stamp)
    assert client.post('/api/account/mfa/confirm',json={'code':old_code,'setup_id':setup.json()['setup_id']}).status_code==409
    setup=regenerated
    code=pyotp.TOTP(setup.json()['manual_key']).at(stamp+timedelta(seconds=clock_offset))
    assert client.post('/api/account/mfa/confirm',json={'code':code+'extra','setup_id':setup.json()['setup_id']}).status_code==400
    confirmed=client.post('/api/account/mfa/confirm',json={'code':code,'setup_id':setup.json()['setup_id']})
    assert confirmed.status_code==200
    with database.transaction() as db:
        user=db.get(User,owner)
        assert user.mfa_last_counter==int(stamp.timestamp())//30+clock_offset//30
        from betafolio.accounts import verify_mfa
        assert not verify_mfa(user,code,database.credentials)
    recovery=confirmed.json()['recovery_codes'][0]
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.get('/api/admin/settings').status_code==200
    assert client.post('/api/auth/logout',json={}).status_code==200
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.post('/api/auth/login',json={'email':'admin@example.com','password':'A strong sample password'}).status_code==401
    assert client.post('/api/auth/login',json={'email':'admin@example.com','password':'A strong sample password','code':recovery}).status_code==200
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    with database.transaction() as db:
        reset=issue_token(db,db.get(User,owner),'reset')
    assert client.post('/api/auth/reset',json={'token':reset,'password':'A different strong password'}).status_code==200
    assert client.get('/api/admin/settings').status_code==401
    client.headers['x-csrf-token']=client.get('/api/session').json()['csrf']
    assert client.post('/api/auth/login',json={'email':'admin@example.com','password':'A different strong password'}).status_code==401
    assert client.post('/api/auth/login',json={'email':'admin@example.com','password':'A different strong password','code':recovery}).status_code==401


@pytest.mark.parametrize('failure', ['model_setup', 'missing_model', 'empty_analysis', 'invalid_input'])
def test_research_failures_are_visible_and_never_publish_fallbacks(database, monkeypatch, failure):
    from betafolio.engine import EngineDependencies
    owner = account(database)
    monkeypatch.setattr('betafolio.jobs.ready', lambda _: True)
    job_id = enqueue(database, owner, {})
    if failure == 'invalid_input':
        with database.transaction() as db:
            db.get(User, owner).portfolio = {'invalid': 'PRIVATE INPUT'}

    def dependencies(*args):
        if failure == 'model_setup':
            raise ValueError('PRIVATE KEY AND PROVIDER PAYLOAD')
        return EngineDependencies(model=None if failure == 'missing_model' else object())

    async def no_analysis(request, deps):
        return AnalysisResult(snapshot_as_of=request.snapshot.as_of,
                              executive_summary='Fallback must not become a briefing')

    monkeypatch.setattr('betafolio.service.build_dependencies', dependencies)
    monkeypatch.setattr('betafolio.engine.analyze_portfolio', no_analysis)
    asyncio.run(execute(database, job_id))
    with database.transaction() as db:
        job = db.get(Job, job_id)
        assert job.status == 'failed' and job.error_code == 'research_failed'
        assert job.activity[0]['tool'] == 'run_setup'
        assert job.activity[-1]['status'] == 'failed'
        assert 'PRIVATE' not in str(job.activity)
        assert job.delivery is None
        assert job.consumed is (failure == 'empty_analysis')
    client = browser(database, owner)
    run = client.get('/api/runs/'+job_id).json()
    assert run['activity'][-1]['status'] == 'failed'
    assert client.get('/reports/'+job_id).status_code != 200


def test_replacing_enrolled_authenticator_requires_password_and_existing_mfa(database):
    import pyotp
    owner=account(database,'secured@example.com','admin')
    secret=pyotp.random_base32()
    with database.transaction() as db:
        db.get(User,owner).mfa_secret=database.credentials.encrypt(secret)
    client=browser(database,owner)
    assert client.post('/api/account/mfa/setup',json={}).status_code==403
    assert client.post('/api/account/mfa/setup',json={'password':'A strong sample password'}).status_code==403
    result=client.post('/api/account/mfa/setup',json={'password':'A strong sample password','code':pyotp.TOTP(secret).now()})
    assert result.status_code==200
    with database.transaction() as db:
        user=db.get(User,owner)
        assert database.credentials.decrypt(user.mfa_secret)==secret
        assert database.credentials.decrypt(user.pending_mfa)==result.json()['manual_key']


def test_totp_matching_tolerates_one_tick_and_rejects_replay(monkeypatch):
    import pyotp
    from betafolio.accounts import match_totp
    stamp=datetime(2026,11,10,17,0,15,tzinfo=timezone.utc)
    monkeypatch.setattr('betafolio.accounts.now',lambda:stamp)
    secret='JBSWY3DPEHPK3PXP'
    current=int(stamp.timestamp())//30
    for offset in (-1,0,1):
        code=pyotp.TOTP(secret).at(stamp+timedelta(seconds=30*offset))
        assert match_totp(secret,code)==current+offset
        assert match_totp(secret,code,after_counter=current+offset) is None
    assert match_totp(secret,pyotp.TOTP(secret).at(stamp+timedelta(seconds=60))) is None
    assert match_totp(secret,'123456extra') is None
