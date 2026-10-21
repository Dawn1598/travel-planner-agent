"""Scan staged Git blobs, not working files. Report paths/rules, never secret values."""
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(['git', *args], cwd=BASE)


def main():
    known = []
    env = BASE / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            name, sep, value = line.partition('=')
            if sep and re.search(r'(KEY|SECRET|TOKEN|PASSWORD)$', name.strip()):
                value = value.strip().strip('"\'')
                if len(value) >= 8:
                    known.append(value.encode())
    patterns = {
        'API/token literal': rb'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
        'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'authenticated URL': rb'https?://[^\s/]+:[^\s/]+@',
        'XHS access token': rb'xsec_token=[A-Za-z0-9_+/%=-]{20,}',
        'literal API key assignment': rb'''(?i)(?:AMAP_API_KEY|DEEPSEEK_API_KEY|DJANGO_SECRET_KEY)\s*=\s*["'][A-Za-z0-9_+/-]{20,}["']''',
    }
    failures = []
    paths = git('ls-files', '-z').decode('utf-8').split('\0')
    for name in filter(None, paths):
        path = Path(name)
        if (any(part in ('.runtime', '.venv', '.idea', '.vscode', '__pycache__') for part in path.parts)
                or (path.name.startswith('.env') and path.name != '.env.example')
                or path.suffix in ('.sqlite3', '.db', '.pem', '.key', '.p12', '.pfx', '.log', '.zip', '.tgz')
                or '.sqlite3-' in name or name.endswith('.tar.gz')
                or path.name in ('xhs-state.json', 'smoke-session.json')):
            failures.append((name, 'private file'))
        blob = git('show', ':' + name)
        if any(secret in blob for secret in known):
            failures.append((name, 'known local secret'))
        for label, pattern in patterns.items():
            if re.search(pattern, blob):
                failures.append((name, label))
    for name, reason in failures:
        print(f'BLOCKED: {name} [{reason}]')
    if failures:
        raise SystemExit(1)
    print(f'PASS: {len([p for p in paths if p])} staged files; no known secrets or blocked files detected.')


if __name__ == '__main__':
    main()
