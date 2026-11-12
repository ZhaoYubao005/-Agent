"""Write actor-attributed events in the same transaction as state changes."""
from datetime import datetime, timezone


def record(db, actor, action, subject, note=None):
    db.execute("""
        INSERT INTO audit_events (actor, role, action, subject, note, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (actor.name, actor.role, action, subject, note,
          datetime.now(timezone.utc).isoformat()))
