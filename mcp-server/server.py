"""Real stdio MCP tools over Medusa Admin APIs; no mock business data."""
import os
from mcp.server.mcpserver import MCPServer
from ecomops.medusa import Medusa

server = MCPServer('ecomops-commerce', version='0.1.0')
medusa = Medusa()


@server.tool()
async def list_products() -> list[dict]:
    """Read all real products from Medusa."""
    return await medusa.list_all('products', 'products')


@server.tool()
async def list_orders() -> list[dict]:
    """Read real orders from Medusa, including their item quantities."""
    return await medusa.list_all('orders', 'orders')


@server.tool()
async def list_inventory() -> list[dict]:
    """Read inventory with per-location stocked/reserved/available quantities."""
    return await medusa.list_all('inventory-items', 'inventory_items')


@server.tool()
async def replenish_inventory(inventory_item_id: str, location_id: str,
                              quantity: int, expected_stocked: int) -> dict:
    """Receive approved demo stock, rejecting stale snapshots. Development only."""
    if os.environ.get('ECOMOPS_ALLOW_REPLENISHMENT') != '1':
        raise ValueError('Stock writes are disabled')
    if not 1 <= quantity <= 10000:
        raise ValueError('quantity must be 1..10000')
    item = await medusa.request('GET', f'/admin/inventory-items/{inventory_item_id}')
    levels = item['inventory_item']['location_levels']
    level = next((x for x in levels if x['location_id'] == location_id), None)
    if level is None or int(level['stocked_quantity']) != expected_stocked:
        raise ValueError('Stale inventory snapshot: run analysis again')
    target = expected_stocked + quantity
    await medusa.request('POST',
        f'/admin/inventory-items/{inventory_item_id}/location-levels/{location_id}',
        json={'stocked_quantity': target})
    after = await medusa.request('GET', f'/admin/inventory-items/{inventory_item_id}')
    verified = next(x for x in after['inventory_item']['location_levels']
                    if x['location_id'] == location_id)
    if int(verified['stocked_quantity']) != target:
        raise RuntimeError('Inventory read-back did not match approved receipt')
    return {'inventory_item_id': inventory_item_id, 'location_id': location_id,
            'before_stocked': expected_stocked, 'after_stocked': target,
            'available_quantity': int(verified['available_quantity'])}


if __name__ == '__main__':
    server.run(transport='stdio')
