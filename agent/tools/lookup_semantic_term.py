"""LLM-callable tool: look up a phrase against the Semantic Ontology Layer.

This tool wraps the same resolver core as ``semantic_resolution_node`` in
``agent.nodes.semantic_resolution``. The node runs deterministically before
SQL generation; this tool is the LLM's escape hatch — it can call
``lookup_semantic_term("gross margin")`` mid-reasoning when it encounters a
phrase whose table/column mapping is unclear.

Promotion to a real LangChain tool (one-line, when ``langchain_core`` is
installed)::

    from langchain_core.tools import tool
    from agent.tools.lookup_semantic_term import lookup_semantic_term as _impl
    lookup_semantic_term = tool(_impl)

Until then this module exposes a plain Python function with a tool-shaped
docstring so the migration is purely a wrapper change.
"""

from __future__ import annotations

import json

from agent._artifact import load_semantic_terms
from agent._payload import to_sql_prompt_payload
from scripts.resolve_semantic_context import resolve


def lookup_semantic_term(text: str) -> str:
    """Look up a business phrase in the Northwind semantic ontology.

    Returns the matched metric/dimension/filter SQL fragments, joinable
    tables, join paths, and clarification/fallback flags as a JSON string.

    Args:
        text: A free-form business phrase, e.g. ``"gross revenue"``,
            ``"active customers"``, ``"stock value"``.

    Returns:
        A JSON-encoded string. Empty ``terms`` list means no ontology
        match — the calling agent should fall back to schema+RAG retrieval.

    Example:
        >>> import json
        >>> out = json.loads(lookup_semantic_term("active customers"))
        >>> out["terms"][0]["id"]
        'metric.distinct_customer_ordering'
        >>> out["terms"][0]["fragment_role"]
        'postgres_aggregate_sql'
    """
    artifact = load_semantic_terms()
    ctx = resolve(text, artifact)
    return json.dumps(to_sql_prompt_payload(ctx), ensure_ascii=False)
