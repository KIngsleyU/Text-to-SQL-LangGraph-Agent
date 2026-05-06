#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Emit the Step 1 **semantic_terms** ontology artifact (Semantic Ontology Layer).

Aligned with ``system_design_docs/step1/Data and RAG Preparation Pipeline -
Execution Template.md`` §2:

  * Business terms mapped to deterministic technical meaning
  * Metric expressions, dimensions, default filters / time semantics where applicable
  * Table/column **lineage** grounded in ``metadata_catalog.<schema>.json``
  * Owner + definition_version on each term (governance-friendly)
  * Explicit **fallback** when a question is outside ontology coverage

The script loads the exported metadata catalog and **validates** that every lineage
reference points at a real table/column before writing:

  ``data/artifacts/semantic_terms.<schema>.json``

Usage::

    python scripts/generate_semantic_terms.py
    python scripts/generate_semantic_terms.py --schema northwind --catalog path/to/metadata_catalog.northwind.json
    python scripts/generate_semantic_terms.py --ontology-version 1.0.0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
SPEC_VERSION = "1.0"
DEFAULT_SCHEMA = "northwind"
REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "data" / "artifacts"

DEFAULT_FALLBACK = (
    "If the user's question cannot be grounded in ontology_term ids or aliases in "
    "`semantic_terms`, resolve intent using schema+RAG retrieval from the "
    "metadata_catalog and value retrieval; do not invent business definitions."
)

# Curated Northwind ontology: terms are validated against metadata_catalog columns.
NORTHWIND_TERMS: list[dict[str, Any]] = [
    # --- Metrics (order-line grain unless noted) ---
    {
        "id": "metric.line_extended_amount",
        "kind": "metric",
        "canonical_label": "Line extended amount",
        "aliases": [
            "line revenue",
            "order line total",
            "extended line price",
            "line sales",
            "revenue",
        ],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": (
            "Revenue for each order line after line-level discount: "
            "quantity × unit_price × (1 − discount). Sum for totals."
        ),
        "postgres_aggregate_sql": (
            "SUM(od.quantity::numeric * od.unit_price::numeric "
            "* (1::numeric - od.discount::numeric))"
        ),
        "default_grain": "order_line",
        "primary_fact_table": "order_details",
        "lineage": [
            {"table": "order_details", "column": "quantity", "role": "measure"},
            {"table": "order_details", "column": "unit_price", "role": "measure"},
            {"table": "order_details", "column": "discount", "role": "measure"},
        ],
        "agent_join_notes": (
            "From northwind.order_details od. Join northwind.orders o ON o.order_id = od.order_id "
            "for order_date or customer dimensions."
        ),
    },
    {
        "id": "metric.order_line_count",
        "kind": "metric",
        "canonical_label": "Order line count",
        "aliases": ["line items", "number of order lines", "detail rows"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Count of rows in order_details (one per product line on an order).",
        "postgres_aggregate_sql": "COUNT(*)",
        "default_grain": "order_line",
        "primary_fact_table": "order_details",
        "lineage": [{"table": "order_details", "column": "order_id", "role": "fact_key"}],
        "agent_join_notes": "COUNT(*) over northwind.order_details with optional filters.",
    },
    {
        "id": "metric.distinct_order_count",
        "kind": "metric",
        "canonical_label": "Distinct order count",
        "aliases": ["order count", "number of orders", "orders placed"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Count of distinct orders (order headers).",
        "postgres_aggregate_sql": "COUNT(DISTINCT o.order_id)",
        "default_grain": "order",
        "primary_fact_table": "orders",
        "lineage": [{"table": "orders", "column": "order_id", "role": "primary_key"}],
        "agent_join_notes": "Use northwind.orders o; COUNT(DISTINCT o.order_id) with optional date filters on o.order_date.",
    },
    {
        "id": "metric.distinct_customer_ordering",
        "kind": "metric",
        "canonical_label": "Customers placing orders",
        "aliases": [
            "active customers",
            "customers with orders",
            "distinct ordering customers",
        ],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": (
            "Count of distinct customer_id on orders where customer_id is not null. "
            "For time-bounded 'active', filter o.order_date in the requested window."
        ),
        "postgres_aggregate_sql": "COUNT(DISTINCT o.customer_id)",
        "default_grain": "customer",
        "primary_fact_table": "orders",
        "lineage": [
            {"table": "orders", "column": "customer_id", "role": "foreign_key"},
            {"table": "orders", "column": "order_date", "role": "optional_time_filter"},
        ],
        "agent_join_notes": (
            "Filter o.customer_id IS NOT NULL. For rolling activity windows, constrain o.order_date."
        ),
    },
    {
        "id": "metric.total_freight",
        "kind": "metric",
        "canonical_label": "Total freight charges",
        "aliases": ["freight cost", "shipping freight", "sum of freight"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Sum of freight from order headers (shipment-level charge, not duplicated per line).",
        "postgres_aggregate_sql": "SUM(o.freight::numeric)",
        "default_grain": "order",
        "primary_fact_table": "orders",
        "lineage": [{"table": "orders", "column": "freight", "role": "measure"}],
        "agent_join_notes": "Avoid double-counting: aggregate at orders grain, do not SUM freight across duplicated line joins.",
    },
    {
        "id": "metric.units_ordered",
        "kind": "metric",
        "canonical_label": "Units sold (detail quantity)",
        "aliases": ["units sold", "quantity sold", "total quantity"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Sum of order_details.quantity across selected lines.",
        "postgres_aggregate_sql": "SUM(od.quantity::numeric)",
        "default_grain": "order_line",
        "primary_fact_table": "order_details",
        "lineage": [{"table": "order_details", "column": "quantity", "role": "measure"}],
        "agent_join_notes": "From northwind.order_details od.",
    },
    {
        "id": "metric.avg_line_discount_fraction",
        "kind": "metric",
        "canonical_label": "Average line discount fraction",
        "aliases": ["average discount", "mean line discount"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Average of order_details.discount (0.10 = 10%).",
        "postgres_aggregate_sql": "AVG(od.discount::numeric)",
        "default_grain": "order_line",
        "primary_fact_table": "order_details",
        "lineage": [{"table": "order_details", "column": "discount", "role": "measure"}],
        "agent_join_notes": None,
    },
    {
        "id": "metric.inventory_units_in_stock",
        "kind": "metric",
        "canonical_label": "Inventory units on hand",
        "aliases": ["stock units", "units in stock"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Sum of products.units_in_stock (warehouse snapshot in Northwind demo).",
        "postgres_aggregate_sql": "SUM(p.units_in_stock::numeric)",
        "default_grain": "product",
        "primary_fact_table": "products",
        "lineage": [{"table": "products", "column": "units_in_stock", "role": "measure"}],
        "agent_join_notes": "Grain is product catalog, not shipments. Often paired with filter_non_discontinued_product.",
    },
    {
        "id": "metric.inventory_at_catalog_unit_price",
        "kind": "metric",
        "canonical_label": "Inventory value at list unit price",
        "aliases": ["stock value at list price", "inventory list value"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Sum over products of units_in_stock × unit_price (simplified valuation).",
        "postgres_aggregate_sql": "SUM(p.units_in_stock::numeric * p.unit_price::numeric)",
        "default_grain": "product",
        "primary_fact_table": "products",
        "lineage": [
            {"table": "products", "column": "units_in_stock", "role": "measure"},
            {"table": "products", "column": "unit_price", "role": "measure"},
        ],
        "agent_join_notes": "Northwind snapshot; excludes cost basis from suppliers.",
    },
    {
        "id": "metric.employee_headcount",
        "kind": "metric",
        "canonical_label": "Employee headcount",
        "aliases": ["number of employees", "staff count"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Count of employee rows.",
        "postgres_aggregate_sql": "COUNT(*)::bigint",
        "default_grain": "employee",
        "primary_fact_table": "employees",
        "lineage": [{"table": "employees", "column": "employee_id", "role": "primary_key"}],
        "agent_join_notes": "Simple COUNT(*) FROM northwind.employees with optional filters (title, hire_date).",
    },
    # --- Dimensions (SQL fragments use common alias hints) ---
    {
        "id": "dimension.order_date",
        "kind": "dimension",
        "canonical_label": "Order date",
        "aliases": ["order day", "placed on", "order_time"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Date the order was placed (orders.order_date).",
        "postgres_expression": "o.order_date",
        "default_grain": "order",
        "lineage": [{"table": "orders", "column": "order_date", "role": "dimension"}],
        "agent_join_notes": "Alias orders as o.",
    },
    {
        "id": "dimension.required_date",
        "kind": "dimension",
        "canonical_label": "Required ship date",
        "aliases": ["due date", "customer required date"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "orders.required_date",
        "postgres_expression": "o.required_date",
        "default_grain": "order",
        "lineage": [{"table": "orders", "column": "required_date", "role": "dimension"}],
        "agent_join_notes": "Alias orders as o.",
    },
    {
        "id": "dimension.shipped_date",
        "kind": "dimension",
        "canonical_label": "Shipped date",
        "aliases": ["ship date", "date shipped"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "orders.shipped_date (may be null if not shipped).",
        "postgres_expression": "o.shipped_date",
        "default_grain": "order",
        "lineage": [{"table": "orders", "column": "shipped_date", "role": "dimension"}],
        "agent_join_notes": "Alias orders as o.",
    },
    {
        "id": "dimension.customer_country",
        "kind": "dimension",
        "canonical_label": "Customer country",
        "aliases": ["customer nation", "buyer country"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Country on the customer record (billing / master address).",
        "postgres_expression": "c.country",
        "default_grain": "customer",
        "lineage": [{"table": "customers", "column": "country", "role": "dimension"}],
        "agent_join_notes": "Join orders o to customers c on c.customer_id = o.customer_id.",
    },
    {
        "id": "dimension.ship_country",
        "kind": "dimension",
        "canonical_label": "Ship-to country",
        "aliases": ["destination country", "shipment country"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "orders.ship_country (destination).",
        "postgres_expression": "o.ship_country",
        "default_grain": "order",
        "lineage": [{"table": "orders", "column": "ship_country", "role": "dimension"}],
        "agent_join_notes": "Distinct from customer country.",
    },
    {
        "id": "dimension.customer_company_name",
        "kind": "dimension",
        "canonical_label": "Customer company",
        "aliases": ["customer name", "account name"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "customers.company_name",
        "postgres_expression": "c.company_name",
        "default_grain": "customer",
        "lineage": [{"table": "customers", "column": "company_name", "role": "dimension"}],
        "agent_join_notes": "Join customers c to orders.",
    },
    {
        "id": "dimension.product_name",
        "kind": "dimension",
        "canonical_label": "Product name",
        "aliases": ["sku name", "item name"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "products.product_name",
        "postgres_expression": "p.product_name",
        "default_grain": "product",
        "lineage": [{"table": "products", "column": "product_name", "role": "dimension"}],
        "agent_join_notes": "Join order_details od to products p on p.product_id = od.product_id.",
    },
    {
        "id": "dimension.category_name",
        "kind": "dimension",
        "canonical_label": "Product category",
        "aliases": ["category"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "categories.category_name",
        "postgres_expression": "cat.category_name",
        "default_grain": "product",
        "lineage": [{"table": "categories", "column": "category_name", "role": "dimension"}],
        "agent_join_notes": "Join products p to categories cat on cat.category_id = p.category_id.",
    },
    {
        "id": "dimension.supplier_company_name",
        "kind": "dimension",
        "canonical_label": "Supplier company",
        "aliases": ["vendor", "supplier name"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "suppliers.company_name",
        "postgres_expression": "s.company_name",
        "default_grain": "supplier",
        "lineage": [{"table": "suppliers", "column": "company_name", "role": "dimension"}],
        "agent_join_notes": "Join products p to suppliers s on s.supplier_id = p.supplier_id.",
    },
    {
        "id": "dimension.shipper_company_name",
        "kind": "dimension",
        "canonical_label": "Shipper / carrier",
        "aliases": ["carrier", "shipping company"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "shippers.company_name",
        "postgres_expression": "sh.company_name",
        "default_grain": "order",
        "lineage": [{"table": "shippers", "column": "company_name", "role": "dimension"}],
        "agent_join_notes": "Join orders o to shippers sh on sh.shipper_id = o.ship_via.",
    },
    {
        "id": "dimension.employee_display_name",
        "kind": "dimension",
        "canonical_label": "Employee name",
        "aliases": ["sales rep name", "owner employee"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Concatenate first_name and last_name for display.",
        "postgres_expression": "TRIM(CONCAT_WS(' ', e.first_name, e.last_name))",
        "default_grain": "employee",
        "lineage": [
            {"table": "employees", "column": "first_name", "role": "dimension"},
            {"table": "employees", "column": "last_name", "role": "dimension"},
        ],
        "agent_join_notes": "Join orders o to employees e on e.employee_id = o.employee_id.",
    },
    {
        "id": "dimension.territory_description",
        "kind": "dimension",
        "canonical_label": "Sales territory",
        "aliases": ["territory"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Territory label from territories.territory_description.",
        "postgres_expression": "t.territory_description",
        "default_grain": "employee_territory",
        "lineage": [{"table": "territories", "column": "territory_description", "role": "dimension"}],
        "agent_join_notes": (
            "Join employees → employee_territories → territories. Avoid duplicating revenue without careful grain handling."
        ),
    },
    {
        "id": "dimension.company_sales_region",
        "kind": "dimension",
        "canonical_label": "Macro sales region",
        "aliases": ["region Eastern", "corporate region"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "region.region_description (Eastern/Western/Northern/Southern).",
        "postgres_expression": "reg.region_description",
        "default_grain": "territory",
        "lineage": [{"table": "region", "column": "region_description", "role": "dimension"}],
        "agent_join_notes": "Join territories t to region reg on reg.region_id = t.region_id.",
    },
    # --- Entity identifiers ---
    {
        "id": "entity.customer_id",
        "kind": "entity",
        "canonical_label": "Customer identifier",
        "aliases": ["customer key", "customer code"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Primary key customers.customer_id (text code in Northwind).",
        "postgres_expression": "c.customer_id",
        "lineage": [{"table": "customers", "column": "customer_id", "role": "primary_key"}],
        "agent_join_notes": None,
    },
    {
        "id": "entity.order_id",
        "kind": "entity",
        "canonical_label": "Order identifier",
        "aliases": ["order key", "order number"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "orders.order_id surrogate key.",
        "postgres_expression": "o.order_id",
        "lineage": [{"table": "orders", "column": "order_id", "role": "primary_key"}],
        "agent_join_notes": None,
    },
    {
        "id": "entity.product_id",
        "kind": "entity",
        "canonical_label": "Product identifier",
        "aliases": ["product key", "SKU id"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "products.product_id.",
        "postgres_expression": "p.product_id",
        "lineage": [{"table": "products", "column": "product_id", "role": "primary_key"}],
        "agent_join_notes": None,
    },
    {
        "id": "entity.employee_id",
        "kind": "entity",
        "canonical_label": "Employee identifier",
        "aliases": ["employee key", "sales rep id"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "employees.employee_id.",
        "postgres_expression": "e.employee_id",
        "lineage": [{"table": "employees", "column": "employee_id", "role": "primary_key"}],
        "agent_join_notes": None,
    },
    # --- Filter presets (deterministic predicates) ---
    {
        "id": "filter.non_discontinued_products",
        "kind": "filter_preset",
        "canonical_label": "Exclude discontinued catalog products",
        "aliases": ["active products only", "not discontinued"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Northwind uses products.discontinued = 0 for in-catalog items.",
        "postgres_predicate": "COALESCE(p.discontinued, 0) = 0",
        "lineage": [{"table": "products", "column": "discontinued", "role": "constraint"}],
        "agent_join_notes": "Requires products aliased as p.",
    },
    {
        "id": "filter.shipped_orders_only",
        "kind": "filter_preset",
        "canonical_label": "Shipped orders only",
        "aliases": ["orders that shipped", "has ship date"],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "orders.shipped_date IS NOT NULL.",
        "postgres_predicate": "o.shipped_date IS NOT NULL",
        "lineage": [{"table": "orders", "column": "shipped_date", "role": "constraint"}],
        "agent_join_notes": "Alias orders as o.",
    },
    {
        "id": "filter.customers_who_have_ordered",
        "kind": "filter_preset",
        "canonical_label": "Customers with at least one order",
        "aliases": [],
        "owner": "step1_seed",
        "definition_version": "1.0.0",
        "description": "Customer appears on at least one order (customer_id not null).",
        "postgres_predicate": "EXISTS (SELECT 1 FROM northwind.orders o2 WHERE o2.customer_id = c.customer_id)",
        "lineage": [
            {"table": "customers", "column": "customer_id", "role": "primary_key"},
            {"table": "orders", "column": "customer_id", "role": "foreign_key"},
        ],
        "agent_join_notes": "Use on customers c; correlate with EXISTS to avoid losing customers without orders in inner joins.",
    },
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _catalog_column_map(catalog: dict[str, Any]) -> dict[str, set[str]]:
    tables = catalog.get("tables") or []
    out: dict[str, set[str]] = {}
    for t in tables:
        name = t.get("table_name")
        cols = {c["column_name"] for c in t.get("columns", []) if "column_name" in c}
        if name:
            out[str(name)] = cols
    return out


def _validate_terms(column_map: dict[str, set[str]], terms: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    for term in terms:
        tid = term.get("id", "?")
        for link in term.get("lineage") or []:
            tbl = link.get("table")
            col = link.get("column")
            if not tbl or not col:
                errors.append(f"{tid}: lineage entry missing table/column: {link!r}")
                continue
            if tbl not in column_map:
                errors.append(f"{tid}: unknown table {tbl!r}")
                continue
            if col not in column_map[tbl]:
                errors.append(f"{tid}: unknown column {tbl}.{col!r}")
    if errors:
        raise SystemExit("Catalog validation failed:\n" + "\n".join(errors))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate semantic_terms.<schema>.json for Step 1.")
    p.add_argument("--schema", default=DEFAULT_SCHEMA, help="Postgres schema name (artifact binding).")
    p.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Path to metadata_catalog.<schema>.json (default: data/artifacts/).",
    )
    p.add_argument(
        "--ontology-version",
        default="1.0.0",
        help="Semantic ontology release string (definition bundle version).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: data/artifacts/semantic_terms.<schema>.json).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    schema = args.schema
    catalog_path = args.catalog or (ARTIFACTS_DIR / f"metadata_catalog.{schema}.json")
    if not catalog_path.is_file():
        print(f"Catalog not found: {catalog_path}", file=sys.stderr)
        sys.exit(1)

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema") != schema:
        print(
            f"Warning: catalog schema {catalog.get('schema')!r} != --schema {schema!r}",
            file=sys.stderr,
        )

    column_map = _catalog_column_map(catalog)
    _validate_terms(column_map, NORTHWIND_TERMS)

    fk_rows = catalog.get("foreign_keys") or []
    join_paths: list[dict[str, Any]] = []
    for fk in fk_rows:
        join_paths.append(
            {
                "from_table": fk.get("source_table"),
                "from_column": fk.get("source_column"),
                "to_table": fk.get("target_table"),
                "to_column": fk.get("target_column"),
                "constraint": fk.get("constraint_name"),
            }
        )

    doc: dict[str, Any] = {
        "semantic_terms_spec_version": SPEC_VERSION,
        "ontology_version": args.ontology_version,
        "generated_at": _utc_now_iso(),
        "database_kind": catalog.get("database_kind") or "postgresql",
        "schema": schema,
        "catalog_binding": {
            "metadata_catalog_file": catalog_path.name,
            "catalog_spec_version": catalog.get("catalog_spec_version"),
            "catalog_version": catalog.get("catalog_version"),
            "structural_content_sha256": catalog.get("structural_content_sha256"),
            "catalog_generated_at": catalog.get("generated_at"),
        },
        "fallback_policy": DEFAULT_FALLBACK,
        "foreign_key_paths": join_paths,
        "terms": NORTHWIND_TERMS,
        "export_notes": {
            "term_count": len(NORTHWIND_TERMS),
            "alignment": (
                "Execution Template §2: business terms → metric/dimension lineage; "
                "metrics use postgres_aggregate_sql; dimensions/filters use postgres_expression or postgres_predicate."
            ),
        },
    }

    out_path = args.output or (ARTIFACTS_DIR / f"semantic_terms.{schema}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(NORTHWIND_TERMS)} terms)")


if __name__ == "__main__":
    main()
