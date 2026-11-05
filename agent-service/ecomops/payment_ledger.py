"""Summarize posted order transactions by date and currency."""
from collections import Counter
from datetime import datetime
from decimal import Decimal

from ecomops.operations import amount, timestamp


SOURCE = 'medusa_order_transactions'
ZERO = Decimal(0)


def posted_payment_totals(orders, metrics, as_of):
    """Return only Medusa postings observed as of report generation."""
    unavailable = {'status': 'unavailable', 'source': SOURCE, 'by_currency': None}
    if not isinstance(orders, list):
        return {**unavailable, 'reason': 'Medusa order transactions could not be read.'}

    start = datetime.fromisoformat(metrics['period_start'])
    end = datetime.fromisoformat(metrics['period_end'])
    capture_counts, refund_counts = Counter(), Counter()
    captures, refunds = Counter(), Counter()
    seen = set()
    invalid = {**unavailable, 'reason': 'Medusa order transactions were incomplete or invalid.'}

    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get('transactions'), list):
            return invalid
        for row in order['transactions']:
            if not isinstance(row, dict) or 'reference' not in row:
                return invalid
            reference = row['reference']
            if reference not in ('capture', 'refund'):
                continue
            posted_at = timestamp(row.get('created_at'))
            if posted_at is None:
                return invalid
            if not start <= posted_at < end or posted_at > as_of:
                continue
            reference_id = row.get('reference_id')
            currency = row.get('currency_code')
            raw = row.get('raw_amount')
            value = amount(raw.get('value') if isinstance(raw, dict) else row.get('amount'))
            if (not isinstance(reference_id, str) or not reference_id
                    or (reference, reference_id) in seen
                    or not isinstance(currency, str) or len(currency) != 3
                    or not currency.isascii() or not currency.isalpha()
                    or value is None or (reference == 'capture' and value <= 0)
                    or (reference == 'refund' and value >= 0)):
                return invalid
            seen.add((reference, reference_id))
            currency = currency.lower()
            if reference == 'capture':
                captures[currency] += value
                capture_counts[currency] += 1
            else:
                refunds[currency] += value.copy_abs()
                refund_counts[currency] += 1

    return {
        'status': 'available',
        'source': SOURCE,
        'by_currency': {
            currency: {
                'capture_count': capture_counts[currency],
                'refund_count': refund_counts[currency],
                'captured_amount': format(captures.get(currency, ZERO), 'f'),
                'refunded_amount': format(refunds.get(currency, ZERO), 'f'),
                'net_posted_amount': format(captures[currency] - refunds[currency], 'f'),
            }
            for currency in sorted(captures.keys() | refunds.keys())
        },
    }
