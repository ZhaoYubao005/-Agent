"""Real admin login/rendering check; no credential-bearing screenshots."""
import asyncio
import json
import os
import shutil
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    executable = os.environ.get('CHROMIUM_PATH') or shutil.which('chromium')
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable, headless=True,
                                         args=['--no-sandbox'])
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto('http://127.0.0.1:9000/app/login', wait_until='networkidle')
        await page.screenshot(path=str(ROOT / 'docs/evidence/admin-login.png'), full_page=True)
        await page.locator('input[name=email]').fill(os.environ['MEDUSA_ADMIN_EMAIL'])
        await page.locator('input[name=password]').fill(os.environ['MEDUSA_ADMIN_PASSWORD'])
        await page.get_by_role('button', name='Continue with Email').click()
        await page.wait_for_url(lambda url: '/login' not in url, timeout=30000)
        await page.goto('http://127.0.0.1:9000/app/products', wait_until='networkidle')
        await page.get_by_text('EcomOps Demo Mug', exact=True).wait_for(timeout=30000)
        await page.screenshot(path=str(ROOT / 'docs/evidence/admin-products.png'), full_page=True)
        await page.goto('http://127.0.0.1:9000/app/orders', wait_until='networkidle')
        await page.get_by_text('fictional-buyer-1@example.test', exact=True).wait_for(timeout=30000)
        await page.screenshot(path=str(ROOT / 'docs/evidence/admin-orders.png'), full_page=True)
        assert not errors, errors
        (ROOT / 'docs/evidence/browser.json').write_text(json.dumps({
            'login': 'passed', 'products_visible': True, 'orders_visible': True,
            'page_errors': errors,
        }, indent=2))
        await browser.close()
        print('Admin login, products, orders rendered; no page errors')


asyncio.run(main())
