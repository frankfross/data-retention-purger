# data-retention-purger

Two compliance workflows every data team ends up hand-rolling: **retention
policies** and **right-to-erasure (GDPR-style) requests**, implemented as a
small, auditable Python CLI over SQLite.

- **Retention policies** are declared in `policies.yaml` (table, retention
  window, what to do on expiry: `delete` or `anonymize`). The purger
  estimates impact first, then deletes or scrubs PII, and writes every run
  to an audit log.
- **Erasure requests** follow a `pending → verified → completed` lifecycle
  with a verification token, a legal-hold check, and per-table deletion /
  PII scrubbing — also audit-logged.

The audit tables themselves are excluded from user policies (they get their
own, longer retention in production).

## Quickstart

```bash
pip install pyyaml pytest
python -m retention_purger.cli init-demo --customers 40
python -m retention_purger.cli status
python -m retention_purger.cli apply-policies --dry-run
```

Example output:

```
seeded retention.db: customers=40, orders=175, clickstream=628
customers    40 rows
orders       175 rows
clickstream  628 rows
[DRY-RUN] clickstream-90d: 578 rows in clickstream older than 2026-07-25 (delete)
[DRY-RUN] orders-365d: 107 rows in orders older than 2025-10-23 (delete)
[DRY-RUN] customers-730d: 6 rows in customers older than 2024-10-23 (anonymize)
total rows affected: 691
```

Apply the policies for real (drop `--dry-run`), then run an erasure request:

```bash
python -m retention_purger.cli apply-policies
python -m retention_purger.cli request-erasure --customer-id 7
# → prints a request id and a verification token
python -m retention_purger.cli verify-erasure --request-id ER-XXXX --token <token>
python -m retention_purger.cli process-erasure --request-id ER-XXXX
python -m retention_purger.cli requests
python -m retention_purger.cli audit --limit 10
```

```
request ER-30764865 opened for customer 7
verification token (deliver to requester out-of-band): J2qyTE3aHudLnIoW0PufFQ
request ER-30764865 verified
request ER-30764865 completed for customer 7:
  orders: 2 rows deleted/scrubbed
  clickstream: 5 rows deleted/scrubbed
  customers: 1 rows deleted/scrubbed
```

## Policies

`policies.yaml`:

```yaml
policies:
  - name: clickstream-90d
    table: clickstream
    retention_days: 90
    on_expire: delete
  - name: orders-365d
    table: orders
    retention_days: 365
    on_expire: delete
  - name: customers-730d
    table: customers
    retention_days: 730
    on_expire: anonymize   # keep the row, scrub name/email
```

One policy per table (enforced at load time). `anonymize` requires a
`customer_id` column so the purger can find whose PII to scrub.

## Design notes

- **Dry-run first is the default mindset.** `estimate()` counts doomed
  rows without touching anything, and `apply-policies --dry-run` prints the
  impact per policy. Retention purges are irreversible — a human reviews
  the dry-run before the real run.
- **Everything is audited.** `purge_audit` records every retention and
  erasure action with timestamp, policy/request id, table, row count, and
  the cutoff used. Compliance is worthless without a paper trail.
- **Erasure is a state machine, not a DELETE statement.**
  `pending → verified → completed`, with `blocked` as a terminal state
  when a legal hold exists. Identity verification (token), the hold check,
  and the deletion itself are separate steps so each can be reviewed.
- **Anonymize vs. delete is a policy choice, not a code choice.** Event
  data (`clickstream`) is simply dropped; customer records are anonymized
  (name/email scrubbed) so downstream foreign keys don't break. Financial
  rows (`orders`) get the longest retention — tax/audit reality.
- **The purger's own tables are out of scope.** `purge_audit`,
  `erasure_requests`, and `legal_holds` are never subject to user policies;
  in production they'd sit under their own (longer) retention rule.

## Tests

```bash
pytest -q
```

15 tests covering policy validation (duplicates, table reuse, negative
windows), dry-run isolation, delete/anonymize purging with audit rows, and
the erasure lifecycle (verification, wrong token, unknown customer, legal
hold blocking with zero deletions, audit completeness).

## Honest scope note

This is a knowledge demo of retention/erasure mechanics on SQLite — not a
production GDPR system. A real deployment would add identity verification
behind the token, key-based access controls, an approval step before
`process-erasure`, and per-jurisdiction retention schedules.
