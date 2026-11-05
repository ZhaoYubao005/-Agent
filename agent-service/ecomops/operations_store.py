"""Durable order alerts and report snapshots in the operations database."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone


@contextmanager
def operations_db(connect):
    db = connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def list_alerts(db):
    rows = db.execute('SELECT payload, status FROM order_alerts ORDER BY id')
    return [{**json.loads(row['payload']), 'status': row['status']} for row in rows]


def refresh_alerts(db, alerts):
    for alert in alerts:
        db.execute("""
            INSERT INTO order_alerts VALUES (?, ?, 'open')
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload
        """, (alert['id'], json.dumps(alert)))
    return list_alerts(db)


def change_alert(db, alert_id, target):
    row = db.execute(
        'SELECT status FROM order_alerts WHERE id=?', (alert_id,),
    ).fetchone()
    if row is None:
        raise KeyError(alert_id)
    if row['status'] == target:
        return False
    transitions = {
        'open': {'acknowledged', 'ignored', 'closed'},
        'acknowledged': {'ignored', 'closed'},
        'ignored': {'open'},
        'closed': {'open'},
    }
    if target not in transitions[row['status']]:
        raise ValueError('Alert status does not allow this action')
    changed = db.execute(
        'UPDATE order_alerts SET status=? WHERE id=? AND status=?',
        (target, alert_id, row['status']),
    ).rowcount
    if not changed:
        raise ValueError('Alert changed concurrently')
    db.execute('INSERT INTO order_alert_events (alert_id, status, occurred_at) VALUES (?, ?, ?)',
               (alert_id, target, datetime.now(timezone.utc).isoformat()))
    return True


def save_report(db, day, zone, report):
    db.execute("""
        INSERT INTO daily_reports VALUES (?, ?, ?, ?)
        ON CONFLICT(day, zone) DO NOTHING
    """, (day, zone, json.dumps(report), datetime.now(timezone.utc).isoformat()))
    return get_report(db, day, zone)


def get_report(db, day, zone):
    row = db.execute(
        'SELECT payload, generated_at FROM daily_reports WHERE day=? AND zone=?',
        (day, zone),
    ).fetchone()
    if row:
        return {'day': day, 'zone': zone, 'generated_at': row['generated_at'],
                **json.loads(row['payload'])}
