"""Real stdio MCP tools over Medusa Admin APIs; no mock business data."""
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


if __name__ == '__main__':
    server.run(transport='stdio')
