from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import stable_id
from .audit import record_revision, row_to_dict


ROLE_ALIASES = {
    'part1': 'part_1',
    'part_1': 'part_1',
    'part 1': 'part_1',
    'first': 'part_1',
    'prvi': 'part_1',
    'part2': 'part_2',
    'part_2': 'part_2',
    'part 2': 'part_2',
    'second': 'part_2',
    'drugi': 'part_2',
    'combined': 'combined_or_full',
    'full': 'combined_or_full',
    'complete': 'combined_or_full',
}


def normalize_project_key(value: str) -> str:
    text = (value or '').strip().lower()
    if not text:
        return ''
    if 'enoch' in text or 'enoh' in text:
        return 'enoch'
    return ''.join(ch if ch.isalnum() else '_' for ch in text).strip('_')


def normalize_role(value: str) -> str:
    text = (value or '').strip().lower().replace('-', '_')
    return ROLE_ALIASES.get(text, ''.join(ch if ch.isalnum() else '_' for ch in text).strip('_'))


def infer_project_key(text: str) -> str:
    lowered = (text or '').lower()
    if 'enoch' in lowered or 'enoh' in lowered:
        return 'enoch'
    return ''


def infer_roles(text: str) -> List[str]:
    lowered = (text or '').lower()
    roles: List[str] = []
    if any(token in lowered for token in ['part 1', 'part1', 'prvi', 'first']):
        roles.append('part_1')
    if any(token in lowered for token in ['part 2', 'part2', 'drugi', 'second']):
        roles.append('part_2')
    if any(token in lowered for token in ['ona dva', 'oba', 'both', 'two videos', 'dva videa']):
        for role in ['part_1', 'part_2']:
            if role not in roles:
                roles.append(role)
    return roles


def artifact_query_signal(text: str) -> bool:
    lowered = (text or '').lower()
    return any(token in lowered for token in [
        'pošalj', 'posalj', 'send', 'video', 'videa', 'fajl', 'file', 'artifact',
        'artefakt', 'part', 'deo', 'dio', 'slik', 'image', 'enoch', 'enoh', 'tačni fajlovi', 'tacni fajlovi',
        'plugin.yaml', 'plugin yml', 'manifest',
    ])


class ArtifactRegistry:
    def __init__(self, conn):
        self.conn = conn

    def upsert_artifact(
        self,
        *,
        project_key: str,
        role: str,
        path: str,
        label: str = '',
        status: str = 'verified',
        confidence: float = 1.0,
        source: str = 'manual',
        supersedes_artifact_id: str = '',
        evidence: Optional[Dict[str, Any]] = None,
        scope_tags: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        project = normalize_project_key(project_key)
        normalized_role = normalize_role(role)
        artifact_path = str(Path(path).expanduser())
        now = time.time()
        artifact_id = stable_id('artifact', f'{project}:{normalized_role}:{artifact_path}')
        before = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
        size_bytes = 0
        mime_type = mimetypes.guess_type(artifact_path)[0] or ''
        if os.path.exists(artifact_path):
            try:
                size_bytes = os.path.getsize(artifact_path)
            except OSError:
                size_bytes = 0
        self.conn.execute(
            """
            INSERT INTO verified_artifacts (
                artifact_id, project_key, role, path, label, status, confidence, source,
                mime_type, size_bytes, duration_seconds, checksum, supersedes_artifact_id,
                evidence_json, scope_tags_json, verified_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                label=excluded.label,
                status=excluded.status,
                confidence=excluded.confidence,
                source=excluded.source,
                mime_type=excluded.mime_type,
                size_bytes=excluded.size_bytes,
                supersedes_artifact_id=excluded.supersedes_artifact_id,
                evidence_json=excluded.evidence_json,
                scope_tags_json=excluded.scope_tags_json,
                verified_at=excluded.verified_at,
                updated_at=excluded.updated_at
            """,
            (
                artifact_id,
                project,
                normalized_role,
                artifact_path,
                label,
                status,
                float(confidence),
                source,
                mime_type,
                int(size_bytes),
                supersedes_artifact_id,
                json.dumps(evidence or {}, ensure_ascii=False),
                json.dumps(scope_tags or {}, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
        record_revision(self.conn, object_type='verified_artifact', object_id=artifact_id, action='upsert', reason=source or 'artifact_registry', before=before, after=after, created_at=now)
        return {'status': 'ok', 'artifact_id': artifact_id, 'project_key': project, 'role': normalized_role, 'path': artifact_path}

    def list_project(self, project_key: str, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        project = normalize_project_key(project_key)
        where = 'project_key=?'
        params: list[Any] = [project]
        if not include_inactive:
            where += " AND status IN ('verified','candidate')"
        rows = self.conn.execute(
            f"SELECT * FROM verified_artifacts WHERE {where} ORDER BY role, status='verified' DESC, confidence DESC, verified_at DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve(self, project_key: str, role: str, *, allow_candidate: bool = False) -> Dict[str, Any]:
        project = normalize_project_key(project_key)
        normalized_role = normalize_role(role)
        statuses = ["verified"] + (["candidate"] if allow_candidate else [])
        placeholders = ','.join('?' for _ in statuses)
        rows = self.conn.execute(
            f"""
            SELECT * FROM verified_artifacts
            WHERE project_key=? AND role=? AND status IN ({placeholders})
            ORDER BY status='verified' DESC, confidence DESC, verified_at DESC, updated_at DESC
            LIMIT 10
            """,
            [project, normalized_role, *statuses],
        ).fetchall()
        candidates = [dict(row) for row in rows]
        missing = []
        for candidate in candidates:
            path = candidate.get('path') or ''
            if path and os.path.exists(path):
                return {
                    'status': 'resolved',
                    'artifact': candidate,
                    'path': path,
                    'project_key': project,
                    'role': normalized_role,
                    'confidence': candidate.get('confidence', 0),
                    'must_use': True,
                }
            missing.append(candidate)
            before = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (candidate.get('artifact_id'),)).fetchone())
            missing_at = time.time()
            self.conn.execute(
                "UPDATE verified_artifacts SET status='missing', updated_at=? WHERE artifact_id=?",
                (missing_at, candidate.get('artifact_id')),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (candidate.get('artifact_id'),)).fetchone())
            record_revision(self.conn, object_type='verified_artifact', object_id=str(candidate.get('artifact_id') or ''), action='mark_missing', reason='resolved_path_not_found', before=before, after=after, created_at=missing_at)
        return {
            'status': 'needs_resolution',
            'project_key': project,
            'role': normalized_role,
            'reason': 'no_existing_verified_artifact' if missing else 'no_verified_artifact',
            'candidates': candidates,
        }

    def mark_status(self, *, path: str, status: str, reason: str = '') -> Dict[str, Any]:
        now = time.time()
        artifact_path = str(Path(path).expanduser())
        rows = self.conn.execute("SELECT artifact_id FROM verified_artifacts WHERE path=?", (artifact_path,)).fetchall()
        for row in rows:
            before = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (row['artifact_id'],)).fetchone())
            self.conn.execute(
                "UPDATE verified_artifacts SET status=?, updated_at=? WHERE artifact_id=?",
                (status, now, row['artifact_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM verified_artifacts WHERE artifact_id=?", (row['artifact_id'],)).fetchone())
            record_revision(self.conn, object_type='verified_artifact', object_id=row['artifact_id'], action=f'mark_{status}', reason=reason or 'artifact_status_change', before=before, after=after, created_at=now)
            self.conn.execute(
                "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'verified_artifact', ?, ?, ?, ?, ?)",
                (
                    stable_id('audit', f"artifact:{row['artifact_id']}:{status}:{now}"),
                    row['artifact_id'],
                    f'mark_{status}',
                    reason,
                    json.dumps({'path': artifact_path}, ensure_ascii=False),
                    now,
                ),
            )
        return {'status': 'ok', 'updated': len(rows), 'path': artifact_path, 'new_status': status}

    def infer_project_for_roles(self, roles: List[str]) -> str:
        if not roles:
            return ''
        placeholders = ','.join('?' for _ in roles)
        rows = self.conn.execute(
            f"""
            SELECT project_key, COUNT(DISTINCT role) matched_roles
            FROM verified_artifacts
            WHERE role IN ({placeholders}) AND status IN ('verified','candidate')
            GROUP BY project_key
            ORDER BY matched_roles DESC, MAX(confidence) DESC, MAX(updated_at) DESC
            LIMIT 3
            """,
            roles,
        ).fetchall()
        if not rows:
            return ''
        best = rows[0]
        if len(rows) == 1:
            return str(best['project_key'] or '')
        if int(best['matched_roles'] or 0) >= len(roles) and int(rows[1]['matched_roles'] or 0) < int(best['matched_roles'] or 0):
            return str(best['project_key'] or '')
        return ''

    def context_lines_for_query(self, query_text: str, *, limit: int = 5) -> List[str]:
        if not artifact_query_signal(query_text):
            return []
        project = infer_project_key(query_text)
        roles = infer_roles(query_text)
        lowered = (query_text or '').lower()
        if not project and ('plugin.yaml' in lowered or 'manifest' in lowered):
            rows = self.conn.execute(
                """
                SELECT project_key, role, label, path, status
                FROM verified_artifacts
                WHERE status IN ('verified', 'candidate')
                  AND lower(path) LIKE '%plugin.yaml%'
                ORDER BY status='verified' DESC, confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                f"project={row['project_key']} role={row['role']} label={row['label'] or row['role']} path={row['path']} status={row['status']}"
                for row in rows
            ]
        if not roles:
            roles = ['part_1', 'part_2', 'combined_or_full']
        if not project:
            project = self.infer_project_for_roles(roles)
        if not project:
            return []
        lines: List[str] = []
        seen = set()
        for role in roles:
            resolved = self.resolve(project, role)
            if resolved.get('status') != 'resolved':
                continue
            artifact = resolved.get('artifact') or {}
            key = (artifact.get('project_key'), artifact.get('role'), artifact.get('path'))
            if key in seen:
                continue
            seen.add(key)
            label = artifact.get('label') or artifact.get('role') or role
            lines.append(
                f"project={artifact.get('project_key')} role={artifact.get('role')} label={label} path={artifact.get('path')} status={artifact.get('status')}"
            )
            if len(lines) >= limit:
                break
        return lines
