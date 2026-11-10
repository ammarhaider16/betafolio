"""Local/hosted service commands and the standalone research engine."""
import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path


def migrate(database):
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option('script_location', str(Path(__file__).parent/'migrations'))
    with database.engine.begin() as connection:
        # Deployment starts may overlap; one schema upgrade at a time.
        if database.engine.dialect.name == 'postgresql':
            from sqlalchemy import text
            connection.execute(text('SELECT pg_advisory_xact_lock(736452910)'))
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'head')


def bootstrap(database, email=None, password=None):
    from email_validator import validate_email
    from sqlalchemy import select
    from .accounts import check_password
    from .database import User
    email = email or os.environ.get('BETAFOLIO_BOOTSTRAP_ADMIN_EMAIL')
    password = password or os.environ.get('BETAFOLIO_BOOTSTRAP_ADMIN_PASSWORD')
    if not email or not password:
        return False
    email = validate_email(email,check_deliverability=False).normalized.lower()
    with database.transaction() as db:
        # Serialize bootstrap across replicas, without modifying an existing admin.
        if database.engine.dialect.name == 'postgresql':
            from sqlalchemy import text
            db.execute(text('SELECT pg_advisory_xact_lock(736452911)'))
        if db.scalar(select(User.id).where(User.role == 'admin')):
            return False
        if db.scalar(select(User.id).where(User.email == email)):
            raise ValueError('Bootstrap address is already registered. Choose a different administrator address.')
        db.add(User(email=email,password_hash=check_password(password),role='admin',verified=True))
    return True


def parser():
    cli = argparse.ArgumentParser(description='Betafolio research and agent command center')
    cli.add_argument('--data-dir',type=Path,help='Local encryption-key directory')
    commands = cli.add_subparsers(dest='command',required=True)
    serve = commands.add_parser('serve',help='Start the authenticated web application')
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=int(os.environ.get('PORT','8000')))
    serve.add_argument('--server',action='store_true')
    commands.add_parser('worker',help='Run the queue, price refresh and daily scheduler')
    commands.add_parser('migrate',help='Apply the database schema')
    admin = commands.add_parser('bootstrap-admin',help='Create the first administrator only')
    admin.add_argument('--email',required=True)
    recover = commands.add_parser('recover-admin',help='Operator-only reset of an administrator authenticator')
    recover.add_argument('--email',required=True)
    run = commands.add_parser('analyze',help='Run the standalone engine without a dashboard account')
    run.add_argument('--request',type=Path)
    run.add_argument('--demo',action='store_true')
    run.add_argument('--offline',action='store_true')
    run.add_argument('--format',choices=['json','text','html'],default='json')
    run.add_argument('--as-of')
    schema = commands.add_parser('schema')
    schema.add_argument('name',choices=['request','result','submission'])
    return cli


async def analyze(args):
    from .models import AnalysisRequest
    from .engine import analyze_portfolio, EngineDependencies
    from .demo import demo_snapshot
    from .delivery import render_report
    if args.demo:
        request = AnalysisRequest(snapshot=demo_snapshot())
    elif args.request:
        request = AnalysisRequest.model_validate_json(args.request.read_text(encoding='utf-8'))
    else:
        raise ValueError('Provide --request or --demo. Web portfolios require user authentication.')
    if args.as_of:
        from .research_clock import research_timestamp
        request = AnalysisRequest.model_validate({**request.model_dump(), 'research_as_of':research_timestamp(args.as_of,'America/New_York')})
    if args.offline or args.demo:
        deps = EngineDependencies(offline=True)
    else:
        from .web import open_database
        from .production import settings
        from .service import build_dependencies
        database = open_database(server=os.environ.get('PORTFOLIO_SERVER_MODE')=='1')
        deps = build_dependencies(settings(database).research,database.credentials)
    result = await analyze_portfolio(request,deps)
    html, text = render_report(result)
    print(result.model_dump_json(indent=2) if args.format=='json' else html if args.format=='html' else text)
    return 1 if result.status=='failed' else 0


def main():
    args = parser().parse_args()
    if args.data_dir:
        os.environ['PORTFOLIO_DATA_DIR'] = str(args.data_dir.resolve())
    stage = 'loading application dependencies'
    try:
        if args.command=='schema':
            from .models import AnalysisRequest, AnalysisResult
            from .web import RunSubmission
            print(json.dumps({'request':AnalysisRequest,'result':AnalysisResult,'submission':RunSubmission}[args.name].model_json_schema(),indent=2))
            return 0
        if args.command=='analyze':
            return asyncio.run(analyze(args))
        from .web import open_database
        server = getattr(args,'server',False) or os.environ.get('PORTFOLIO_SERVER_MODE')=='1'
        stage = 'opening database configuration and encryption key'
        database = open_database(args.data_dir,server)
        stage = 'applying database migrations'
        if args.command=='migrate':
            migrate(database)
            return 0
        if args.command=='bootstrap-admin':
            created = bootstrap(database,args.email,getpass.getpass('Administrator password (12+ characters): '))
            print('Administrator created. Sign in and enroll an authenticator.' if created else 'An administrator already exists; no changes made.')
            return 0
        if args.command=='recover-admin':
            from sqlalchemy import select
            from .database import User
            from .accounts import revoke_sessions
            confirmation = input('Type RESET to revoke this administrator\'s MFA and sessions: ')
            if confirmation!='RESET':
                return 1
            with database.transaction() as db:
                user = db.scalar(select(User).where(User.email==args.email.strip().lower(),User.role=='admin').with_for_update())
                if not user:
                    raise ValueError('Administrator not found')
                user.mfa_secret,user.pending_mfa,user.recovery_hashes,user.mfa_last_counter = None,None,[],-1
                revoke_sessions(db,user.id)
            database.audit(None,'operator_mfa_recovery')
            print('Sessions revoked. Sign in and enroll a new authenticator.')
            return 0
        if args.command=='worker':
            from .jobs import worker
            migrate(database)
            stage = 'running the background worker'
            return asyncio.run(worker(database))
        if args.host not in {'localhost','127.0.0.1','::1'} and not server:
            # Docker binds internally on all interfaces while its published port is loopback-only.
            if os.environ.get('BETAFOLIO_CONTAINER_LOCAL')!='1':
                raise ValueError('Public listening requires --server and an HTTPS public URL')
        migrate(database)
        stage = 'creating the initial administrator'
        bootstrap(database)
        from .web import create_app
        import uvicorn
        stage = 'starting the web server'
        uvicorn.run(create_app(args.data_dir,server=server,database=database),host=args.host,port=args.port,
                    workers=1,access_log=False,log_level='critical',proxy_headers=False)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Operator errors must not echo database URLs, credentials or provider payloads.
        reason = f'{type(exc).__name__}; see README setup instructions.'
        if type(exc).__name__ == 'OperationalError':
            if 'password authentication failed' in str(exc.orig).lower():
                reason = ('Database password authentication failed. An existing Postgres volume keeps its original '
                          'password when .env changes. See docs/deployment.md: Local database password mismatch. '
                          'Do not delete the database volume.')
            else:
                reason = 'Database connection failed. Check that Postgres is reachable and DATABASE_URL is correct.'
        print(f'Betafolio failed while {stage}: {reason}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
