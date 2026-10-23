"""Retention policy definitions: load + validate a YAML policy file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ACTIONS = {"delete", "anonymize"}


@dataclass(frozen=True)
class RetentionPolicy:
    name: str
    table: str
    retention_days: int
    date_column: str = "created_at"
    on_expire: str = "delete"  # 'delete' | 'anonymize'

    def __post_init__(self) -> None:
        if self.retention_days < 0:
            raise ValueError(f"policy {self.name!r}: retention_days must be >= 0")
        if self.on_expire not in ACTIONS:
            raise ValueError(
                f"policy {self.name!r}: on_expire must be one of {sorted(ACTIONS)}")


def load_policies(path: str | Path) -> list[RetentionPolicy]:
    """Load and validate retention policies from a YAML file.

    Expected shape:
        policies:
          - name: clickstream-90d
            table: clickstream
            retention_days: 90
            date_column: created_at   # optional, defaults to created_at
            on_expire: delete         # optional: delete | anonymize
    """
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    raw = doc.get("policies", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected a non-empty 'policies' list")

    policies = [RetentionPolicy(**p) for p in raw]
    names = [p.name for p in policies]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: duplicate policy names")
    tables = [p.table for p in policies]
    if len(set(tables)) != len(tables):
        raise ValueError(f"{path}: a table may appear in only one policy")
    return policies
