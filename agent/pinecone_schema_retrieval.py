"""Runtime schema chunk retrieval from Pinecone (Step 1 ``schema_vector_index``).

Uses ``schema_vector_index_manifest.<schema>.json`` produced by
``scripts/build_schema_index.py``. Follows ``.agents/PINECONE-python.md``:
namespace is mandatory; optional metadata filter; wait after upserts is the
builder's responsibility — not repeated here.

Environment:

    PINECONE_API_KEY   Required for search.
"""

from __future__ import annotations

import os
from typing import Any

from pinecone import Pinecone

from agent._artifact import load_schema_vector_manifest
from agent.pinecone_compat import search_by_text


def _normalize_hits(resp: Any) -> list[dict[str, Any]]:
    hits_raw: list[Any] = []
    if hasattr(resp, "result") and resp.result is not None:
        rh = getattr(resp.result, "hits", None)
        if rh is not None:
            hits_raw = list(rh)
    elif isinstance(resp, dict):
        hits_raw = list((resp.get("result") or {}).get("hits") or [])

    out: list[dict[str, Any]] = []
    for h in hits_raw:
        if isinstance(h, dict):
            hid = h.get("_id")
            score = h.get("_score")
            fields = h.get("fields") or {}
        else:
            hid = getattr(h, "_id", None)
            score = getattr(h, "_score", None)
            fields = getattr(h, "fields", None)
            if fields is not None and hasattr(fields, "model_dump"):
                fields = fields.model_dump()
            elif fields is not None and hasattr(fields, "__dict__"):
                fields = dict(fields.__dict__)
            else:
                fields = fields or {}
        out.append({"id": hid, "score": score, "fields": dict(fields)})
    return out


def search_schema_chunks(
    query: str,
    *,
    schema: str = "northwind",
    top_k: int = 8,
    table_name: str | None = None,
    rerank: bool = False,
) -> list[dict[str, Any]]:
    """Semantic search over schema chunks, filtered to one Postgres schema name.

    Args:
        query: Natural-language question or search string.
        schema: Catalog schema (must match ``schema_name`` metadata in the index).
        top_k: Number of hits to return after optional rerank.
        table_name: If set, restrict to ``table_name`` metadata.
        rerank: If True, apply hosted reranker (adds latency; better precision).

    Returns:
        List of ``{"id", "score", "fields"}`` dicts (``fields`` includes ``content``).
    """
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        raise ValueError("PINECONE_API_KEY is required for schema chunk search")

    manifest = load_schema_vector_manifest(schema)
    index_name = str(manifest["pinecone_index_name"])
    namespace = str(manifest["pinecone_namespace"])
    text_field = str(manifest.get("text_field", "content"))

    flt: dict[str, Any] = {"schema_name": {"$eq": schema}}
    if table_name:
        flt = {"$and": [flt, {"table_name": {"$eq": table_name}}]}

    search_k = top_k * 2 if rerank else top_k

    pc = Pinecone(api_key=api_key)
    index = pc.Index(index_name)

    rerank_kw: dict[str, Any] | None = None
    if rerank:
        rerank_kw = {
            "model": "bge-reranker-v2-m3",
            "top_n": top_k,
            "rank_fields": [text_field],
        }

    resp = search_by_text(
        index,
        namespace=namespace,
        query_text=query,
        top_k=search_k,
        metadata_filter=flt,
        rerank=rerank_kw,
    )
    return _normalize_hits(resp)[:top_k]
