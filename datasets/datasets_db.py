#!/usr/bin/env python3
"""
datasets/datasets_db.py — a small SQLite store for persistent research datasets.

One table per dataset (mmi now; more to come). Kept separate from the production
`data/*.db` files — this is a queryable research/feature store the experiments read from.
Adding a dataset = a CREATE TABLE + an upsert in its own ingest script.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "datasets.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def upsert(con: sqlite3.Connection, table: str, rows: list[dict], key: str) -> int:
    """Insert-or-replace `rows` into `table` keyed on `key` (idempotent re-ingest)."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join(f":{c}" for c in cols)
    con.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c != key),
        rows,
    )
    con.commit()
    return len(rows)
