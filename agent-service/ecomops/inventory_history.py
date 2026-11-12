"""Store observed inventory levels without estimating missing observations."""
from datetime import datetime, timezone


def save_levels(db, inventory):
    observed_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (item['id'], level['location_id'], observed_at, item.get('sku'),
         str(level['stocked_quantity']), str(level['reserved_quantity']),
         str(level['available_quantity']))
        for item in inventory for level in item.get('location_levels') or []
    ]
    db.executemany('INSERT INTO inventory_observations VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
