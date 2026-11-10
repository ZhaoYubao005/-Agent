"""Access, persistence, and evidence separation for the finance API."""
import json

from fastapi.testclient import TestClient

from ecomops import finance_api
from ecomops.app import app


BANK = (
    'event_id,event_type,reference_id,currency,gross,fee,net,posted_at\n'
    'bank_1,capture,cap_1,EUR,20.00,1.25,18.75,2026-11-10T00:30:00Z\n'
)
REVENUE = 'entry_id,date,currency,amount\nrecognized_1,2026-11-10,EUR,14.00\n'
BANK_URL = '/operations/finance/import/bank'
REVENUE_URL = '/operations/finance/import/revenue'
REPORT_URL = '/operations/finance/reconciliation?day=2026-11-10&zone=Asia/Shanghai'
CSV = {'Content-Type': 'text/csv'}


def clients(monkeypatch, tmp_path):
    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'finance.db'))
    monkeypatch.setenv('ECOMOPS_ACCOUNTS', json.dumps({
        'finance-admin': {'role': 'admin', 'token': 'fixture-admin'},
        'viewer': {'role': 'viewer', 'token': 'fixture-viewer'},
    }))
    return (
        TestClient(app, headers={'Authorization': 'Bearer fixture-admin'}),
        TestClient(app, headers={'Authorization': 'Bearer fixture-viewer'}),
    )


def test_imports_are_authorized_and_transactional(monkeypatch, tmp_path):
    admin, viewer = clients(monkeypatch, tmp_path)
    assert viewer.post(BANK_URL, content=BANK, headers=CSV).status_code == 403
    assert viewer.get(REPORT_URL).status_code == 403
    assert admin.post(BANK_URL, content=BANK).status_code == 415
    first = admin.post(BANK_URL, content=BANK, headers=CSV)
    assert first.status_code == 200, first.text
    assert first.json()['imported_count'] == 1
    assert first.json()['duplicate_count'] == 0
    replay = admin.post(BANK_URL, content=BANK, headers=CSV)
    assert replay.json()['sha256'] == first.json()['sha256']
    assert replay.json()['imported_count'] == 0
    assert replay.json()['duplicate_count'] == 1
    conflict = BANK.replace('20.00,1.25,18.75', '21.00,1.25,19.75')
    conflict = conflict.splitlines()[0] + '\n' + BANK.splitlines()[1].replace('bank_1', 'bank_2') + '\n' + conflict.splitlines()[1] + '\n'
    assert admin.post(BANK_URL, content=conflict, headers=CSV).status_code == 409
    retry = BANK.replace('bank_1', 'bank_2')
    assert admin.post(BANK_URL, content=retry, headers=CSV).json()['imported_count'] == 1
    events = admin.get('/operations/audit').json()
    assert [event['actor'] for event in events if event['action'] == 'finance_import'] == [
        'finance-admin', 'finance-admin',
    ]


def test_report_keeps_bank_net_and_recognized_amount_separate(monkeypatch, tmp_path):
    admin, _ = clients(monkeypatch, tmp_path)

    async def postings():
        return [{'id': 'order_1', 'transactions': [{
            'id': 'ordtrans_1', 'reference': 'capture', 'reference_id': 'cap_1',
            'created_at': '2026-11-08T10:00:00Z', 'currency_code': 'eur', 'amount': 20,
        }]}]

    monkeypatch.setattr(finance_api, 'fetch_medusa_ledger', postings)
    empty = admin.get(REPORT_URL)
    assert empty.status_code == 200
    assert empty.json()['bank_reconciliation']['bank_status'] == 'unavailable'
    assert empty.json()['recognized_revenue']['status'] == 'unavailable'
    assert admin.post(BANK_URL, content=BANK, headers=CSV).status_code == 200
    assert admin.post(REVENUE_URL, content=REVENUE, headers=CSV).status_code == 200
    report = admin.get(REPORT_URL)
    assert report.status_code == 200, report.text
    assert report.headers['cache-control'] == 'no-store'
    data = report.json()
    assert data['bank_reconciliation']['counts']['matched'] == 1
    assert data['bank_reconciliation']['by_currency']['eur']['net'] == '18.75'
    assert data['recognized_revenue']['by_currency']['eur']['recognized_amount'] == '14.00'
