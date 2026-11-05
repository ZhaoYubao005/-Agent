"""Capture actual completed LangGraph nodes without storing raw tool data."""
import asyncio
from time import perf_counter

from ecomops.tools import MCPUnavailable


async def run_graph(workflow, inputs):
    state = inputs
    trace = []
    started = last = perf_counter()
    try:
        async with asyncio.timeout(120):
            async for mode, update in workflow.astream(inputs, stream_mode=['updates', 'values']):
                if mode == 'values':
                    state = update
                else:
                    completed = perf_counter()
                    for node in update:
                        trace.append({'node': node, 'elapsed_ms': round((completed - last) * 1000, 2)})
                    last = completed
    except TimeoutError as error:
        raise MCPUnavailable('Workflow exceeded its time budget') from error
    return state, {'nodes': trace, 'total_ms': round((perf_counter() - started) * 1000, 2)}
