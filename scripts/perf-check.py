"""Measure the small seeded data workload against the live Agent."""
import asyncio
import json
import math
import os
from statistics import median
from time import perf_counter

import httpx


async def main():
    paths = {
        'stored_report_history': ('/operations/reports/history', 10),
        'live_inventory_via_mcp': ('/inventory/levels?sku=ECOMOPS-MUG', 5),
        'live_order_monitor_via_mcp': ('/operations/orders/alerts', 5),
    }
    output = {}
    headers = {'Authorization': f'Bearer {os.environ["ECOMOPS_API_TOKEN"]}'}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', headers=headers,
                                 timeout=125, trust_env=False) as client:
        for name, (path, count) in paths.items():
            (await client.get(path)).raise_for_status()
            samples = []
            for _ in range(count):
                started = perf_counter()
                response = await client.get(path)
                response.raise_for_status()
                response.json()
                samples.append((perf_counter() - started) * 1000)
            ordered = sorted(samples)
            output[name] = {
                'requests': count,
                'p50_ms': round(median(ordered), 2),
                'p95_ms_nearest_rank': round(ordered[math.ceil(0.95 * count) - 1], 2),
                'max_ms': round(ordered[-1], 2),
            }
    print(json.dumps({'workload': 'seeded demo, serial requests, one warmup per endpoint', 'results': output}))


asyncio.run(main())
