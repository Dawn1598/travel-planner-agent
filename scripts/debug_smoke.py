"""Real-browser verification of opt-in debug traces and JSONL downloads."""
import json
from pathlib import Path
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8017/', wait_until='networkidle')
    page.wait_for_function("document.querySelector('#messages').textContent.length > 0")
    assert page.locator('#debug-toggle').get_attribute('aria-checked') == 'false'
    assert not page.locator('#debug-panel').is_visible()
    page.locator('#debug-toggle').click()
    page.wait_for_function("document.querySelector('#debug-toggle').getAttribute('aria-checked') === 'true'")
    assert page.locator('#debug-panel').is_visible()
    trip_date = (date.today() + timedelta(days=7)).isoformat()
    page.locator('#start-location').fill('北京站')
    page.locator('#start-date').fill(trip_date)
    page.locator('#end-date').fill(trip_date)
    page.locator('#requirements-input').fill('我想出去旅行，目的地还没确定，请先问我。')
    page.locator('#plan-button').click()
    page.wait_for_function("document.querySelectorAll('.message.assistant').length > 0 && !document.querySelector('#send-button').disabled", timeout=160000)
    page.wait_for_selector('.debug-entry')
    count = page.locator('.debug-entry').count()
    assert count >= 5, count
    text = page.locator('#debug-log').text_content()
    assert '模型请求' in text and '模型返回' in text and '结构化结果' in text
    page.locator('.debug-entry').filter(has_text='模型请求').first.locator('summary').click()
    page.screenshot(path=str(BASE / '.runtime' / 'debug-desktop.png'), full_page=True)
    with page.expect_download() as event:
        page.locator('#debug-download').click()
    download = event.value
    download.save_as(str(BASE / '.runtime' / 'debug-smoke-download.jsonl'))
    entries = [json.loads(line) for line in (BASE / '.runtime' / 'debug-smoke-download.jsonl').read_text(encoding='utf-8').splitlines()]
    assert entries[0]['kind'] == 'run.start'
    assert entries[-1]['kind'] == 'run.finish'
    page.reload(wait_until='networkidle')
    page.wait_for_function("document.querySelector('#debug-toggle').getAttribute('aria-checked') === 'true'")
    page.wait_for_selector('.debug-entry')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='行程', exact=True).click()
    page.screenshot(path=str(BASE / '.runtime' / 'debug-mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.locator('#debug-toggle').click()
    page.wait_for_function("document.querySelector('#debug-toggle').getAttribute('aria-checked') === 'false'")
    assert not page.locator('#debug-panel').is_visible()
    assert not errors, errors
    browser.close()
print(f'Debug toggle, {len(entries)} real trace records, JSONL download, persistence, mobile and off state: OK')
