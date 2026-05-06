#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Generate and optionally apply PostgreSQL COMMENT metadata for a schema.

Why this exists:
  - Your metadata catalog exporter reads PostgreSQL comments via obj_description()
    and col_description().
  - If tables/columns have no COMMENT ON metadata, descriptions appear as null.
  - This script fills that gap by generating practical descriptions that fit
    Text-to-SQL use cases, then writing/applying COMMENT statements.

Design goals:
  1) Be deterministic and transparent (rule-based, not black-box LLM output).
  2) Be safe by default: dry-run mode writes SQL without changing DB.
  3) Preserve existing curated comments unless --overwrite is specified.
  4) Be heavily documented so you can understand and modify behavior.

Usage examples:
  # Dry run: generate SQL only
  python scripts/generate_schema_descriptions.py --schema northwind

  # Apply comments into Neon (only where missing)
  python scripts/generate_schema_descriptions.py --schema northwind --apply

  # Force overwrite existing comments
  python scripts/generate_schema_descriptions.py --schema northwind --apply --overwrite
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate COMMENT ON statements from schema + data profile."
    )
    parser.add_argument("--schema", default="northwind", help="Target schema name.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute generated COMMENT ON statements against the database.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing comments. Default behavior only fills missing comments.",
    )
    parser.add_argument(
        "--output-sql",
        default=None,
        help="Optional output SQL path. Default: data/artifacts/schema_descriptions.<schema>.sql",
    )
    return parser.parse_args()


def get_db_url() -> str:
    load_dotenv()
    db_url = os.getenv("NEON_TEXT2SQL_URL", "").strip()
    if not db_url:
        raise RuntimeError("Missing NEON_TEXT2SQL_URL in environment/.env")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        raise RuntimeError("NEON_TEXT2SQL_URL must be a PostgreSQL URI")
    return db_url


def titleize(identifier: str) -> str:
    return identifier.replace("_", " ").strip().title()


def load_schema_metadata(
    cur: psycopg.Cursor[Any], schema: str
) -> tuple[list[str], dict[str, list[dict[str, Any]]], dict[str, list[str]], dict[tuple[str, str], tuple[str, str]], dict[str, str | None], dict[tuple[str, str], str | None]]:
    """Collect all metadata needed for comment generation.

    Returns:
      - table_names
      - columns_by_table
      - pk_by_table
      - fk_map[(table, column)] -> (target_table, target_column)
      - existing_table_comments
      - existing_column_comments[(table, column)] -> comment|None
    """
    # Base tables.
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
    tables = [r[0] for r in cur.fetchall()]

    # Column metadata.
    cur.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table_name, column_name, data_type, is_nullable, ordinal_position in cur.fetchall():
        columns_by_table[table_name].append(
            {
                "column_name": column_name,
                "data_type": data_type,
                "is_nullable": is_nullable == "YES",
                "ordinal_position": ordinal_position,
            }
        )

    # Primary keys.
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name
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
    pk_by_table: dict[str, list[str]] = defaultdict(list)
    for table_name, column_name in cur.fetchall():
        pk_by_table[table_name].append(column_name)

    # Foreign key map keyed by source table+column.
    cur.execute(
        """
        SELECT
          tc.table_name AS source_table,
          kcu.column_name AS source_column,
          ccu.table_name AS target_table,
          ccu.column_name AS target_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.table_schema = %s
          AND tc.constraint_type = 'FOREIGN KEY'
        """,
        (schema,),
    )
    fk_map: dict[tuple[str, str], tuple[str, str]] = {}
    for source_table, source_column, target_table, target_column in cur.fetchall():
        fk_map[(source_table, source_column)] = (target_table, target_column)

    # Existing table comments.
    cur.execute(
        """
        SELECT c.relname, pg_catalog.obj_description(c.oid, 'pg_class')
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind = 'r'
        ORDER BY c.relname
        """,
        (schema,),
    )
    existing_table_comments = {name: desc for name, desc in cur.fetchall()}

    # Existing column comments.
    cur.execute(
        """
        SELECT
          c.relname AS table_name,
          a.attname AS column_name,
          pg_catalog.col_description(a.attrelid, a.attnum) AS description
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
        JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s
          AND c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
        """,
        (schema,),
    )
    existing_column_comments = {
        (table_name, column_name): desc
        for table_name, column_name, desc in cur.fetchall()
    }

    return (
        tables,
        dict(columns_by_table),
        dict(pk_by_table),
        fk_map,
        existing_table_comments,
        existing_column_comments,
    )


def sample_values(
    cur: psycopg.Cursor[Any], schema: str, table: str, column: str, limit: int = 3
) -> list[str]:
    query = sql.SQL(
        """
        SELECT DISTINCT CAST({col} AS text)
        FROM {schema}.{table}
        WHERE {col} IS NOT NULL
        LIMIT {limit}
        """
    ).format(
        col=sql.Identifier(column),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        limit=sql.Literal(limit),
    )
    cur.execute(query)
    return [r[0] for r in cur.fetchall()]


def infer_table_description(table: str) -> str:
    """Return a concise domain-friendly description for a table."""
    curated = {
        "categories": "Product category lookup used to group products for reporting and filtering.",
        "customers": "Customer master records used for orders, segmentation, and geography analysis.",
        "customer_demographics": "Lookup of demographic categories that can be associated with customers.",
        "customer_customer_demo": "Bridge table linking customers to demographic categories.",
        "employees": "Employee master records for sales and order ownership reporting.",
        "employee_territories": "Bridge table linking employees to sales territories.",
        "orders": "Order header records containing customer, employee, shipper, and shipment details.",
        "order_details": "Order line items with product, quantity, unit price, and discount values.",
        "products": "Product catalog including supplier, category, pricing, and stock metadata.",
        "suppliers": "Supplier/vendor master records referenced by products.",
        "shippers": "Shipping carrier lookup referenced by orders.",
        "region": "Region lookup used to organize territories.",
        "territories": "Sales territory lookup linked to regions and employees.",
        "us_states": "Reference table of U.S. states with code and name.",
    }
    if table in curated:
        return curated[table]
    return f"Stores {titleize(table).lower()} data used in analytics and query generation."


def infer_column_description(
    *,
    table: str,
    column: str,
    data_type: str,
    is_nullable: bool,
    pk_columns: set[str],
    fk_target: tuple[str, str] | None,
    sample_hint: list[str],
) -> str:
    """Infer a practical column description from schema semantics + naming patterns."""
    if column in pk_columns:
        return f"Primary key that uniquely identifies each row in {table}."
    if fk_target:
        target_table, target_col = fk_target
        return (
            f"Foreign key referencing {target_table}.{target_col} to support joins."
        )

    # Northwind-specific high-value names.
    explicit = {
        "unit_price": "Monetary unit price used for line-level or product-level revenue calculations.",
        "quantity": "Item quantity used for volume and revenue calculations.",
        "discount": "Discount fraction applied at line level (for example 0.10 means 10%).",
        "order_date": "Date when the order was placed.",
        "required_date": "Date when the order is required by the customer.",
        "shipped_date": "Date when the order was shipped.",
        "freight": "Shipping freight charge associated with the order.",
        "company_name": "Business/company display name.",
        "contact_name": "Primary contact person name.",
        "contact_title": "Job title of the primary contact.",
        "phone": "Phone number text field.",
        "fax": "Fax number text field.",
        "postal_code": "Postal/ZIP code value.",
        "country": "Country value used for geography filters.",
        "region": "Region/state/province text value used for geography filters.",
        "city": "City value used for geography filters.",
        "address": "Street address text field.",
        "product_name": "Human-readable product name.",
        "category_name": "Human-readable category name.",
        "supplier_id": "Supplier identifier used to join products to suppliers.",
        "category_id": "Category identifier used to join products to categories.",
        "units_in_stock": "Current inventory units available in stock.",
        "units_on_order": "Units currently on purchase order.",
        "reorder_level": "Stock threshold that triggers product reorder.",
        "discontinued": "Product lifecycle flag (true/1 indicates discontinued).",
        "birth_date": "Employee date of birth.",
        "hire_date": "Employee hiring date.",
        "reports_to": "Employee identifier for the manager/supervisor relationship.",
        "territory_description": "Human-readable territory name/description.",
        "region_description": "Human-readable region name/description.",
        "state_name": "U.S. state full name.",
        "state_abbr": "U.S. state abbreviation code.",
        "ship_name": "Recipient or destination name for shipment.",
        "ship_address": "Shipment destination street address.",
        "ship_city": "Shipment destination city.",
        "ship_region": "Shipment destination region/state/province.",
        "ship_postal_code": "Shipment destination postal/ZIP code.",
        "ship_country": "Shipment destination country.",
    }
    if column in explicit:
        return explicit[column]

    # General-purpose naming heuristics (fallback path).
    if column.endswith("_id"):
        entity = column[: -len("_id")].replace("_", " ")
        return f"Identifier for {entity}; used for joins, filters, or grouping."
    if re.search(r"(name)$", column):
        return "Human-readable name field used for labels, grouping, and filters."
    if re.search(r"(date)$", column):
        return "Date value used in time-based filters and trend analysis."
    if re.search(r"(price|amount|cost|freight|total)$", column):
        return "Numeric monetary value used in financial calculations and aggregations."
    if re.search(r"(qty|quantity|units|count)$", column):
        return "Numeric count/quantity value used for volume metrics."
    if re.search(r"(phone|fax|email)$", column):
        return "Contact information field stored as text."
    if data_type in {"character varying", "text", "character"}:
        if sample_hint:
            return (
                f"Text field. Sample values include: {', '.join(sample_hint)}."
            )
        return "Text field used for filtering, grouping, or display."

    nullability = "nullable" if is_nullable else "required"
    return f"{titleize(column)} column ({data_type}, {nullability})."


def quote_comment_text(text: str) -> str:
    return text.replace("'", "''")


def build_comment_sql(
    *,
    schema: str,
    table_comments: dict[str, str],
    column_comments: dict[tuple[str, str], str],
) -> str:
    lines: list[str] = []
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines.append(f"-- Auto-generated schema descriptions for {schema}")
    lines.append(f"-- Generated at: {timestamp}")
    lines.append("")

    for table, comment in sorted(table_comments.items()):
        lines.append(
            f"COMMENT ON TABLE {schema}.{table} IS '{quote_comment_text(comment)}';"
        )
    if table_comments:
        lines.append("")

    for (table, column), comment in sorted(column_comments.items()):
        lines.append(
            f"COMMENT ON COLUMN {schema}.{table}.{column} IS '{quote_comment_text(comment)}';"
        )

    lines.append("")
    return "\n".join(lines)


def apply_comments(
    cur: psycopg.Cursor[Any],
    schema: str,
    table_comments: dict[str, str],
    column_comments: dict[tuple[str, str], str],
) -> None:
    # Apply table comments.
    for table, comment in table_comments.items():
        cur.execute(
            sql.SQL("COMMENT ON TABLE {}.{} IS {}").format(
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.Literal(comment),
            )
        )
    # Apply column comments.
    for (table, column), comment in column_comments.items():
        cur.execute(
            sql.SQL("COMMENT ON COLUMN {}.{}.{} IS {}").format(
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.Identifier(column),
                sql.Literal(comment),
            )
        )


def main() -> int:
    args = parse_args()
    schema = args.schema
    db_url = get_db_url()
    output_sql = (
        Path(args.output_sql)
        if args.output_sql
        else Path("data") / "artifacts" / f"schema_descriptions.{schema}.sql"
    )
    output_sql.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            (
                tables,
                columns_by_table,
                pk_by_table,
                fk_map,
                existing_table_comments,
                existing_column_comments,
            ) = load_schema_metadata(cur, schema)

            table_comments_to_set: dict[str, str] = {}
            column_comments_to_set: dict[tuple[str, str], str] = {}

            for table in tables:
                existing_table = existing_table_comments.get(table)
                if args.overwrite or not existing_table:
                    table_comments_to_set[table] = infer_table_description(table)

                pk_cols = set(pk_by_table.get(table, []))
                for col_meta in columns_by_table.get(table, []):
                    col = col_meta["column_name"]
                    existing_col = existing_column_comments.get((table, col))
                    if not args.overwrite and existing_col:
                        continue

                    data_type = col_meta["data_type"]
                    is_nullable = col_meta["is_nullable"]
                    fk_target = fk_map.get((table, col))
                    samples = []
                    if data_type in {"character varying", "text", "character"}:
                        samples = sample_values(cur, schema, table, col, limit=3)

                    column_comments_to_set[(table, col)] = infer_column_description(
                        table=table,
                        column=col,
                        data_type=data_type,
                        is_nullable=is_nullable,
                        pk_columns=pk_cols,
                        fk_target=fk_target,
                        sample_hint=samples,
                    )

            sql_text = build_comment_sql(
                schema=schema,
                table_comments=table_comments_to_set,
                column_comments=column_comments_to_set,
            )
            output_sql.write_text(sql_text, encoding="utf-8")

            if args.apply:
                apply_comments(
                    cur,
                    schema=schema,
                    table_comments=table_comments_to_set,
                    column_comments=column_comments_to_set,
                )

    print(f"Generated SQL: {output_sql}")
    print(f"Table comments prepared: {len(table_comments_to_set)}")
    print(f"Column comments prepared: {len(column_comments_to_set)}")
    print(f"Applied to database: {'yes' if args.apply else 'no (dry-run)'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"Description generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

