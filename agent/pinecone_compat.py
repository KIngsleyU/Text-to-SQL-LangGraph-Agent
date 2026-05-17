"""Pinecone Python SDK compatibility (v7 positional vs v9 keyword-only APIs).

Pinecone 9.x (see docs.pinecone.io / AGENTS-PYTHON) uses::

    index.upsert_records(namespace=..., records=...)
    index.search(namespace=..., top_k=..., inputs={...}, filter=...)

Older 7.x SDK used positional upsert and a nested ``query`` dict for search.
"""

from __future__ import annotations

import inspect
import os
from typing import Any


def resolve_embed_document_field(pc: Any, index_name: str) -> str:
    """Return the record field name that holds embeddable text for this integrated index.

    Pinecone SDK 9 ``field_map`` maps **document field name → embedding role**
    (e.g. ``{"text": "text"}`` → records must include a ``text`` key).

  Override with env ``PINECONE_TEXT_FIELD`` if needed.
    """
    override = os.getenv("PINECONE_TEXT_FIELD")
    if override:
        return override.strip()

    desc = pc.describe_index(index_name)
    embed = getattr(desc, "embed", None)
    field_map = getattr(embed, "field_map", None) if embed is not None else None
    if field_map:
        # Keys are document fields (see pinecone.models.indexes.index.ModelIndexEmbed).
        return str(next(iter(field_map.keys())))
    return "text"


def upsert_records_batch(index: Any, namespace: str, records: list[dict[str, Any]]) -> Any:
    """Upsert integrated-index records into ``namespace`` (batch already sized ≤96)."""
    sig = inspect.signature(index.upsert_records)
    records_param = sig.parameters.get("records")
    if records_param is not None and records_param.kind == inspect.Parameter.KEYWORD_ONLY:
        return index.upsert_records(namespace=namespace, records=records)
    return index.upsert_records(namespace, records)


def search_by_text(
    index: Any,
    *,
    namespace: str,
    query_text: str,
    top_k: int,
    metadata_filter: dict[str, Any] | None = None,
    rerank: dict[str, Any] | None = None,
) -> Any:
    """Semantic search on an integrated index using natural-language ``query_text``."""
    sig = inspect.signature(index.search)
    if "top_k" in sig.parameters:
        kwargs: dict[str, Any] = {
            "namespace": namespace,
            "top_k": top_k,
            "inputs": {"text": query_text},
        }
        if metadata_filter is not None:
            kwargs["filter"] = metadata_filter
        if rerank is not None:
            kwargs["rerank"] = rerank
        return index.search(**kwargs)

    query: dict[str, Any] = {
        "top_k": top_k,
        "inputs": {"text": query_text},
    }
    if metadata_filter is not None:
        query["filter"] = metadata_filter
    return index.search(namespace=namespace, query=query, rerank=rerank)
