#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Build the Step 1 **schema_vector_index** in Pinecone from ``metadata_catalog``.

Aligned with ``system_design_docs/step1`` (Execution Template §3, Modeling Notes):

  * Chunk enriched schema (tables + optional per-column units) from
    ``data/artifacts/metadata_catalog.<schema>.json``.
  * Upsert into a Pinecone **integrated** index (hosted embeddings) using the
    ``content`` text field (``--field_map text=content`` at index creation).
  * Attach **flat** metadata for coarse filtering (schema, table, chunk kind).
  * Emit ``data/artifacts/schema_vector_index_manifest.<schema>.json`` binding
    the index to ``structural_content_sha256`` (same contract as ``semantic_terms``
    ``catalog_binding``).

Pinecone agent rules (``.agents/PINECONE-python.md``):

  * Every upsert targets an explicit **namespace**.
  * Text upsert batches ≤ 96 records; metadata must be **flat** (string / number / bool / string lists only).
  * After upserts, wait **≥10s** before optional smoke search (MANDATORY for consistent reads).

**Index setup** (one-time; CLI per PINECONE guide)::

    pc index create -n YOUR_INDEX -m cosine -c aws -r us-east-1 \\
      --model llama-text-embed-v2 --field_map text=content

Environment::

    PINECONE_API_KEY   Required unless ``--dry-run``.
    PINECONE_INDEX     Target index name (default: ``text2sql-schema``).

Usage::

    python scripts/build_schema_index.py --dry-run
    python scripts/build_schema_index.py --schema northwind
    python scripts/build_schema_index.py --schema northwind --wipe-namespace
    python scripts/build_schema_index.py --schema northwind --smoke-query "order revenue by customer"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(_path: Path | None = None) -> None:
        return

# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "data" / "artifacts"
CONNECTOR_REGISTRY = ARTIFACTS_DIR / "connector_registry.json"

DEFAULT_SCHEMA = "northwind"
MANIFEST_SPEC_VERSION = "1.0"
TEXT_FIELD = "content"
INTEGRATED_MODEL_DEFAULT = "llama-text-embed-v2"
PINECONE_TEXT_BATCH = 96
POST_UPSERT_SLEEP_SEC = 10

# Pinecone record _id: letters, numbers, dash, underscore (stay conservative).
_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# pragma: no cover
# pylint: disable=unused-argument
def _try_git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=REPO_ROOT,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _safe_record_id(raw: str) -> str:
    s = _ID_SAFE.sub("_", raw)
    if len(s) > 512:
        s = s[:512]
    return s


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_source_id(catalog_schema: str) -> str:
    """Pick connector ``sources[].id`` whose allowed_schemas contains ``catalog_schema``."""
    if not CONNECTOR_REGISTRY.is_file():
        return "unknown"
    doc = _load_json(CONNECTOR_REGISTRY)
    for src in doc.get("sources", []):
        allowed = (src.get("routing") or {}).get("allowed_schemas") or []
        if catalog_schema in allowed:
            return str(src.get("id", "unknown"))
    if doc.get("sources"):
        return str(doc["sources"][0].get("id", "unknown"))
    return "unknown"


def _fks_for_table(table_name: str, foreign_keys: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for fk in foreign_keys:
        st, sc = fk["source_table"], fk["source_column"]
        tt, tc = fk["target_table"], fk["target_column"]
        if st == table_name:
            lines.append(f"Outgoing FK: {sc} -> {tt}.{tc}")
        if tt == table_name:
            lines.append(f"Incoming FK: {st}.{sc} -> {tc}")
    return lines


def _format_sample_values(values: list[Any], max_chars: int = 400) -> str:
    if not values:
        return ""
    parts: list[str] = []
    for v in values[:12]:
        parts.append(str(v).replace("\n", " "))
    text = "; ".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def _column_brief(col: dict[str, Any]) -> str:
    parts = [
        f"{col['column_name']} ({col['data_type']})",
        "NULL" if col.get("is_nullable") else "NOT NULL",
    ]
    desc = col.get("description")
    if desc:
        parts.append(str(desc))
    sv = _format_sample_values(col.get("sample_values") or [])
    if sv:
        parts.append(f"Examples: {sv}")
    return " | ".join(parts)


def iter_schema_records(
    catalog: dict[str, Any],
    *,
    source_id: str,
    include_table_chunks: bool,
    include_column_chunks: bool,
) -> Iterator[dict[str, Any]]:
    """Yield Pinecone-ready record dicts (_id + content + flat metadata)."""
    schema = str(catalog.get("schema", ""))
    db_kind = str(catalog.get("database_kind", "postgresql"))
    catalog_source = str(catalog.get("source", ""))
    tables: list[dict[str, Any]] = catalog.get("tables") or []
    foreign_keys: list[dict[str, Any]] = catalog.get("foreign_keys") or []

    for table in tables:
        tname = str(table["table_name"])
        tdesc = table.get("description") or ""
        pk = table.get("primary_key_columns") or []
        cols: list[dict[str, Any]] = table.get("columns") or []
        rc = table.get("row_count")
        fk_lines = _fks_for_table(tname, foreign_keys)

        if include_table_chunks:
            col_block = "\n".join(f"  - {_column_brief(c)}" for c in cols)
            fk_block = "\n".join(f"  - {line}" for line in fk_lines) if fk_lines else "  (none listed)"
            text = (
                f"Database schema: {schema}\n"
                f"Table: {tname}\n"
                f"Row estimate: {rc}\n"
                f"Primary key columns: {', '.join(pk) if pk else '(none)'}\n"
                f"Table description: {tdesc}\n"
                f"Columns:\n{col_block}\n"
                f"Foreign keys touching this table:\n{fk_block}\n"
            )
            rid = _safe_record_id(f"{schema}__{tname}__table")
            yield {
                "_id": rid,
                TEXT_FIELD: text,
                "schema_name": schema,
                "table_name": tname,
                "chunk_kind": "table",
                "column_name": "",
                "source_id": source_id,
                "database_kind": db_kind,
                "catalog_source": catalog_source,
            }

        if include_column_chunks:
            for col in cols:
                cname = str(col["column_name"])
                text = (
                    f"Schema {schema} table {tname} column {cname}.\n"
                    f"{_column_brief(col)}\n"
                    f"Table context: {tdesc}\n"
                    f"Primary key: {', '.join(pk) if pk else '(none)'}\n"
                )
                rid = _safe_record_id(f"{schema}__{tname}__col__{cname}")
                yield {
                    "_id": rid,
                    TEXT_FIELD: text,
                    "schema_name": schema,
                    "table_name": tname,
                    "chunk_kind": "column",
                    "column_name": cname,
                    "source_id": source_id,
                    "database_kind": db_kind,
                    "catalog_source": catalog_source,
                }


def _batched(records: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(records), size):
        yield records[i : i + size]


def _upsert_with_backoff(index: Any, namespace: str, batch: list[dict[str, Any]]) -> None:
    from pinecone.exceptions import PineconeException

    delay = 1.0
    for attempt in range(6):
        try:
            index.upsert_records(namespace, batch)
            return
        except PineconeException as e:
            status = getattr(e, "status", None) or getattr(e, "http_status", None)
            if status in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 30)
            else:
                raise


def _smoke_search(
    index: Any,
    namespace: str,
    query_text: str,
    *,
    schema_filter: str,
    top_k: int = 5,
) -> None:
    flt: dict[str, Any] = {"schema_name": {"$eq": schema_filter}}
    q: dict[str, Any] = {
        "top_k": top_k,
        "inputs": {"text": query_text},
        "filter": flt,
    }
    resp = index.search(namespace=namespace, query=q)
    hits = []
    if hasattr(resp, "result") and resp.result and hasattr(resp.result, "hits"):
        hits = list(resp.result.hits)
    elif isinstance(resp, dict):
        hits = list((resp.get("result") or {}).get("hits") or [])
    print(f"\nSmoke search ({top_k} hits, filter schema_name={schema_filter!r}):")
    for h in hits:
        if isinstance(h, dict):
            hid = h.get("_id")
            score = h.get("_score")
            fields = h.get("fields") or {}
        else:
            hid = getattr(h, "_id", None)
            score = getattr(h, "_score", None)
            fields = getattr(h, "fields", None) or {}
        if hasattr(fields, "get"):
            preview = (fields.get(TEXT_FIELD) or "")[:160]
        else:
            preview = str(fields)[:160]
        print(f"  id={hid} score={score} preview={preview!r}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build Pinecone schema_vector_index from metadata_catalog (Step 1).",
    )
    p.add_argument("--schema", default=DEFAULT_SCHEMA, help="Catalog schema name / namespace key.")
    p.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Path to metadata_catalog JSON (default: data/artifacts/metadata_catalog.<schema>.json).",
    )
    p.add_argument(
        "--namespace",
        default=None,
        help="Pinecone namespace (default: schema_<schema>).",
    )
    p.add_argument(
        "--index-name",
        default=None,
        help="Pinecone index name (default: env PINECONE_INDEX or 'text2sql-schema').",
    )
    p.add_argument("--no-table-chunks", action="store_true", help="Skip table-level chunks.")
    p.add_argument("--no-column-chunks", action="store_true", help="Skip column-level chunks.")
    p.add_argument(
        "--wipe-namespace",
        action="store_true",
        help="Delete all vectors in the namespace before upserting.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Load catalog and print chunk counts only; no Pinecone I/O.",
    )
    p.add_argument(
        "--smoke-query",
        default=None,
        metavar="TEXT",
        help="After upsert + wait, run one filtered search (requires Pinecone).",
    )
    p.add_argument(
        "--skip-post-wait",
        action="store_true",
        help="Skip the 10s post-upsert sleep (not recommended; for tests only).",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = parse_args()
    schema = args.schema
    catalog_path = args.catalog or (ARTIFACTS_DIR / f"metadata_catalog.{schema}.json")
    if not catalog_path.is_file():
        print(f"ERROR: metadata catalog not found: {catalog_path}", file=sys.stderr)
        return 1

    catalog = _load_json(catalog_path)
    source_id = resolve_source_id(schema)
    include_table = not args.no_table_chunks
    include_column = not args.no_column_chunks
    if not include_table and not include_column:
        print("ERROR: need at least one of table or column chunks.", file=sys.stderr)
        return 1

    records = list(
        iter_schema_records(
            catalog,
            source_id=source_id,
            include_table_chunks=include_table,
            include_column_chunks=include_column,
        )
    )
    n_table = sum(1 for r in records if r.get("chunk_kind") == "table")
    n_col = sum(1 for r in records if r.get("chunk_kind") == "column")
    print(f"Prepared {len(records)} records (table={n_table}, column={n_col}) from {catalog_path.name}")

    if args.dry_run:
        print("Dry run: no Pinecone upsert.")
        return 0

    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: PINECONE_API_KEY not set (or use --dry-run).", file=sys.stderr)
        return 1

    index_name = args.index_name or os.getenv("PINECONE_INDEX") or "text2sql-schema"
    namespace = args.namespace or f"schema_{schema}"

    from pinecone import Pinecone

    pc = Pinecone(api_key=api_key)
    if not pc.has_index(index_name):
        print(
            f"ERROR: Pinecone index {index_name!r} does not exist.\n"
            f"Create it first, e.g.:\n"
            f'  pc index create -n {index_name} -m cosine -c aws -r us-east-1 \\\n'
            f"    --model {INTEGRATED_MODEL_DEFAULT} --field_map text={TEXT_FIELD}",
            file=sys.stderr,
        )
        return 1

    index = pc.Index(index_name)

    if args.wipe_namespace:
        print(f"Wiping namespace {namespace!r} on index {index_name!r} ...")
        index.delete(namespace=namespace, delete_all=True)
        time.sleep(2)

    print(f"Upserting to index={index_name!r} namespace={namespace!r} ...")
    total = 0
    for batch in _batched(records, PINECONE_TEXT_BATCH):
        _upsert_with_backoff(index, namespace, batch)
        total += len(batch)
        print(f"  upserted {total}/{len(records)}")

    if not args.skip_post_wait:
        print(f"Waiting {POST_UPSERT_SLEEP_SEC}s for vectors to be queryable (Pinecone guide) ...")
        time.sleep(POST_UPSERT_SLEEP_SEC)

    stats = index.describe_index_stats()
    print(f"describe_index_stats: {stats}")

    structural_hash = str(catalog.get("structural_content_sha256", ""))
    manifest: dict[str, Any] = {
        "schema_vector_index_manifest_spec_version": MANIFEST_SPEC_VERSION,
        "generated_at": _utc_now_iso(),
        "git_commit_short": _try_git_sha(),
        "pinecone_index_name": index_name,
        "pinecone_namespace": namespace,
        "text_field": TEXT_FIELD,
        "integrated_embedding_model": os.getenv("PINECONE_EMBEDDING_MODEL", INTEGRATED_MODEL_DEFAULT),
        "metric": "cosine",
        "chunking": {
            "table_chunks": include_table,
            "column_chunks": include_column,
        },
        "record_count": len(records),
        "chunk_counts": {"table": n_table, "column": n_col},
        "source_id": source_id,
        "catalog_binding": {
            "metadata_catalog_file": catalog_path.name,
            "catalog_spec_version": str(catalog.get("catalog_spec_version", "")),
            "catalog_version": str(catalog.get("catalog_version", "")),
            "structural_content_sha256": structural_hash,
            "catalog_generated_at": str(catalog.get("generated_at", "")),
        },
    }
    out_manifest = ARTIFACTS_DIR / f"schema_vector_index_manifest.{schema}.json"
    out_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_manifest.relative_to(REPO_ROOT)}")

    if args.smoke_query:
        _smoke_search(index, namespace, args.smoke_query, schema_filter=schema)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
