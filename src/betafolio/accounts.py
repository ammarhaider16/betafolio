"""Self-contained accounts, revocable sessions, and QR-based MFA."""
import hashlib
import os
import secrets
from datetime import timedelta
from pathlib import Path
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
import pyotp
from sqlalchemy import delete, select
from .database import AuthToken, Credential, RateBucket, Session, SignupRequest, User, now

PASSWORDS = PasswordHash.recommended()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encryption_key(directory, server):
    key = os.environ.get('BETAFOLIO_ENCRYPTION_KEY')
    if key:
        return key.encode()
    if server:
        raise ValueError('Set BETAFOLIO_ENCRYPTION_KEY before starting production.')
    path = Path(directory) / 'encryption.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open('xb') as stream:
                stream.write(Fernet.generate_key())
            path.chmod(0o600)
        except FileExistsError:
            pass
    return path.read_bytes().strip()


class Credentials:
    NAMES = {'model_api_key_openai', 'model_api_key_anthropic', 'model_api_key_custom',
             'tavily_api_key', 'exa_api_key', 'perplexity_api_key', 'email_api_key', 'smtp_password'}

    def __init__(self, database, key):
        self.database, self.cipher = database, Fernet(key)
        self.csrf_key = hashlib.sha256(b'betafolio-csrf:'+key).digest()

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()

    def get(self, name):
        with self.database.transaction() as db:
            row = db.get(Credential, name)
            return self.decrypt(row.ciphertext) if row else None

    def set(self, name, value):
        if name not in self.NAMES or not isinstance(value, str) or not 0 < len(value) <= 4096:
            raise ValueError('Invalid credential')
        with self.database.transaction() as db:
            db.merge(Credential(name=name, ciphertext=self.encrypt(value), saved_at=now()))

    def delete(self, name):
        if name not in self.NAMES:
            raise ValueError('Invalid credential')
        with self.database.transaction() as db:
            db.execute(delete(Credential).where(Credential.name == name))

    def status(self):
        with self.database.transaction() as db:
            return {row.name: {'present': True, 'saved_at': row.saved_at.isoformat()}
                    for row in db.scalars(select(Credential))}


def check_password(value):
    if not 12 <= len(value) <= 128:
        raise ValueError('Use a password between 12 and 128 characters')
    return PASSWORDS.hash(value)


def new_session(db, user, *, mfa=False):
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=digest(token), user_id=user.id, expires=now()+timedelta(hours=12), mfa=mfa))
    return token


def revoke_sessions(db, user_id):
    db.execute(delete(Session).where(Session.user_id == user_id))


def issue_token(db, user, kind):
    db.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.kind == kind))
    token = secrets.token_urlsafe(32)
    db.add(AuthToken(token_hash=digest(token), user_id=user.id, kind=kind,
                     expires=now()+timedelta(minutes=30 if kind == 'reset' else 1440)))
    return token


def consume_token(db, value, kind):
    token = db.scalar(select(AuthToken).where(AuthToken.token_hash == digest(value),
                      AuthToken.kind == kind, AuthToken.expires > now()).with_for_update())
    if token is None:
        raise ValueError('This link has expired or has already been used')
    user = db.get(User, token.user_id)
    db.delete(token)
    if user is None or user.suspended:
        raise ValueError('Account unavailable')
    return user


def approved_signup(db, token):
    invitation = db.scalar(select(SignupRequest).where(
        SignupRequest.token_hash == digest(token), SignupRequest.status == 'approved',
        SignupRequest.expires > now()).with_for_update())
    if invitation is None:
        raise ValueError('This account-creation link has expired or has already been used. Request a new link from the administrator.')
    return invitation


def match_totp(secret, code, *, after_counter=-1):
    code = code.strip()
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int(now().timestamp()) // 30
    for counter in (current, current-1, current+1):
        if counter > after_counter and secrets.compare_digest(totp.at(counter*30), code):
            return counter
    return None


def verify_mfa(user, code, credentials):
    if not user.mfa_secret:
        return False
    code = code.strip()
    counter = match_totp(credentials.decrypt(user.mfa_secret), code, after_counter=user.mfa_last_counter)
    if counter is not None:
        user.mfa_last_counter = counter
        return True
    hashes = list(user.recovery_hashes)
    supplied = digest(code)
    if supplied in hashes:
        hashes.remove(supplied)
        user.recovery_hashes = hashes
        return True
    return False


def rate_limit(database, key, limit, seconds):
    # Fixed-window keys keep inserts independent across windows; unique-key races are retried.
    from sqlalchemy.exc import IntegrityError
    window = int(now().timestamp()) // seconds
    hashed = digest(f'{key}:{window}')
    for attempt in range(2):
        try:
            with database.transaction() as db:
                row = db.scalar(select(RateBucket).where(RateBucket.key == hashed).with_for_update())
                if row is None:
                    db.add(RateBucket(key=hashed, count=1, expires=now()+timedelta(seconds=seconds*2)))
                elif row.count >= limit:
                    return False
                else:
                    row.count += 1
            return True
        except IntegrityError:
            if attempt:
                return False
    return False


def csrf_token(cookie, credentials):
    import hmac
    return hmac.new(credentials.csrf_key, ('csrf:'+cookie).encode(), hashlib.sha256).hexdigest()
