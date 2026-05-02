from __future__ import annotations

import time
from typing import Optional
from .utils import stable_id
from .audit import record_revision, row_to_dict



class CausalManager:
    def __init__(self, conn, store=None):
        self.conn = conn
        self._store = store

    def invalidate_cascading(self, belief_ids: list[str]) -> None:
        """Invalidate multiple beliefs and their dependents. Used after supersession."""
        if not self._store:
            return
        for bid in belief_ids:
            self._store.invalidate_belief(bid)

    def mark_belief(self, belief_id: str | None, claim_text: str, action: str, evidence_text: str | None = None, session_id: str = '', scope_key: str = '', caused_by_work_item_id: str = '') -> dict:
        now = time.time()
        if not belief_id:
            belief_id = stable_id("belief", claim_text, action)
        before = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)).fetchone())
        row = self.conn.execute(
            "SELECT belief_id, claim_text, belief_kind, confidence, status FROM beliefs WHERE belief_id = ?",
            (belief_id,),
        ).fetchone()
        if not row:
            belief_kind = "hypothesis" if action not in ("validated", "ruled_out") else ("validated_cause" if action == "validated" else "ruled_out_cause")
            status = "validated" if action == "validated" else ("falsified" if action == "falsified" else ("validated" if action == "ruled_out" else "open"))
            confidence = 0.85 if action == "validated" else (0.7 if action == "ruled_out" else 0.55)
            self.conn.execute(
                "INSERT OR REPLACE INTO beliefs (belief_id, episode_id, claim_text, belief_kind, confidence, status, created_at, updated_at, validated_by, supersedes_belief_id, caused_by_work_item_id, session_id, scope_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (belief_id, None, claim_text, belief_kind, confidence, status, now, now, None, None, caused_by_work_item_id, session_id, scope_key),
            )
            # If this creates a stronger belief, supersede weaker open duplicates with same claim.
            if action in ('validated', 'ruled_out', 'falsified'):
                old = self.conn.execute(
                    "SELECT * FROM beliefs WHERE claim_text = ? AND belief_id != ? AND status = 'open'",
                    (claim_text, belief_id),
                ).fetchall()
                old_ids = [r['belief_id'] for r in old]
                self.conn.execute(
                    "UPDATE beliefs SET status = 'superseded', supersedes_belief_id = ?, updated_at = ? WHERE claim_text = ? AND belief_id != ? AND status = 'open'",
                    (belief_id, now, claim_text, belief_id),
                )
                for old_row in old:
                    after_old = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id=?", (old_row['belief_id'],)).fetchone())
                    record_revision(self.conn, object_type='belief', object_id=old_row['belief_id'], action='supersede', reason=f'belief_mark_{action}', before=row_to_dict(old_row), after=after_old, created_at=now)
                if old_ids:
                    self.conn.commit()
                    self.invalidate_cascading(old_ids)
        else:
            belief_kind = row[2]
            status = row[4]
            confidence = row[3]
            if action == "validated":
                belief_kind = "validated_cause"
                status = "validated"
                confidence = max(confidence, 0.85)
            elif action == "falsified":
                status = "falsified"
                confidence = min(confidence, 0.2)
            elif action == "ruled_out":
                belief_kind = "ruled_out_cause"
                status = "validated"
                confidence = max(confidence, 0.7)
            elif action == "hypothesis":
                belief_kind = "hypothesis"
                status = "open"
                confidence = min(confidence, 0.6)
            self.conn.execute(
                "UPDATE beliefs SET claim_text = ?, belief_kind = ?, confidence = ?, status = ?, updated_at = ?, session_id = CASE WHEN ? != '' THEN ? ELSE session_id END, scope_key = CASE WHEN ? != '' THEN ? ELSE scope_key END, caused_by_work_item_id = CASE WHEN ? != '' THEN ? ELSE caused_by_work_item_id END WHERE belief_id = ?",
                (claim_text or row[1], belief_kind, confidence, status, now, session_id, session_id, scope_key, scope_key, caused_by_work_item_id, caused_by_work_item_id, belief_id),
            )

        after = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)).fetchone())
        record_revision(
            self.conn,
            object_type='belief',
            object_id=belief_id,
            action=action,
            reason=(evidence_text or f'belief_mark_{action}')[:300],
            before=before,
            after=after,
            created_at=now,
        )
        self.conn.commit()
        updated = self.conn.execute(
            "SELECT belief_id, claim_text, belief_kind, confidence, status, validated_by FROM beliefs WHERE belief_id = ?",
            (belief_id,),
        ).fetchone()
        return dict(updated)
