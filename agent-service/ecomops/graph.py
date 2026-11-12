"""Auditable LangGraph: real MCP snapshot -> deterministic replenishment policy."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from ecomops.operations import amount, json_number, timestamp
from ecomops.tools import commerce_tools, call


class State(TypedDict, total=False):
    threshold: int
    target: int
    demand_window_days: int | None
    lead_time_days: int
    safety_stock: int
    now: datetime
    products: list
    orders: list
    inventory: list
    recommendations: list
    skipped_inventory_levels: list


async def snapshot(state: State):
    async with commerce_tools() as session:
        products = await call(session, 'list_products')
        purpose = {'purpose': 'demand'} if state.get('demand_window_days') else None
        orders = await call(session, 'list_orders', purpose)
        inventory = await call(session, 'list_inventory')
    return {'products': products, 'orders': orders, 'inventory': inventory}


def warehouse_history(orders, start, end):
    """Count physical inventory units by fulfillment location and inventory item."""
    demand = Counter()
    seen = set()
    for order in orders:
        if not isinstance(order.get('fulfillments'), list):
            return None
        for fulfillment in order['fulfillments']:
            if fulfillment.get('canceled_at'):
                continue
            occurred = timestamp(fulfillment.get('created_at'))
            if occurred is None or 'canceled_at' not in fulfillment:
                return None
            if not start <= occurred < end:
                continue
            fulfillment_id = fulfillment.get('id')
            location = fulfillment.get('location_id')
            if not fulfillment_id or not location or not isinstance(fulfillment.get('items'), list):
                return None
            if fulfillment_id in seen:
                continue
            seen.add(fulfillment_id)
            for line in fulfillment['items']:
                if 'inventory_item_id' not in line:
                    return None
                if line['inventory_item_id'] is None:
                    continue  # Unmanaged line with no Medusa inventory consumption.
                quantity = amount(line.get('quantity'))
                if quantity is None or quantity <= 0:
                    return None
                demand[(line['inventory_item_id'], location)] += quantity
    return demand


def recommend(state: State):
    recommendations = []
    skipped = []
    window = state.get('demand_window_days')
    now = state.get('now') or datetime.now(timezone.utc)
    history = warehouse_history(state.get('orders', []), now - timedelta(days=window), now) if window else None
    for item in state['inventory']:
        for level in item.get('location_levels') or []:
            stocked = amount(level.get('stocked_quantity'))
            reserved = amount(level.get('reserved_quantity'))
            available = amount(level.get('available_quantity'))
            if (stocked is None or reserved is None or reserved < 0 or available is None
                    or available != stocked - reserved or not level.get('location_id')
                    or stocked != stocked.to_integral_value() or reserved != reserved.to_integral_value()):
                skipped.append({'inventory_item_id': item['id'], 'location_id': level.get('location_id'),
                                'reason': 'Inventory snapshot does not support an exact whole-unit receipt.'})
                continue

            threshold, target = state['threshold'], state['target']
            evidence = {'policy_source': 'fixed_threshold'}
            units = history.get((item['id'], level['location_id'])) if history is not None else None
            if units:
                lead_time_demand = ceil(units * state['lead_time_days'] / window)
                threshold = max(threshold, lead_time_demand + state['safety_stock'])
                target = max(target, threshold)
                evidence = {
                    'policy_source': 'warehouse_fulfillment_history',
                    'fulfilled_units': json_number(units), 'demand_window_days': window,
                    'lead_time_days': state['lead_time_days'], 'lead_time_demand': lead_time_demand,
                    'safety_stock': state['safety_stock'],
                }
            elif window:
                evidence = {'policy_source': 'threshold_fallback', 'fallback_reason': (
                    'Warehouse fulfillment history is incomplete.' if history is None
                    else 'No attributable fulfillment units for this item and location in the window.'
                )}
            if available < threshold:
                quantity = min(target - int(available), 10000)
                recommendations.append({
                    'inventory_item_id': item['id'], 'sku': item.get('sku'),
                    'location_id': level['location_id'],
                    'available_quantity': int(available),
                    'expected_stocked': int(stocked),
                    'reserved_quantity': int(reserved),
                    'quantity': quantity,
                    'reorder_point': threshold, 'target_available': target, **evidence,
                    'reason': f'Available {available} below reorder point {threshold}. '
                              f'Target availability is {target} with a maximum of 10000 units per receipt.',
                })
    return {'recommendations': recommendations, 'skipped_inventory_levels': skipped}


builder = StateGraph(State)
builder.add_node('read_medusa_via_mcp', snapshot)
builder.add_node('inventory_policy', recommend)
builder.add_edge(START, 'read_medusa_via_mcp')
builder.add_edge('read_medusa_via_mcp', 'inventory_policy')
builder.add_edge('inventory_policy', END)
graph = builder.compile()
