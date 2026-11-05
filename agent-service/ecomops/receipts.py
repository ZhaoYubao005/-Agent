from ecomops.medusa import Medusa
import os


async def replenish_inventory(inventory_item_id: str, location_id: str,
                              quantity: int, expected_stocked: int, expected_reserved: int) -> dict:
    """Receive approved demo stock, rejecting stale snapshots. Development only."""
    if os.environ.get('ECOMOPS_ALLOW_REPLENISHMENT') != '1':
        raise ValueError('Stock writes are disabled')
    if not 1 <= quantity <= 10000:
        raise ValueError('quantity must be 1..10000')
    medusa = Medusa()
    item = await medusa.request('GET', f'/admin/inventory-items/{inventory_item_id}')
    levels = item['inventory_item']['location_levels']
    level = next((x for x in levels if x['location_id'] == location_id), None)
    if (level is None or int(level['stocked_quantity']) != expected_stocked
            or int(level['reserved_quantity']) != expected_reserved):
        raise ValueError('Stale inventory snapshot: run analysis again')
    target = expected_stocked + quantity
    await medusa.request('POST',
        f'/admin/inventory-items/{inventory_item_id}/location-levels/{location_id}',
        json={'stocked_quantity': target})
    after = await medusa.request('GET', f'/admin/inventory-items/{inventory_item_id}')
    verified = next(x for x in after['inventory_item']['location_levels']
                    if x['location_id'] == location_id)
    if int(verified['stocked_quantity']) != target:
        raise RuntimeError('Inventory read-back did not match approved receipt')
    return {'inventory_item_id': inventory_item_id, 'location_id': location_id,
            'before_stocked': expected_stocked, 'after_stocked': target,
            'available_quantity': int(verified['available_quantity'])}
