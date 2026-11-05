from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pytest

from ecomops import operations_graph
from ecomops.tools import MCPUnavailable


DAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def posting(reference_id, reference, created_at, value, currency='EUR'):
    return {
        'id': f'posting-{reference_id}',
        'reference_id': reference_id,
        'reference': reference,
        'created_at': created_at,
        'currency_code': currency,
        'raw_amount': {'value': value, 'precision': 20},
    }


async def test_daily_graph_uses_posting_date_and_currency():
    # The order predates both reporting periods and is cancelled.
    order = {'id': 'old', 'status': 'canceled', 'created_at': '2026-11-01T00:00:00+00:00'}
    order['transactions'] = [
        posting('capt-0', 'capture', '2026-11-04T15:59:59+00:00', '3'),
        posting('capt-1', 'capture', '2026-11-04T16:00:00+00:00', '20.75'),
        posting('ref-1', 'refund', '2026-11-05T15:59:59+00:00', '-4.25'),
        posting('ref-2', 'refund', '2026-11-05T15:59:59+00:00', '-1', 'USD'),
        posting('capt-2', 'capture', '2026-11-05T16:00:00+00:00', '100'),
        posting('credit-1', 'credit_line', '2026-11-05T01:00:00+00:00', '200'),
    ]
    result = (await operations_graph.analyze({
        'orders': [order], 'ledger_orders': [order], 'inventory': [],
        'day': DAY, 'zone': 'Asia/Shanghai', 'now': NOW, 'sla_hours': 24,
    }))['result']
    current = result['current']
    assert current['orders'] == 0
    assert current['revenue'] is None
    assert 'independent financial records' in current['revenue_unavailable_reason']
    assert current['refund_amount'] is None
    assert 'payment_ledger' in current['refund_amount_unavailable_reason']
    assert current['payment_ledger'] == {
        'status': 'available',
        'source': 'medusa_order_transactions',
        'by_currency': {
            'eur': {'capture_count': 1, 'refund_count': 1, 'captured_amount': '20.75',
                    'refunded_amount': '4.25', 'net_posted_amount': '16.50'},
            'usd': {'capture_count': 0, 'refund_count': 1, 'captured_amount': '0',
                    'refunded_amount': '1', 'net_posted_amount': '-1'},
        },
    }
    assert result['previous']['payment_ledger']['by_currency']['eur']['captured_amount'] == '3'


@pytest.mark.parametrize('transactions', [
    None,
    [posting('ref-1', 'refund', '2026-11-05T00:00:00+00:00', '4.25')],
    [posting('ref-1', 'refund', '2026-11-05T00:00:00', '-4.25')],
    [posting('capt-1', 'capture', '2026-11-05T00:00:00+00:00', '2')] * 2,
])
async def test_incomplete_or_invalid_ledger_never_looks_like_no_activity(transactions):
    result = (await operations_graph.analyze({
        'orders': [], 'ledger_orders': [{'id': 'order', 'transactions': transactions}], 'inventory': [],
        'day': DAY, 'zone': 'UTC', 'now': NOW, 'sla_hours': 24,
    }))['result']
    assert result['current']['payment_ledger']['status'] == 'unavailable'
    assert result['current']['payment_ledger']['by_currency'] is None


async def test_mcp_ledger_failure_leaves_existing_report_available(monkeypatch):
    async def call(_session, name, args=None):
        if name == 'list_orders' and args == {'purpose': 'ledger'}:
            raise MCPUnavailable('ledger unavailable')
        return []

    monkeypatch.setattr(operations_graph, 'call', call)

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(operations_graph, 'commerce_tools', session)
    state = {'day': DAY, 'zone': 'UTC', 'now': NOW, 'sla_hours': 24}
    result = (await operations_graph.operations_graph.ainvoke(state))['result']
    assert result['current']['orders'] == 0
    assert result['current']['payment_ledger']['status'] == 'unavailable'
