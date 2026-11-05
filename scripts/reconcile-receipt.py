"""Reconcile a stranded receipt after stopping all Agent writers and checking Medusa."""
import argparse
import json

from ecomops.audit import record
from ecomops.auth import Actor
from ecomops.database import transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('proposal_id')
    parser.add_argument('--actor', required=True, help='Named maintenance operator')
    parser.add_argument('--outcome', required=True, choices=['received', 'not_received', 'unknown'])
    parser.add_argument('--note', required=True, help='Outcome of physical and Medusa verification')
    parser.add_argument('--verified-offline', action='store_true', required=True,
                        help='Confirm all Agent processes are stopped and Medusa outcome was checked')
    args = parser.parse_args()
    actor = args.actor.strip()
    note = args.note.strip()
    if not 1 <= len(actor) <= 100 or not 8 <= len(note) <= 500:
        parser.error('Provide an actor and a reconciliation note between 8 and 500 characters')
    target = 'needs_review' if args.outcome == 'unknown' else 'reconciled'
    with transaction() as db:
        changed = db.execute("""
            UPDATE proposals SET status=?
            WHERE id=? AND status='applying'
        """, (target, args.proposal_id)).rowcount
        if not changed:
            parser.error('Proposal is missing or not in the applying state')
        db.execute(
            "UPDATE receipt_batches SET status=?, receipt=? WHERE proposal_id=? AND status='applying'",
            (target, json.dumps({'outcome': args.outcome}), args.proposal_id),
        )
        record(db, Actor(actor, 'maintenance'), target, args.proposal_id, f'Outcome {args.outcome}: {note}')
    print('Reconciliation recorded.')


if __name__ == '__main__':
    main()
