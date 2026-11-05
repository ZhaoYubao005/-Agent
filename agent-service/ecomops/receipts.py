"""Approved stock receipts for isolated demo fixtures."""
import os
import re

from ecomops.medusa import Medusa


_DEMO_SKUS = {'ECOMOPS-MUG', 'ECOMOPS-BAG', 'ECOMOPS-BOTTLE'}
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,255}')
_LOOPBACK_URL = re.compile(r'https?://(?:127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?/?')
_MAX_SAFE_INTEGER = 2 ** 53 - 1


def _fixture_endpoint(url: str) -> bool:
    if url == 'http://medusa:9000':
        return os.environ.get('ECOMOPS_CONTAINER_FIXTURES') == '1'
    match = _LOOPBACK_URL.fullmatch(url)
    return bool(match and (match[1] is None or 1 <= int(match[1]) <= 65535))


def _safe_integer(value: object) -> bool:
    return type(value) is int and abs(value) <= _MAX_SAFE_INTEGER


def _fixture_level(response: dict, item_id: str, location_id: str) -> tuple[str, dict]:
    try:
        item = response['inventory_item']
        sku = item['sku']
        if item['id'] != item_id or sku not in _DEMO_SKUS:
            raise ValueError('Stock receipts require an isolated demo fixture')
        levels = [level for level in item['location_levels'] if level['location_id'] == location_id]
        if len(levels) != 1:
            raise ValueError('Expected one inventory level at the approved location')
        level = levels[0]
        if level['inventory_item_id'] != item_id or not _IDENTIFIER.fullmatch(level['id']):
            raise ValueError('Inventory level did not match the approved item')
        stocked, reserved, available = (level[field] for field in (
            'stocked_quantity', 'reserved_quantity', 'available_quantity',
        ))
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid Medusa inventory snapshot') from exc
    if not all(_safe_integer(value) for value in (stocked, reserved, available)):
        raise ValueError('Stock receipts require whole inventory quantities')
    if reserved < 0 or available != stocked - reserved:
        raise ValueError('Inconsistent Medusa inventory quantities')
    return sku, level


def validate_receipt_environment():
    if os.environ.get('ECOMOPS_ALLOW_REPLENISHMENT') != '1' or os.environ.get('ECOMOPS_ISOLATED_FIXTURES') != '1':
        raise ValueError('Stock writes are disabled')
    if not _fixture_endpoint(Medusa().url):
        raise ValueError('Stock receipts require a local fixture Medusa endpoint')


async def replenish_inventory(inventory_item_id: str, location_id: str,
                              quantity: int, expected_stocked: int, expected_reserved: int) -> dict:
    """Receive approved demo stock in an environment with exclusive fixture writes."""
    validate_receipt_environment()
    if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value)
               for value in (inventory_item_id, location_id)):
        raise ValueError('Invalid Medusa item or location identifier')
    if type(quantity) is not int or not 1 <= quantity <= 10000:
        raise ValueError('quantity must be 1..10000')
    if not _safe_integer(expected_stocked) or not _safe_integer(expected_reserved) or expected_reserved < 0:
        raise ValueError('Invalid approved inventory quantities')
    target = expected_stocked + quantity
    if not _safe_integer(target):
        raise ValueError('Receipt exceeds the safe integer range')

    medusa = Medusa()
    item_path = f'/admin/inventory-items/{inventory_item_id}'
    before = await medusa.request('GET', item_path)
    sku, level = _fixture_level(before, inventory_item_id, location_id)
    if level['stocked_quantity'] != expected_stocked or level['reserved_quantity'] != expected_reserved:
        raise ValueError('Stale inventory snapshot: run analysis again')

    # The stock check and absolute update are separate Medusa requests.
    await medusa.request('POST', f'{item_path}/location-levels/{location_id}', json={'stocked_quantity': target})
    after = await medusa.request('GET', item_path)
    try:
        after_sku, verified = _fixture_level(after, inventory_item_id, location_id)
    except ValueError as exc:
        raise RuntimeError('Invalid inventory read-back after stock update') from exc
    if (after_sku != sku or verified['id'] != level['id'] or verified['stocked_quantity'] != target
            or verified['reserved_quantity'] != expected_reserved):
        raise RuntimeError('Inventory read-back did not match approved receipt')
    return {'inventory_item_id': inventory_item_id, 'location_id': location_id,
            'before_stocked': expected_stocked, 'after_stocked': target,
            'available_quantity': verified['available_quantity']}
