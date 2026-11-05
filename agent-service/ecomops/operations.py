"""Deterministic order monitoring and daily order-cohort metrics."""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def created_at(order):
    return datetime.fromisoformat(order['created_at'])


def sla_alerts(orders, now, hours):
    alerts = []
    for order in orders:
        fulfillment = order.get('fulfillment_status')
        if order.get('status') in ('canceled', 'completed') or fulfillment not in (
            'not_fulfilled', 'partially_fulfilled',
        ):
            continue
        age = now - created_at(order)
        if age >= timedelta(hours=hours):
            alerts.append({
                'id': f"fulfillment_sla:{order['id']}",
                'order_id': order['id'],
                'rule': 'fulfillment_sla',
                'severity': 'high' if age >= timedelta(hours=2 * hours) else 'medium',
                'age_hours': int(age.total_seconds() // 3600),
                'fulfillment_status': fulfillment,
                'suggestion': 'Verify stock reservations and confirm the fulfillment schedule.',
            })
    return alerts


def daily_metrics(orders, day: date, zone: ZoneInfo):
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    cohort = [order for order in orders if start <= created_at(order) < end]
    statuses = Counter()
    quantities = Counter()
    missing_items = 0
    for order in cohort:
        status = order.get('status') or 'unknown'
        statuses[status] += 1
        if status == 'canceled':
            continue
        if order.get('items') is None:
            missing_items += 1
        for item in order.get('items') or []:
            variant_id = item.get('variant_id')
            quantity = item.get('quantity')
            if variant_id and isinstance(quantity, int) and quantity > 0:
                quantities[variant_id] += quantity
    return {
        'period_start': start.isoformat(),
        'period_end': end.isoformat(),
        'orders': len(cohort),
        'statuses': dict(statuses),
        'ordered_units_by_variant': dict(quantities.most_common()),
        'orders_missing_items': missing_items,
        'revenue': None,
        'revenue_unavailable_reason': 'Payment and refund ledger not included in this snapshot.',
    }
