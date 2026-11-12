"""Post and reconcile fictional payments after tests that expect unpaid fixtures."""
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from ecomops.medusa import Medusa
from ecomops.receipts import _fixture_endpoint


REFUND_KEY = 'ecomops-fixture-payment-ledger-v1'
PROVIDER = 'pp_system_default'


@pytest.mark.skipif(os.environ.get('ECOMOPS_ISOLATED_FIXTURES') != '1',
                    reason='requires disposable local Medusa fixtures')
async def test_live_daily_payment_ledger_reconciles_to_manual_provider_records():
    medusa = Medusa()
    assert _fixture_endpoint(medusa.url)
    orders = await medusa.list_all('orders', 'orders')
    assert len(orders) == 2
    assert all((order.get('metadata') or {}).get('ecomops_demo') is True for order in orders)
    by_reference = {order['metadata']['ecomops_reference']: order for order in orders}
    assert set(by_reference) == {'ecomops-demo-order-1', 'ecomops-demo-order-2'}
    order_id = by_reference['ecomops-demo-order-1']['id']
    order_path = f'/admin/orders/{order_id}'
    order = (await medusa.request('GET', order_path, params={'fields': '+email'}))['order']
    assert order['email'] == 'fictional-buyer-1@example.test'
    capture_amount = Decimal(str(order['original_total']))
    refund_amount = Decimal('3.25')
    assert capture_amount == Decimal('20')
    collections = order.get('payment_collections') or []
    if not collections:
        collection = (await medusa.request('POST', '/admin/payment-collections', json={
            'order_id': order_id, 'amount': float(capture_amount),
        }))['payment_collection']
    else:
        assert len(collections) == 1
        collection = collections[0]
        assert Decimal(str(collection['amount'])) == capture_amount

    if not collection.get('payments'):
        collection = (await medusa.request(
            'POST', f'/admin/payment-collections/{collection["id"]}/mark-as-paid',
            json={'order_id': order_id, 'provider_id': PROVIDER},
        ))['payment_collection']
    payment, = collection['payments']
    payment_path = f'/admin/payments/{payment["id"]}'
    payment_fields = {
        'fields': 'id,provider_id,currency_code,captures.id,captures.amount,'
                  'refunds.id,refunds.amount,refunds.idempotency_key,refunds.status',
    }
    payment = (await medusa.request('GET', payment_path, params=payment_fields))['payment']
    assert payment['provider_id'] == PROVIDER
    if not payment.get('refunds'):
        await medusa.request('POST', f'{payment_path}/refund', json={
            'amount': float(refund_amount), 'idempotency_key': REFUND_KEY,
        })
        payment = (await medusa.request('GET', payment_path, params=payment_fields))['payment']

    capture, = payment['captures']
    refund, = payment['refunds']
    assert Decimal(str(capture['amount'])) == capture_amount
    assert Decimal(str(refund['amount'])) == refund_amount
    assert refund['status'] == 'succeeded'
    assert refund['idempotency_key'] == REFUND_KEY
    currency = payment['currency_code']

    detail = (await medusa.request('GET', order_path, params={
        'fields': 'id,transactions.id,transactions.reference_id,transactions.reference,'
                  'transactions.created_at,transactions.amount,transactions.currency_code',
    }))['order']
    postings = {row['reference']: row for row in detail['transactions']}
    assert set(postings) == {'capture', 'refund'}
    expected_by_day = defaultdict(lambda: {'capture': Decimal(0), 'refund': Decimal(0)})
    for kind, record, expected in [('capture', capture, capture_amount), ('refund', refund, refund_amount)]:
        posting = postings[kind]
        assert posting['reference_id'] == record['id']
        assert posting['currency_code'] == currency
        assert Decimal(str(posting['amount'])) == (expected if kind == 'capture' else -expected)
        day = datetime.fromisoformat(posting['created_at']).astimezone(timezone.utc).date()
        expected_by_day[day][kind] = expected

    headers = {'Authorization': f'Bearer {os.environ["ECOMOPS_API_TOKEN"]}'}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', headers=headers,
                                 timeout=120, trust_env=False) as agent:
        for day, expected in expected_by_day.items():
            result = await agent.get('/operations/reports/daily', params={'day': day.isoformat(), 'zone': 'UTC'})
            assert result.status_code == 200, result.text
            current = result.json()['current']
            ledger = current['payment_ledger']
            assert ledger['status'] == 'available', ledger
            assert ledger['source'] == 'medusa_order_transactions'
            assert set(ledger['by_currency']) == {currency}
            amounts = ledger['by_currency'][currency]
            assert Decimal(amounts['captured_amount']) == expected['capture']
            assert Decimal(amounts['refunded_amount']) == expected['refund']
            assert Decimal(amounts['net_posted_amount']) == expected['capture'] - expected['refund']
            assert amounts['capture_count'] == int(expected['capture'] > 0)
            assert amounts['refund_count'] == int(expected['refund'] > 0)
            assert current['revenue'] is None
            assert current['refund_amount'] is None

        csv_headers = {'Content-Type': 'text/csv'}
        bank_rows = ['event_id,event_type,reference_id,currency,gross,fee,net,posted_at']
        for kind, posting in postings.items():
            gross = Decimal(str(posting['amount']))
            fee = Decimal('1.25') if kind == 'capture' else Decimal(0)
            bank_rows.append(','.join(map(str, (
                f"bank_{posting['reference_id']}", kind, posting['reference_id'], currency,
                gross, fee, gross - fee, posting['created_at'],
            ))))
        bank_csv = '\n'.join(bank_rows) + '\n'
        imported = await agent.post('/operations/finance/import/bank', content=bank_csv, headers=csv_headers)
        assert imported.status_code == 200, imported.text
        replay = await agent.post('/operations/finance/import/bank', content=bank_csv, headers=csv_headers)
        assert replay.json()['duplicate_count'] == 2

        for day, expected in expected_by_day.items():
            revenue_csv = f'entry_id,date,currency,amount\nrecognized_{day},{day},{currency},14.20\n'
            imported = await agent.post('/operations/finance/import/revenue', content=revenue_csv, headers=csv_headers)
            assert imported.status_code == 200, imported.text
            report = await agent.get('/operations/finance/reconciliation',
                                     params={'day': day.isoformat(), 'zone': 'UTC'})
            assert report.status_code == 200, report.text
            finance = report.json()
            bank = finance['bank_reconciliation']
            assert bank['counts']['matched'] == int(expected['capture'] > 0) + int(expected['refund'] > 0)
            fee = Decimal('1.25') if expected['capture'] else Decimal(0)
            assert Decimal(bank['by_currency'][currency]['net']) == expected['capture'] - expected['refund'] - fee
            assert Decimal(finance['recognized_revenue']['by_currency'][currency]['recognized_amount']) == Decimal('14.20')
