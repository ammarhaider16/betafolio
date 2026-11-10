"""Generate first-launch local deployment credentials; does not start anything."""
import base64
import os
from pathlib import Path
import secrets
from email_validator import EmailNotValidError, validate_email


def main():
    root = Path(__file__).resolve().parents[1]
    path = root/'.env'
    if path.exists():
        raise SystemExit('.env already exists. It was not overwritten.')
    email = input('Administrator email: ').strip().lower()
    try:
        email = validate_email(email,check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise SystemExit('Enter a valid administrator email address.') from None
    if '@' not in email or any(c in email for c in '\r\n$#\"\' '):
        raise SystemExit('Enter a valid administrator email address.')
    admin_password = secrets.token_urlsafe(24)
    values = {
        'POSTGRES_DB':'betafolio','POSTGRES_USER':'betafolio','POSTGRES_PASSWORD':secrets.token_urlsafe(32),
        'BETAFOLIO_ENCRYPTION_KEY':base64.urlsafe_b64encode(os.urandom(32)).decode(),
        'BETAFOLIO_BOOTSTRAP_ADMIN_EMAIL':email,'BETAFOLIO_BOOTSTRAP_ADMIN_PASSWORD':admin_password,
        'PORTFOLIO_PUBLIC_URL':'http://127.0.0.1:8000',
    }
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write('\n'.join(f'{key}={value}' for key,value in values.items())+'\n')
    path.chmod(0o600)
    print('Created .env. Keep it private and back up the encryption key separately.')
    print('Administrator email:',email)
    print('Initial administrator password:',admin_password)
    print('Next: docker compose up --build -d')
    print('Open http://127.0.0.1:8000 and enroll your authenticator.')


if __name__=='__main__':
    main()
