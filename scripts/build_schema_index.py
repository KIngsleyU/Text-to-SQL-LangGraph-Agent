#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Build the Step 1 **schema_vector_index** in Pinecone from ``metadata_catalog``.

Aligned with ``system_design_docs/step1`` (Execution Template §3, Modeling Notes):

  * Chunk enriched schema (tables + optional per-column units) from
    ``data/artifacts/metadata_catalog.<schema>.json``.
  * Upsert into a Pinecone **integrated** index (hosted embeddings) using the
    an embeddable text field matching the index ``field_map`` (often ``text``).
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
      --model llama-text-embed-v2 --field_map text=text

Environment::

    PINECONE_API_KEY   Required unless ``--dry-run``.
    PINECONE_INDEX     Target index name (default: ``text2sql-schema``).

Usage::

    python scripts/build_schema_index.py --dry-run
    python scripts/build_schema_index.py --schema northwind
    python scripts/build_schema_index.py --schema northwind --wipe-namespace
    python scripts/build_schema_index.py --schema northwind --smoke-query "order revenue by customer"

End-to-end example (northwind, one table, table chunks only)
--------------------------------------------------------------

**Input** — fragment of ``data/artifacts/metadata_catalog.northwind.json``::

    {
      "schema": "northwind",
      "database_kind": "postgresql",
      "structural_content_sha256": "abc123...",
      "tables": [{
        "table_name": "orders",
        "description": "Customer purchase orders",
        "primary_key_columns": ["order_id"],
        "row_count": 830,
        "columns": [{
          "column_name": "order_id",
          "data_type": "integer",
          "is_nullable": false,
          "description": "Primary key",
          "sample_values": [10248, 10249]
        }]
      }],
      "foreign_keys": []
    }

**Intermediate** — one Pinecone record produced by ``iter_schema_records`` (embedding
is computed by Pinecone from ``content``)::

    {
      "_id": "northwind__orders__table",
      "text": "Database schema: northwind\\nTable: orders\\n...",
      "schema_name": "northwind",
      "table_name": "orders",
      "chunk_kind": "table",
      "column_name": "",
      "source_id": "neon-northwind",
      "database_kind": "postgresql",
      "catalog_source": "..."
    }

**Output** — vectors in Pinecone index ``text2sql-schema``, namespace ``schema_northwind``,
plus ``data/artifacts/schema_vector_index_manifest.northwind.json`` listing index name,
record counts, and ``catalog_binding.structural_content_sha256`` for reproducibility.
"""

from __future__ import annotations  # Allow forward refs in type hints (e.g. str | None)

# --- Standard library: CLI, JSON I/O, env, regex for IDs, git subprocess, timing ---
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

_REPO_ROOT_EARLY = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT_EARLY) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_EARLY))

from agent.pinecone_compat import (
    resolve_embed_document_field,
    search_by_text,
    upsert_records_batch,
)

# Optional: load PINECONE_API_KEY from repo .env in main().
# If python-dotenv is not installed, stub load_dotenv so the script still runs.
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(_path: Path | None = None) -> None:
        return

# ---------------------------------------------------------------------------
# Paths and constants — single place for defaults used across the pipeline.
# ---------------------------------------------------------------------------
# Repo root = parent of scripts/ (where this file lives).
REPO_ROOT = Path(__file__).resolve().parent.parent
# Step 1 artifacts: metadata_catalog.*.json, manifests, connector_registry.json.
ARTIFACTS_DIR = REPO_ROOT / "data" / "artifacts"
CONNECTOR_REGISTRY = ARTIFACTS_DIR / "connector_registry.json"

DEFAULT_SCHEMA = "northwind"  # CLI --schema default; also drives default catalog filename.
MANIFEST_SPEC_VERSION = "1.0"  # Version field in written manifest JSON for consumers.
DEFAULT_EMBED_DOCUMENT_FIELD = "text"  # Typical integrated index field_map key; resolved at runtime.
INTEGRATED_MODEL_DEFAULT = "llama-text-embed-v2"  # Documented in manifest; index must use same model.
PINECONE_TEXT_BATCH = 96  # Max records per upsert_records call (Pinecone integrated-index limit).
POST_UPSERT_SLEEP_SEC = 10  # Eventual consistency: search before this may miss new vectors.

# Pinecone record _id: letters, numbers, dash, underscore only (conservative; max 512 chars).
_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp for manifest ``generated_at`` (Z suffix, no +00:00)."""
    # timezone.utc = always UTC; isoformat() might end with "+00:00" which we normalize to "Z".
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# pragma: no cover
# pylint: disable=unused-argument
def _try_git_sha() -> str | None:
    """Best-effort short Git commit for manifest provenance (optional; not used for upsert).

    Example: repo at commit e1f2a3b4... → returns ``"e1f2a3b"``.
    Not a git repo / git missing → returns ``None`` (manifest still written).
    """
    try:
        # Run from REPO_ROOT so we get this project's commit, not some other cwd.
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # e.g. "a1b2c3d"
            capture_output=True,  # don't print to terminal
            text=True,  # decode stdout as str
            timeout=5,  # avoid hanging if git blocks
            cwd=REPO_ROOT,
        )
        if out.returncode == 0:
            return out.stdout.strip()  # success → short SHA string
    except (OSError, subprocess.TimeoutExpired):
        pass  # git missing, not a repo, or timed out → fall through
    return None  # manifest will show null for git_commit_short


def _safe_record_id(raw: str) -> str:
    """Sanitize a human-readable id into a Pinecone-safe ``_id``.

    Why: SQL names may contain spaces, dots, or unicode; Pinecone ids are UTF-8 strings
    capped at 512 characters. We keep ids deterministic from schema/table/column.

    Example:
        raw = ``"northwind__Order Details__col__Unit Price"``
        → ``"northwind__Order_Details__col__Unit_Price"`` (non-alnum → ``_``)
    """
    # Replace any run of "unsafe" chars (space, dot, unicode, etc.) with a single underscore.
    s = _ID_SAFE.sub("_", raw)
    # Pinecone enforces max id length 512; truncate rather than fail upsert on long table names.
    if len(s) > 512:
        s = s[:512]
    return s


def _load_json(path: Path) -> dict[str, Any]:
    """Read UTF-8 JSON file into a dict (metadata_catalog or connector_registry)."""
    # Whole file in memory is fine: catalogs are MB-scale, not GB.
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_source_id(catalog_schema: str) -> str:
    """Pick connector ``sources[].id`` whose allowed_schemas contains ``catalog_schema``.

    Why: metadata on each vector tags which DB connector produced the catalog, so the
    agent can route or filter by data source later.

    Example:
        catalog_schema = ``"northwind"``
        connector_registry has source id ``"neon-northwind"`` with allowed_schemas
        containing ``"northwind"`` → returns ``"neon-northwind"``.
        Missing registry file → ``"unknown"``.
    """
    # No registry yet (early pipeline) → tag vectors with a sentinel, not an error.
    if not CONNECTOR_REGISTRY.is_file():
        return "unknown"
    doc = _load_json(CONNECTOR_REGISTRY)
    # Prefer exact match: which Neon/Postgres "source" owns this schema name.
    for src in doc.get("sources", []):
        allowed = (src.get("routing") or {}).get("allowed_schemas") or []
        if catalog_schema in allowed:
            return str(src.get("id", "unknown"))
    # Fallback: first source in file (better than unknown if routing metadata is incomplete).
    if doc.get("sources"):
        return str(doc["sources"][0].get("id", "unknown"))
    return "unknown"


def _fks_for_table(table_name: str, foreign_keys: list[dict[str, Any]]) -> list[str]:
    """Build human-readable FK lines for embedding text (incoming + outgoing for this table).

    Example:
        table_name = ``"orders"``, FK orders.customer_id → customers.customer_id
        → [``"Outgoing FK: customer_id -> customers.customer_id"``]
    """
    lines: list[str] = []
    # Catalog lists all FKs once; we filter to the table we're embedding right now.
    for fk in foreign_keys:
        st, sc = fk["source_table"], fk["source_column"]  # child side of FK
        tt, tc = fk["target_table"], fk["target_column"]  # parent side of FK
        # This table holds the FK column pointing outward (e.g. orders.customer_id → customers).
        if st == table_name:
            lines.append(f"Outgoing FK: {sc} -> {tt}.{tc}")
        # Another table points at us (e.g. order_details.order_id → orders.order_id).
        if tt == table_name:
            lines.append(f"Incoming FK: {st}.{sc} -> {tc}")
    return lines


def _format_sample_values(values: list[Any], max_chars: int = 400) -> str:
    """Turn catalog sample_values into a short inline string for the embedding text.

    Why: Example cell values help semantic search ("status codes", country names) without
    stuffing entire tables into Pinecone. Capped so one wide column does not blow chunk size.

    Example: ``[10248, 10249, 10250]`` → ``"10248; 10249; 10250"``
    """
    if not values:
        return ""  # omit "Examples:" line in _column_brief when empty
    parts: list[str] = []
    for v in values[:12]:  # cap count so embedding text stays bounded
        parts.append(str(v).replace("\n", " "))  # one line per value in the chunk
    text = "; ".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."  # truncate with ellipsis
    return text


def _column_brief(col: dict[str, Any]) -> str:
    """One-line column summary for table/column chunk ``content`` fields.

    Example output:
        ``"order_id (integer) | NOT NULL | Primary key | Examples: 10248; 10249"``
    """
    # Build a single readable line reused in both table-level and column-level chunks.
    parts = [
        f"{col['column_name']} ({col['data_type']})",
        "NULL" if col.get("is_nullable") else "NOT NULL",
    ]
    desc = col.get("description")  # LLM-enriched from generate_schema_descriptions.py
    if desc:
        parts.append(str(desc))
    sv = _format_sample_values(col.get("sample_values") or [])
    if sv:
        parts.append(f"Examples: {sv}")  # optional; from catalog profiling
    return " | ".join(parts)


def iter_schema_records(
    catalog: dict[str, Any],
    *,
    source_id: str,
    embed_document_field: str,
    include_table_chunks: bool,
    include_column_chunks: bool,
) -> Iterator[dict[str, Any]]:
    """Yield Pinecone-ready record dicts (_id + embed text field + flat metadata).

    Core chunking step: one metadata_catalog → many searchable units.

    Table chunk: whole table + all columns + FKs in one ``content`` string (good for
    "which table has orders?"). Column chunk: one column with table context (good for
    "which column is unit price?").

    Example yield (table chunk, abbreviated)::

        {
          "_id": "northwind__orders__table",
          "text": "Database schema: northwind\\nTable: orders\\n...",
          "schema_name": "northwind",
          "table_name": "orders",
          "chunk_kind": "table",
          "column_name": "",
          "source_id": "neon-northwind",
          ...
        }
    """
    # --- Catalog-level fields copied onto every chunk's metadata ---
    schema = str(catalog.get("schema", ""))  # e.g. "northwind"; used in Pinecone filters
    db_kind = str(catalog.get("database_kind", "postgresql"))  # dialect hint for the agent
    catalog_source = str(catalog.get("source", ""))  # how catalog was produced (export path, etc.)
    tables: list[dict[str, Any]] = catalog.get("tables") or []
    foreign_keys: list[dict[str, Any]] = catalog.get("foreign_keys") or []

    # One loop iteration = one physical table in the database.
    for table in tables:
        tname = str(table["table_name"])
        tdesc = table.get("description") or ""  # may be "" if descriptions not generated yet
        pk = table.get("primary_key_columns") or []
        cols: list[dict[str, Any]] = table.get("columns") or []
        rc = table.get("row_count")  # optional estimate from profiling
        fk_lines = _fks_for_table(tname, foreign_keys)  # both directions for JOIN hints in RAG

        # ----- TABLE CHUNK: 1 vector per table (wide context) -----
        if include_table_chunks:
            # List every column on indented lines so the embedder sees the full shape at once.
            col_block = "\n".join(f"  - {_column_brief(c)}" for c in cols)
            fk_block = "\n".join(f"  - {line}" for line in fk_lines) if fk_lines else "  (none listed)"
            # This string is what Pinecone embeds (key must match index field_map document field).
            text = (
                f"Database schema: {schema}\n"
                f"Table: {tname}\n"
                f"Row estimate: {rc}\n"
                f"Primary key columns: {', '.join(pk) if pk else '(none)'}\n"
                f"Table description: {tdesc}\n"
                f"Columns:\n{col_block}\n"
                f"Foreign keys touching this table:\n{fk_block}\n"
            )
            # Stable id: re-running upsert with same catalog overwrites same _id (idempotent).
            rid = _safe_record_id(f"{schema}__{tname}__table")
            yield {
                "_id": rid,
                embed_document_field: text,
                # --- metadata: flat key/value only; agent filters e.g. chunk_kind == "table" ---
                "schema_name": schema,
                "table_name": tname,
                "chunk_kind": "table",  # distinguishes from per-column chunks
                "column_name": "",  # empty string = table-level (Pinecone filters need a scalar)
                "source_id": source_id,  # from connector_registry.json
                "database_kind": db_kind,
                "catalog_source": catalog_source,
            }

        # ----- COLUMN CHUNK: 1 vector per column (narrow, precise retrieval) -----
        if include_column_chunks:
            for col in cols:
                cname = str(col["column_name"])
                # Repeat table PK/description so a column hit still has join context.
                text = (
                    f"Schema {schema} table {tname} column {cname}.\n"
                    f"{_column_brief(col)}\n"
                    f"Table context: {tdesc}\n"
                    f"Primary key: {', '.join(pk) if pk else '(none)'}\n"
                )
                rid = _safe_record_id(f"{schema}__{tname}__col__{cname}")
                yield {
                    "_id": rid,
                    embed_document_field: text,
                    "schema_name": schema,
                    "table_name": tname,
                    "chunk_kind": "column",
                    "column_name": cname,  # non-empty → filter to one column
                    "source_id": source_id,
                    "database_kind": db_kind,
                    "catalog_source": catalog_source,
                }


def _batched(records: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Split record list into fixed-size slices for upsert_records (max 96 per call).

    Example: 250 records, size=96 → yields 3 batches of 96, 96, 58.
    """
    # Step by `size` (96): indices 0..95, 96..191, ... last partial batch is fine.
    for i in range(0, len(records), size):
        yield records[i : i + size]


def _upsert_with_backoff(index: Any, namespace: str, batch: list[dict[str, Any]]) -> None:
    """Upsert one batch with exponential backoff on transient Pinecone errors.

    Why: Rate limits (429) and server errors (5xx) are common on large catalogs; retry
    avoids failing a long run for a blip. Non-retryable errors propagate immediately.
    """
    from pinecone.exceptions import PineconeException

    delay = 1.0  # seconds before first retry
    for attempt in range(6):  # attempts 0..5 → up to 5 retries after first failure
        try:
            # Integrated index: Pinecone reads embed_document_field from each dict server-side.
            upsert_records_batch(index, namespace, batch)
            return  # batch succeeded; caller moves to next batch
        except PineconeException as e:
            status = getattr(e, "status", None) or getattr(e, "http_status", None)
            # Retry only transient errors; 4xx (except 429) should fail fast (bad record, auth, etc.).
            if status in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 30)  # exponential backoff capped at 30s
            else:
                raise  # give up: wrong API key, invalid metadata, etc.


def _smoke_search(
    index: Any,
    namespace: str,
    query_text: str,
    *,
    schema_filter: str,
    embed_document_field: str,
    top_k: int = 5,
) -> None:
    """Optional sanity check: semantic search after upsert (requires post-upsert wait).

    Uses integrated index ``inputs.text`` + metadata filter on ``schema_name`` so hits
    stay within the schema we just loaded.

    Example:
        query_text = ``"customer order totals"``, schema_filter = ``"northwind"``
        → prints top 5 hit ids, scores, and first 160 chars of ``content``.
    """
    flt: dict[str, Any] = {"schema_name": {"$eq": schema_filter}}
    resp = search_by_text(
        index,
        namespace=namespace,
        query_text=query_text,
        top_k=top_k,
        metadata_filter=flt,
    )
    # Normalize hits: Pinecone Python SDK version may return objects or plain dicts.
    hits = []
    if hasattr(resp, "result") and resp.result and hasattr(resp.result, "hits"):
        hits = list(resp.result.hits)
    elif isinstance(resp, dict):
        hits = list((resp.get("result") or {}).get("hits") or [])
    print(f"\nSmoke search ({top_k} hits, filter schema_name={schema_filter!r}):")
    for h in hits:
        # Extract _id, similarity score, and stored fields from either representation.
        if isinstance(h, dict):
            hid = h.get("_id")
            score = h.get("_score")
            fields = h.get("fields") or {}
        else:
            hid = getattr(h, "_id", None)
            score = getattr(h, "_score", None)
            fields = getattr(h, "fields", None) or {}
        if hasattr(fields, "get"):
            preview = (fields.get(embed_document_field) or "")[:160]
        else:
            preview = str(fields)[:160]
        print(f"  id={hid} score={score} preview={preview!r}")


def parse_args() -> argparse.Namespace:
    """CLI flags for catalog path, Pinecone target, chunk modes, dry-run, and smoke test."""
    p = argparse.ArgumentParser(
        description="Build Pinecone schema_vector_index from metadata_catalog (Step 1).",
    )
    # Which DB schema's catalog file to read (filename + default namespace suffix).
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
    # Chunk toggles: column-only or table-only indexing for experiments / smaller indexes.
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
    """Orchestrate: load catalog → build records → upsert → manifest → optional smoke search.

    Exit 0 on success; 1 on missing catalog, missing API key, missing index, or bad flags.
    """
    # -------------------------------------------------------------------------
    # Phase 1: Configuration — env, CLI args, paths
    # -------------------------------------------------------------------------
    load_dotenv(REPO_ROOT / ".env")  # loads PINECONE_API_KEY, PINECONE_INDEX, etc.
    args = parse_args()
    schema = args.schema  # e.g. northwind → default catalog + default namespace
    catalog_path = args.catalog or (ARTIFACTS_DIR / f"metadata_catalog.{schema}.json")
    if not catalog_path.is_file():
        print(f"ERROR: metadata catalog not found: {catalog_path}", file=sys.stderr)
        return 1

    # -------------------------------------------------------------------------
    # Phase 2: Build in-memory records (no Pinecone yet) — safe to dry-run
    # -------------------------------------------------------------------------
    catalog = _load_json(catalog_path)
    source_id = resolve_source_id(schema)  # stamped on every record's metadata
    include_table = not args.no_table_chunks
    include_column = not args.no_column_chunks
    if not include_table and not include_column:
        print("ERROR: need at least one of table or column chunks.", file=sys.stderr)
        return 1

    if args.dry_run:
        embed_document_field = os.getenv("PINECONE_TEXT_FIELD") or DEFAULT_EMBED_DOCUMENT_FIELD
        records = list(
            iter_schema_records(
                catalog,
                source_id=source_id,
                embed_document_field=embed_document_field,
                include_table_chunks=include_table,
                include_column_chunks=include_column,
            )
        )
        n_table = sum(1 for r in records if r.get("chunk_kind") == "table")
        n_col = sum(1 for r in records if r.get("chunk_kind") == "column")
        print(
            f"Prepared {len(records)} records (table={n_table}, column={n_col}) "
            f"embed_field={embed_document_field!r} from {catalog_path.name}"
        )
        print("Dry run: no Pinecone upsert.")
        return 0

    # -------------------------------------------------------------------------
    # Phase 3: Pinecone client — require API key and pre-created integrated index
    # -------------------------------------------------------------------------
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: PINECONE_API_KEY not set (or use --dry-run).", file=sys.stderr)
        return 1

    # CLI --index-name overrides env PINECONE_INDEX overrides hardcoded default.
    index_name = args.index_name or os.getenv("PINECONE_INDEX") or "text2sql-schema"
    # Default namespace isolates schemas on one index: schema_northwind vs schema_other.
    namespace = args.namespace or f"schema_{schema}"

    from pinecone import Pinecone

    pc = Pinecone(api_key=api_key)
    if not pc.has_index(index_name):
        print(
            f"ERROR: Pinecone index {index_name!r} does not exist.\n"
            f"Create it first, e.g.:\n"
            f'  pc index create -n {index_name} -m cosine -c aws -r us-east-1 \\\n'
            f"    --model {INTEGRATED_MODEL_DEFAULT} --field_map text=text",
            file=sys.stderr,
        )
        return 1

    embed_document_field = resolve_embed_document_field(pc, index_name)
    print(f"Using embed document field {embed_document_field!r} (from index field_map)")

    records = list(
        iter_schema_records(
            catalog,
            source_id=source_id,
            embed_document_field=embed_document_field,
            include_table_chunks=include_table,
            include_column_chunks=include_column,
        )
    )
    n_table = sum(1 for r in records if r.get("chunk_kind") == "table")
    n_col = sum(1 for r in records if r.get("chunk_kind") == "column")
    print(f"Prepared {len(records)} records (table={n_table}, column={n_col}) from {catalog_path.name}")

    index = pc.Index(index_name)  # handle for upsert/search on that index

    # -------------------------------------------------------------------------
    # Phase 4: Optional wipe + batched upsert
    # -------------------------------------------------------------------------
    if args.wipe_namespace:
        # Full namespace replace: avoid stale vectors when catalog shrinks or ids change.
        print(f"Wiping namespace {namespace!r} on index {index_name!r} ...")
        index.delete(namespace=namespace, delete_all=True)
        time.sleep(2)  # brief pause after delete before upsert

    print(f"Upserting to index={index_name!r} namespace={namespace!r} ...")
    total = 0
    for batch in _batched(records, PINECONE_TEXT_BATCH):
        _upsert_with_backoff(index, namespace, batch)
        total += len(batch)
        print(f"  upserted {total}/{len(records)}")

    # -------------------------------------------------------------------------
    # Phase 5: Wait for indexing, log stats, write manifest artifact
    # -------------------------------------------------------------------------
    if not args.skip_post_wait:
        # Without this, --smoke-query immediately after upsert often returns 0 hits.
        print(f"Waiting {POST_UPSERT_SLEEP_SEC}s for vectors to be queryable (Pinecone guide) ...")
        time.sleep(POST_UPSERT_SLEEP_SEC)

    stats = index.describe_index_stats()  # sanity check vector counts per namespace
    print(f"describe_index_stats: {stats}")

    structural_hash = str(catalog.get("structural_content_sha256", ""))
    # Manifest = contract for agent/tools: "this index matches this catalog snapshot".
    manifest: dict[str, Any] = {
        "schema_vector_index_manifest_spec_version": MANIFEST_SPEC_VERSION,
        "generated_at": _utc_now_iso(),  # when this script finished
        "git_commit_short": _try_git_sha(),  # which code revision ran (optional)
        "pinecone_index_name": index_name,
        "pinecone_namespace": namespace,
        "text_field": embed_document_field,
        "embed_field_map": getattr(pc.describe_index(index_name).embed, "field_map", None),
        "integrated_embedding_model": os.getenv("PINECONE_EMBEDDING_MODEL", INTEGRATED_MODEL_DEFAULT),
        "metric": "cosine",  # must match index metric at create time
        "chunking": {
            "table_chunks": include_table,
            "column_chunks": include_column,
        },
        "record_count": len(records),
        "chunk_counts": {"table": n_table, "column": n_col},
        "source_id": source_id,
        "catalog_binding": {
            # Links vectors back to exact catalog file + hash (detect stale index).
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

    # -------------------------------------------------------------------------
    # Phase 6: Optional CLI smoke test (--smoke-query "natural language question")
    # -------------------------------------------------------------------------
    if args.smoke_query:
        _smoke_search(
            index,
            namespace,
            args.smoke_query,
            schema_filter=schema,
            embed_document_field=embed_document_field,
        )

    return 0


if __name__ == "__main__":
    # Propagate main()'s exit code (0 = success, 1 = error) to the shell.
    raise SystemExit(main())
