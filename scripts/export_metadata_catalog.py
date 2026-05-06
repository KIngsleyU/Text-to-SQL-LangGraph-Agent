#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Export the Step 1 **metadata catalog** artifact for a PostgreSQL schema.

================================================================================
Purpose (Alignment with system_design_docs/step1)
================================================================================

**Data and RAG Preparation Pipeline** and the **Execution Template** require a rich
``metadata_catalog`` *before* dumping raw DDL into an LLM:

  * Tables, columns, data types, nullability, **row counts**
  * **Primary keys and foreign keys** (join path grounding)
  * **Sample values** for text-like columns (value-format hints for WHERE clauses)
  * **Table and column comments** from PostgreSQL (``COMMENT ON``) — *template §1*
  * **Versioning / snapshot discipline** — *template §7* and exit gate

This script connects to Neon (or any Postgres) using ``NEON_TEXT2SQL_URL``,
introspects **one schema** (default: ``northwind``), and writes:

``data/artifacts/metadata_catalog.<schema>.json``

Optional: append a line to ``data/artifacts/metadata_catalog_export_history.jsonl``
for a lightweight audit trail (one JSON object per export).

================================================================================
Versioning fields in the output JSON
================================================================================

  * ``catalog_spec_version`` — Format of this JSON file (bump if you rename keys).
  * ``catalog_version`` — Logical release of the catalog (semantic string).
        Set via ``--catalog-version`` or env ``METADATA_CATALOG_VERSION`` (default ``1.0.0``).
  * ``generated_at`` — UTC ISO-8601 timestamp when this file was produced.
  * ``structural_content_sha256`` — SHA-256 of a **canonical JSON** snapshot of schema
        structure (tables, columns without samples, FKs). Use this to detect whether
        two exports differ *structurally* even if ``generated_at`` differs.
  * ``git_commit_short`` — Best-effort current ``git rev-parse --short HEAD``.

Row counts and sample values **are excluded** from the structural hash so the hash
reflects DDL-shaped metadata, not data churn.

================================================================================
Usage
================================================================================

  python scripts/export_metadata_catalog.py
  python scripts/export_metadata_catalog.py --schema northwind --max-sample-values 5
  python scripts/export_metadata_catalog.py --catalog-version 1.1.0 --no-history

Environment:

  NEON_TEXT2SQL_URL       PostgreSQL connection URI (required).

Optional:

  METADATA_CATALOG_VERSION    Default semantic version string for ``catalog_version``.
  RECORD_CATALOG_HISTORY      If ``false``, skip appending export history (default: true).

See also: ``generate_connector_registry.py`` for the companion connector artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Increment when you change the JSON shape (keys / nesting) so consumers can migrate.
CATALOG_SPEC_VERSION = "1.1"

DEFAULT_SCHEMA = "northwind"
ARTIFACTS_DIR = Path("data") / "artifacts"
HISTORY_FILENAME = "metadata_catalog_export_history.jsonl"


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _try_git_sha() -> str | None:
    """Return short git SHA if running inside a git repo; else None."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=repo_root,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def parse_args() -> argparse.Namespace:
    """CLI: schema, sample limit, catalog version, history toggle."""
    p = argparse.ArgumentParser(
        description="Export metadata_catalog.<schema>.json for Step 1 RAG prep.",
    )
    p.add_argument("--schema", default=DEFAULT_SCHEMA, help="Postgres schema name.")
    p.add_argument(
        "--max-sample-values",
        type=int,
        default=5,
        help="Max DISTINCT sample strings per text-like column.",
    )
    p.add_argument(
        "--catalog-version",
        default=None,
        help="Semantic version for this catalog (e.g. 1.2.0). "
        "Overrides env METADATA_CATALOG_VERSION.",
    )
    p.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append a line to metadata_catalog_export_history.jsonl.",
    )
    return p.parse_args()


def get_db_url() -> str:
    """Load and validate ``NEON_TEXT2SQL_URL`` from .env / environment."""
    load_dotenv()
    db_url = os.getenv("NEON_TEXT2SQL_URL", "").strip()
    if not db_url:
        raise RuntimeError("Missing NEON_TEXT2SQL_URL in environment/.env")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        raise RuntimeError("NEON_TEXT2SQL_URL must be a Postgres URI")
    return db_url


def fetch_tables(cur: psycopg.Cursor[Any], schema: str) -> list[dict[str, Any]]:
    """List base tables in ``schema`` (information_schema)."""
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    return [{"table_name": r[0]} for r in cur.fetchall()]


def fetch_columns(cur: psycopg.Cursor[Any], schema: str) -> dict[str, list[dict[str, Any]]]:
    """Column metadata per table: name, data_type, nullability, ordinal."""
    cur.execute(
        """
        SELECT
          table_name,
          column_name,
          data_type,
          is_nullable,
          ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for table_name, column_name, data_type, is_nullable, ordinal in cur.fetchall():
        result.setdefault(table_name, []).append(
            {
                "column_name": column_name,
                "data_type": data_type,
                "is_nullable": is_nullable == "YES",
                "ordinal_position": ordinal,
            }
        )
    return result


def fetch_table_descriptions(cur: psycopg.Cursor[Any], schema: str) -> dict[str, str | None]:
    """
    Fetch **table-level** COMMENT ON ... IS from PostgreSQL system catalogs.

    Uses ``pg_catalog.obj_description(c.oid, 'pg_class')``.
    Northwind seed SQL often has no comments; keys may map to None — that is OK.
    """
    cur.execute(
        """
        SELECT
          c.relname AS table_name,
          pg_catalog.obj_description(c.oid, 'pg_class') AS description
        FROM pg_catalog.pg_class c
        INNER JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind = 'r'
        ORDER BY c.relname
        """,
        (schema,),
    )
    out: dict[str, str | None] = {}
    for table_name, desc in cur.fetchall():
        out[table_name] = desc
    return out


def fetch_column_descriptions(
    cur: psycopg.Cursor[Any], schema: str
) -> dict[str, dict[str, str | None]]:
    """
    Fetch **column-level** COMMENT ON COLUMN ... IS via ``col_description``.

    Returns nested dict: table_name -> column_name -> description or None.
    """
    cur.execute(
        """
        SELECT
          c.relname AS table_name,
          a.attname AS column_name,
          pg_catalog.col_description(a.attrelid, a.attnum) AS description
        FROM pg_catalog.pg_attribute a
        INNER JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
        INNER JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s
          AND c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
        """,
        (schema,),
    )
    out: dict[str, dict[str, str | None]] = {}
    for table_name, column_name, desc in cur.fetchall():
        out.setdefault(table_name, {})[column_name] = desc
    return out


def fetch_primary_keys(cur: psycopg.Cursor[Any], schema: str) -> dict[str, list[str]]:
    """Primary key columns per table (ordered)."""
    cur.execute(
        """
        SELECT
          tc.table_name,
          kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY tc.table_name, kcu.ordinal_position
        """,
        (schema,),
    )
    pks: dict[str, list[str]] = {}
    for table_name, column_name in cur.fetchall():
        pks.setdefault(table_name, []).append(column_name)
    return pks


def fetch_foreign_keys(cur: psycopg.Cursor[Any], schema: str) -> list[dict[str, Any]]:
    """Foreign keys where the constrained table lives in ``schema``."""
    cur.execute(
        """
        SELECT
          tc.table_name AS source_table,
          kcu.column_name AS source_column,
          ccu.table_name AS target_table,
          ccu.column_name AS target_column,
          tc.constraint_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s
        ORDER BY source_table, source_column
        """,
        (schema,),
    )
    return [
        {
            "source_table": row[0],
            "source_column": row[1],
            "target_table": row[2],
            "target_column": row[3],
            "constraint_name": row[4],
        }
        for row in cur.fetchall()
    ]


def fetch_row_count(cur: psycopg.Cursor[Any], schema: str, table: str) -> int:
    """Exact row count per table (may be slow on huge tables; OK for Northwind)."""
    query = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        sql.Identifier(schema), sql.Identifier(table)
    )
    cur.execute(query)
    return int(cur.fetchone()[0])


def fetch_sample_values(
    cur: psycopg.Cursor[Any],
    schema: str,
    table: str,
    column: str,
    max_values: int,
) -> list[str]:
    """
    Sample DISTINCT text representations for grounding literals in prompts.

    Limited to a small ``max_values`` to avoid dumping full columns (Step 1 sampling).
    """
    query = sql.SQL(
        """
        SELECT DISTINCT CAST({column} AS text)
        FROM {schema}.{table}
        WHERE {column} IS NOT NULL
        LIMIT {limit}
        """
    ).format(
        column=sql.Identifier(column),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        limit=sql.Literal(max_values),
    )
    cur.execute(query)
    return [r[0] for r in cur.fetchall()]


def build_structural_payload(
    schema_name: str,
    tables: list[dict[str, Any]],
    foreign_keys: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build a JSON-serializable object that captures **schema shape only**.

    Excludes: sample_values, row_count, generated_at, comments (comments are data
    about schema — we *include* descriptions in structural hash so COMMENT changes
    bump the hash; omit row_count and samples).

    Actually re-read user request: structural hash for "schema drift" - comments changing
    should invalidate - include descriptions in structural payload.

    Exclude: row_count, sample_values only.
    """
    # Strip volatile / data-volume fields from tables
    slim_tables = []
    for t in tables:
        cols = []
        for c in t.get("columns", []):
            cols.append(
                {
                    "column_name": c["column_name"],
                    "data_type": c["data_type"],
                    "is_nullable": c["is_nullable"],
                    "ordinal_position": c["ordinal_position"],
                    "description": c.get("description"),
                }
            )
        slim_tables.append(
            {
                "table_name": t["table_name"],
                "description": t.get("description"),
                "primary_key_columns": t.get("primary_key_columns"),
                "columns": cols,
            }
        )
    slim_tables.sort(key=lambda x: x["table_name"])
    fk_sorted = sorted(
        foreign_keys,
        key=lambda x: (
            x["source_table"],
            x["source_column"],
            x["constraint_name"],
        ),
    )
    return {
        "schema": schema_name,
        "tables": slim_tables,
        "foreign_keys": fk_sorted,
    }


def compute_structural_sha256(payload: dict[str, Any]) -> str:
    """Deterministic SHA-256 over canonical JSON (sorted keys, compact)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_export_history(
    artifacts_dir: Path,
    record: dict[str, Any],
) -> None:
    """Append one JSON line for audit / rollback traceability (Execution Template §7)."""
    path = artifacts_dir / HISTORY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def main() -> int:
    """Orchestrate introspection and write ``metadata_catalog.<schema>.json``."""
    args = parse_args()
    db_url = get_db_url()
    schema = args.schema
    max_values = args.max_sample_values

    catalog_version = (
        args.catalog_version
        or os.getenv("METADATA_CATALOG_VERSION", "").strip()
        or "1.0.0"
    )

    record_history = not args.no_history
    if os.getenv("RECORD_CATALOG_HISTORY", "").lower() in ("0", "false", "no"):
        record_history = False

    generated_at = _utc_now_iso()
    git_sha = _try_git_sha()

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            tables_list = fetch_tables(cur, schema)
            columns_by_table = fetch_columns(cur, schema)
            pks = fetch_primary_keys(cur, schema)
            fks = fetch_foreign_keys(cur, schema)
            table_desc_map = fetch_table_descriptions(cur, schema)
            column_desc_map = fetch_column_descriptions(cur, schema)

            catalog_tables: list[dict[str, Any]] = []
            for table_item in tables_list:
                table = table_item["table_name"]
                row_count = fetch_row_count(cur, schema, table)
                table_columns = columns_by_table.get(table, [])

                # Merge per-column descriptions from pg_catalog
                col_descriptions = column_desc_map.get(table, {})
                for col in table_columns:
                    col["description"] = col_descriptions.get(col["column_name"])

                    if col["data_type"] in {
                        "character varying",
                        "text",
                        "character",
                        "uuid",
                    }:
                        col["sample_values"] = fetch_sample_values(
                            cur, schema, table, col["column_name"], max_values
                        )
                    else:
                        col["sample_values"] = []

                catalog_tables.append(
                    {
                        "table_name": table,
                        "description": table_desc_map.get(table),
                        "row_count": row_count,
                        "primary_key_columns": pks.get(table, []),
                        "columns": table_columns,
                    }
                )

    # Structural hash (schema + comments + PK/FK shape; no samples / row counts)
    structural = build_structural_payload(schema, catalog_tables, fks)
    content_sha = compute_structural_sha256(structural)

    artifact: dict[str, Any] = {
        "catalog_spec_version": CATALOG_SPEC_VERSION,
        "catalog_version": catalog_version,
        "generated_at": generated_at,
        "git_commit_short": git_sha,
        "source": "neon",
        "database_kind": "postgresql",
        "schema": schema,
        "structural_content_sha256": content_sha,
        "tables": catalog_tables,
        "foreign_keys": fks,
        "export_notes": {
            "sample_values": (
                f"Up to {max_values} DISTINCT text casts per text-like column; "
                "not exhaustive domain enumeration."
            ),
            "comments": (
                "Table/column descriptions from PostgreSQL COMMENT ON; "
                "null means no comment in the database."
            ),
        },
    }

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARTIFACTS_DIR / f"metadata_catalog.{schema}.json"
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if record_history:
        append_export_history(
            ARTIFACTS_DIR,
            {
                "event": "metadata_catalog_export",
                "schema": schema,
                "catalog_version": catalog_version,
                "catalog_spec_version": CATALOG_SPEC_VERSION,
                "generated_at": generated_at,
                "structural_content_sha256": content_sha,
                "artifact_path": str(out_path),
                "git_commit_short": git_sha,
            },
        )

    print(f"Metadata catalog exported: {out_path}")
    print(f"  tables: {len(catalog_tables)}")
    print(f"  foreign_keys: {len(fks)}")
    print(f"  structural_content_sha256: {content_sha}")
    print(f"  catalog_version: {catalog_version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - user-facing CLI diagnostics
        print(f"Export failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
