"""Data retention purger + GDPR-style erasure requests on SQLite.

A focused, auditable implementation of two compliance workflows data
engineers are frequently asked to build:

1. Retention policies — rows older than a per-table retention window are
   deleted (or anonymized) on a schedule.
2. Right-to-erasure requests — a data subject asks for their data to be
   removed; the request is verified, checked against legal holds, and the
   subject's data is deleted or anonymized across every table that holds
   it. Every action lands in an audit log.
"""
