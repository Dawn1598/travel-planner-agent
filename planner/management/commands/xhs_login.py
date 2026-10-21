import json
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from planner.services.crawler import LOGIN_FILE, STATE_FILE, browser_options


class Command(BaseCommand):
    help = '打开小红书登录窗口，并将登录状态保存在本地。'

    def handle(self, *args, **options):
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(settings.RUNTIME_DIR / 'xhs-profile'), **{**browser_options(), 'headless': False}, locale='zh-CN',
                    viewport={'width': 1120, 'height': 780})
                page = context.pages[0] if context.pages else context.new_page()
                page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded', timeout=45000)
                deadline = time.time() + 900
                while time.time() < deadline and context.pages:
                    try:
                        temporary = STATE_FILE.with_suffix('.tmp')
                        context.storage_state(path=str(temporary))
                        temporary.replace(STATE_FILE)
                        LOGIN_FILE.write_text(json.dumps({'running': True, 'heartbeat': time.time()}), encoding='utf-8')
                        page.wait_for_timeout(2000)
                    except Exception:
                        break
                context.close()
        except Exception as exc:
            from planner.services.llm import safe_error
            LOGIN_FILE.write_text(json.dumps({'running': False, 'error': safe_error(exc)}), encoding='utf-8')
        finally:
            if LOGIN_FILE.exists():
                try:
                    data = json.loads(LOGIN_FILE.read_text(encoding='utf-8'))
                    data['running'] = False
                    LOGIN_FILE.write_text(json.dumps(data), encoding='utf-8')
                except (ValueError, OSError):
                    pass
