"""GDPR-style right-to-erasure request workflow.

Lifecycle: pending -> verified -> completed (or blocked).

1. A request is created for a customer_id; a verification token is issued
   (in real life this would go through an identity-verification step).
2. Verification checks the token and flips the request to 'verified'.
3. Processing deletes the subject's rows from transactional tables
   (orders, clickstream) and scrubs PII in the customers table, after
   checking the legal_holds table. Every deletion is audit-logged.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import anonymize_subject

# Tables that hold a data subject's data. The tuple is (table, pii_columns).
SUBJECT_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("orders", ()),
    ("clickstream", ()),
    ("customers", ("name", "email")),
)


@dataclass
class ErasureRequest:
    request_id: str
    customer_id: int
    status: str
    requested_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_request(conn: sqlite3.Connection,
                   customer_id: int) -> tuple[ErasureRequest, str]:
    """Open a new erasure request; returns it with the verify token printed
    by the CLI (in real life this would be delivered out-of-band)."""
    exists = conn.execute(
        "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    if not exists:
        raise ValueError(f"no customer with customer_id={customer_id}")

    request_id = "ER-" + secrets.token_hex(4).upper()
    token = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO erasure_requests (request_id, customer_id, status,"
        " requested_at, verify_token) VALUES (?, ?, 'pending', ?, ?)",
        (request_id, customer_id, _now(), token),
    )
    conn.commit()
    return (ErasureRequest(request_id, customer_id, "pending", _now()), token)


def verify_request(conn: sqlite3.Connection, request_id: str,
                   token: str) -> ErasureRequest:
    row = conn.execute(
        "SELECT * FROM erasure_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown request {request_id!r}")
    if row["status"] != "pending":
        raise ValueError(f"request {request_id!r} is {row['status']},"
                         " expected 'pending'")
    if not secrets.compare_digest(row["verify_token"], token):
        raise ValueError("verification token does not match")
    conn.execute(
        "UPDATE erasure_requests SET status = 'verified', verified_at = ?"
        " WHERE request_id = ?",
        (_now(), request_id),
    )
    conn.commit()
    return ErasureRequest(request_id, row["customer_id"], "verified",
                          row["requested_at"])


def process_request(conn: sqlite3.Connection, request_id: str) -> dict:
    """Execute a verified erasure request.

    Deletes the subject's transactional rows, scrubs PII, checks legal
    holds first, and audit-logs everything. Idempotent per request:
    completing an already-completed request is a no-op error.
    """
    row = conn.execute(
        "SELECT * FROM erasure_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown request {request_id!r}")
    if row["status"] == "blocked":
        raise ValueError(f"request {request_id!r} is blocked by a legal hold")
    if row["status"] != "verified":
        raise ValueError(f"request {request_id!r} is {row['status']},"
                         " expected 'verified'")
    customer_id = row["customer_id"]

    hold = conn.execute(
        "SELECT reason FROM legal_holds WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    if hold:
        conn.execute(
            "UPDATE erasure_requests SET status = 'blocked' WHERE request_id = ?",
            (request_id,),
        )
        conn.commit()
        raise PermissionError(
            f"erasure blocked: legal hold on customer {customer_id}"
            f" ({hold['reason']})")

    cur = conn.cursor()
    per_table: dict[str, int] = {}
    for table, pii_cols in SUBJECT_TABLES:
        if pii_cols:
            scrubbed = anonymize_subject(cur, customer_id)
            per_table[table] = scrubbed.get(table, 0)
        else:
            cur.execute(
                f"DELETE FROM {table} WHERE customer_id = ?", (customer_id,))
            per_table[table] = cur.rowcount
        cur.execute(
            "INSERT INTO purge_audit (run_at, action, policy, table_name,"
            " rows_affected, detail) VALUES (?, 'erasure', ?, ?, ?, ?)",
            (_now(), request_id, table, per_table[table],
             f"customer_id={customer_id}"),
        )
    conn.execute(
        "UPDATE erasure_requests SET status = 'completed', completed_at = ?"
        " WHERE request_id = ?",
        (_now(), request_id),
    )
    conn.commit()
    return {"request_id": request_id, "customer_id": customer_id,
            "per_table": per_table}


def list_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT request_id, customer_id, status, requested_at, verified_at,"
        " completed_at FROM erasure_requests ORDER BY requested_at DESC"
    ).fetchall()
