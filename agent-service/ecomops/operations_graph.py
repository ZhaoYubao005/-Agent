"""MCP-backed order monitoring and reporting workflows."""
from datetime import date, datetime, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from ecomops.advice import interpret_report
from ecomops.operations import daily_metrics, sla_alerts
from ecomops.tools import call, commerce_tools


class OperationsState(TypedDict, total=False):
    day: date
    zone: str
    now: datetime
    sla_hours: int
    orders: list[dict]
    result: dict


async def read_orders(state):
    async with commerce_tools() as session:
        return {'orders': await call(session, 'list_orders')}


async def analyze(state):
    orders = state['orders']
    result = {'alerts': sla_alerts(orders, state['now'], state['sla_hours'])}
    if 'day' in state:
        day = state['day']
        zone = ZoneInfo(state['zone'])
        result['current'] = daily_metrics(orders, day, zone)
        result['previous'] = daily_metrics(orders, day - timedelta(days=1), zone)
        result['advice'] = await interpret_report(result['current'], result['previous'])
    return {'result': result}


builder = StateGraph(OperationsState)
builder.add_node('read_orders_via_mcp', read_orders)
builder.add_node('apply_operations_rules', analyze)
builder.add_edge(START, 'read_orders_via_mcp')
builder.add_edge('read_orders_via_mcp', 'apply_operations_rules')
builder.add_edge('apply_operations_rules', END)
operations_graph = builder.compile()
