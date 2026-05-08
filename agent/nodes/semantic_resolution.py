"""LangGraph node: resolve a query against the Semantic Ontology Layer."""

from __future__ import annotations

from typing import Any

from agent._artifact import load_semantic_terms
from agent._payload import to_sql_prompt_payload
from scripts.resolve_semantic_context import resolve


def semantic_resolution_node(query: str) -> dict[str, Any]:
    """Resolve a NL query into routing flags and a slim SQL-prompt payload.

    LangGraph entry node that bridges natural language to the deterministic,
    ontology-grounded context the SQL-generation node needs. It loads
    ``semantic_terms.<schema>.json`` (the Semantic Ontology Layer artifact)
    and runs the three resolver stages from
    ``scripts.resolve_semantic_context``:

        1. Detect candidate terms — score the query against every term's
           ``canonical_label`` and ``aliases``.
        2. Disambiguate — apply intent-cue rules; if competitors remain
           tied, set ``needs_clarification`` and emit a clarification
           question.
        3. Assemble context — keep only the load-bearing fields (SQL
           fragments, lineage, join paths). Audit/debug fields (ontology
           version, catalog binding, match scores, ``matched_via``) are
           dropped from the prompt payload.

    Args:
        query: The user's natural-language question.

    Returns:
        A dict with three top-level keys::

            {
              "query": str,                       # echoed input
              "router": {                         # used by conditional edges
                "needs_clarification": bool,
                "clarification_question": str | None,
                "fallback_used": bool,            # True when no term matched
                "fallback_policy": str | None,    # use when fallback_used
                "notes": list[str],
              },
              "semantic_prompt_payload": {        # feeds SQL generation
                "schema": str,
                "terms": list[{
                  "id": str,                          # e.g. "metric.line_extended_amount"
                  "kind": str,                        # metric | dimension | filter_preset | entity
                  "label": str,
                  "fragment_role": str,               # postgres_aggregate_sql / _expression / _predicate
                  "sql_fragment": str,                # SQL snippet to splice in
                  "default_grain": str | None,
                  "primary_fact_table": str | None,
                  "lineage": list[{"table","column","role"}],
                  "join_notes": str | None,
                }],
                "joinable_tables": list[str],
                "join_paths": list[{"from_table","from_column","to_table","to_column","constraint"}],
                ...
              },
            }

    Example:
        >>> out = semantic_resolution_node(
        ...     "Show top 5 customer countries by revenue in 1997, shipped orders only"
        ... )
        >>> out["router"]["needs_clarification"]
        False
        >>> [t["id"] for t in out["semantic_prompt_payload"]["terms"]][:2]
        ['filter.shipped_orders_only', 'metric.line_extended_amount']
        >>> out["semantic_prompt_payload"]["joinable_tables"]
        ['customers', 'order_details', 'orders']

    LangGraph integration:
        Wire this as the first node after intent classification. Use a
        conditional edge driven by ``state["router"]``:

          * ``needs_clarification == True`` → route to HITL/clarification node
          * ``fallback_used        == True`` → route to schema+RAG fallback node
          * otherwise                       → route to SQL-generation node,
            passing ``state["semantic_prompt_payload"]`` into the prompt.
    """
    artifact = load_semantic_terms()
    ctx = resolve(query, artifact)
    prompt_payload = to_sql_prompt_payload(ctx)

    return {
        "query": query,
        "router": {
            "needs_clarification": ctx["needs_clarification"],
            "clarification_question": (
                ctx["clarification_questions"][0]["question"]
                if ctx["clarification_questions"]
                else None
            ),
            "fallback_used": ctx["fallback_used"],
            "fallback_policy": ctx["fallback_policy"],
            "notes": ctx["notes"],
        },
        "semantic_prompt_payload": prompt_payload,
    }
