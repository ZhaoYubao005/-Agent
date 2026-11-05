from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

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
