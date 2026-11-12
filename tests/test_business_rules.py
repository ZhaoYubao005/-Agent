import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ecomops.medusa import Medusa
from ecomops.operations import daily_metrics


NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def level(location, stocked, reserved):
    return {'location_id': location, 'stocked_quantity': stocked,
            'reserved_quantity': reserved, 'available_quantity': stocked - reserved}


def test_shortage_uses_order_reservation_and_channel_inventory():
    from ecomops.operations import shortage_alerts

    order = {
        'id': 'ord', 'status': 'pending', 'fulfillment_status': 'partially_shipped',
        'eligible_location_ids': ['warehouse'],
        'items': [{'id': 'line', 'quantity': 3, 'detail': {'fulfilled_quantity': 1},
                   'variant': {'manage_inventory': True, 'inventory_items': [
                       {'inventory_item_id': 'item', 'required_quantity': '0.5'},
                   ]}}],
        'reservations': [{'line_item_id': 'line', 'inventory_item_id': 'item',
                          'location_id': 'warehouse', 'quantity': '0.5'}],
    }
    inventory = [{'id': 'item', 'location_levels': [level('warehouse', 2, 2), level('other', 100, 0)]}]
    alerts, assessment = shortage_alerts([order], inventory)
    assert alerts[0]['id'] == 'inventory_shortage:ord'
    assert alerts[0]['shortages'] == [{'inventory_item_id': 'item', 'required': 1,
                                       'reserved_for_order': 0.5, 'unreserved_available': 0,
                                       'shortfall': 0.5}]
    assert assessment['unknown_orders'] == []

    order['reservations'][0]['quantity'] = 1
    assert shortage_alerts([order], inventory)[0] == []

    del order['reservations']
    alerts, assessment = shortage_alerts([order], inventory)
    assert alerts == []
    assert assessment['unknown_orders'][0]['order_id'] == 'ord'


def test_daily_report_separates_order_value_by_currency_and_ledger_metrics():
    orders = [
        {'created_at': NOW.isoformat(), 'status': 'pending', 'currency_code': 'usd',
         'total': '20.13', 'payment_status': 'partially_refunded', 'items': []},
        {'created_at': NOW.isoformat(), 'status': 'completed', 'currency_code': 'eur',
         'total': 10, 'payment_status': 'captured', 'items': []},
        {'created_at': NOW.isoformat(), 'status': 'canceled', 'currency_code': 'usd',
         'total': 99, 'items': []},
    ]
    result = daily_metrics(orders, NOW.date(), ZoneInfo('UTC'))
    assert result['order_value_by_currency'] == {'eur': '10', 'usd': '20.13'}
    assert result['payment_statuses']['partially_refunded'] == 1
    assert result['revenue'] is None
    assert result['refund_amount'] is None


def test_replenishment_uses_attributed_warehouse_history_and_excludes_canceled_fulfillment():
    from ecomops.graph import recommend

    state = {
        'threshold': 10, 'target': 30, 'demand_window_days': 14,
        'lead_time_days': 7, 'safety_stock': 5, 'now': NOW,
        'inventory': [{'id': 'item', 'location_levels': [level('a', 13, 0), level('b', 3, 0)]}],
        'orders': [{'fulfillments': [
            {'id': 'f1', 'location_id': 'a', 'created_at': '2026-11-02T00:00:00Z',
             'canceled_at': None, 'items': [{'inventory_item_id': 'item', 'quantity': 28}]},
            {'id': 'f2', 'location_id': 'a', 'created_at': '2026-11-02T00:00:00Z',
             'canceled_at': '2026-11-03T00:00:00Z', 'items': [{'inventory_item_id': 'item', 'quantity': 500}]},
        ]}],
    }
    by_location = {row['location_id']: row for row in recommend(state)['recommendations']}
    assert by_location['a']['policy_source'] == 'warehouse_fulfillment_history'
    assert by_location['a']['reorder_point'] == 19
    assert by_location['a']['quantity'] == 17
    assert by_location['b']['policy_source'] == 'threshold_fallback'
    assert by_location['b']['quantity'] == 27


def test_medusa_order_monitor_snapshot_fetches_scoped_reservations_and_locations(monkeypatch):
    adapter = Medusa()
    seen = {}

    async def request(method, path, *, params):
        seen[path] = params
        if path == '/admin/orders':
            rows = [{'id': 'ord', 'status': 'pending', 'sales_channel_id': 'channel',
                     'fulfillment_status': 'not_fulfilled', 'items': [{'id': 'line'}]}]
            return {'orders': rows, 'count': 1}
        if path == '/admin/reservations':
            rows = [{'id': 'res', 'line_item_id': 'line', 'inventory_item_id': 'item',
                     'location_id': 'warehouse', 'quantity': 1}]
            return {'reservations': rows, 'count': 1}
        if path == '/admin/stock-locations':
            return {'stock_locations': [{'id': 'warehouse'}], 'count': 1}
        pytest.fail(f'unexpected path {path}')

    monkeypatch.setattr(adapter, 'request', request)
    orders = asyncio.run(adapter.operational_orders('monitor'))
    assert orders[0]['eligible_location_ids'] == ['warehouse']
    assert orders[0]['reservations'][0]['id'] == 'res'
    assert seen['/admin/stock-locations']['sales_channel_id'] == 'channel'
    assert 'items.detail.fulfilled_quantity' in seen['/admin/orders']['fields']


@pytest.mark.asyncio
async def test_mcp_report_reads_saved_aggregates_with_service_permission(tmp_path, monkeypatch):
    from ecomops.database import transaction
    from ecomops.operations_store import save_report
    from ecomops.tools import call, commerce_tools

    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'report.sqlite3'))
    args = {'day': '2026-11-05'}
    with transaction() as db:
        save_report(db, args['day'], 'UTC', {'current': {'orders': 2}, 'alerts': [{'order_id': 'ord'}]})

    monkeypatch.delenv('ECOMOPS_MCP_REPORT_READ', raising=False)
    async with commerce_tools() as session:
        response = await call(session, 'read_daily_report', args)
        assert response == {'status': 'forbidden'}

    monkeypatch.setenv('ECOMOPS_MCP_REPORT_READ', '1')
    async with commerce_tools() as session:
        response = await call(session, 'read_daily_report', args)
        assert response['current'] == {'orders': 2}
        assert 'alerts' not in response
