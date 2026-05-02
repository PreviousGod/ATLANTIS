#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
LIVE_BRAIN = ROOT / '.hermes' / 'plugins' / 'live_brain'
PKG = 'live_brain_recipe_metrics_pkg'


def load_store():
    package = types.ModuleType(PKG)
    package.__path__ = [str(LIVE_BRAIN)]
    sys.modules[PKG] = package
    spec = importlib.util.spec_from_file_location(f'{PKG}.store', LIVE_BRAIN / 'store.py')
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[f'{PKG}.store'] = mod
    spec.loader.exec_module(mod)
    return mod


def health_bucket(ratio: float | None) -> str:
    if ratio is None:
        return 'no_data'
    if ratio >= 0.7:
        return 'healthy'
    if ratio >= 0.3:
        return 'watch'
    if ratio >= 0.1:
        return 'warning'
    return 'critical'


def median_or_none(values: list[float]) -> float | None:
    return round(float(median(values)), 2) if values else None


def query_counts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, int]:
    return {str(row[0] or 'none'): int(row[1] or 0) for row in conn.execute(sql, params).fetchall()}


def main() -> int:
    parser = argparse.ArgumentParser(description='Live Brain recipe learning metrics.')
    parser.add_argument('--db', default=str(Path.home() / '.hermes' / 'live_brain' / 'live_brain.db'))
    parser.add_argument('--scope-key', default='')
    parser.add_argument('--days', type=int, default=30)
    args = parser.parse_args()

    store_mod = load_store()
    store = store_mod.LiveBrainStore(args.db)
    store.initialize_schema()
    conn = store.conn
    report_7d = store.attribution_report(scope_key=args.scope_key, days=7)
    report_30d = store.attribution_report(scope_key=args.scope_key, days=args.days)
    scope_clause = ' AND scope_key=?' if args.scope_key else ''
    scope_params = (args.scope_key,) if args.scope_key else ()

    lag_rows = conn.execute(
        f"SELECT updated_at - created_at AS lag FROM context_impressions WHERE outcome IN ('success','failure') AND updated_at >= created_at AND created_at >= strftime('%s','now') - ? * 86400{scope_clause}",
        (args.days, *scope_params),
    ).fetchall()
    lags = [float(row['lag']) for row in lag_rows if row['lag'] is not None]

    candidate_rows = conn.execute(
        f"SELECT promoted_at - candidate_since AS lag FROM fix_recipes WHERE promoted_at IS NOT NULL AND candidate_since IS NOT NULL AND promoted_at >= candidate_since{scope_clause}",
        scope_params,
    ).fetchall()
    candidate_lags = [float(row['lag']) for row in candidate_rows if row['lag'] is not None]

    active_zero_impressions = conn.execute(
        f"SELECT COUNT(*) FROM fix_recipes r WHERE r.status='active'{scope_clause} AND NOT EXISTS (SELECT 1 FROM context_impressions i WHERE i.recipe_ids_json LIKE '%' || r.recipe_id || '%')",
        scope_params,
    ).fetchone()[0]
    active_zero_attributed = conn.execute(
        f"SELECT COUNT(*) FROM fix_recipes r WHERE r.status='active'{scope_clause} AND NOT EXISTS (SELECT 1 FROM context_impressions i WHERE i.outcome IN ('success','failure') AND i.recipe_ids_json LIKE '%' || r.recipe_id || '%')",
        scope_params,
    ).fetchone()[0]
    recovered = conn.execute(
        f"SELECT COUNT(*) FROM fix_recipes WHERE status IN ('candidate','active') AND last_reviewed_at IS NOT NULL{scope_clause}",
        scope_params,
    ).fetchone()[0]
    active_stale_cutoff = 45
    candidate_stale_cutoff = 30
    stale_active = conn.execute(
        f"""
        SELECT COUNT(*) FROM fix_recipes r
        WHERE r.status='active'{scope_clause}
          AND COALESCE(r.promoted_at, r.updated_at, r.created_at) < strftime('%s','now') - ? * 86400
          AND NOT EXISTS (
              SELECT 1 FROM context_impressions i
              WHERE i.recipe_ids_json LIKE '%' || r.recipe_id || '%'
                AND i.created_at >= strftime('%s','now') - ? * 86400
          )
        """,
        (*scope_params, active_stale_cutoff, active_stale_cutoff),
    ).fetchone()[0]
    stale_candidate = conn.execute(
        f"""
        SELECT COUNT(*) FROM fix_recipes r
        WHERE r.status='candidate'{scope_clause}
          AND COALESCE(r.candidate_since, r.updated_at, r.created_at) < strftime('%s','now') - ? * 86400
          AND NOT EXISTS (
              SELECT 1 FROM context_impressions i
              WHERE i.recipe_ids_json LIKE '%' || r.recipe_id || '%'
                AND i.outcome='success'
                AND i.created_at >= strftime('%s','now') - ? * 86400
          )
        """,
        (*scope_params, candidate_stale_cutoff, candidate_stale_cutoff),
    ).fetchone()[0]
    ageing_audit = query_counts(conn, "SELECT reason, COUNT(*) FROM audit_log WHERE object_type='fix_recipe' AND action IN ('degrade','review') GROUP BY reason")

    causal_daily = conn.execute(
        f"SELECT date(created_at,'unixepoch') day, COUNT(*) total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) success FROM causal_activations WHERE created_at >= strftime('%s','now') - ? * 86400{scope_clause} GROUP BY day ORDER BY day DESC",
        (args.days, *scope_params),
    ).fetchall()
    candidate_daily = conn.execute(
        f"SELECT date(created_at,'unixepoch') day, COUNT(*) total FROM fix_recipes WHERE created_at >= strftime('%s','now') - ? * 86400{scope_clause} GROUP BY day ORDER BY day DESC",
        (args.days, *scope_params),
    ).fetchall()
    impression_daily = conn.execute(
        f"SELECT date(created_at,'unixepoch') day, COUNT(*) total, SUM(CASE WHEN outcome IN ('success','failure') THEN 1 ELSE 0 END) feedback FROM context_impressions WHERE created_at >= strftime('%s','now') - ? * 86400{scope_clause} GROUP BY day ORDER BY day DESC",
        (args.days, *scope_params),
    ).fetchall()
    first_activation = conn.execute(f"SELECT MIN(created_at) FROM causal_activations WHERE success=1{scope_clause}", scope_params).fetchone()[0]
    first_candidate = conn.execute(f"SELECT MIN(created_at) FROM fix_recipes WHERE source='causal_activation'{scope_clause}", scope_params).fetchone()[0]
    first_candidate_latency = round(float(first_candidate - first_activation), 2) if first_activation and first_candidate and first_candidate >= first_activation else None

    result = {
        'scope_key': args.scope_key,
        'days': args.days,
        'precision_ratio_7d': report_7d['precision_ratio'],
        'precision_ratio_30d': report_30d['precision_ratio'],
        'health_7d': health_bucket(report_7d['precision_ratio']),
        'health_30d': health_bucket(report_30d['precision_ratio']),
        'alert': health_bucket(report_30d['precision_ratio']) == 'critical',
        'attribution_counts_30d': report_30d['counts'],
        'candidate_rejections_by_reason': query_counts(conn, f"SELECT reason, COUNT(*) FROM recipe_rejections WHERE created_at >= strftime('%s','now') - ? * 86400{scope_clause} GROUP BY reason", (args.days, *scope_params)),
        'causal_activations_daily': [{'day': row['day'], 'total': int(row['total'] or 0), 'success': int(row['success'] or 0)} for row in causal_daily],
        'candidate_creations_daily': [{'day': row['day'], 'total': int(row['total'] or 0)} for row in candidate_daily],
        'feedback_daily': [{'day': row['day'], 'impressions': int(row['total'] or 0), 'feedback': int(row['feedback'] or 0)} for row in impression_daily],
        'first_success_activation_to_first_candidate_seconds': first_candidate_latency,
        'median_attribution_lag_seconds': median_or_none(lags),
        'median_candidate_to_promotion_seconds': median_or_none(candidate_lags),
        'recipe_status_counts': query_counts(conn, f"SELECT status, COUNT(*) FROM fix_recipes WHERE 1=1{scope_clause} GROUP BY status", scope_params),
        'recipe_source_counts': query_counts(conn, f"SELECT source, COUNT(*) FROM fix_recipes WHERE 1=1{scope_clause} GROUP BY source", scope_params),
        'active_recipes_zero_impressions': int(active_zero_impressions),
        'active_recipes_zero_attributed_outcomes': int(active_zero_attributed),
        'needs_review_recovered_count': int(recovered),
        'stale_policy_days': {'active': active_stale_cutoff, 'candidate': candidate_stale_cutoff},
        'stale_active_recipes': int(stale_active),
        'stale_candidate_recipes': int(stale_candidate),
        'recipe_ageing_audit_counts': ageing_audit,
    }
    store.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
