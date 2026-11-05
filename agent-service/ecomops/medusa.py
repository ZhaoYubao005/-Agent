"""Authenticated Medusa API adapter. Credentials never enter tool results."""
import asyncio
from collections import defaultdict
import os
import httpx

from ecomops.operations import INACTIVE_ORDER_STATUSES, OPEN_FULFILLMENT_STATUSES


ORDER_FIELDS = (
    'id', 'display_id', 'status', 'created_at', 'currency_code', 'total',
    'items.id', 'items.variant_id', 'items.quantity', 'items.raw_quantity', 'items.detail.fulfilled_quantity',
    'items.detail.raw_fulfilled_quantity',
)
MONITOR_FIELDS = (
    'sales_channel_id', 'items.variant.manage_inventory',
    'items.variant.inventory_items.inventory.id',
    'items.variant.inventory_items.required_quantity',
)
DEMAND_FIELDS = (
    'fulfillments.id', 'fulfillments.location_id', 'fulfillments.created_at', 'fulfillments.canceled_at',
    'fulfillments.items.inventory_item_id', 'fulfillments.items.quantity',
)
LEDGER_FIELDS = (
    'id', 'transactions.id', 'transactions.reference', 'transactions.reference_id',
    'transactions.created_at', 'transactions.currency_code', 'transactions.raw_amount', 'transactions.amount',
)


class Medusa:
    def __init__(self):
        self.url = os.environ.get('MEDUSA_URL', 'http://127.0.0.1:9000')

    async def request(self, method, path, **kwargs):
        attempts = 2 if method.upper() in {'GET', 'HEAD'} else 1
        for attempt in range(attempts):
            try:
                return await self._request_once(method, path, **kwargs)
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                transient = isinstance(error, httpx.TransportError) or error.response.status_code in {502, 503, 504}
                if not transient or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(0.2)

    async def _request_once(self, method, path, **kwargs):
        async with httpx.AsyncClient(base_url=self.url, timeout=30, trust_env=False) as client:
            auth = await client.post('/auth/user/emailpass', json={
                'email': os.environ['MEDUSA_ADMIN_EMAIL'],
                'password': os.environ['MEDUSA_ADMIN_PASSWORD'],
            })
            auth.raise_for_status()
            response = await client.request(method, path, headers={
                'Authorization': f'Bearer {auth.json()["token"]}'}, **kwargs)
            response.raise_for_status()
            return response.json()

    async def list_all(self, resource, key, params=None):
        rows = []
        ids = set()
        expected_count = None
        while len(rows) < 10000:
            page = await self.request('GET', f'/admin/{resource}', params={
                **(params or {}), 'order': 'id', 'limit': 100, 'offset': len(rows),
            })
            batch = page[key]
            count = page['count']
            if count > 10000:
                break
            if expected_count is not None and count != expected_count:
                raise RuntimeError('Snapshot count changed during pagination; retry analysis')
            expected_count = count
            batch_ids = {row['id'] for row in batch}
            if ids & batch_ids or len(batch_ids) != len(batch) or len(rows) + len(batch) > count:
                raise RuntimeError('Snapshot changed during pagination; refusing partial analysis')
            ids.update(batch_ids)
            rows.extend(batch)
            if len(rows) == count:
                return rows
            if not batch:
                raise RuntimeError('Snapshot ended before the reported count; refusing partial analysis')
        raise RuntimeError('Snapshot exceeds 10000-record limit; refusing partial analysis')

    async def operational_orders(self, purpose):
        if purpose == 'ledger':
            orders = await self.list_all('orders', 'orders', {'fields': ','.join(LEDGER_FIELDS)})
            return [{'id': order['id'], 'transactions': order.get('transactions')} for order in orders]
        extra_fields = {'monitor': MONITOR_FIELDS, 'demand': DEMAND_FIELDS}[purpose]
        orders = await self.list_all('orders', 'orders', {'fields': ','.join(ORDER_FIELDS + extra_fields)})
        if purpose != 'monitor':
            return orders

        candidates = [order for order in orders
                      if order.get('status') not in INACTIVE_ORDER_STATUSES
                      and order.get('fulfillment_status') in OPEN_FULFILLMENT_STATUSES]
        by_channel = {}
        for channel in sorted({order['sales_channel_id'] for order in candidates if order.get('sales_channel_id')}):
            try:
                locations = await self.list_all('stock-locations', 'stock_locations', {
                    'fields': 'id', 'sales_channel_id': channel,
                })
                by_channel[channel] = [location['id'] for location in locations]
            except (httpx.HTTPError, RuntimeError):
                by_channel[channel] = None

        line_ids = sorted({item['id'] for order in candidates for item in order.get('items') or [] if item.get('id')})
        reservations = defaultdict(list)
        for offset in range(0, len(line_ids), 50):
            batch = line_ids[offset:offset + 50]
            try:
                rows = await self.list_all('reservations', 'reservations', {
                    'fields': 'id,line_item_id,inventory_item_id,location_id,quantity',
                    'line_item_id[]': batch,
                })
                if any(row.get('line_item_id') not in batch or not row.get('inventory_item_id')
                       or not row.get('location_id') for row in rows):
                    raise RuntimeError('Incomplete reservation snapshot')
            except (httpx.HTTPError, RuntimeError):
                for line_id in batch:
                    reservations[line_id] = None
                continue
            for row in rows:
                reservations[row['line_item_id']].append(row)

        for order in candidates:
            order['eligible_location_ids'] = by_channel.get(order.get('sales_channel_id'))
            per_line = [reservations[item['id']] for item in order.get('items') or [] if item.get('id')]
            order['reservations'] = None if any(rows is None for rows in per_line) else [
                row for rows in per_line for row in rows
            ]
        return orders
