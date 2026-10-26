"""Package code and a consistent local data snapshot; never print secrets."""
import hashlib
import argparse
import json
import secrets
import sqlite3
import tarfile
from pathlib import Path

from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent.parent
OUTPUT = BASE / '.runtime' / 'deployment'
OUTPUT.mkdir(parents=True, exist_ok=True)
parser = argparse.ArgumentParser(description='Build code and PRIVATE deployment archives')
parser.add_argument('--hosts', required=True, help='Comma-separated server domains/IPs for Django ALLOWED_HOSTS')
args = parser.parse_args()

with sqlite3.connect(BASE / 'db.sqlite3') as source, sqlite3.connect(OUTPUT / 'db.sqlite3') as target:
    source.backup(target)
    target.execute("UPDATE planner_run SET status='error', error='本地任务未迁移执行，请重新发送消息。' WHERE status='running'")
    target.commit()

local = dotenv_values(BASE / '.env')
config = {key: value for key, value in local.items() if key in (
    'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL', 'AMAP_API_KEY',
    'EMBEDDING_MODEL', 'MAX_AGENT_STEPS', 'MAX_REVISIONS', 'XHS_CANDIDATE_LIMIT',
    'XHS_TARGET_NEW', 'XHS_MAX_VISITS', 'XHS_TIME_BUDGET_SECONDS', 'XHS_RETRIES',
    'XHS_STAGNANT_ROUNDS', 'XHS_REFRESH_HOURS') and value is not None}
if not config.get('DEEPSEEK_API_KEY') or not config.get('AMAP_API_KEY'):
    raise RuntimeError('Missing backend API configuration')
config.update(DJANGO_DEBUG='0', DJANGO_SECRET_KEY=secrets.token_hex(48),
              DJANGO_ALLOWED_HOSTS=args.hosts + ',127.0.0.1,localhost', XHS_LOGIN_ENABLED='0',
              XHS_BROWSER_EXECUTABLE='/opt/travel-planner/shared/browser/opt/google/chrome/chrome')
# JSON string quoting is valid python-dotenv syntax for these scalar values.
(OUTPUT / '.env').write_text(''.join(f'{key}={json.dumps(str(value))}\n' for key, value in config.items()), encoding='utf-8')

with tarfile.open(OUTPUT / 'code.tar.gz', 'w:gz') as archive:
    for directory in ('config', 'planner', 'static', 'templates', 'scripts', 'deploy'):
        for path in (BASE / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                archive.add(path, arcname=path.relative_to(BASE).as_posix())
    for filename in ('manage.py', 'requirements.txt', 'README.md', 'PROMPTS.md'):
        archive.add(BASE / filename, arcname=filename)

with tarfile.open(OUTPUT / 'private.tar.gz', 'w:gz') as archive:
    for filename in ('db.sqlite3', '.env'):
        archive.add(OUTPUT / filename, arcname=filename)
    state = BASE / '.runtime' / 'xhs-state.json'
    if state.exists():
        archive.add(state, arcname='runtime/xhs-state.json')
    embeddings = BASE / '.runtime' / 'embeddings'
    if embeddings.exists():
        archive.add(embeddings, arcname='runtime/embeddings')

report = {name: {'bytes': (OUTPUT / name).stat().st_size,
                 'sha256': hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest()}
          for name in ('code.tar.gz', 'private.tar.gz')}
(OUTPUT / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
