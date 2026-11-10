"""Tenant-owned persistence and durable jobs; local and hosted installs use Postgres."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from uuid import uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(12), default='user')
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80), default='America/New_York')
    schedule_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    email_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    allowance: Mapped[int | None] = mapped_column(Integer)
    portfolio: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    profile: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    report_length: Mapped[str] = mapped_column(String(12), default='detailed')
    mfa_secret: Mapped[str | None] = mapped_column(Text)
    pending_mfa: Mapped[str | None] = mapped_column(Text)
    recovery_hashes: Mapped[list] = mapped_column(JSON, default=list)
    mfa_last_counter: Mapped[int] = mapped_column(Integer, default=-1)


class Session(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mfa: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthToken(Base):
    __tablename__ = 'auth_tokens'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppState(Base):
    __tablename__ = 'app_state'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Credential(Base):
    __tablename__ = 'credentials'
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SignupRequest(Base):
    __tablename__ = 'signup_requests'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (UniqueConstraint('user_id', 'schedule_key', name='uq_user_schedule'),
        CheckConstraint("(kind = 'signup_email' AND signup_request_id IS NOT NULL AND user_id IS NULL) OR "
                        "(kind <> 'signup_email' AND user_id IS NOT NULL AND signup_request_id IS NULL)",
                        name='ck_job_owner'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    signup_request_id: Mapped[str | None] = mapped_column(ForeignKey('signup_requests.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(20), default='research')
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(36))
    schedule_key: Mapped[str | None] = mapped_column(String(80))
    allowance_day: Mapped[str | None] = mapped_column(String(10))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    activity: Mapped[list] = mapped_column(JSON, default=list)
    budget: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(50))
    delivery: Mapped[str | None] = mapped_column(String(20))
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RateBucket(Base):
    __tablename__ = 'rate_buckets'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, url=None):
        url = url or os.environ.get('DATABASE_URL')
        if not url:
            raise ValueError('DATABASE_URL is required. Start the local Postgres service with Docker Compose.')
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+psycopg://' + url.split('://', 1)[1]
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as db:
            yield db

    def get(self, key, default=None):
        with self.transaction() as db:
            row = db.get(AppState, key)
            return row.value if row else default

    def set(self, key, value):
        with self.transaction() as db:
            if self.engine.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
                statement = insert(AppState).values(key=key,value=value)
                db.execute(statement.on_conflict_do_update(index_elements=['key'],set_={'value':value}))
                return
            row = db.get(AppState, key)
            if row:
                row.value = value
            else:
                db.add(AppState(key=key, value=value))

    def audit(self, actor_id, action):
        with self.transaction() as db:
            db.add(Audit(actor_id=actor_id, action=action))

    def owned_job(self, user_id, job_id):
        with self.transaction() as db:
            return db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
