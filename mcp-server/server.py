"""Real stdio MCP tools over Medusa Admin APIs; no mock business data."""
import os
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from ecomops.database import connect_read_only
from ecomops.medusa import Medusa
from ecomops.operations_store import get_report

server = MCPServer('ecomops-commerce', version='0.1.0')
medusa = Medusa()


@server.tool()
async def list_products() -> list[dict]:
    """Read all real products from Medusa."""
    return await medusa.list_all('products', 'products')


@server.tool()
async def list_orders(purpose: Literal['default', 'monitor', 'demand'] = 'default') -> list[dict]:
    """Read real orders from Medusa, including their item quantities."""
    if purpose != 'default':
        return await medusa.operational_orders(purpose)
    return await medusa.list_all('orders', 'orders')


@server.tool()
async def list_inventory() -> list[dict]:
    """Read inventory with per-location stocked/reserved/available quantities."""
    return await medusa.list_all('inventory-items', 'inventory_items')


@server.tool()
def read_daily_report(day: str, zone: str = 'UTC') -> dict:
    """Read a saved aggregate report when the local service grants report access."""
    if os.environ.get('ECOMOPS_MCP_REPORT_READ') != '1':
        return {'status': 'forbidden'}
    try:
        day = date.fromisoformat(day).isoformat()
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return {'status': 'invalid_date_or_zone'}

    try:
        with connect_read_only() as db:
            report = get_report(db, day, zone)
    except Exception:
        # The driver error may carry private connection or file path details.
        return {'status': 'unavailable'}
    if report is None:
        return {'status': 'not_found'}
    return {key: report[key] for key in ('day', 'zone', 'generated_at', 'current', 'previous', 'snapshot')
            if key in report}


if __name__ == '__main__':
    server.run(transport='stdio')
