#!/usr/bin/env python3
"""Load Northwind into Neon Postgres under schema `northwind`.

Usage:
  python scripts/load_northwind_to_neon.py

Environment:
  NEON_TEXT2SQL_URL=postgresql://.../dbname?sslmode=require
  NORTHWIND_SQL_URL=optional override for source SQL file
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

import psycopg
from dotenv import load_dotenv

DEFAULT_NORTHWIND_SQL_URL = (
    "https://raw.githubusercontent.com/pthom/northwind_psql/master/northwind.sql"
)


def _read_env() -> tuple[str, str]:
    load_dotenv()
    db_url = os.getenv("NEON_TEXT2SQL_URL", "").strip()
    if not db_url:
        raise RuntimeError("Missing NEON_TEXT2SQL_URL in environment/.env")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        raise RuntimeError(
            "NEON_TEXT2SQL_URL must be a Postgres URI "
            "(postgresql://... with sslmode=require), not an HTTP REST URL."
        )
    source_url = os.getenv("NORTHWIND_SQL_URL", DEFAULT_NORTHWIND_SQL_URL).strip()
    return db_url, source_url


def _download_sql(source_url: str) -> str:
    with urllib.request.urlopen(source_url, timeout=60) as response:
        content = response.read()
    return content.decode("utf-8")


def _prepare_sql(raw_sql: str) -> str:
    # Force all unqualified objects into `northwind` while preserving script logic.
    preamble = "\n".join(
        [
            "CREATE SCHEMA IF NOT EXISTS northwind;",
            "SET search_path TO northwind, public;",
            "",
        ]
    )
    return preamble + raw_sql


def _write_cache_file(sql_text: str) -> Path:
    cache_dir = Path("data") / "northwind"
    cache_dir.mkdir(parents=True, exist_ok=True)
    sql_path = cache_dir / "northwind.sql"
    sql_path.write_text(sql_text, encoding="utf-8")
    return sql_path


def _execute_sql(db_url: str, sql_text: str) -> None:
    # Use a temp file with psql-compatible batching via psycopg execute.
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)


def _verify_load(db_url: str) -> tuple[int, int]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'northwind'
                  AND table_type = 'BASE TABLE'
                """
            )
            table_count = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT COALESCE(SUM(row_count), 0)
                FROM (
                  SELECT (xpath('/row/cnt/text()',
                    query_to_xml(
                      format('SELECT COUNT(*) AS cnt FROM %I.%I', schemaname, tablename),
                      false, true, ''
                    )
                  ))[1]::text::bigint AS row_count
                  FROM pg_tables
                  WHERE schemaname = 'northwind'
                ) q
                """
            )
            total_rows = int(cur.fetchone()[0])
    return table_count, total_rows


def main() -> int:
    try:
        db_url, source_url = _read_env()
        print(f"[1/4] Downloading Northwind SQL from: {source_url}")
        raw_sql = _download_sql(source_url)

        print("[2/4] Preparing SQL for schema `northwind`")
        sql_text = _prepare_sql(raw_sql)
        sql_path = _write_cache_file(sql_text)
        print(f"      Cached SQL at: {sql_path}")

        print("[3/4] Loading data into Neon...")
        _execute_sql(db_url, sql_text)

        print("[4/4] Verifying load...")
        table_count, total_rows = _verify_load(db_url)
        print(f"      Loaded tables: {table_count}")
        print(f"      Total rows across northwind schema: {total_rows}")
        print("Northwind load completed successfully.")
        return 0
    except Exception as exc:  # pragma: no cover
        print(f"Failed to load Northwind: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
