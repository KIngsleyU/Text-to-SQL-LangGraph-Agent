"""Slim down resolver context into a payload safe for the SQL-generation prompt.

The resolver (``scripts.resolve_semantic_context.resolve``) returns a rich
context dict that includes audit/debug fields useful for tracing and
evaluation. Those fields should be logged via LangSmith / state, NOT burned
into the LLM prompt. This module produces the load-bearing subset.
"""

from __future__ import annotations

from typing import Any


def to_sql_prompt_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    """Keep only the semantic context fields the SQL-generation prompt needs.

    Drops, relative to the full resolver context:

    * ``ontology_version`` and ``catalog_binding`` (version pin / audit)
    * Per-term ``score`` and ``matched_via`` (resolver debug)
    * ``notes`` (resolver debug; surfaced via the router instead)

    Args:
        ctx: The dict returned by ``resolve(query, artifact)``.

    Returns:
        A dict with ``schema``, slim ``terms``, ``joinable_tables``,
        ``join_paths``, plus router-relevant flags
        (``needs_clarification``, ``clarification_questions``,
        ``fallback_used``, ``fallback_policy``).
    """
    prompt_terms = [
        {
            "id": term["id"],
            "kind": term["kind"],
            "label": term["canonical_label"],
            "fragment_role": term["fragment_role"],
            "sql_fragment": term["sql_fragment"],
            "default_grain": term["default_grain"],
            "primary_fact_table": term["primary_fact_table"],
            "lineage": term["lineage"],
            "join_notes": term["agent_join_notes"],
        }
        for term in ctx["matched_terms"]
    ]

    return {
        "schema": ctx["schema"],
        "terms": prompt_terms,
        "joinable_tables": ctx["joinable_tables"],
        "join_paths": ctx["join_paths"],
        "needs_clarification": ctx["needs_clarification"],
        "clarification_questions": ctx["clarification_questions"],
        "fallback_used": ctx["fallback_used"],
        "fallback_policy": ctx["fallback_policy"],
    }
