"""Local development diagnostics; never prints credentials or authenticated URLs."""
import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()


async def amap():
    from planner.services.agent import amap_tools, content_text
    tools = await amap_tools()
    result = await next(t for t in tools if t.name == 'maps_text_search').ainvoke({'keywords': '西湖', 'city': '杭州', 'citylimit': True})
    data = json.loads(content_text(result))
    pois = data.get('pois', [])
    print(json.dumps({'count': len(pois), 'fields': list(pois[0].keys()) if pois else [],
                      'first': pois[0] if pois else data}, ensure_ascii=True))
    if pois:
        detail = await next(t for t in tools if t.name == 'maps_search_detail').ainvoke({'id': pois[0]['id']})
        print(content_text(detail).encode('unicode_escape').decode()[:5000])


def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        for channel in ('chromium', 'msedge'):
            try:
                options = {} if channel == 'chromium' else {'channel': channel}
                browser = p.chromium.launch(headless=True, **options)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: print('PAGE ERROR:', str(error)))
                page.goto('http://127.0.0.1:8017/', wait_until='networkidle')
                page.screenshot(path=str(BASE / '.runtime' / 'desktop.png'), full_page=True)
                print('UI:', page.locator('#messages').inner_text(), page.locator('#toast').text_content())
                print('BROWSER ' + channel + ': OK, ' + page.title())
                browser.close()
                return
            except Exception as exc:
                print('BROWSER ' + channel + ': ' + str(exc)[:600])


if __name__ == '__main__':
    if sys.argv[-1] == 'amap':
        asyncio.run(amap())
    else:
        browser()
