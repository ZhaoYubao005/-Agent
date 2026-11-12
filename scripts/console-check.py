"""Exercise the operations console against seeded Medusa and Agent services."""
import asyncio
import json
import os
from datetime import datetime, timezone
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
        await page.locator('#day').fill(datetime.now(timezone.utc).date().isoformat())
        await page.locator('#zone').fill('UTC')
        finance = await operation('finance-report', '/operations/finance/reconciliation')
        assert finance['scope'] == 'imported_records_only'
        today = datetime.now(timezone.utc).date().isoformat()
        await page.locator('#revenue-csv').set_input_files({
            'name': 'preview-fixture.csv', 'mimeType': 'text/csv',
            'buffer': f'entry_id,date,currency,amount\npreview_only,{today},EUR,123.45\n'.encode(),
        })
        preview = await operation('preview-revenue', '/operations/finance/preview/revenue')
        assert preview['new_count'] == 1 and preview['ready_to_import'] is True
        after_preview = await operation('finance-report', '/operations/finance/reconciliation')
        assert after_preview['recognized_revenue'] == finance['recognized_revenue']
        audit = await operation('audit', '/operations/audit')
        assert {'approved', 'applied'} <= {row['action'] for row in audit if row['subject'] == bag['id']}
        # Delay a financial response until after logout to exercise the display boundary.
        await page.evaluate("""() => {
            window.fetch = () => new Promise(resolve => {
                window.completeFinanceResponse = () => resolve({
                    ok: true,
                    json: async () => {
                        window.financeResponseRead = true;
                        return {sensitive_marker: 'fixture'};
                    },
                });
            });
        }""")
        await page.locator('#finance-report').click()
        await page.wait_for_function("typeof window.completeFinanceResponse === 'function'")
        await page.locator('#logout').click()
        await page.evaluate("window.completeFinanceResponse()")
        await page.wait_for_function("window.financeResponseRead === true")
        await page.evaluate("() => new Promise(requestAnimationFrame)")
        await expect(page.locator('#status')).to_have_text('已退出')
        await expect(page.locator('#result')).to_have_text('')
        assert not errors, errors
        print(json.dumps({'inventory': True, 'approved_and_received': True, 'alerts': True,
                          'report_history': True, 'finance_reconciliation': True, 'finance_preview': True, 'logout_discards_finance_response': True, 'audit': True, 'latency_ms': latencies, 'errors': errors}))
        await browser.close()


asyncio.run(main())
