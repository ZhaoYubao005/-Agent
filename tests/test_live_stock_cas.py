"""Exercise the receipt endpoint against disposable Medusa and PostgreSQL."""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from ecomops.medusa import Medusa
from ecomops.receipts import _fixture_endpoint


RECEIPTS = '/admin/ecomops/inventory-receipts'


pytestmark = pytest.mark.skipif(
    os.environ.get('ECOMOPS_ISOLATED_FIXTURES') != '1' or not os.environ.get('ECOMOPS_STOCK_CAS_MODE'),
    reason='requires a disposable Medusa instance with controlled stock receipts enabled',
)


@pytest.fixture
async def bag_fixture():
    assert os.environ['ECOMOPS_STOCK_CAS_MODE'] == 'controlled'
    token = os.environ['ECOMOPS_STOCK_CAS_TOKEN']
    scope = os.environ['ECOMOPS_STOCK_CAS_SCOPE'].split(',')
    medusa = Medusa()
    assert _fixture_endpoint(medusa.url)

    item, = await medusa.list_all('inventory-items', 'inventory_items', {'sku': 'ECOMOPS-BAG'})
    assert item['sku'] == 'ECOMOPS-BAG'
    level, = item['location_levels']
    item_id, location_id = item['id'], level['location_id']
    assert level['inventory_item_id'] == item_id
    assert f'{item_id}:{location_id}' in scope
    return medusa, token, level


async def test_live_stock_receipt_competition_and_replay(bag_fixture):
    medusa, token, level = bag_fixture
    item_id, location_id = level['inventory_item_id'], level['location_id']
    item_path = f'/admin/inventory-items/{item_id}'
    stocked, reserved = level['stocked_quantity'], level['reserved_quantity']
    assert type(stocked) is int and type(reserved) is int

    async def receive(payload, receipt_token=token):
        return await medusa.request('POST', RECEIPTS, json=payload, headers={
            'x-ecomops-receipt-token': receipt_token,
        })

    async def read_level():
        result = await medusa.request('GET', item_path)
        return next(row for row in result['inventory_item']['location_levels'] if row['id'] == level['id'])

    async def restore_stock():
        await medusa.request('POST', f'{item_path}/location-levels/{location_id}', json={
            'stocked_quantity': stocked,
        })

    def assert_quantities(row, expected_stocked):
        assert row['stocked_quantity'] == expected_stocked
        assert row['reserved_quantity'] == reserved
        assert row['available_quantity'] == expected_stocked - reserved
        assert Decimal(row['raw_stocked_quantity']['value']) == expected_stocked
        assert row['raw_reserved_quantity'] == level['raw_reserved_quantity']

    base = {
        'inventory_item_id': item_id, 'location_id': location_id,
        'expected_stocked': stocked, 'expected_reserved': reserved,
    }
    requests = [dict(base, idempotency_key=str(uuid4()), quantity=quantity) for quantity in (2, 5)]

    try:
        with pytest.raises(httpx.HTTPStatusError) as bad_token:
            await receive(requests[0], receipt_token=str(uuid4()))
        assert bad_token.value.response.status_code == 404
        assert bad_token.value.response.json()['code'] == 'stock_cas_unavailable'

        outside_scope = dict(requests[0], location_id=f'sloc_{uuid4().hex}')
        with pytest.raises(httpx.HTTPStatusError) as denied:
            await receive(outside_scope)
        assert denied.value.response.status_code == 404
        assert denied.value.response.json()['code'] == 'stock_cas_unavailable'

        with pytest.raises(httpx.HTTPStatusError) as stale_reserved:
            await receive(dict(requests[0], idempotency_key=str(uuid4()), expected_reserved=reserved + 1))
        assert stale_reserved.value.response.status_code == 409
        assert stale_reserved.value.response.json()['code'] == 'stale_inventory_snapshot'
        assert_quantities(await read_level(), stocked)

        outcomes = await asyncio.gather(*(receive(payload) for payload in requests), return_exceptions=True)
        winners = [index for index, result in enumerate(outcomes) if isinstance(result, dict)]
        winner, = winners
        failure = outcomes[1 - winner]
        assert isinstance(failure, httpx.HTTPStatusError), outcomes
        assert failure.response.status_code == 409
        assert failure.response.json()['code'] == 'stale_inventory_snapshot'
        winning_request = requests[winner]
        after_race = stocked + winning_request['quantity']
        receipt = outcomes[winner]
        assert receipt == {
            'inventory_item_id': item_id, 'location_id': location_id,
            'before_stocked': stocked, 'after_stocked': after_race,
            'available_quantity': after_race - reserved,
        }
        assert_quantities(await read_level(), after_race)

        next_request = dict(base, idempotency_key=str(uuid4()), quantity=3, expected_stocked=after_race)
        duplicates = await asyncio.gather(receive(next_request), receive(next_request), return_exceptions=True)
        assert duplicates[0] == duplicates[1] == {
            'inventory_item_id': item_id, 'location_id': location_id,
            'before_stocked': after_race, 'after_stocked': after_race + 3,
            'available_quantity': after_race + 3 - reserved,
        }
        assert await receive(winning_request) == receipt

        with pytest.raises(httpx.HTTPStatusError) as fingerprint_conflict:
            await receive(dict(winning_request, quantity=winning_request['quantity'] + 1))
        assert fingerprint_conflict.value.response.status_code == 409
        assert fingerprint_conflict.value.response.json()['code'] == 'idempotency_conflict'
        assert_quantities(await read_level(), after_race + 3)

        await restore_stock()
        with pytest.raises(httpx.HTTPStatusError) as stale_replay:
            await receive(requests[1 - winner])
        assert stale_replay.value.response.status_code == 409
        assert stale_replay.value.response.json()['code'] == 'stale_inventory_snapshot'
        assert_quantities(await read_level(), stocked)
    finally:
        # Restore the disposable bag fixture for the rest of the live suite.
        await restore_stock()
        assert_quantities(await read_level(), stocked)


async def test_live_stock_receipt_checks_snapshot_after_database_writer(bag_fixture):
    dsn = os.environ.get('ECOMOPS_TEST_POSTGRES_URL')
    if not dsn:
        pytest.skip('requires the disposable Medusa PostgreSQL connection')
    from psycopg import AsyncConnection
    from psycopg.conninfo import conninfo_to_dict

    connection = conninfo_to_dict(dsn)
    assert connection.get('host') in ('127.0.0.1', '::1')
    assert connection.get('hostaddr', connection['host']) == connection['host']
    medusa, token, level = bag_fixture
    item_id, location_id = level['inventory_item_id'], level['location_id']
    stocked, reserved = level['stocked_quantity'], level['reserved_quantity']
    item_path = f'/admin/inventory-items/{item_id}'
    update_path = f'{item_path}/location-levels/{location_id}'
    pending = None
    try:
        async with await AsyncConnection.connect(dsn, autocommit=True, connect_timeout=5) as writer:
            async with writer.transaction():
                updated = await writer.execute('''
                    UPDATE inventory_level AS level
                    SET stocked_quantity = level.stocked_quantity + 1,
                        raw_stocked_quantity = jsonb_set(level.raw_stocked_quantity, '{value}',
                            to_jsonb((level.stocked_quantity + 1)::text))
                    FROM inventory_item AS item
                    WHERE item.id = level.inventory_item_id AND item.sku = 'ECOMOPS-BAG'
                      AND item.deleted_at IS NULL AND level.deleted_at IS NULL
                      AND level.id = %s AND level.inventory_item_id = %s AND level.location_id = %s
                      AND level.stocked_quantity = %s AND level.reserved_quantity = %s
                ''', (level['id'], item_id, location_id, stocked, reserved))
                assert updated.rowcount == 1
                pending = asyncio.create_task(medusa.request('POST', RECEIPTS, headers={
                    'x-ecomops-receipt-token': token,
                }, json={
                    'idempotency_key': str(uuid4()), 'inventory_item_id': item_id, 'location_id': location_id,
                    'quantity': 7, 'expected_stocked': stocked, 'expected_reserved': reserved,
                }))
                async with asyncio.timeout(12):
                    while True:
                        await writer.execute('SELECT pg_stat_clear_snapshot()')
                        cursor = await writer.execute('''
                            SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                            WHERE datname = current_database() AND pid <> pg_backend_pid()
                              AND pg_backend_pid() = ANY(pg_blocking_pids(pid)))
                        ''')
                        if (await cursor.fetchone())[0]:
                            break
                        assert not pending.done(), 'receipt returned without waiting for the stock writer'
                        await asyncio.sleep(0.05)

        with pytest.raises(httpx.HTTPStatusError) as stale:
            await pending
        assert stale.value.response.status_code == 409
        assert stale.value.response.json()['code'] == 'stale_inventory_snapshot'
        after, = (await medusa.request('GET', item_path))['inventory_item']['location_levels']
        assert after['stocked_quantity'] == stocked + 1
        assert Decimal(after['raw_stocked_quantity']['value']) == stocked + 1
        assert after['raw_reserved_quantity'] == level['raw_reserved_quantity']
        assert after['reserved_quantity'] == reserved
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
            await medusa.request('POST', update_path, json={'stocked_quantity': stocked})
            restored, = (await medusa.request('GET', item_path))['inventory_item']['location_levels']
            assert restored['stocked_quantity'] == stocked
