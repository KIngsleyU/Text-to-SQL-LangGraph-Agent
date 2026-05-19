"""LangGraph node: retrieve schema chunks from Pinecone when ontology falls back."""

from __future__ import annotations

from typing import Any

from agent._artifact import load_schema_vector_manifest
from agent.pinecone_schema_retrieval import search_schema_chunks
from agent.state import TextToSQLState


def schema_retrieval_node(state: TextToSQLState) -> dict[str, Any]:
    """Run schema RAG only when semantic resolution signals fallback.

    Returns a compact payload for downstream SQL generation: raw hits plus a
    concatenated text block of the top-k chunks.
    """
    router = state.get("router") or {}
    if not router.get("fallback_used"):
        return {"schema_rag_used": False}

    query = state.get("original_query") or state.get("query") or ""
    schema = router.get("schema") or "northwind"
    hits = search_schema_chunks(query, schema=schema, top_k=8)

    manifest = load_schema_vector_manifest(schema)
    text_field = str(manifest.get("text_field", "text"))
    chunk_texts = [
        h["fields"].get(text_field, "")
        for h in hits
        if isinstance(h.get("fields"), dict) and h["fields"].get(text_field)
    ]

    return {
        "schema_rag_used": True,
        "schema_rag_query": query,
        "schema_rag_hits": hits,
        "schema_rag_text": "\n\n".join(chunk_texts),
    }
