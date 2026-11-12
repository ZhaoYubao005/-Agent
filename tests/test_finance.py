from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ecomops import finance


BANK_HEADER = b'event_id,event_type,reference_id,currency,gross,fee,net,posted_at\n'
REVENUE_HEADER = b'entry_id,date,currency,amount\n'
BANK_ROW = b'event-1,capture,cap-1,EUR,20.750,0.50,20.250,2026-11-05T00:00:00Z\n'


def test_imports_bank_rows_with_exact_amounts_and_utc_time():
    row, = finance.parse_bank_csv(b'\xef\xbb\xbf' + BANK_HEADER + BANK_ROW)

    assert row.gross == Decimal('20.750')
    assert row.fee == Decimal('0.50')
    assert row.net == Decimal('20.250')
    assert row.currency == 'eur'
    assert row.posted_at == datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert finance.BankSettlement.model_validate_json(row.model_dump_json()) == row


@pytest.mark.parametrize(('document', 'reason'), [
    (BANK_HEADER + BANK_ROW + BANK_ROW, 'duplicate event_id'),
    (BANK_HEADER + BANK_ROW.replace(b'EUR', 'ÉUR'.encode()), 'currency'),
    (BANK_HEADER + BANK_ROW.replace(b'20.750', b'2.075e1'), 'gross'),
    (BANK_HEADER + BANK_ROW.replace(b'0.50', b'-0.50'), 'fee'),
    (BANK_HEADER + BANK_ROW.replace(b'20.250', b'20.251'), 'gross minus fee'),
    (BANK_HEADER + BANK_ROW.replace(b',capture,', b',refund,'), 'gross'),
    (BANK_HEADER + BANK_ROW.replace(b'2026-11-05', b'2026-02-29'), 'posted_at'),
    (BANK_HEADER + BANK_ROW.replace(b'00Z', b'00+01:00'), 'posted_at'),
    (BANK_HEADER + BANK_ROW.replace(b'00Z', b'00'), 'posted_at'),
    (BANK_HEADER + BANK_ROW.replace(b'event-1', b'\xff'), 'UTF-8'),
    (BANK_HEADER + BANK_ROW.replace(b'event-1', b'"event-1'), 'malformed CSV'),
    (BANK_HEADER.replace(b'event_id', b'currency') + BANK_ROW, 'headers'),
    (BANK_HEADER + BANK_ROW.replace(b'event-1,', b''), 'column count'),
    (BANK_HEADER + BANK_ROW.replace(b'event-1,', b'event-1,extra,'), 'column count'),
])
def test_bank_import_rejects_invalid_documents(document, reason):
    with pytest.raises(finance.FinanceInputError, match=reason):
        finance.parse_bank_csv(document)


def test_bank_csv_applies_document_byte_limit_before_decoding():
    with pytest.raises(finance.FinanceInputError, match='byte limit'):
        finance.parse_bank_csv(b'\xff' * (finance.MAX_CSV_BYTES + 1))


def test_bank_csv_applies_document_row_limit():
    rows = b''.join(BANK_ROW.replace(b'event-1', f'event-{i}'.encode())
                    for i in range(finance.MAX_CSV_ROWS + 1))

    with pytest.raises(finance.FinanceInputError, match='row limit'):
        finance.parse_bank_csv(BANK_HEADER + rows)


def test_recognized_revenue_sums_signed_entries_per_currency():
    rows = finance.parse_revenue_csv(REVENUE_HEADER + (
        '收入-1,2026-11-05,EUR,12.000000000000000001\n'
        'entry-2,2026-11-05,eur,-2.1\n'
        'entry-3,2026-11-05,USD,9\n'
    ).encode())

    assert finance.summarize_revenue(rows)['by_currency'] == {
        'eur': {'count': 2, 'recognized_amount': '9.900000000000000001'},
        'usd': {'count': 1, 'recognized_amount': '9'},
    }
    assert finance.RevenueEntry.model_validate_json(rows[0].model_dump_json()) == rows[0]


def test_large_and_small_amounts_preserve_every_decimal_place():
    rows = finance.parse_revenue_csv(REVENUE_HEADER + (
        b'large,2026-11-05,EUR,999999999999999999.99999999999999999999\n'
        b'small,2026-11-05,EUR,0.00000000000000000001\n'
    ))

    total = finance.summarize_revenue(rows)['by_currency']['eur']['recognized_amount']
    assert total == '1000000000000000000.00000000000000000000'


@pytest.mark.parametrize(('rows', 'reason'), [
    (b'e,2026-11-05,USD,1\ne,2026-11-06,USD,1\n', 'duplicate entry_id'),
    (b'e,2026-11-31,USD,1\n', 'date'),
    (b'e,20261105,USD,1\n', 'date'),
    (b'e,2026-11-05,USD,NaN\n', 'amount'),
    (b'e,2026-11-05,USD,0.000000000000000000000\n', 'amount'),
])
def test_revenue_import_rejects_invalid_records(rows, reason):
    with pytest.raises(finance.FinanceInputError, match=reason):
        finance.parse_revenue_csv(REVENUE_HEADER + rows)


def medusa(reference_id, gross='20.75', currency='eur', event_type='capture'):
    return finance.MedusaTransaction(
        event_type=event_type, reference_id=reference_id, currency=currency, gross=Decimal(gross),
    )


def test_reconciliation_matches_type_and_reference_and_classifies_exceptions():
    bank_rows = finance.parse_bank_csv(BANK_HEADER + BANK_ROW + (
        b'event-2,refund,cap-1,EUR,-5,0.25,-5.25,2026-11-05T00:00:00Z\n'
        b'event-3,capture,cap-2,EUR,10,1,9,2026-11-05T00:00:00Z\n'
        b'event-4,capture,cap-3,USD,100,2,98,2026-11-05T00:00:00Z\n'
        b'event-5,capture,cap-4,USD,8,0,8,2026-11-05T00:00:00Z\n'
        b'event-6,capture,cap-5,EUR,11,0,11,2026-11-05T00:00:00Z\n'
        b'event-7,capture,cap-5,EUR,10,0,10,2026-11-05T00:00:00Z\n'
    ))
    comparison = finance.reconcile_bank(bank_rows, [
        medusa('cap-1'), medusa('cap-1', '-5', event_type='refund'),
        medusa('cap-2', '9'), medusa('cap-3', '90', currency='jpy'),
        medusa('cap-5', '21'), medusa('cap-6'),
    ])

    assert comparison['status'] == 'available'
    assert comparison['counts'] == {
        'matched': 2, 'bank_only': 1, 'medusa_only': 1,
        'amount_mismatch': 1, 'currency_mismatch': 1, 'ambiguous_reference': 1,
    }
    assert comparison['by_currency'] == {
        'eur': {'count': 5, 'gross': '46.750', 'fee': '1.75', 'net': '45.000'},
        'usd': {'count': 2, 'gross': '108', 'fee': '2', 'net': '106'},
    }
    entries = {(entry['event_type'], entry['reference_id']): entry for entry in comparison['entries']}
    assert entries['capture', 'cap-3']['status'] == 'currency_mismatch'
    assert entries['capture', 'cap-3']['bank'][0]['event_id'] == 'event-4'
    assert entries['capture', 'cap-3']['medusa'][0]['gross'] == '90'
    assert entries['capture', 'cap-5']['status'] == 'ambiguous_reference'
    assert entries['refund', 'cap-1']['status'] == 'matched'


def test_missing_sources_do_not_become_empty_or_unmatched():
    bank_rows = finance.parse_bank_csv(BANK_HEADER + BANK_ROW)
    assert finance.reconcile_bank(None, [medusa('cap-1')]) == {
        'status': 'unavailable', 'source': 'imported_bank_settlements',
        'bank_status': 'unavailable', 'medusa_status': 'available',
        'by_currency': None, 'counts': None, 'entries': None,
    }
    comparison = finance.reconcile_bank(bank_rows, None)
    assert comparison['status'] == 'unavailable'
    assert comparison['by_currency']['eur']['net'] == '20.250'
    assert comparison['entries'] is None
    assert comparison['counts'] is None
    assert finance.summarize_revenue(None)['by_currency'] is None
    assert finance.reconcile_bank([], [])['entries'] == []


def posting(reference_id, created_at, *, reference='capture', gross='20.75'):
    return {
        'reference': reference, 'reference_id': reference_id,
        'currency_code': 'eur', 'created_at': created_at, 'raw_amount': {'value': gross},
    }


def test_selects_in_period_medusa_postings_and_bank_references_across_days():
    orders = [{'transactions': [
        posting('cross-day', '2026-11-04T23:59:00Z'),
        posting('older', '2026-11-04T23:59:00Z'),
        posting('start', '2026-11-05T00:00:00Z'),
        posting('before-cutoff', '2026-11-05T10:00:00Z', reference='refund', gross='-1'),
        posting('after-cutoff', '2026-11-05T11:00:01Z'),
        posting('end', '2026-11-06T00:00:00Z'),
        {'reference': 'credit_line'},
    ]}]

    rows = finance.select_medusa_transactions(
        orders, start=datetime(2026, 11, 5, tzinfo=timezone.utc),
        end=datetime(2026, 11, 6, tzinfo=timezone.utc),
        as_of=datetime(2026, 11, 5, 11, tzinfo=timezone.utc),
        bank_refs={('capture', 'cross-day')},
    )

    assert {row.reference_id for row in rows} == {'cross-day', 'start', 'before-cutoff'}
    assert rows[-1].gross == Decimal('-1')
    assert rows[0].posted_at == datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize('orders', [
    None,
    [{'transactions': None}],
    [{'transactions': [posting('bad', '2026-11-05T00:00:00', gross='20.75')]}],
    [{'transactions': [posting('bad', '2026-11-05T00:00:00Z', gross='-20.75')]}],
    [{'transactions': [posting('duplicate', '2026-11-05T00:00:00Z')] * 2}],
])
def test_unreliable_medusa_snapshot_does_not_produce_false_mismatches(orders):
    assert finance.select_medusa_transactions(
        orders, start=datetime(2026, 11, 5, tzinfo=timezone.utc),
        end=datetime(2026, 11, 6, tzinfo=timezone.utc), bank_refs=set(),
    ) is None
