"""Binary-safe local Compose backups; restore requires explicit operator confirmation."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path',type=Path)
    parser.add_argument('--restore',action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prefix = ['docker','compose','exec','-T','db','sh','-c']
    if args.restore:
        if input('This replaces the current database. Stop web/worker first. Type RESTORE: ')!='RESTORE':
            raise SystemExit('Restore cancelled')
        with args.path.open('rb') as source:
            subprocess.run([*prefix,'exec pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                           stdin=source,check=True,cwd=root)
    else:
        with args.path.open('xb') as target:
            subprocess.run([*prefix,'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                           stdout=target,check=True,cwd=root)
    print('Restore complete.' if args.restore else 'Backup written. Protect this file and the separate encryption key.')


if __name__=='__main__':
    main()
