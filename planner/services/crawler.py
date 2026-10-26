"""Read notes through a normal, user-authenticated browser. No signature emulation."""
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone
from .debug import record
from .xhs_extract import extract_note, id_from_url, parse_published
from .xhs_pipeline import Budget, BudgetExceeded, Limits, run_collection

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


LINKS = 'a[href*="/explore/"], a[href*="/search_result/"]'
DETAIL = '#noteContainer, .note-container, .note-detail-mask .note-detail'


class XhsBrowser:
    """One search page and one reused detail page, each with its own response buffer."""
    def __init__(self, context):
        self.context = context
        self.search = context.new_page()
        self.detail = context.new_page()
        self.search_data, self.detail_data = [], []
        self.search_generation = 0
        self.sort_response_confirmed = False
        self.search_has_more = None
        self.auth_error = False
        self.report = {}
        self.search.on('response', self.capture_search)
        self.detail.on('response', self.capture_detail)

    def response_data(self, response):
        if '/api/sns/web/' not in response.url or not any(part in response.url for part in ('/feed', '/search/notes')):
            return None
        try:
            data = response.json()
            message = str(data.get('msg') or data.get('message') or '')
            if any(word in message for word in ('登录', '验证', '访问频次')) or response.status in (401, 403, 429):
                self.auth_error = True
            return data
        except Exception:
            record('crawl.response_failed', error='response_not_json')
            return None

    def capture_search(self, response):
        data = self.response_data(response)
        if data is None:
            return
        self.search_data.append(data)
        if '/search/notes' in response.url:
            self.search_generation += 1
            body = data.get('data', {})
            if isinstance(body, dict):
                value = body.get('has_more', body.get('hasMore'))
                if isinstance(value, bool):
                    self.search_has_more = value
            try:
                posted = response.request.post_data_json or {}
                if posted.get('sort') in ('time_descending', 'latest'):
                    self.sort_response_confirmed = True
            except Exception:
                pass

    def capture_detail(self, response):
        data = self.response_data(response)
        if data is not None:
            self.detail_data.append(data)

    def check_auth(self, page, budget=None):
        # Inspect actual visible login/challenge text; no bypass or signature simulation.
        visible = page.locator('body').inner_text(timeout=budget.timeout(2000) if budget else 2000)
        if self.auth_error or any(text in visible for text in ('扫码登录', '手机号登录', '登录后查看', '安全验证', '访问频次', '请完成验证')):
            raise LoginRequired('小红书需要登录或验证。请点击“小红书登录”完成后，再发送“继续”。')

    def open_search(self, query, budget):
        self.search.set_default_timeout(budget.timeout())
        url = f'https://www.xiaohongshu.com/search_result?keyword={quote(query)}&source=web_search_result_notes'
        self.search.goto(url, wait_until='domcontentloaded', timeout=budget.timeout(35000))
        try:
            self.search.locator(LINKS).first.wait_for(state='attached', timeout=budget.timeout(10000))
        except Exception:
            budget.check()
        self.check_auth(self.search, budget)

    def sort_latest(self, budget):
        self.search.set_default_timeout(budget.timeout())
        try:
            control = self.search.get_by_text('最新', exact=True).first
            if not control.is_visible():
                for label in ('综合', '筛选'):
                    trigger = self.search.get_by_text(label, exact=True).first
                    if trigger.is_visible():
                        trigger.hover(timeout=budget.timeout(2500))
                        break
            # Discard old comprehensive-search responses; wait for a new response or selected UI state.
            self.search_data.clear()
            self.search_has_more = None
            self.sort_response_confirmed = False
            generation = self.search_generation
            try:
                with self.search.expect_response(lambda response: '/api/sns/web/' in response.url and '/search/notes' in response.url,
                                                 timeout=budget.timeout(6000)):
                    control.click(timeout=budget.timeout(3000))
            except Exception:
                budget.check()  # UI confirmation below can still establish client-side sorting.
            try:
                self.search.wait_for_function('''() => [...document.querySelectorAll('*')].some(e =>
                    e.textContent.trim() === '最新' && (e.getAttribute('aria-selected') === 'true' ||
                    e.getAttribute('aria-checked') === 'true' || /(?:^|\\s)(?:active|selected|checked)(?:\\s|$)/.test(e.className || '')))''',
                    timeout=budget.timeout(4000))
                confirmed = True
            except Exception:
                confirmed = self.sort_response_confirmed and self.search_generation > generation
            self.check_auth(self.search, budget)
            return {'confirmed': confirmed, 'reason': 'selected_control_or_response' if confirmed else 'sort_not_confirmed_local_date_sort'}
        except LoginRequired:
            raise
        except BudgetExceeded:
            raise
        except Exception as exc:
            record('crawl.sort_fallback', reason='visible_control_failed', error_type=type(exc).__name__)
            return {'confirmed': False, 'reason': 'visible_control_failed_local_date_sort'}

    def search_snapshot(self, budget):
        self.search.set_default_timeout(budget.timeout())
        self.check_auth(self.search, budget)
        rows = self.search.locator(LINKS).evaluate_all('''els => els.map(e => {
            const card = e.closest('section.note-item, .note-item, [data-note-id]') || e;
            return {url:e.href, title:(card.querySelector('.title')?.textContent || '').trim(),
                    summary:(card.querySelector('.desc')?.textContent || '').trim()};
        })''')
        for row in rows:
            # Snapshots contain only objects bound to this ID, never the whole authenticated state.
            from .xhs_extract import matching_records
            target = id_from_url(row['url'])
            row['sources'] = [('search_response', {'note_id': target, 'payload': node})
                              for payload in self.search_data for _, node in matching_records(payload, target)]
        no_more = self.search_has_more is False or self.search.get_by_text(re.compile(r'^(没有更多了|没有更多内容|暂无搜索结果|没有找到相关内容)$')).count() > 0
        return rows, no_more

    def scroll(self, budget):
        self.search.set_default_timeout(budget.timeout())
        previous = self.search.locator(LINKS).evaluate_all('(els) => els.map(e => e.href).join("|")')
        self.search.locator(LINKS).last.scroll_into_view_if_needed(timeout=budget.timeout(3000)) if previous else None
        self.search.evaluate('''() => {
            const card = document.querySelector('section.note-item, .note-item');
            let parent = card?.parentElement;
            while (parent && parent !== document.body) {
                if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
                    parent.scrollTop = parent.scrollHeight; return;
                }
                parent = parent.parentElement;
            }
            window.scrollTo(0, document.documentElement.scrollHeight);
        }''')
        try:
            self.search.wait_for_function('''previous =>
                [...document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]')].map(e => e.href).join('|') !== previous ||
                /没有更多了|没有更多内容|暂无搜索结果/.test(document.body.innerText)''',
                arg=previous, timeout=budget.timeout(4500))
        except Exception:
            budget.check()  # A timeout means a stagnant round, not success or an endless wait.
        self.check_auth(self.search, budget)

    def detail_snapshot(self, candidate):
        target = candidate['note_id']
        initial = self.detail.evaluate('''() => {
            try { return JSON.parse(JSON.stringify(window.__INITIAL_STATE__?.note || {})); }
            catch (_) { return {}; }
        }''')
        visible = self.detail.evaluate('''target => {
            const root = document.querySelector('#noteContainer, .note-container, .note-detail-mask .note-detail');
            if (!root) return {};
            const id = root.getAttribute('data-note-id') || window.__INITIAL_STATE__?.note?.currentNoteId;
            const canonical = document.querySelector('link[rel="canonical"]')?.href || '';
            const bound = id === target || (!id && canonical.split('?')[0].endsWith('/' + target));
            if (!bound) return {};
            const video = root.querySelector('video');
            const images = root.querySelector('.image-container img, .image-list img, .swiper-slide img');
            return {noteId:target, title:root.querySelector('#detail-title')?.innerText || '',
                    desc:root.querySelector('#detail-desc, .note-content .desc, .note-scroller .desc')?.innerText || '',
                    publishTime:root.querySelector('.note-content .date, .bottom-container .date')?.innerText || '',
                    type:video ? 'video' : images ? 'normal' : 'unknown'};
        }''', target) if id_from_url(self.detail.url) == target else {}
        return [('detail_response', value) for value in self.detail_data] + [('initial_state', initial), ('visible_page', visible)]

    def read_detail(self, candidate, budget):
        self.detail.set_default_timeout(budget.timeout())
        self.detail_data.clear()
        self.detail.goto(candidate['url'], wait_until='domcontentloaded', timeout=budget.timeout(25000))
        self.check_auth(self.detail, budget)
        sources = self.detail_snapshot(candidate)
        note = extract_note(candidate['note_id'], candidate['url'], sources)
        from .xhs_extract import validate_note
        if validate_note(note):
            # Date readiness is independent of body readiness. New response/state/DOM can finish later.
            before = self.detail.evaluate('''() => JSON.stringify(window.__INITIAL_STATE__?.note || {}) +
                (document.querySelector('#noteContainer, .note-container')?.innerText || '')''')
            try:
                self.detail.wait_for_function('''before => JSON.stringify(window.__INITIAL_STATE__?.note || {}) +
                    (document.querySelector('#noteContainer, .note-container')?.innerText || '') !== before''',
                    arg=before, timeout=budget.timeout(6000))
            except Exception:
                budget.check()
            self.check_auth(self.detail, budget)
            sources = self.detail_snapshot(candidate)
        return sources


def crawl(query, city='', emit=lambda *_: None):
    """Collect, verify, judge and persist. Return actual ingestion counts plus diagnostics."""
    if city and city not in query:
        query = city + ' ' + query
    if not login_status()['logged_in']:
        record('crawl.login_required', query=query)
        raise LoginRequired('请点击“小红书登录”，在打开的浏览器中登录后，回来发送“继续”。')
    if not CRAWL_LOCK.acquire(timeout=3):
        raise RuntimeError('小红书正在采集另一项资料，请稍后重试。')
    adapter = None
    try:
        from playwright.sync_api import sync_playwright
        from .knowledge import existing_notes, judge_notes, save_notes

        record('crawl.start', query=query)
        limits = Limits.configured()
        budget = Budget(limits)
        with sync_playwright() as p:
            browser = p.chromium.launch(**browser_options(), timeout=budget.timeout(30000))
            context = None
            try:
                context = browser.new_context(storage_state=str(STATE_FILE), locale='zh-CN', timezone_id='Asia/Shanghai', service_workers='block')
                context.route('**/*', lambda route: route.abort() if route.request.resource_type in ('image', 'media') else route.continue_())
                adapter = XhsBrowser(context)
                return run_collection(adapter, query, existing=existing_notes, judge=judge_notes, save=save_notes,
                                      emit=emit, limits=limits, budget=budget)
            finally:
                for resource in (context, browser):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception as exc:
                            record('crawl.cleanup_failed', error_type=type(exc).__name__)
    except Exception as exc:
        if adapter and adapter.report:
            exc.crawl_report = adapter.report
        raise
    finally:
        CRAWL_LOCK.release()
