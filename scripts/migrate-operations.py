"""Import an offline SQLite backup into an empty Agent PostgreSQL schema."""
import argparse
import os
import sqlite3
from pathlib import Path

from ecomops.database import PostgresConnection, repair_unresolved_receipts, transaction


TABLES = {
    'receipt_batches': ('proposal_id', 'request_id', 'quantity', 'status', 'receipt'),
    'proposals': ('id', 'run_id', 'payload', 'status', 'receipt'),
    'audit_events': ('sequence', 'actor', 'role', 'action', 'subject', 'note', 'occurred_at'),
    'inventory_observations': ('inventory_item_id', 'location_id', 'observed_at', 'sku', 'stocked', 'reserved', 'available'),
    'order_alerts': ('id', 'payload', 'status'),
    'order_alert_events': ('sequence', 'alert_id', 'status', 'occurred_at'),
    'daily_reports': ('day', 'zone', 'payload', 'generated_at'),
    'order_alert_scan_jobs': ('id', 'claim', 'state', 'next_run_at', 'lease_until', 'started_at',
                              'finished_at', 'failure_count', 'alert_count', 'error'),
    'finance_records': ('kind', 'source_id', 'effective_at', 'payload', 'source_digest', 'imported_at'),
    'report_jobs': ('day', 'zone', 'claim', 'state'),
}


def migrate(source_path):
    if not os.environ.get('ECOMOPS_DATABASE_URL'):
        raise SystemExit('Set ECOMOPS_DATABASE_URL before importing')
    source = sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with transaction() as target:
            if not isinstance(target, PostgresConnection):
                raise SystemExit('Expected a PostgreSQL target')
            target.execute(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE")
            existing_tables = {row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
            if 'proposals' not in existing_tables:
                raise SystemExit('Source is missing the proposals table')
            if any(target.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in TABLES):
                raise SystemExit('Target schema must be empty')
            counts = {}
            for table, columns in TABLES.items():
                if table not in existing_tables:
                    continue
                fields = ', '.join(columns)
                placeholders = ', '.join('?' for _ in columns)
                rows = source.execute(f'SELECT {fields} FROM {table}')
                counts[table] = 0
                while batch := rows.fetchmany(1000):
                    target.executemany(f'INSERT INTO {table} ({fields}) VALUES ({placeholders})', batch)
                    counts[table] += len(batch)
                if counts[table] and columns[0] == 'sequence':
                    target.execute(
                        f"""SELECT setval(pg_get_serial_sequence(?, 'sequence'),
                                         (SELECT max(sequence) FROM {table}))""",
                        (f'ecomops_agent.{table}',),
                    )
            repair_unresolved_receipts(target)
    finally:
        source.close()
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    print(migrate(parser.parse_args().source))
