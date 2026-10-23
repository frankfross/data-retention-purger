"""CLI for the data retention purger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import erasure, purge
from .policies import load_policies
from .store import connect, init_db, seed_demo, table_counts

DEFAULT_DB = "retention.db"


def _conn(args) -> "sqlite3.Connection":
    import sqlite3

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"database not found: {args.db} (run 'init-demo' first)")
    from .store import connect

    return connect(db)


def cmd_init_demo(args) -> int:
    db = Path(args.db)
    if db.exists() and not args.force:
        print(f"{args.db} already exists; use --force to reseed")
        return 1
    if db.exists():
        db.unlink()
    conn = init_db(db)
    counts = seed_demo(conn, n_customers=args.customers)
    conn.close()
    print(f"seeded {args.db}: " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    return 0


def cmd_apply(args) -> int:
    policies = load_policies(args.policies)
    conn = _conn(args)
    total = 0
    for policy in policies:
        result = purge.apply(conn, policy, dry_run=args.dry_run)
        tag = "DRY-RUN" if args.dry_run else "APPLIED"
        print(f"[{tag}] {result.policy}: {result.rows_affected} rows in"
              f" {result.table} older than {result.cutoff[:10]}"
              f" ({result.action})")
        total += result.rows_affected
    conn.close()
    print(f"total rows affected: {total}")
    return 0


def cmd_request_erasure(args) -> int:
    conn = _conn(args)
    try:
        req, token = erasure.create_request(conn, args.customer_id)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    conn.close()
    print(f"request {req.request_id} opened for customer {req.customer_id}")
    print(f"verification token (deliver to requester out-of-band): {token}")
    return 0


def cmd_verify_erasure(args) -> int:
    conn = _conn(args)
    try:
        req = erasure.verify_request(conn, args.request_id, args.token)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    conn.close()
    print(f"request {req.request_id} verified")
    return 0


def cmd_process_erasure(args) -> int:
    conn = _conn(args)
    try:
        summary = erasure.process_request(conn, args.request_id)
    except (ValueError, PermissionError) as exc:
        print(f"error: {exc}")
        return 1
    conn.close()
    print(f"request {summary['request_id']} completed"
          f" for customer {summary['customer_id']}:")
    for table, n in summary["per_table"].items():
        print(f"  {table}: {n} rows deleted/scrubbed")
    return 0


def cmd_requests(args) -> int:
    conn = _conn(args)
    rows = erasure.list_requests(conn)
    conn.close()
    if not rows:
        print("no erasure requests")
        return 0
    print(f"{'request_id':<14}{'customer':<10}{'status':<12}{'requested_at'}")
    for r in rows:
        print(f"{r['request_id']:<14}{r['customer_id']:<10}{r['status']:<12}"
              f"{r['requested_at'][:19]}")
    return 0


def cmd_audit(args) -> int:
    conn = _conn(args)
    rows = conn.execute(
        "SELECT run_at, action, policy, table_name, rows_affected, detail"
        " FROM purge_audit ORDER BY audit_id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("audit log is empty")
        return 0
    for r in rows:
        print(f"{r['run_at'][:19]}  {r['action']:<10} {str(r['policy']):<14}"
              f"{r['table_name']:<12} rows={r['rows_affected']}  {r['detail']}")
    return 0


def cmd_status(args) -> int:
    conn = _conn(args)
    for table, n in table_counts(conn).items():
        print(f"{table:<12} {n} rows")
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="retention-purger",
        description="Apply data retention policies and handle erasure requests.")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-demo", help="create and seed a demo database")
    s.add_argument("--customers", type=int, default=40)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_demo)

    s = sub.add_parser("apply-policies", help="run retention policies")
    s.add_argument("--policies", default="policies.yaml")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("request-erasure", help="open an erasure request")
    s.add_argument("--customer-id", type=int, required=True)
    s.set_defaults(func=cmd_request_erasure)

    s = sub.add_parser("verify-erasure", help="verify an erasure request")
    s.add_argument("--request-id", required=True)
    s.add_argument("--token", required=True)
    s.set_defaults(func=cmd_verify_erasure)

    s = sub.add_parser("process-erasure", help="execute a verified request")
    s.add_argument("--request-id", required=True)
    s.set_defaults(func=cmd_process_erasure)

    s = sub.add_parser("requests", help="list erasure requests")
    s.set_defaults(func=cmd_requests)

    s = sub.add_parser("audit", help="show the audit log")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("status", help="show row counts")
    s.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
