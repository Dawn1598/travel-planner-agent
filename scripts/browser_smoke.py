"""Exercise the real local webpage and DeepSeek clarification flow."""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(os.getenv('TRAVEL_BASE_URL', 'http://127.0.0.1:8017/'), wait_until='networkidle')
    page.wait_for_function("document.querySelector('#messages').textContent.length > 0", timeout=10000)
    page.screenshot(path=str(BASE / '.runtime' / 'desktop.png'), full_page=True)
    assert not errors, errors
    trip_date = (date.today() + timedelta(days=7)).isoformat()
    page.locator('#start-location').fill('杭州东站')
    page.locator('#start-date').fill(trip_date)
    page.locator('#end-date').fill(trip_date)
    page.locator('#requirements-input').fill('我想出去旅行，目的地还没确定，请先问我。')
    page.locator('#plan-button').click()
    page.wait_for_function("document.querySelectorAll('.message.assistant').length > 0 && !document.querySelector('#send-button').disabled", timeout=160000)
    reply = page.locator('.message.assistant .message-body').last.inner_text()
    assert reply and any(word in reply for word in ('哪', '目的地', '天', '城市')), reply
    print('Clarification flow: OK')
    print('Reply:', reply)
    page.reload(wait_until='networkidle')
    page.wait_for_selector('.message.assistant')
    assert page.locator('.message.user').count() == 1
    print('Session persistence: OK')
    if '--plan' in sys.argv:
        page.locator('#message-input').fill('杭州，当天上午9点从杭州东站出发，去西湖断桥，下午13点结束。1个人，预算300元，公共交通。只安排这两个地点之间的交通和湖边散步，不需要门票、预约、酒店或餐馆，也暂时不需要查询游玩攻略。')
        page.locator('#send-button').click()
        page.wait_for_function("!document.querySelector('#send-button').disabled", timeout=300000)
        reply = page.locator('.message.assistant .message-body').last.inner_text()
        print('Planning reply:', reply)
        print('Plan visible:', page.locator('#plan').is_visible())
        print('Progress:', page.locator('#progress-events').inner_text())
        if page.locator('#plan').is_visible():
            page.screenshot(path=str(BASE / '.runtime' / 'plan-desktop.png'), full_page=True)
        assert page.locator('#plan').is_visible(), reply
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='旅行参数', exact=True).click()
    page.screenshot(path=str(BASE / '.runtime' / 'mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert not errors, errors
    print('Responsive layout and JavaScript: OK')
    (BASE / '.runtime' / 'smoke-session.json').write_text(json.dumps(context.storage_state()), encoding='utf-8')
    browser.close()
