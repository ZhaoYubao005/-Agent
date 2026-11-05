from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ecomops.operations import daily_metrics, sla_alerts


def test_fulfillment_sla_uses_business_status_and_exact_boundary():
    orders = [
        {'id': 'old', 'status': 'pending', 'fulfillment_status': 'not_fulfilled',
         'created_at': '2026-11-04T00:00:00Z'},
        {'id': 'canceled', 'status': 'canceled', 'fulfillment_status': 'not_fulfilled',
         'created_at': '2026-11-03T00:00:00Z'},
        {'id': 'unknown', 'status': 'pending', 'created_at': '2026-11-03T00:00:00Z'},
    ]
    alerts = sla_alerts(orders, datetime(2026, 11, 5, tzinfo=timezone.utc), 24)
    assert [(alert['id'], alert['severity']) for alert in alerts] == [
        ('fulfillment_sla:old', 'medium'),
    ]


def test_daily_cohort_obeys_local_day_and_excludes_canceled_units():
    orders = [
        {'created_at': '2026-11-04T16:00:00Z', 'status': 'pending',
         'items': [{'variant_id': 'demo', 'quantity': 2}]},
        {'created_at': '2026-11-05T15:59:59Z', 'status': 'canceled',
         'items': [{'variant_id': 'demo', 'quantity': 5}]},
        {'created_at': '2026-11-05T16:00:00Z', 'status': 'pending'},
    ]
    result = daily_metrics(orders, date(2026, 11, 5), ZoneInfo('Asia/Shanghai'))
    assert result['orders'] == 2
    assert result['statuses'] == {'pending': 1, 'canceled': 1}
    assert result['ordered_units_by_variant'] == {'demo': 2}
    assert result['revenue'] is None


def test_alert_refresh_preserves_operator_status_and_audit(tmp_path, monkeypatch):
    from ecomops.app import connect
    from ecomops.operations_store import change_alert, operations_db, refresh_alerts

    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'audit.sqlite3'))
    alert = {'id': 'fulfillment_sla:order_1', 'age_hours': 24}
    with operations_db(connect) as db:
        assert refresh_alerts(db, [alert])[0]['status'] == 'open'
        change_alert(db, alert['id'], 'acknowledged')
        change_alert(db, alert['id'], 'acknowledged')
    with operations_db(connect) as db:
        refreshed = refresh_alerts(db, [{**alert, 'age_hours': 48}])
        assert refreshed == [{**alert, 'age_hours': 48, 'status': 'acknowledged'}]
        assert db.execute('SELECT count(*) FROM order_alert_events').fetchone()[0] == 1


def test_report_generation_is_idempotent(tmp_path, monkeypatch):
    from ecomops.app import connect
    from ecomops.operations_store import operations_db, save_report

    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'audit.sqlite3'))
    with operations_db(connect) as db:
        first = save_report(db, '2026-11-05', 'UTC', {'current': {'orders': 2}})
        second = save_report(db, '2026-11-05', 'UTC', {'current': {'orders': 5}})
        assert first == second


@pytest.mark.asyncio
async def test_report_advice_uses_rules_without_provider(monkeypatch):
    from ecomops.advice import interpret_report

    monkeypatch.delenv('ECOMOPS_LLM_URL', raising=False)
    advice = await interpret_report({'orders': 2, 'revenue': None}, {'orders': 1})
    assert advice['source'] == 'rules'
    assert advice['trace'] == 'provider_disabled'


def test_role_permissions(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from ecomops.app import app

    monkeypatch.setenv('ECOMOPS_ACCOUNTS', json.dumps({
        'read-only': {'role': 'viewer', 'token': 'fixture-viewer'},
        'receiver': {'role': 'operator', 'token': 'fixture-operator'},
        'approver': {'role': 'admin', 'token': 'fixture-admin'},
    }))
    client = TestClient(app)
    path = '/recommendations/missing/approve'
    assert client.post(path, headers={'Authorization': 'Bearer fixture-viewer'}).status_code == 403
    assert client.post(path, headers={'Authorization': 'Bearer fixture-operator'}).status_code == 403
    assert client.post(path, headers={'Authorization': 'Bearer fixture-admin'}).status_code == 404
