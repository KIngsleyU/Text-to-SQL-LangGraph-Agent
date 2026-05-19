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


def _hit_fields_dict(fields: Any) -> dict[str, Any]:
    if fields is None:
        return {}
    if isinstance(fields, dict):
        return dict(fields)
    if hasattr(fields, "model_dump"):
        return dict(fields.model_dump())
    if hasattr(fields, "to_dict"):
        return dict(fields.to_dict())
    if hasattr(fields, "__dict__"):
        return dict(fields.__dict__)
    return {}


def normalize_hit(hit: Any) -> dict[str, Any]:
    """Normalize one search hit to ``{id, score, fields}`` (SDK v7–v9)."""
    if isinstance(hit, dict):
        hid = hit.get("id") or hit.get("_id")
        score = hit.get("score") if hit.get("score") is not None else hit.get("_score")
        fields = _hit_fields_dict(hit.get("fields"))
        return {"id": hid, "score": score, "fields": fields}

    hid = getattr(hit, "id", None) or getattr(hit, "_id", None)
    score = getattr(hit, "score", None)
    if score is None:
        score = getattr(hit, "_score", None)
    fields = _hit_fields_dict(getattr(hit, "fields", None))
    return {"id": hid, "score": score, "fields": fields}


def normalize_search_hits(resp: Any) -> list[dict[str, Any]]:
    """Extract hits from a search response and normalize each to ``{id, score, fields}``."""
    hits_raw: list[Any] = []
    if hasattr(resp, "result") and resp.result is not None:
        rh = getattr(resp.result, "hits", None)
        if rh is not None:
            hits_raw = list(rh)
    elif isinstance(resp, dict):
        hits_raw = list((resp.get("result") or {}).get("hits") or [])

    return [normalize_hit(h) for h in hits_raw]


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
