"""Fixture receipt transport and read-back checks without writing to Medusa."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from ecomops import receipts


ITEM_ID = 'iitem_fixture'
LOCATION_ID = 'sloc_fixture'
ITEM_PATH = f'/admin/inventory-items/{ITEM_ID}'
LEVEL_PATH = f'{ITEM_PATH}/location-levels/{LOCATION_ID}'


def inventory(stocked=5, reserved=2):
    return {'inventory_item': {'id': ITEM_ID, 'sku': 'ECOMOPS-MUG', 'location_levels': [{
        'id': 'ilev_fixture', 'inventory_item_id': ITEM_ID, 'location_id': LOCATION_ID,
        'stocked_quantity': stocked, 'reserved_quantity': reserved,
        'available_quantity': stocked - reserved,
    }]}}


@pytest.fixture
def medusa(monkeypatch):
    monkeypatch.setenv('ECOMOPS_ALLOW_REPLENISHMENT', '1')
    monkeypatch.setenv('ECOMOPS_ISOLATED_FIXTURES', '1')
    monkeypatch.delenv('ECOMOPS_CONTAINER_FIXTURES', raising=False)
    monkeypatch.setenv('MEDUSA_URL', 'http://127.0.0.1:9000')
    request = AsyncMock(side_effect=[inventory(), inventory(32), inventory(32)])
    monkeypatch.setattr(receipts.Medusa, 'request', request)
    return request


def receive(**overrides):
    return asyncio.run(receipts.replenish_inventory(**{
        'inventory_item_id': ITEM_ID, 'location_id': LOCATION_ID, 'quantity': 27,
        'expected_stocked': 5, 'expected_reserved': 2, **overrides,
    }))


@pytest.mark.parametrize('flag', ['ECOMOPS_ALLOW_REPLENISHMENT', 'ECOMOPS_ISOLATED_FIXTURES'])
def test_receipt_requires_both_explicit_write_flags(monkeypatch, medusa, flag):
    monkeypatch.delenv(flag)
    with pytest.raises(ValueError, match='Stock writes are disabled'):
        receive()
    medusa.assert_not_awaited()


@pytest.mark.parametrize('url', [
    'https://store.example.test', 'http://medusa:9000', 'http://127.0.0.1@store.example.test:9000',
    'http://127.0.0.1:9000/forward', 'http://127.0.0.1:9000?forward=1',
])
def test_receipt_refuses_nonfixture_endpoint_before_authentication(monkeypatch, medusa, url):
    monkeypatch.setenv('MEDUSA_URL', url)
    with pytest.raises(ValueError, match='local fixture Medusa endpoint'):
        receive()
    medusa.assert_not_awaited()


@pytest.mark.parametrize('url', ['http://127.0.0.1:9000', 'http://[::1]:9000', 'http://medusa:9000'])
def test_receipt_accepts_explicit_fixture_endpoints(monkeypatch, medusa, url):
    monkeypatch.setenv('MEDUSA_URL', url)
    if url == 'http://medusa:9000':
        monkeypatch.setenv('ECOMOPS_CONTAINER_FIXTURES', '1')
    assert receive() == {
        'inventory_item_id': ITEM_ID, 'location_id': LOCATION_ID, 'before_stocked': 5,
        'after_stocked': 32, 'available_quantity': 30,
    }
    assert [call.args for call in medusa.await_args_list] == [
        ('GET', ITEM_PATH), ('POST', LEVEL_PATH), ('GET', ITEM_PATH),
    ]
    assert medusa.await_args_list[1].kwargs == {'json': {'stocked_quantity': 32}}


@pytest.mark.parametrize('overrides', [
    {'inventory_item_id': '../inventory-items'}, {'location_id': 'x/y'}, {'quantity': True},
    {'quantity': 27.5}, {'expected_stocked': 5.5}, {'expected_reserved': -1},
    {'expected_stocked': 2 ** 53 - 1},
])
def test_receipt_rejects_invalid_approval_payload_before_network(medusa, overrides):
    with pytest.raises(ValueError):
        receive(**overrides)
    medusa.assert_not_awaited()


@pytest.mark.parametrize('mutation', ['wrong_id', 'wrong_level_item', 'non_fixture_sku',
                                      'fractional_stock', 'duplicate_level', 'stale_reserved'])
def test_receipt_refuses_untrusted_or_stale_snapshot_before_writing(medusa, mutation):
    before = inventory()
    item = before['inventory_item']
    level = item['location_levels'][0]
    if mutation == 'wrong_id':
        item['id'] = 'iitem_unrelated'
    elif mutation == 'wrong_level_item':
        level['inventory_item_id'] = 'iitem_unrelated'
    elif mutation == 'non_fixture_sku':
        item['sku'] = 'OTHER-MUG'
    elif mutation == 'fractional_stock':
        level['stocked_quantity'] = 5.9
    elif mutation == 'duplicate_level':
        item['location_levels'].append(deepcopy(level))
    elif mutation == 'stale_reserved':
        level.update(reserved_quantity=3, available_quantity=2)
    medusa.side_effect = [before, inventory(32), inventory(32)]
    with pytest.raises(ValueError):
        receive()
    medusa.assert_awaited_once_with('GET', ITEM_PATH)


@pytest.mark.parametrize('mutation', ['reservation_changed', 'invalid_available', 'level_replaced'])
def test_receipt_requires_consistent_readback_after_write(medusa, mutation):
    after = inventory(32)
    level = after['inventory_item']['location_levels'][0]
    if mutation == 'reservation_changed':
        level.update(reserved_quantity=3, available_quantity=29)
    elif mutation == 'invalid_available':
        level['available_quantity'] = 29
    elif mutation == 'level_replaced':
        level['id'] = 'ilev_replaced'
    medusa.side_effect = [inventory(), inventory(32), after]
    with pytest.raises(RuntimeError, match='read-back'):
        receive()
    assert [call.args[0] for call in medusa.await_args_list] == ['GET', 'POST', 'GET']
