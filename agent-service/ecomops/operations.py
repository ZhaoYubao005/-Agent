"""Deterministic order monitoring and daily order-cohort metrics."""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


INACTIVE_ORDER_STATUSES = {'archived', 'canceled', 'completed'}
OPEN_FULFILLMENT_STATUSES = {
    'not_fulfilled', 'partially_fulfilled', 'partially_shipped', 'partially_delivered', 'canceled',
}
FINISHED_FULFILLMENT_STATUSES = {'fulfilled', 'shipped', 'delivered'}


def amount(value):
    if not isinstance(value, (int, float, str, Decimal)) or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def json_number(number):
    return int(number) if number == number.to_integral_value() else float(number)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo is not None else None


def created_at(order):
    return timestamp(order.get('created_at'))


def sla_alerts(orders, now, hours):
    alerts = []
    for order in orders:
        fulfillment = order.get('fulfillment_status')
        created = created_at(order)
        if (not order.get('status') or order['status'] in INACTIVE_ORDER_STATUSES
                or fulfillment not in OPEN_FULFILLMENT_STATUSES or not created or not order.get('id')):
            continue
        age = now - created
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


def outstanding_inventory(order):
    """Resolve remaining units per line and inventory component using explicit links."""
    requirements = {}
    if not order.get('items'):
        return None
    for line in order['items']:
        quantity = amount(line.get('quantity'))
        fulfilled = amount((line.get('detail') or {}).get('fulfilled_quantity'))
        if quantity is None or fulfilled is None or not 0 <= fulfilled <= quantity:
            return None
        if fulfilled == quantity:
            continue
        variant = line.get('variant') or {}
        if variant.get('manage_inventory') is False:
            continue
        if variant.get('manage_inventory') is not True or not variant.get('inventory_items') or not line.get('id'):
            return None
        for component in variant['inventory_items']:
            item_id = component.get('inventory_item_id') or (component.get('inventory') or {}).get('id')
            multiplier = amount(component.get('required_quantity'))
            key = (line['id'], item_id)
            if not item_id or multiplier is None or multiplier <= 0 or key in requirements:
                return None
            requirements[key] = (quantity - fulfilled) * multiplier
    return requirements


def assess_shortage(order, inventory_by_id):
    requirements = outstanding_inventory(order)
    if requirements is None:
        return [], 'Outstanding line quantities or inventory component links are unavailable.'
    if not requirements:
        return [], None
    locations = order.get('eligible_location_ids')
    reservations = order.get('reservations')
    if locations is None or reservations is None:
        return [], 'Sales-channel locations or line-level reservations are unavailable.'

    assigned = defaultdict(Counter)
    for row in reservations:
        key = (row.get('line_item_id'), row.get('inventory_item_id'))
        if key not in requirements or row.get('location_id') not in locations:
            continue
        reserved = amount(row.get('quantity'))
        if reserved is None or reserved < 0:
            return [], 'A line-level reservation quantity is unavailable.'
        assigned[key][row['location_id']] += reserved

    total_needed = Counter()
    reservation_coverage = Counter()
    total_reserved = defaultdict(Counter)
    for key, required in requirements.items():
        _, item_id = key
        total_needed[item_id] += required
        # Extra reservations for one line do not cover a different line.
        reservation_coverage[item_id] += min(required, sum(assigned[key].values()))
        for location, reserved in assigned[key].items():
            total_reserved[item_id][location] += reserved

    shortages = []
    unknown = None
    for item_id, required in sorted(total_needed.items()):
        levels = (inventory_by_id.get(item_id) or {}).get('location_levels')
        if levels is None:
            unknown = 'Current warehouse inventory is unavailable for an order component.'
            continue
        available = best_case_reserved = Decimal(0)
        seen_locations = set()
        inconsistent = False
        overcommitted = False
        for level in levels:
            location = level.get('location_id')
            if location not in locations:
                continue
            stocked = amount(level.get('stocked_quantity'))
            total_location_reserved = amount(level.get('reserved_quantity'))
            order_reserved = total_reserved[item_id][location]
            if (stocked is None or total_location_reserved is None or location in seen_locations
                    or not 0 <= order_reserved <= total_location_reserved):
                inconsistent = True
                break
            seen_locations.add(location)
            available += max(0, stocked - total_location_reserved)
            best_case_reserved += min(order_reserved, max(0, stocked))
            overcommitted |= order_reserved > 0 and stocked < total_location_reserved
        if inconsistent or total_reserved[item_id].keys() - seen_locations:
            unknown = 'Warehouse stock and order reservations do not reconcile.'
            continue

        reserved_for_order = min(reservation_coverage[item_id], best_case_reserved)
        shortfall = required - reserved_for_order - available
        if shortfall > 0:
            shortages.append({
                'inventory_item_id': item_id, 'required': json_number(required),
                'reserved_for_order': json_number(reserved_for_order),
                'unreserved_available': json_number(available), 'shortfall': json_number(shortfall),
            })
        elif overcommitted:
            unknown = 'Warehouse stock cannot cover all reservations, and order priority is unavailable.'
    return shortages, unknown


def shortage_alerts(orders, inventory):
    inventory_by_id = {item['id']: item for item in inventory if item.get('id')}
    alerts, unknown_orders = [], []
    checked = 0
    for order in orders:
        status, fulfillment = order.get('status'), order.get('fulfillment_status')
        if status in INACTIVE_ORDER_STATUSES or fulfillment in FINISHED_FULFILLMENT_STATUSES:
            continue
        if status and fulfillment in OPEN_FULFILLMENT_STATUSES and order.get('id'):
            shortages, unknown = assess_shortage(order, inventory_by_id)
        else:
            shortages, unknown = [], 'Order identity or fulfillment status is unavailable.'
        if shortages:
            alerts.append({
                'id': f"inventory_shortage:{order['id']}", 'order_id': order['id'],
                'rule': 'inventory_shortage', 'severity': 'high', 'fulfillment_status': fulfillment,
                'shortages': shortages,
                'suggestion': 'Review eligible stock and reservations before assigning fulfillment.',
            })
        if unknown:
            unknown_orders.append({'order_id': order.get('id'), 'reason': unknown})
        else:
            checked += 1
    return alerts, {'orders_fully_assessed': checked, 'unknown_orders': unknown_orders}


def daily_metrics(orders, day: date, zone: ZoneInfo):
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    cohort = [order for order in orders if (created := created_at(order)) and start <= created < end]
    statuses = Counter()
    fulfillment_statuses = Counter()
    payment_statuses = Counter()
    quantities = Counter()
    values = Counter()
    missing_items = 0
    missing_value = 0
    unattributed_lines = 0
    for order in cohort:
        status = order.get('status') or 'unknown'
        statuses[status] += 1
        fulfillment_statuses[order.get('fulfillment_status') or 'unknown'] += 1
        payment_statuses[order.get('payment_status') or 'unknown'] += 1
        if status in ('canceled', 'unknown'):
            continue
        currency = order.get('currency_code')
        total = amount(order.get('total'))
        if (isinstance(currency, str) and len(currency) == 3 and currency.isascii()
                and currency.isalpha() and total is not None):
            values[currency.lower()] += total
        else:
            missing_value += 1
        items = order.get('items')
        if not isinstance(items, list):
            missing_items += 1
            continue
        for item in items:
            variant_id = item.get('variant_id')
            quantity = amount(item.get('quantity'))
            if variant_id and quantity is not None and quantity > 0:
                quantities[variant_id] += quantity
            else:
                unattributed_lines += 1
    ledger_reason = 'The order snapshot has no dated payment capture or refund ledger.'
    return {
        'period_start': start.isoformat(),
        'period_end': end.isoformat(),
        'orders': len(cohort),
        'statuses': dict(statuses),
        'fulfillment_statuses': dict(fulfillment_statuses),
        'payment_statuses': dict(payment_statuses),
        'ordered_units_by_variant': {key: json_number(value) for key, value in quantities.most_common()},
        'orders_missing_items': missing_items,
        'lines_missing_variant_or_quantity': unattributed_lines,
        'order_value_by_currency': {currency: format(total, 'f') for currency, total in sorted(values.items())},
        'orders_missing_value': missing_value,
        'orders_missing_created_at': sum(created_at(order) is None for order in orders),
        'revenue': None,
        'revenue_unavailable_reason': ledger_reason,
        'refund_amount': None,
        'refund_amount_unavailable_reason': ledger_reason,
    }
