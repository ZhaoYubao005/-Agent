"""Exercise the operations console against seeded Medusa and Agent services."""
import asyncio
import json
import os

from playwright.async_api import async_playwright, expect


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto('http://127.0.0.1:8100/console')
        await page.locator('#token').fill(os.environ['ECOMOPS_API_TOKEN'])
        for button, field in [
            ('inventory', 'recommendations'),
            ('refresh', None),
            ('alerts', None),
            ('history', 'generated_at'),
        ]:
            async with page.expect_response(
                lambda response: '/inventory/check' in response.url or '/operations/' in response.url
            ) as event:
                await page.locator(f'#{button}').click()
            assert (await event.value).ok
            await expect(page.locator('#status')).to_have_text('完成')
            if field:
                assert field in await page.locator('#result').inner_text()
        assert not errors, errors
        print(json.dumps({'inventory': True, 'alerts': True, 'report_history': True, 'errors': errors}))
        await browser.close()


asyncio.run(main())
