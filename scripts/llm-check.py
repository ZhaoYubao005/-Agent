"""Verify the live provider, using exclusively synthetic aggregate counts."""
import asyncio
import json
from time import perf_counter

from ecomops.advice import interpret_report


async def main():
    start = perf_counter()
    result = await interpret_report(
        {'orders': 12, 'revenue': None}, {'orders': 9},
        {'fulfillment_sla_alerts': 2, 'inventory_shortage_alerts': 1, 'inventory_levels_out_of_stock': 3},
    )
    assert result['source'] == 'llm', result['trace']
    print(json.dumps({'advice': result, 'elapsed_ms': round((perf_counter() - start) * 1000, 2)}))


asyncio.run(main())
