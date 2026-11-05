"""Auditable LangGraph: real MCP snapshot -> deterministic replenishment policy."""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from ecomops.tools import commerce_tools, call


class State(TypedDict, total=False):
    threshold: int
    target: int
    products: list
    orders: list
    inventory: list
    recommendations: list


async def snapshot(state: State):
    async with commerce_tools() as session:
        products = await call(session, 'list_products')
        orders = await call(session, 'list_orders')
        inventory = await call(session, 'list_inventory')
    return {'products': products, 'orders': orders, 'inventory': inventory}


def recommend(state: State):
    recommendations = []
    for item in state['inventory']:
        for level in item.get('location_levels', []):
            available = int(level['available_quantity'])
            if available < state['threshold']:
                recommendations.append({
                    'inventory_item_id': item['id'], 'sku': item.get('sku'),
                    'location_id': level['location_id'],
                    'available_quantity': available,
                    'expected_stocked': int(level['stocked_quantity']),
                    'reserved_quantity': int(level['reserved_quantity']),
                    'quantity': state['target'] - available,
                    'reason': f'Available {available} below threshold {state["threshold"]}; '
                              f'restore available stock to {state["target"]}',
                })
    return {'recommendations': recommendations}


builder = StateGraph(State)
builder.add_node('read_medusa_via_mcp', snapshot)
builder.add_node('inventory_policy', recommend)
builder.add_edge(START, 'read_medusa_via_mcp')
builder.add_edge('read_medusa_via_mcp', 'inventory_policy')
builder.add_edge('inventory_policy', END)
graph = builder.compile()
