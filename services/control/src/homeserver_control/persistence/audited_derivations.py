"""Same-database extra-space ledger for sealed audited stream-copy outputs.

These are physical commitments, never gateway permits or selectable sources.
Unknown writes stay reserved until their exact recorded output is recovered.
"""

import json

SCHEMA = """CREATE TABLE IF NOT EXISTS audited_derivations (
    file_id TEXT PRIMARY KEY, permit_id TEXT NOT NULL, reservation_id TEXT NOT NULL,
    pool_id TEXT NOT NULL, filesystem_id TEXT, budget_bytes INTEGER NOT NULL CHECK(budget_bytes>0),
    binding_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'reserved'
      CHECK(state IN ('reserved','writing','verified','published')),
    receipt_json TEXT
)"""


def decode(row):
    if row is None:
        return None
    result = dict(row)
    result["binding"] = json.loads(result.pop("binding_json"))
    result["receipt"] = json.loads(result.pop("receipt_json")) if row["receipt_json"] else None
    return result
