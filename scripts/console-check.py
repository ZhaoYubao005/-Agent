"""Exercise the operations console against seeded Medusa and Agent services."""
import asyncio
import json
import os
from time import perf_counter

from playwright.async_api import async_playwright, expect


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        latencies = {}
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        await page.goto('http://127.0.0.1:8100/console')
        await page.locator('#token').fill(os.environ['ECOMOPS_API_TOKEN'])

        async def operation(button, path):
            started = perf_counter()
            async with page.expect_response(lambda response: path in response.url) as event:
                await page.locator(f'#{button}').click()
            response = await event.value
            assert response.ok, await response.text()
            await expect(page.locator('#status')).to_have_text('完成')
            latencies[button] = round((perf_counter() - started) * 1000, 2)
            return await response.json()

        identity = await operation('login', '/identity')
        assert identity['role'] == 'admin'
        await page.locator('#threshold').fill('60')
        await page.locator('#target').fill('70')
        snapshot = await operation('inventory', '/inventory/check')
        bag = next(row for row in snapshot['recommendations'] if row['sku'] == 'ECOMOPS-BAG')
        await page.locator('#proposal').fill(bag['id'])
        approved = await operation('approval', '/approve')
        assert approved['status'] == 'approved' and approved['receipt'] is None
        received = await operation('receipt', '/receive')
        assert received['status'] == 'applied'
        assert received['receipt']['available_quantity'] == 70
        await operation('refresh', '/operations/orders/alerts/refresh')
        await operation('alerts', '/operations/orders/alerts/saved')
        assert await operation('history', '/operations/reports/history')
        audit = await operation('audit', '/operations/audit')
        assert {'approved', 'applied'} <= {row['action'] for row in audit if row['subject'] == bag['id']}
        assert not errors, errors
        print(json.dumps({'inventory': True, 'approved_and_received': True, 'alerts': True,
                          'report_history': True, 'audit': True, 'latency_ms': latencies, 'errors': errors}))
        await browser.close()


asyncio.run(main())
