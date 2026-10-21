"""Render a genuinely generated plan and check desktop/mobile presentation."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent.parent
plan = json.loads((BASE / '.runtime' / 'validated-plan.json').read_text(encoding='utf-8'))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8017/', wait_until='networkidle')
    page.wait_for_function("document.querySelector('#messages').textContent.length > 0")
    page.evaluate('(plan) => renderPlan(plan)', plan)
    assert page.locator('.activity').count() == len(plan['days'][0]['activities'])
    assert page.locator('.day-tabs button').count() == len(plan['days'])
    page.screenshot(path=str(BASE / '.runtime' / 'plan-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='行程', exact=True).click()
    assert page.locator('#plan').is_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(BASE / '.runtime' / 'plan-mobile.png'), full_page=True)
    malicious = {**plan, 'title': '<img src=x onerror="window.injected=true">', 'map_url': 'javascript:alert(1)'}
    page.evaluate('(plan) => renderPlan(plan)', malicious)
    assert page.locator('#plan img').count() == 0
    assert page.locator('a[href^="javascript:"]').count() == 0
    assert not errors, errors
    browser.close()
print('Real plan rendering, mobile layout and HTML/link escaping: OK')
