"""Verify the live provider, using exclusively synthetic aggregate counts."""
import asyncio
import json
from time import perf_counter

from ecomops.advice import interpret_report


async def main():
    start = perf_counter()
    result = await interpret_report({'orders': 12, 'revenue': None}, {'orders': 9})
    assert result['source'] == 'llm', result['trace']
    print(json.dumps({'advice': result, 'elapsed_ms': round((perf_counter() - start) * 1000, 2)}))


asyncio.run(main())
