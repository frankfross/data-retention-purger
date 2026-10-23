"""Tests for retention policies + erasure workflow."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from retention_purger import erasure, purge
from retention_purger.policies import RetentionPolicy, load_policies
from retention_purger.store import anonymize_subject, connect, init_db


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def _seed(db, days: list[int]):
    cur = db.cursor()
    for i, d in enumerate(days, start=1):
        cur.execute(
            "INSERT INTO customers (customer_id, name, email, country, created_at)"
            " VALUES (?, ?, ?, 'US', ?)",
            (i, f"User {i}", f"user{i}@example.com", _iso(d)),
        )
        cur.execute(
            "INSERT INTO orders (customer_id, amount_cents, status, created_at)"
            " VALUES (?, 100, 'paid', ?)",
            (i, _iso(d)),
        )
        cur.execute(
            "INSERT INTO clickstream (customer_id, event_type, page, created_at)"
            " VALUES (?, 'view', '/p/1', ?)",
            (i, _iso(d)),
        )
    db.commit()


# --- policy loading ---------------------------------------------------------

def test_load_policies(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(
        "policies:\n"
        "  - name: a\n    table: t1\n    retention_days: 30\n"
        "  - name: b\n    table: t2\n    retention_days: 90\n    on_expire: anonymize\n"
    )
    policies = load_policies(f)
    assert [p.name for p in policies] == ["a", "b"]
    assert policies[0].on_expire == "delete"
    assert policies[1].on_expire == "anonymize"


def test_load_policies_rejects_duplicates(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(
        "policies:\n"
        "  - name: a\n    table: t1\n    retention_days: 30\n"
        "  - name: a\n    table: t2\n    retention_days: 30\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_policies(f)


def test_load_policies_rejects_same_table_twice(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(
        "policies:\n"
        "  - name: a\n    table: t1\n    retention_days: 30\n"
        "  - name: b\n    table: t1\n    retention_days: 60\n"
    )
    with pytest.raises(ValueError, match="only one policy"):
        load_policies(f)


def test_negative_retention_rejected():
    with pytest.raises(ValueError):
        RetentionPolicy(name="x", table="t", retention_days=-1)


# --- purging -----------------------------------------------------------------

def test_dry_run_touches_nothing(db):
    _seed(db, [10, 100, 400])
    policy = RetentionPolicy(name="orders-365d", table="orders",
                             retention_days=365)
    result = purge.apply(db, policy, dry_run=True)
    assert result.rows_affected == 1 and result.dry_run
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_delete_policy_removes_old_rows_and_audits(db):
    _seed(db, [10, 100, 400])
    policy = RetentionPolicy(name="orders-365d", table="orders",
                             retention_days=365)
    result = purge.apply(db, policy)
    assert result.rows_affected == 1
    remaining = [r[0] for r in db.execute(
        "SELECT created_at FROM orders ORDER BY created_at").fetchall()]
    assert len(remaining) == 2
    audit = db.execute(
        "SELECT action, policy, table_name, rows_affected FROM purge_audit"
    ).fetchall()
    assert len(audit) == 1
    assert tuple(audit[0]) == ("retention", "orders-365d", "orders", 1)


def test_anonymize_policy_scrubs_pii(db):
    _seed(db, [10, 800])
    policy = RetentionPolicy(name="customers-730d", table="customers",
                             retention_days=730, on_expire="anonymize")
    result = purge.apply(db, policy)
    assert result.rows_affected == 1
    row = db.execute(
        "SELECT name, email FROM customers WHERE customer_id = 2").fetchone()
    assert row[0] == "DELETED" and "@anonymized.invalid" in row[1]
    fresh = db.execute(
        "SELECT name FROM customers WHERE customer_id = 1").fetchone()
    assert fresh[0] == "User 1"


def test_anonymize_requires_customer_id(db):
    policy = RetentionPolicy(name="x", table="purge_audit",
                             retention_days=30, on_expire="anonymize")
    with pytest.raises(ValueError, match="customer_id"):
        purge.apply(db, policy)


def test_estimate_matches_apply(db):
    _seed(db, [5, 50, 500, 900])
    policy = RetentionPolicy(name="click-90d", table="clickstream",
                             retention_days=90)
    est = purge.estimate(db, policy)
    applied = purge.apply(db, policy)
    assert est.rows_affected == applied.rows_affected == 2


# --- erasure -----------------------------------------------------------------

def _open_and_verify(db, customer_id):
    req, token = erasure.create_request(db, customer_id)
    return erasure.verify_request(db, req.request_id, token)


def test_erasure_happy_path(db):
    _seed(db, [10])
    req = _open_and_verify(db, 1)
    summary = erasure.process_request(db, req.request_id)
    assert summary["per_table"] == {"orders": 1, "clickstream": 1,
                                    "customers": 1}
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM clickstream").fetchone()[0] == 0
    row = db.execute(
        "SELECT name FROM customers WHERE customer_id = 1").fetchone()
    assert row[0] == "DELETED"
    status = db.execute(
        "SELECT status FROM erasure_requests"
        " WHERE request_id = ?", (req.request_id,)).fetchone()[0]
    assert status == "completed"


def test_erasure_needs_verification_first(db):
    _seed(db, [10])
    req, _token = erasure.create_request(db, 1)
    with pytest.raises(ValueError, match="expected 'verified'"):
        erasure.process_request(db, req.request_id)


def test_erasure_wrong_token(db):
    _seed(db, [10])
    req, _token = erasure.create_request(db, 1)
    with pytest.raises(ValueError, match="token"):
        erasure.verify_request(db, req.request_id, "nope")


def test_erasure_unknown_customer(db):
    with pytest.raises(ValueError, match="no customer"):
        erasure.create_request(db, 999)


def test_erasure_blocked_by_legal_hold(db):
    _seed(db, [10])
    db.execute(
        "INSERT INTO legal_holds (customer_id, reason, placed_at)"
        " VALUES (1, 'open dispute', ?)",
        (_iso(0),),
    )
    db.commit()
    req = _open_and_verify(db, 1)
    with pytest.raises(PermissionError, match="legal hold"):
        erasure.process_request(db, req.request_id)
    # nothing was deleted
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    status = db.execute(
        "SELECT status FROM erasure_requests"
        " WHERE request_id = ?", (req.request_id,)).fetchone()[0]
    assert status == "blocked"


def test_erasure_audit_rows(db):
    _seed(db, [10])
    req = _open_and_verify(db, 1)
    erasure.process_request(db, req.request_id)
    rows = db.execute(
        "SELECT COUNT(*) FROM purge_audit WHERE action = 'erasure'"
        " AND policy = ?",
        (req.request_id,),
    ).fetchone()[0]
    assert rows == 3  # one per subject table
