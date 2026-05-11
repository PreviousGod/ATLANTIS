from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class BackupManager:
    def __init__(self, conn, db_path: str):
        self.conn = conn
        self.db_path = db_path

    def checkpoint_wal(self, *, truncate: bool = True) -> dict:
        mode = 'TRUNCATE' if truncate else 'PASSIVE'
        try:
            rows = self.conn.execute(f'PRAGMA wal_checkpoint({mode})').fetchall()
            row = rows[0] if rows else None
            result = {'status': 'ok', 'mode': mode.lower()}
            if row is not None:
                keys = list(getattr(row, 'keys', lambda: [])())
                if keys:
                    result.update({str(key): row[key] for key in keys})
                else:
                    values = tuple(row)
                    for key, value in zip(('busy', 'log', 'checkpointed'), values):
                        result[key] = value
            return result
        except Exception as exc:
            logger.warning("[live_brain] WAL checkpoint failed: %s", exc)
            return {'status': 'error', 'mode': mode.lower(), 'error': str(exc)[:300]}

    def rotate_backups(self, *, max_age_hours: float = 48.0, max_keep: int = 8, dry_run: bool = False) -> dict:
        source = Path(self.db_path)
        backup_dir = source.parent
        pattern = f"{source.stem}_backup_*{source.suffix}"
        now = time.time()
        cutoff = now - max(0.1, float(max_age_hours or 48.0)) * 3600
        max_keep = max(1, int(max_keep or 8))
        backup_files = sorted(
            [candidate for candidate in backup_dir.glob(pattern) if candidate.is_file()],
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        deleted: list[str] = []
        kept: list[str] = []
        errors: list[dict[str, str]] = []
        for index, candidate in enumerate(backup_files):
            should_delete = candidate.stat().st_mtime < cutoff or index >= max_keep
            if not should_delete:
                kept.append(str(candidate))
                continue
            deleted.append(str(candidate))
            if dry_run:
                continue
            try:
                candidate.unlink()
            except Exception as exc:
                errors.append({'path': str(candidate), 'error': str(exc)[:300]})
        return {
            'status': 'dry_run' if dry_run else ('error' if errors else 'ok'),
            'pattern': pattern,
            'max_age_hours': max_age_hours,
            'max_keep': max_keep,
            'seen': len(backup_files),
            'deleted': len(deleted) - len(errors),
            'deleted_paths': deleted[:20],
            'kept': len(kept),
            'errors': errors,
        }

    def backup_database(self, label: str = 'cleanup') -> str:
        self.conn.commit()
        self.checkpoint_wal(truncate=False)
        source = Path(self.db_path)
        backup_path = source.with_name(f"{source.stem}_backup_{label}_{int(time.time())}{source.suffix}")
        shutil.copy2(source, backup_path)
        return str(backup_path)
