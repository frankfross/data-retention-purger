"""SQLite store: schema, demo data seeding, and low-level helpers."""

from __future__ import annotations

import random
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    country       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    amount_cents  INTEGER NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clickstream (
    event_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER REFERENCES customers(customer_id),
    event_type    TEXT NOT NULL,
    page          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Tables the purger itself owns. These are NOT subject to user policies;
-- they have a fixed, longer retention (see docs).
CREATE TABLE IF NOT EXISTS purge_audit (
    audit_id      INTEGER PRIMARY KEY,
    run_at        TEXT NOT NULL,
    action        TEXT NOT NULL,          -- 'retention' | 'erasure'
    policy        TEXT,                   -- policy name or request id
    table_name    TEXT NOT NULL,
    rows_affected INTEGER NOT NULL,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS erasure_requests (
    request_id    TEXT PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    status        TEXT NOT NULL,          -- pending | verified | completed | blocked
    requested_at  TEXT NOT NULL,
    verified_at   TEXT,
    completed_at  TEXT,
    verify_token  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legal_holds (
    customer_id   INTEGER PRIMARY KEY,
    reason        TEXT NOT NULL,
    placed_at     TEXT NOT NULL
);
"""

_FIRST = ["Ava", "Liam", "Mia", "Noah", "Emma", "Lucas", "Olivia", "Ethan",
          "Sophia", "Mason", "Isabella", "James", "Amelia", "Benjamin", "Harper"]
_LAST = ["Smith", "Garcia", "Kim", "Muller", "Rossi", "Dubois", "Silva",
         "Haddad", "Tanaka", "Novak", "Khan", "Okafor", "Petrov", "Larsen"]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def seed_demo(conn: sqlite3.Connection, n_customers: int = 40,
              rng_seed: int = 42) -> dict[str, int]:
    """Seed demo data with ages spread from 1 day to 900 days old.

    Ages are deliberately spread so retention policies have something to
    bite on while recently created rows survive.
    """
    rng = random.Random(rng_seed)
    cur = conn.cursor()

    for cid in range(1, n_customers + 1):
        name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        email = f"{name.lower().replace(' ', '.')}@example.com"
        cur.execute(
            "INSERT INTO customers (customer_id, name, email, country, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (cid, name, email, rng.choice(["US", "DE", "FR", "BR"]),
             _iso(rng.randint(1, 900))),
        )
        for _ in range(rng.randint(0, 8)):
            cur.execute(
                "INSERT INTO orders (customer_id, amount_cents, status, created_at)"
                " VALUES (?, ?, ?, ?)",
                (cid, rng.randint(500, 20000),
                 rng.choice(["paid", "paid", "shipped", "refunded"]),
                 _iso(rng.randint(1, 900))),
            )
        for _ in range(rng.randint(0, 30)):
            cur.execute(
                "INSERT INTO clickstream (customer_id, event_type, page, created_at)"
                " VALUES (?, ?, ?, ?)",
                (cid, rng.choice(["view", "click", "purchase"]),
                 f"/p/{rng.randint(1, 50)}", _iso(rng.randint(1, 900))),
            )
    conn.commit()
    counts = {
        t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("customers", "orders", "clickstream")
    }
    return counts


def anonymize_subject(cur: sqlite3.Cursor, customer_id: int) -> dict[str, int]:
    """Scrub PII for one customer; returns rows updated per table."""
    token = "".join(random.Random(customer_id).choices(string.hexdigits, k=8))
    cur.execute(
        "UPDATE customers SET name = 'DELETED', email = ? WHERE customer_id = ?",
        (f"deleted-{token}@anonymized.invalid", customer_id),
    )
    customers = cur.rowcount
    return {"customers": customers}


def table_counts(conn: sqlite3.Connection,
                 tables: tuple[str, ...] = ("customers", "orders", "clickstream")) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables
    }
