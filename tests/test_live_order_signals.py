"""Live regression tests for orders in the disposable Medusa seed."""

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from ecomops import app as agent_app
from ecomops.alert_scheduler import run_alert_scan_tick
from ecomops.database import connect
from ecomops.medusa import Medusa
from ecomops.receipts import _fixture_endpoint
from ecomops.tools import call, commerce_tools


ALERTS = '/operations/orders/alerts'


@pytest.fixture
def agent_headers():
    return {'Authorization': f'Bearer {os.environ["ECOMOPS_API_TOKEN"]}'}


@pytest.fixture
async def seeded_orders():
    medusa = Medusa()
    listed = await medusa.list_all('orders', 'orders')
    assert {row['metadata']['ecomops_reference'] for row in listed} == {
        'ecomops-demo-order-1', 'ecomops-demo-order-2',
    }
    assert len(listed) == 2
    orders = []
    for row in listed:
        detail = await medusa.request('GET', f'/admin/orders/{row["id"]}', params={
            'fields': 'id,status,created_at,currency_code,sales_channel_id,total,items.id,items.variant_id,items.quantity',
        })
        order = detail['order']
        assert order['status'] == 'pending'
        assert order['fulfillment_status'] == 'not_fulfilled'
        assert len(order['items']) == 1
        assert order['items'][0]['quantity'] == 1
        orders.append(order)
    return orders


async def read_alerts(client):
    response = await client.get(ALERTS, params={'sla_hours': 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert [node['node'] for node in result['trace']['nodes']] == [
        'read_medusa_via_mcp', 'apply_operations_rules',
    ]
    return result


@pytest.mark.asyncio
async def test_live_order_reservations_and_shortage(seeded_orders, agent_headers):
    medusa = Medusa()
    inventory = await medusa.list_all('inventory-items', 'inventory_items', {'sku': 'ECOMOPS-MUG'})
    assert len(inventory) == 1
    mug = inventory[0]
    assert mug['sku'] == 'ECOMOPS-MUG'
    assert len(mug['location_levels']) == 1
    level = mug['location_levels'][0]
    assert level['inventory_item_id'] == mug['id']
    assert level['reserved_quantity'] == 2
    assert level['stocked_quantity'] >= 2

    async with commerce_tools() as session:
        snapshot = await call(session, 'list_orders', {'purpose': 'monitor'})
    monitored = {order['id']: order for order in snapshot}
    for order in seeded_orders:
        line_id = order['items'][0]['id']
        direct = await medusa.list_all('reservations', 'reservations', {'line_item_id': line_id})
        assert len(direct) == 1
        reservation = direct[0]
        expected = {
            'id': reservation['id'], 'line_item_id': line_id, 'inventory_item_id': mug['id'],
            'location_id': level['location_id'], 'quantity': 1,
        }
        assert {key: reservation[key] for key in expected} == expected
        observed = monitored[order['id']]
        assert observed['sales_channel_id'] == order['sales_channel_id']
        assert level['location_id'] in observed['eligible_location_ids']
        assert observed['reservations'] == [expected]

    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', headers=agent_headers,
                                 timeout=120, trust_env=False) as agent:
        before = await read_alerts(agent)
        assert before['shortage_assessment'] == {'orders_fully_assessed': 2, 'unknown_orders': []}
        assert all(row['rule'] != 'inventory_shortage' for row in before['alerts'])

        # Only the disposable fixture may be changed, and its exact stock is restored.
        assert os.environ.get('ECOMOPS_ISOLATED_FIXTURES') == '1'
        assert _fixture_endpoint(medusa.url)
        item_path = f'/admin/inventory-items/{mug["id"]}'
        update_path = f'{item_path}/location-levels/{level["location_id"]}'
        stocked = level['stocked_quantity']
        try:
            await medusa.request('POST', update_path, json={'stocked_quantity': 0})
            readback = await medusa.request('GET', item_path)
            changed = readback['inventory_item']['location_levels'][0]
            assert changed['id'] == level['id']
            assert changed['stocked_quantity'] == 0
            assert changed['reserved_quantity'] == 2

            after = await read_alerts(agent)
            assert after['shortage_assessment'] == {'orders_fully_assessed': 2, 'unknown_orders': []}
            alerts = {row['order_id']: row for row in after['alerts'] if row['rule'] == 'inventory_shortage'}
            assert set(alerts) == {order['id'] for order in seeded_orders}
            for order_id, alert in alerts.items():
                assert alert['id'] == f'inventory_shortage:{order_id}'
                assert alert['severity'] == 'high'
                assert alert['shortages'] == [{
                    'inventory_item_id': mug['id'], 'required': 1, 'reserved_for_order': 0,
                    'unreserved_available': 0, 'shortfall': 1,
                }]
        finally:
            await medusa.request('POST', update_path, json={'stocked_quantity': stocked})
            readback = await medusa.request('GET', item_path)
            restored = readback['inventory_item']['location_levels'][0]
            assert restored['id'] == level['id']
            assert restored['stocked_quantity'] == stocked
            assert restored['reserved_quantity'] == 2

        restored_alerts = await read_alerts(agent)
        assert restored_alerts['shortage_assessment'] == before['shortage_assessment']
        assert all(row['rule'] != 'inventory_shortage' for row in restored_alerts['alerts'])


@pytest.mark.asyncio
async def test_live_fulfillment_sla_with_controlled_clock(
    seeded_orders, agent_headers, monkeypatch, tmp_path,
):
    created = {order['id']: datetime.fromisoformat(order['created_at']) for order in seeded_orders}
    clock = max(created.values()) + timedelta(hours=2, minutes=1)

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz)

    # Keep the real ASGI route, graph, MCP process and Medusa data. Control only the clock.
    monkeypatch.setattr(agent_app, 'datetime', TestClock)
    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'scheduled-alerts.db'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app.app),
                                 base_url='http://test', headers=agent_headers) as agent:
        result = await read_alerts(agent)
        assert await run_alert_scan_tick(connect, agent_app._scan_order_alerts, 60, 1, now=clock)
        saved = await agent.get(f'{ALERTS}/saved')
        assert saved.status_code == 200, saved.text
        assert {row['id'] for row in saved.json()} == {row['id'] for row in result['alerts']}
        assert all(row['status'] == 'open' for row in saved.json())
        assert not await run_alert_scan_tick(connect, agent_app._scan_order_alerts, 60, 1, now=clock)
        status = await agent.get('/operations/orders/scan-status')
        assert status.status_code == 200, status.text
        assert status.json()['last_run']['state'] == 'completed'
        assert status.json()['last_run']['alert_count'] == len(result['alerts'])
    overdue = {row['order_id']: row for row in result['alerts'] if row['rule'] == 'fulfillment_sla'}
    assert set(overdue) == set(created)
    for order_id, alert in overdue.items():
        assert alert['id'] == f'fulfillment_sla:{order_id}'
        assert alert['severity'] == 'high'
        assert alert['fulfillment_status'] == 'not_fulfilled'
        assert alert['age_hours'] == (clock - created[order_id]) // timedelta(hours=1)


@pytest.mark.asyncio
async def test_live_daily_report_reconciles_to_medusa_order_details(seeded_orders, agent_headers):
    cohorts = {}
    for order in seeded_orders:
        day = datetime.fromisoformat(order['created_at']).astimezone(timezone.utc).date()
        cohorts.setdefault(day, []).append(order)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', headers=agent_headers,
                                 timeout=120, trust_env=False) as agent:
        for day, orders in cohorts.items():
            response = await agent.get('/operations/reports/daily', params={'day': day.isoformat(), 'zone': 'UTC'})
            assert response.status_code == 200, response.text
            current = response.json()['current']
            variants = Counter()
            values = Counter()
            for order in orders:
                for line in order['items']:
                    variants[line['variant_id']] += line['quantity']
                total = Decimal(str(order['total']))
                assert total > 0
                values[order['currency_code'].lower()] += total

            assert current['orders'] == len(orders)
            assert current['statuses'] == {'pending': len(orders)}
            assert current['fulfillment_statuses'] == {'not_fulfilled': len(orders)}
            assert current['payment_statuses'] == dict(Counter(order['payment_status'] for order in orders))
            assert current['ordered_units_by_variant'] == dict(variants)
            assert {code: Decimal(value) for code, value in current['order_value_by_currency'].items()} == dict(values)
            assert current['orders_missing_items'] == current['orders_missing_value'] == 0
            assert current['lines_missing_variant_or_quantity'] == current['orders_missing_created_at'] == 0
            assert current['revenue'] is None
            assert current['refund_amount'] is None
