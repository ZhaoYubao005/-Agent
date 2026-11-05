"""Requires running development services; touches ECOMOPS-* fixtures only."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import httpx
import pytest
from ecomops.medusa import Medusa
from ecomops.tools import commerce_tools, call


@pytest.mark.asyncio
async def test_real_api_mcp_langgraph_replenishment():
    medusa = Medusa()
    products = await medusa.list_all('products', 'products')
    orders = await medusa.list_all('orders', 'orders')
    inventory = await medusa.list_all('inventory-items', 'inventory_items')
    demo_products = [p for p in products if (p.get('metadata') or {}).get('ecomops_demo')]
    demo_orders = [o for o in orders if (o.get('metadata') or {}).get('ecomops_demo')]
    demo_inventory = {i['sku']: i for i in inventory if (i.get('sku') or '').startswith('ECOMOPS-')}
    assert len(demo_products) == 3
    assert len(demo_orders) == 2
    assert set(demo_inventory) == {'ECOMOPS-MUG', 'ECOMOPS-BAG', 'ECOMOPS-BOTTLE'}
    for order in demo_orders:
        detail = await medusa.request('GET', f'/admin/orders/{order["id"]}')
        assert detail['order']['status'] == 'pending'
        assert detail['order']['items'][0]['quantity'] == 1
    # Repeatable test: reset only fictional inventory through the official API.
    # Two real pending orders reserve two mugs; starting available stock is three.
    for sku, stocked in [('ECOMOPS-MUG', 5), ('ECOMOPS-BAG', 50), ('ECOMOPS-BOTTLE', 0)]:
        item = demo_inventory[sku]
        level = item['location_levels'][0]
        await medusa.request('POST', f'/admin/inventory-items/{item["id"]}/location-levels/{level["location_id"]}',
                             json={'stocked_quantity': stocked})
    async with commerce_tools() as session:
        names = [t.name for t in (await session.list_tools()).tools]
        assert set(names) == {'list_products', 'list_orders', 'list_inventory', 'replenish_inventory'}
        assert len(await call(session, 'list_products')) == len(products)
        assert len(await call(session, 'list_orders')) == len(orders)
        mcp_inventory = await call(session, 'list_inventory')
        mug = next(i for i in mcp_inventory if i.get('sku') == 'ECOMOPS-MUG')
        assert mug['location_levels'][0]['reserved_quantity'] == 2
        assert mug['location_levels'][0]['available_quantity'] == 3
    headers = {'Authorization': f'Bearer {os.environ["ECOMOPS_API_TOKEN"]}'}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', timeout=90, trust_env=False) as agent:
        assert (await agent.get('/health')).status_code == 200
        assert (await agent.post('/inventory/check', json={})).status_code in (401, 403)
        assert (await agent.post('/inventory/check', json={}, headers={'Authorization': 'Bearer wrong'})).status_code == 401
        assert (await agent.post('/inventory/check', json={'threshold': 30, 'target': 20}, headers=headers)).status_code == 422
        response = await agent.post('/inventory/check', json={}, headers=headers)
        assert response.status_code == 200, response.text
        before = response.json()
        proposals = {p['sku']: p for p in before['recommendations']}
        assert set(proposals) == {'ECOMOPS-MUG', 'ECOMOPS-BOTTLE'}
        assert proposals['ECOMOPS-MUG']['quantity'] == 27
        assert proposals['ECOMOPS-BOTTLE']['quantity'] == 30
        extra = await agent.post('/inventory/check', json={}, headers=headers)
        assert extra.status_code == 200
        stale_proposal = next(p for p in extra.json()['recommendations'] if p['sku'] == 'ECOMOPS-MUG')
        receipts = []
        for proposal in proposals.values():
            path = f'/recommendations/{proposal["id"]}/approve'
            first = await agent.post(path, headers=headers)
            assert first.status_code == 200, first.text
            applied = first.json()
            assert applied['status'] == 'applied'
            assert applied['receipt']['available_quantity'] == 30
            second = await agent.post(path, headers=headers)
            assert second.status_code == 200
            assert second.json() == applied  # no duplicate stock receipt
            receipts.append(applied)
        after_response = await agent.post('/inventory/check', json={}, headers=headers)
        assert after_response.status_code == 200
        after = after_response.json()
        assert after['recommendations'] == []
        # Another proposal based on the old inventory snapshot must not overwrite receipts.
        stale = await agent.post(f'/recommendations/{stale_proposal["id"]}/approve', headers=headers)
        assert stale.status_code == 409
        stale_audit = await agent.get(f'/recommendations/{stale_proposal["id"]}', headers=headers)
        assert stale_audit.json()['status'] == 'needs_review'
    final = await medusa.list_all('inventory-items', 'inventory_items')
    final_demo = [i for i in final if (i.get('sku') or '').startswith('ECOMOPS-')]
    for item in final_demo:
        level = item['location_levels'][0]
        assert level['available_quantity'] == (50 if item['sku'] == 'ECOMOPS-BAG' else 30)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:9000', trust_env=False) as client:
        health = await client.get('/health')
        admin = await client.get('/app')
        assert health.status_code == admin.status_code == 200
        assert '<html' in admin.text.lower()
    evidence = {
        'executed_at': datetime.now(timezone.utc).isoformat(),
        'medusa_version': '2.22.3', 'http': {'medusa_health': health.status_code, 'admin': admin.status_code},
        'demo_product_ids': [p['id'] for p in demo_products],
        'demo_orders': [{'id': o['id'], 'status': o['status']} for o in demo_orders],
        'mcp_tools': names, 'before': before, 'receipts': receipts, 'after': after,
        'final_demo_inventory': [{'sku': i['sku'], 'levels': i['location_levels']} for i in final_demo],
        'assertions': ['real_admin_api_reads', 'real_mcp_handshake_and_tool_calls', 'reserved_stock_in_policy',
                       'unauthorized_rejected', 'invalid_policy_rejected', 'approved_receipt_readback',
                       'duplicate_approval_idempotent', 'stale_snapshot_rejected', 'alerts_cleared'],
    }
    destination = Path(__file__).resolve().parents[1] / 'docs/evidence/phase1-live.json'
    destination.write_text(json.dumps(evidence, indent=2))
