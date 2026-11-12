"""Read-only import previews for normalized finance CSVs."""
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from ecomops.app import app
from ecomops.finance import MAX_CSV_BYTES, MAX_CSV_ROWS


FINANCE = '/operations/finance'
CSV = {'Content-Type': 'text/csv; charset=utf-8'}
BANK = (
    'event_id,event_type,reference_id,currency,gross,fee,net,posted_at\n'
    'bank_1,capture,cap_1,EUR,20.00,1.25,18.75,2026-11-10T00:30:00Z\n'
)
REVENUE = 'entry_id,date,currency,amount\nbank_1,2026-11-10,EUR,14.00\n'


@pytest.fixture
def finance_client(monkeypatch, tmp_path):
    db = tmp_path / 'finance.db'
    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(db))
    monkeypatch.setenv('ECOMOPS_ACCOUNTS', json.dumps({
        'admin': {'role': 'admin', 'token': 'fixture-admin'},
        'operator': {'role': 'operator', 'token': 'fixture-operator'},
        'viewer': {'role': 'viewer', 'token': 'fixture-viewer'},
    }))
    with TestClient(app, headers={'Authorization': 'Bearer fixture-admin'}) as client:
        yield client, db


def test_previews_an_uninitialized_sqlite_database_without_creating_it(finance_client):
    client, db = finance_client
    response = client.post(f'{FINANCE}/preview/bank', content=BANK, headers=CSV)
    assert response.status_code == 200, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json() == {
        'kind': 'bank',
        'sha256': sha256(BANK.encode()).hexdigest(),
        'record_count': 1,
        'new_count': 1,
        'duplicate_count': 0,
        'conflict_count': 0,
        'ready_to_import': True,
    }
    assert not db.exists()


def test_preview_compares_normalized_values_without_changing_rows_or_audit(finance_client):
    client, db = finance_client
    second_bank = BANK.splitlines()[1].replace('bank_1', 'bank_2')
    assert client.post(f'{FINANCE}/import/bank', content=BANK + second_bank + '\n', headers=CSV).status_code == 200
    before = db.read_bytes()
    comparable = BANK.replace('EUR,20.00,1.25', 'eur,20.0,1.250').replace('00:30:00Z', '00:30:00+00:00')
    conflicting = second_bank.replace('20.00,1.25,18.75', '21.00,1.25,19.75')
    new = second_bank.replace('bank_2', 'bank_3')
    contents = comparable + conflicting + '\n' + new + '\n'
    preview = client.post(f'{FINANCE}/preview/bank', content=contents, headers=CSV)
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        'kind': 'bank',
        'sha256': sha256(contents.encode()).hexdigest(),
        'record_count': 3,
        'new_count': 1,
        'duplicate_count': 1,
        'conflict_count': 1,
        'ready_to_import': False,
    }
    assert client.post(f'{FINANCE}/import/bank', content=contents, headers=CSV).status_code == 409
    separate_kind = client.post(f'{FINANCE}/preview/revenue', content=REVENUE, headers=CSV)
    assert separate_kind.status_code == 200, separate_kind.text
    assert separate_kind.json()['new_count'] == 1
    assert db.read_bytes() == before


def test_preview_limits_and_validation_match_import(finance_client):
    client, db = finance_client
    private = 'sensitive-balance-data'
    for body, headers, expected in [
        (BANK, {}, 415),
        (b'x' * (MAX_CSV_BYTES + 1), CSV, 413),
        (BANK.replace('20.00', private), CSV, 422),
        (BANK + BANK.splitlines()[1] + '\n', CSV, 422),
        (b'\xff', CSV, 422),
    ]:
        preview = client.post(f'{FINANCE}/preview/bank', content=body, headers=headers)
        imported = client.post(f'{FINANCE}/import/bank', content=body, headers=headers)
        assert preview.status_code == imported.status_code == expected
        assert preview.json() == imported.json()
        assert private not in preview.text
    assert not db.exists()


@pytest.mark.parametrize('role', ['operator', 'viewer'])
def test_preview_is_admin_only(finance_client, role):
    client, db = finance_client
    response = client.post(
        f'{FINANCE}/preview/bank', content=BANK,
        headers={**CSV, 'Authorization': f'Bearer fixture-{role}'},
    )
    assert response.status_code == 403
    assert not db.exists()


def test_preview_handles_the_full_row_limit_with_an_existing_database(finance_client):
    client, _ = finance_client
    assert client.post(f'{FINANCE}/import/revenue', content=REVENUE, headers=CSV).status_code == 200
    header, record = REVENUE.splitlines()
    csv = header + '\n' + '\n'.join(record.replace('bank_1', f'row_{n}') for n in range(MAX_CSV_ROWS))
    response = client.post(f'{FINANCE}/preview/revenue', content=csv, headers=CSV)
    assert response.status_code == 200, response.text
    assert response.json()['new_count'] == MAX_CSV_ROWS
