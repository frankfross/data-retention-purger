"""Apply retention policies: dry-run estimates + real purges with audit rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .policies import RetentionPolicy
from .store import anonymize_subject


@dataclass
class PurgeResult:
    policy: str
    table: str
    cutoff: str
    rows_affected: int
    action: str
    dry_run: bool


def _cutoff(retention_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()


def estimate(conn: sqlite3.Connection, policy: RetentionPolicy) -> PurgeResult:
    """Count rows that would be purged; touches nothing."""
    cutoff = _cutoff(policy.retention_days)
    row = conn.execute(
        f"SELECT COUNT(*) FROM {policy.table} WHERE {policy.date_column} < ?",
        (cutoff,),
    ).fetchone()
    return PurgeResult(policy.name, policy.table, cutoff, row[0],
                       policy.on_expire, dry_run=True)


def apply(conn: sqlite3.Connection, policy: RetentionPolicy,
          dry_run: bool = False) -> PurgeResult:
    """Apply one retention policy; writes a purge_audit row unless dry_run."""
    if policy.on_expire == "anonymize":
        cols = {r[1] for r in conn.execute(
            f"PRAGMA table_info({policy.table})").fetchall()}
        if "customer_id" not in cols:
            raise ValueError(
                f"policy {policy.name!r}: on_expire='anonymize' needs a"
                f" customer_id column in {policy.table!r}")

    result = estimate(conn, policy)
    if dry_run:
        return result

    cur = conn.cursor()
    if policy.on_expire == "delete":
        cur.execute(
            f"DELETE FROM {policy.table} WHERE {policy.date_column} < ?",
            (result.cutoff,),
        )
        affected = cur.rowcount
    else:  # anonymize: scrub PII for each expired subject
        rows = cur.execute(
            f"SELECT DISTINCT customer_id FROM {policy.table}"
            f" WHERE {policy.date_column} < ? AND customer_id IS NOT NULL",
            (result.cutoff,),
        ).fetchall()
        for (cid,) in rows:
            anonymize_subject(cur, cid)
        affected = len(rows)

    cur.execute(
        "INSERT INTO purge_audit (run_at, action, policy, table_name,"
        " rows_affected, detail) VALUES (?, 'retention', ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), policy.name,
         policy.table, affected, f"cutoff={result.cutoff}"),
    )
    conn.commit()
    result.rows_affected = affected
    result.dry_run = False
    return result
