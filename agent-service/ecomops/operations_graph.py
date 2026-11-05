"""MCP-backed order monitoring and reporting workflows."""
from datetime import date, datetime, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from ecomops.advice import interpret_report
from ecomops.operations import amount, daily_metrics, shortage_alerts, sla_alerts
from ecomops.tools import call, commerce_tools


class OperationsState(TypedDict, total=False):
    day: date
    zone: str
    now: datetime
    sla_hours: int
    orders: list[dict]
    inventory: list[dict]
    result: dict


async def read_snapshot(state):
    async with commerce_tools() as session:
        orders = await call(session, 'list_orders', {'purpose': 'monitor'})
        inventory = await call(session, 'list_inventory')
    return {'orders': orders, 'inventory': inventory}


async def analyze(state):
    orders = state['orders']
    inventory = state['inventory']
    overdue = sla_alerts(orders, state['now'], state['sla_hours'])
    shortages, assessment = shortage_alerts(orders, inventory)
    if 'day' not in state:
        return {'result': {'alerts': overdue + shortages, 'shortage_assessment': assessment}}

    day = state['day']
    zone = ZoneInfo(state['zone'])
    current = daily_metrics(orders, day, zone)
    previous = daily_metrics(orders, day - timedelta(days=1), zone)
    availability = [amount(level.get('available_quantity')) for item in inventory
                    for level in item.get('location_levels') or []]
    return {'result': {
        'current': current,
        'previous': previous,
        'snapshot': {
            'as_of': state['now'].isoformat(),
            'fulfillment_sla_alerts': len(overdue),
            'inventory_shortage_alerts': len(shortages),
            'orders_with_unknown_shortage_status': len(assessment['unknown_orders']),
            'inventory_levels_out_of_stock': sum(value is not None and value <= 0 for value in availability),
            'inventory_levels_with_unknown_availability': availability.count(None),
        },
        'advice': await interpret_report(current, previous),
    }}


builder = StateGraph(OperationsState)
builder.add_node('read_medusa_via_mcp', read_snapshot)
builder.add_node('apply_operations_rules', analyze)
builder.add_edge(START, 'read_medusa_via_mcp')
builder.add_edge('read_medusa_via_mcp', 'apply_operations_rules')
builder.add_edge('apply_operations_rules', END)
operations_graph = builder.compile()
