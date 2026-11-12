"""Import independent finance records and compare bank settlements with Medusa postings."""

import csv
import io
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date as CalendarDate, datetime, timezone
from decimal import Decimal, localcontext
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer,
    ValidationError, field_validator, model_validator,
)

from ecomops.operations import amount, timestamp


MAX_CSV_BYTES = 1_048_576
MAX_CSV_ROWS = 5_000
MONEY_DECIMAL_PLACES = 20
BANK_FIELDS = ('event_id', 'event_type', 'reference_id', 'currency', 'gross', 'fee', 'net', 'posted_at')
REVENUE_FIELDS = ('entry_id', 'date', 'currency', 'amount')
MATCH_STATUSES = (
    'matched', 'bank_only', 'medusa_only', 'amount_mismatch', 'currency_mismatch', 'ambiguous_reference',
)
FIXED_POINT = re.compile(r'[+-]?[0-9]+(?:\.[0-9]+)?')
UTC_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)')
ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


class FinanceInputError(ValueError):
    """A CSV failed validation. Details contain field names and row numbers only."""


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip() or not value.isprintable():
        raise ValueError('expected a printable identifier of 1 to 128 characters without surrounding spaces')
    return value


def _currency(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z]{3}', value) is None:
        raise ValueError('expected a three-letter ASCII currency')
    return value.lower()


def _fixed_point(value: Decimal) -> str:
    return format(value, 'f')


def _check_scale(value: Decimal) -> Decimal:
    if value.as_tuple().exponent < -MONEY_DECIMAL_PLACES:
        raise ValueError(f'expected at most {MONEY_DECIMAL_PLACES} fractional digits')
    return value


Identifier = Annotated[str, BeforeValidator(_identifier)]
Currency = Annotated[str, BeforeValidator(_currency)]
Money = Annotated[
    Decimal, Field(max_digits=38, decimal_places=MONEY_DECIMAL_PLACES),
    AfterValidator(_check_scale), PlainSerializer(_fixed_point, when_used='json'),
]
EventType = Literal['capture', 'refund']


class _PaymentPosting(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    event_type: EventType
    reference_id: Identifier
    currency: Currency
    gross: Money

    @model_validator(mode='after')
    def check_gross_sign(self):
        if self.event_type == 'capture' and self.gross <= 0:
            raise ValueError('gross must be positive for a capture')
        if self.event_type == 'refund' and self.gross >= 0:
            raise ValueError('gross must be negative for a refund')
        return self


class MedusaTransaction(_PaymentPosting):
    posted_at: AwareDatetime | None = None


class BankSettlement(_PaymentPosting):
    event_id: Identifier
    fee: Annotated[Money, Field(ge=0)]
    net: Money
    posted_at: AwareDatetime

    @field_validator('posted_at', mode='before')
    @classmethod
    def check_posted_at_format(cls, value):
        if isinstance(value, datetime) or isinstance(value, str) and UTC_TIMESTAMP.fullmatch(value):
            return value
        raise ValueError('expected an ISO 8601 timestamp with a UTC offset')

    @field_validator('posted_at')
    @classmethod
    def check_utc(cls, value):
        if value.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError('expected an ISO 8601 timestamp with a UTC offset')
        return value

    @model_validator(mode='after')
    def check_net(self):
        with localcontext() as context:
            context.prec = 80
            if self.net != self.gross - self.fee:
                raise ValueError('net must equal gross minus fee')
        return self


class RevenueEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    entry_id: Identifier
    date: CalendarDate
    currency: Currency
    amount: Money

    @field_validator('date', mode='before')
    @classmethod
    def check_date_format(cls, value):
        if type(value) is CalendarDate or isinstance(value, str) and ISO_DATE.fullmatch(value):
            return value
        raise ValueError('expected a date in YYYY-MM-DD form')


Record = TypeVar('Record', BankSettlement, RevenueEntry)


def _parse_csv(
    content: bytes, fields: tuple[str, ...], model: type[Record], money_fields: tuple[str, ...],
) -> list[Record]:
    if len(content) > MAX_CSV_BYTES:
        raise FinanceInputError('CSV exceeds the byte limit')
    try:
        decoded = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise FinanceInputError('CSV must be UTF-8') from exc

    reader = csv.reader(io.StringIO(decoded, newline=''), strict=True)
    records = []
    seen = set()
    id_field = fields[0]
    try:
        header = next(reader, [])
        if len(header) != len(fields) or set(header) != set(fields):
            raise FinanceInputError(f'CSV headers must be {", ".join(fields)}')
        for row_number, cells in enumerate(reader, start=2):
            if row_number > MAX_CSV_ROWS + 1:
                raise FinanceInputError('CSV exceeds the row limit')
            if len(cells) != len(fields):
                raise FinanceInputError(f'Row {row_number} has the wrong column count')
            values = dict(zip(header, cells))
            for field in money_fields:
                if FIXED_POINT.fullmatch(values[field]) is None:
                    raise FinanceInputError(f'Row {row_number} {field} must be a fixed-point decimal')
            try:
                record = model.model_validate(values)
            except ValidationError as exc:
                error = exc.errors(include_context=False, include_input=False, include_url=False)[0]
                field = error['loc'][0] if error['loc'] else 'value'
                raise FinanceInputError(f'Row {row_number} {field}: {error["msg"]}') from exc
            identifier = values[id_field]
            if identifier in seen:
                raise FinanceInputError(f'Row {row_number} has a duplicate {id_field}')
            seen.add(identifier)
            records.append(record)
    except csv.Error as exc:
        raise FinanceInputError(f'CSV contains malformed CSV near line {reader.line_num}') from exc
    return records


def parse_bank_csv(content: bytes) -> list[BankSettlement]:
    return _parse_csv(content, BANK_FIELDS, BankSettlement, ('gross', 'fee', 'net'))


def parse_revenue_csv(content: bytes) -> list[RevenueEntry]:
    return _parse_csv(content, REVENUE_FIELDS, RevenueEntry, ('amount',))


def select_medusa_transactions(
    orders,
    *,
    start: datetime,
    end: datetime,
    bank_refs: set[tuple[str, str]],
    as_of: datetime | None = None,
) -> list[MedusaTransaction] | None:
    """Select postings in the report period and cross-day matches for its bank rows."""
    if not isinstance(orders, list):
        return None
    selected = []
    seen = set()
    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get('transactions'), list):
            return None
        for row in order['transactions']:
            if not isinstance(row, dict) or 'reference' not in row:
                return None
            event_type = row['reference']
            if event_type not in ('capture', 'refund'):
                continue
            posted_at = timestamp(row.get('created_at'))
            if posted_at is None:
                return None
            if as_of is not None and posted_at > as_of:
                continue
            reference_id = row.get('reference_id')
            if not isinstance(reference_id, str) or not reference_id:
                return None
            key = (event_type, reference_id)
            if not start <= posted_at < end and key not in bank_refs:
                continue
            if key in seen:
                return None
            raw_amount = row.get('raw_amount')
            gross = amount(raw_amount.get('value') if isinstance(raw_amount, dict) else row.get('amount'))
            try:
                selected.append(MedusaTransaction(
                    event_type=event_type, reference_id=reference_id,
                    currency=row.get('currency_code'), gross=gross,
                    posted_at=posted_at.astimezone(timezone.utc),
                ))
            except ValidationError:
                return None
            seen.add(key)
    return selected


def _sum_by_currency(rows, fields):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.currency].append(row)
    with localcontext() as context:
        context.prec = 80
        return {
            currency: {
                'count': len(group),
                **{label: _fixed_point(sum(getattr(row, attr) for row in group)) for label, attr in fields},
            }
            for currency, group in sorted(grouped.items())
        }


def _match_status(bank, medusa):
    if len(bank) > 1 or len(medusa) > 1:
        return 'ambiguous_reference'
    if not bank:
        return 'medusa_only'
    if not medusa:
        return 'bank_only'
    if bank[0].currency != medusa[0].currency:
        return 'currency_mismatch'
    if bank[0].gross != medusa[0].gross:
        return 'amount_mismatch'
    return 'matched'


def reconcile_bank(
    bank_rows: Iterable[BankSettlement] | None, medusa_rows: Iterable[MedusaTransaction] | None,
) -> dict:
    """Compare gross postings, while preserving independently reported fees and net cash."""
    bank = list(bank_rows) if bank_rows is not None else None
    available = bank is not None and medusa_rows is not None
    result = {
        'status': 'available' if available else 'unavailable',
        'source': 'imported_bank_settlements',
        'bank_status': 'available' if bank is not None else 'unavailable',
        'medusa_status': 'available' if medusa_rows is not None else 'unavailable',
        'by_currency': _sum_by_currency(bank, (('gross', 'gross'), ('fee', 'fee'), ('net', 'net')))
        if bank is not None else None,
        'counts': None,
        'entries': None,
    }
    if bank is None or medusa_rows is None:
        return result

    bank_by_ref = defaultdict(list)
    medusa_by_ref = defaultdict(list)
    for row in bank:
        bank_by_ref[(row.event_type, row.reference_id)].append(row)
    for row in medusa_rows:
        medusa_by_ref[(row.event_type, row.reference_id)].append(row)
    entries = []
    counts = Counter(dict.fromkeys(MATCH_STATUSES, 0))
    for event_type, reference_id in sorted(bank_by_ref.keys() | medusa_by_ref.keys()):
        bank_group = bank_by_ref[event_type, reference_id]
        medusa_group = medusa_by_ref[event_type, reference_id]
        status = _match_status(bank_group, medusa_group)
        counts[status] += 1
        entries.append({
            'event_type': event_type,
            'reference_id': reference_id,
            'status': status,
            'bank': [row.model_dump(mode='json') for row in sorted(bank_group, key=lambda row: row.event_id)],
            'medusa': [row.model_dump(mode='json') for row in medusa_group],
        })
    return {**result, 'counts': dict(counts), 'entries': entries}


def summarize_revenue(rows: Iterable[RevenueEntry] | None) -> dict:
    return {
        'status': 'available' if rows is not None else 'unavailable',
        'source': 'imported_revenue_entries',
        'by_currency': _sum_by_currency(rows, (('recognized_amount', 'amount'),)) if rows is not None else None,
    }
