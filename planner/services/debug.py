"""Opt-in, per-run JSONL tracing shared by async code and tool threads."""
import asyncio
import json
import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

_current = ContextVar('travel_debug_trace', default=None)
_write_lock = threading.Lock()
_sensitive = re.compile(r'api.?key|authorization|cookie|password|secret|(?:^|_)(?:access_token|refresh_token|xsec_token|token|session|session_key|signature)(?:$|_)', re.I)
_private_output = {'reasoning_content', 'reasoning', 'thinking'}
_sensitive_names = {'key', 'apikey', 'authorization', 'cookie', 'cookies', 'password', 'secret',
                    'token', 'accesstoken', 'refreshtoken', 'xsectoken', 'session', 'sessionkey', 'websession', 'signature', 'sign'}
MAX_FILE_BYTES = 20 * 1024 * 1024


def sanitize(value, depth=0):
    if depth > 14:
        return '[嵌套过深，已截断]'
    if isinstance(value, dict):
        return {str(k): ('[不记录]' if str(k).lower() in _private_output else
                         '[已隐藏]' if _sensitive.search(str(k)) or re.sub(r'[^a-z0-9]', '', str(k).lower()) in _sensitive_names else sanitize(v, depth + 1))
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        result = [sanitize(v, depth + 1) for v in value[:100]]
        if len(value) > 100:
            result.append(f'[其余 {len(value) - 100} 项已省略]')
        return result
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    for name in ('DEEPSEEK_API_KEY', 'AMAP_API_KEY', 'SECRET_KEY'):
        secret = getattr(settings, name, '')
        if secret:
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[已隐藏]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [已隐藏]', text)
    text = re.sub(r'''(?i)([?&](?:key|api_key|token|xsec_token|sign|signature|session|web_session)=)[^&#\s"'<>]*''', r'\1[已隐藏]', text)
    text = re.sub(r'''(?i)(["'](?:key|api_key|apikey|authorization|cookie|password|secret|token|session|web_session|xsec_token)["']\s*:\s*["'])[^"']*''', r'\1[已隐藏]', text)
    text = re.sub(r'(?im)^(authorization|cookie|set-cookie):[^\r\n]*', r'\1: [已隐藏]', text)
    return text[:24000] + (f'\n[原文 {len(text)} 字符，已截断]' if len(text) > 24000 else '')


def log_path(run_id):
    return settings.RUNTIME_DIR / 'debug' / (str(run_id) + '.jsonl')


class RunTrace:
    def __init__(self, run_id):
        self.run_id = run_id

    def write(self, kind, data):
        try:
            from planner.models import Run
            if not Run.objects.filter(pk=self.run_id, debug_enabled=True).exists():
                return
            entry = {'at': timezone.now().isoformat(), 'kind': kind, 'data': sanitize(data)}
            line = json.dumps(entry, ensure_ascii=False, default=str)
            if len(line) > 100000:
                entry['data'] = {'truncated': True, 'preview': line[:100000]}
                line = json.dumps(entry, ensure_ascii=False)
            path = log_path(self.run_id)
            with _write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size >= MAX_FILE_BYTES:
                    return
                with path.open('ab') as handle:
                    handle.write((line + '\n').encode('utf-8'))
        except Exception as exc:
            # Debugging must never break a planning task or print raw credentials.
            logging.getLogger(__name__).warning('Debug log unavailable (%s)', type(exc).__name__)


@contextmanager
def trace_context(trace):
    token = _current.set(trace)
    try:
        yield
    finally:
        _current.reset(token)


def record(kind, **data):
    trace = _current.get()
    if trace is not None:
        trace.write(kind, data)


async def record_async(kind, **data):
    if _current.get() is not None:
        await asyncio.to_thread(record, kind, **data)


def read_entries(run_id, cursor=0):
    path = log_path(run_id)
    if not path.exists():
        return {'entries': [], 'cursor': 0, 'has_more': False}
    entries = []
    with _write_lock, path.open('rb') as handle:
        size = path.stat().st_size
        handle.seek(min(max(cursor, 0), size))
        start = handle.tell()
        while len(entries) < 100 and handle.tell() - start < 1024 * 1024:
            line = handle.readline()
            if not line:
                break
            try:
                entries.append(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                continue
        cursor = handle.tell()
    return {'entries': entries, 'cursor': cursor, 'has_more': cursor < size}
