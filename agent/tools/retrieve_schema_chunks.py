"""LLM-callable tool: retrieve schema chunks from Pinecone (fallback RAG)."""

from __future__ import annotations

import json

from agent.pinecone_schema_retrieval import search_schema_chunks


def retrieve_schema_chunks(text: str, schema: str = "northwind", top_k: int = 8) -> str:
    """Return Pinecone schema hits as a JSON string.

    Args:
        text: Natural-language query (e.g. "revenue by product category").
        schema: Catalog schema to filter by (defaults to ``northwind``).
        top_k: Number of hits to return.
    """
    hits = search_schema_chunks(text, schema=schema, top_k=top_k)
    return json.dumps(hits, ensure_ascii=False)
