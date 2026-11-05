"""Reconcile a stranded receipt after stopping all Agent writers and checking Medusa."""
import argparse

from ecomops.audit import record
from ecomops.auth import Actor
from ecomops.database import transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('proposal_id')
    parser.add_argument('--actor', required=True, help='Named maintenance operator')
    parser.add_argument('--note', required=True, help='Outcome of physical and Medusa verification')
    parser.add_argument('--verified-offline', action='store_true', required=True,
                        help='Confirm all Agent processes are stopped and Medusa outcome was checked')
    args = parser.parse_args()
    actor = args.actor.strip()
    note = args.note.strip()
    if not 1 <= len(actor) <= 100 or not 8 <= len(note) <= 500:
        parser.error('Provide an actor and a reconciliation note between 8 and 500 characters')
    with transaction() as db:
        changed = db.execute("""
            UPDATE proposals SET status='reconciled'
            WHERE id=? AND status='applying'
        """, (args.proposal_id,)).rowcount
        if not changed:
            parser.error('Proposal is missing or not in the applying state')
        record(db, Actor(actor, 'maintenance'), 'reconciled', args.proposal_id, note)
    print('Reconciliation recorded.')


if __name__ == '__main__':
    main()
