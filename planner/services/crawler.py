"""Read notes through a normal, user-authenticated browser. No signature emulation."""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from django.conf import settings
from django.utils import timezone
from .debug import record

CRAWL_LOCK = threading.Lock()
STATE_FILE = settings.RUNTIME_DIR / 'xhs-state.json'
LOGIN_FILE = settings.RUNTIME_DIR / 'xhs-login.json'


class LoginRequired(RuntimeError):
    pass


def login_status():
    error = ''
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        now = timezone.now().timestamp()
        authenticated = any(c.get('name') == 'web_session' and (c.get('expires', -1) < 0 or c['expires'] > now) for c in state.get('cookies', []))
    except (OSError, ValueError, KeyError):
        authenticated = False
    try:
        status = json.loads(LOGIN_FILE.read_text(encoding='utf-8'))
        opening = now_seconds() - status.get('heartbeat', 0) < 15 and status.get('running', False)
        error = status.get('error', '') if not opening else ''
    except (OSError, ValueError):
        opening = False
    return {'logged_in': authenticated, 'opening': opening, 'error': error}


def now_seconds():
    return timezone.now().timestamp()


def browser_options():
    options = {'headless': True}
    if settings.XHS_BROWSER_EXECUTABLE:
        return {**options, 'executable_path': settings.XHS_BROWSER_EXECUTABLE}
    if os.name == 'nt' and Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').exists():
        options['channel'] = 'msedge'
    return options


def parse_published(value):
    """Unknown dates stay unknown. A modified timestamp is never a publish date."""
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            timestamp = float(value)
            if timestamp > 10**12:
                timestamp /= 1000
            if timestamp < 946684800:
                return None
            result = datetime.fromtimestamp(timestamp, tz=timezone.get_current_timezone())
        else:
            text = str(value).strip()
            if any(word in text for word in ('编辑', '修改', '更新于')):
                return None
            relative = re.search(r'(\d+)\s*(分钟|小时|天)前', text)
            if relative:
                number = int(relative[1])
                delta = {'分钟': timedelta(minutes=number), '小时': timedelta(hours=number), '天': timedelta(days=number)}[relative[2]]
                result = timezone.now() - delta
            elif '昨天' in text:
                result = timezone.now() - timedelta(days=1)
            elif '今天' in text or '刚刚' in text:
                result = timezone.now()
            else:
                match = re.search(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})', text)
                if not match:
                    return None
                result = timezone.make_aware(datetime(*map(int, match.groups())))
        return result if result <= timezone.now() + timedelta(minutes=5) else None
    except (ValueError, OverflowError, OSError):
        return None


def walk_notes(value):
    if isinstance(value, dict):
        if ('desc' in value or 'title' in value) and any(k in value for k in ('noteId', 'note_id')):
            yield value
        for child in value.values():
            yield from walk_notes(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_notes(child)


def note_from_data(data, url, city):
    note_id = str(data.get('noteId') or data.get('note_id') or '')
    title = str(data.get('title') or data.get('displayTitle') or '').strip()
    desc = str(data.get('desc') or '').strip()
    if not note_id or len(desc) < 25:
        return None
    published = parse_published(data.get('time') or data.get('publishTime') or data.get('publish_time'))
    return {'note_id': note_id, 'title': title or desc[:60], 'content': desc,
            'url': url, 'city': city, 'published_at': published,
            'content_hash': hashlib.sha256((title + '\n' + desc).encode()).hexdigest()}


def crawl(query, city='', emit=lambda *_: None):
    if city and city not in query:
        query = city + ' ' + query
    if not login_status()['logged_in']:
        record('crawl.login_required', query=query)
        raise LoginRequired('请点击“小红书登录”，在打开的浏览器中登录后，回来发送“继续”。')
    if not CRAWL_LOCK.acquire(timeout=3):
        raise RuntimeError('小红书正在采集另一项资料，请稍后重试。')
    from playwright.sync_api import sync_playwright
    notes = {}
    try:
        record('crawl.start', query=query, city=city, max_notes=settings.XHS_MAX_NOTES)
        emit('crawl', f'正在小红书查找：{query}')
        with sync_playwright() as p:
            browser = p.chromium.launch(**browser_options())
            context = browser.new_context(storage_state=str(STATE_FILE), locale='zh-CN')
            page = context.new_page()
            page.set_default_timeout(12000)
            captured = []

            def capture(response):
                if '/api/sns/web/' in response.url and any(s in response.url for s in ('/feed', '/search/notes')):
                    try:
                        captured.append(response.json())
                    except Exception:
                        pass

            page.on('response', capture)
            search = f'https://www.xiaohongshu.com/search_result?keyword={quote(query)}&source=web_search_result_notes'
            page.goto(search, wait_until='domcontentloaded', timeout=35000)
            record('crawl.search_opened', url=search, title=page.title())
            try:
                page.locator('a[href*="/explore/"], a[href*="/search_result/"]').first.wait_for(timeout=12000)
            except Exception:
                body = page.locator('body').inner_text()[:1500]
                if any(s in body for s in ('扫码登录', '手机号登录', '登录后查看', '安全验证', '访问频次')):
                    raise LoginRequired('小红书需要登录或验证。请点击“小红书登录”完成后，再发送“继续”。')
            # Use the website's visible sorting control when available.
            try:
                page.get_by_text('综合', exact=True).first.hover(timeout=2000)
                page.get_by_text('最新', exact=True).first.click(timeout=2000)
                page.wait_for_timeout(1200)
            except Exception as exc:
                record('crawl.sort_fallback', message='最新排序控件不可用，使用当前结果并保留实际发布时间', error_type=type(exc).__name__)
            links = page.locator('a[href*="/explore/"], a[href*="/search_result/"]').evaluate_all('(els) => els.map(e => e.href)')
            candidates = []
            for link in links:
                if re.search(r'/(?:explore|search_result)/[a-fA-F0-9]{24}', link) and urlparse(link).hostname in ('www.xiaohongshu.com', 'xiaohongshu.com'):
                    if link not in candidates:
                        candidates.append(link)
            record('crawl.candidates', count=len(candidates), urls=candidates[:settings.XHS_MAX_NOTES])
            for link in candidates[:settings.XHS_MAX_NOTES]:
                try:
                    captured.clear()
                    page.goto(link, wait_until='domcontentloaded', timeout=25000)
                    try:
                        page.locator('#detail-desc, .note-content .desc, .note-scroller .desc').first.wait_for(timeout=7000)
                    except Exception:
                        pass
                    initial = page.evaluate('''() => {
                        const s = window.__INITIAL_STATE__;
                        if (!s) return {};
                        try { return JSON.parse(JSON.stringify(s.note || {})); }
                        catch (_) { return {}; }
                    }''')
                    found = False
                    for data in walk_notes([initial, *captured]):
                        note = note_from_data(data, link, city)
                        if note:
                            notes[note['note_id']] = note
                            record('crawl.note', note=note, extraction='page_state_or_response')
                            found = True
                    if not found:
                        desc = page.locator('#detail-desc, .note-content .desc, .note-scroller .desc').first
                        if desc.count():
                            content = desc.inner_text().strip()
                            title = page.locator('#detail-title').inner_text() if page.locator('#detail-title').count() else content[:60]
                            dates = page.locator('.note-content .date, .bottom-container .date')
                            date = dates.first.inner_text() if dates.count() else None
                            note_id = re.search(r'/(?:explore|search_result)/([a-fA-F0-9]{24})', link)[1]
                            note = note_from_data({'noteId': note_id, 'title': title, 'desc': content, 'publishTime': date}, link, city)
                            if note:
                                notes[note_id] = note
                                record('crawl.note', note=note, extraction='visible_page')
                except LoginRequired:
                    raise
                except Exception as exc:
                    record('crawl.note_error', url=link, error_type=type(exc).__name__, message=str(exc))
                    continue
            context.storage_state(path=str(STATE_FILE))
            browser.close()
        emit('crawl', f'获取 {len(notes)} 篇可读取的攻略' if notes else '未获取到可读取的正文，可能需要重新登录或完成验证')
        record('crawl.finish', count=len(notes), note_ids=list(notes))
        return list(notes.values())
    finally:
        CRAWL_LOCK.release()
