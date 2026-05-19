"""LangGraph composition root for the Text-to-SQL agent.

Wires nodes (and later, tools) into a ``StateGraph`` over
``TextToSQLState``. Other modules MUST NOT import from this file — it
is the leaf of the import graph so it can freely import everything else.

Run as a script for a smoke check::

    python agent/graph.py
    python -m agent.graph
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langgraph.graph import END, StateGraph

from agent.nodes import clarification_node, schema_retrieval_node, semantic_resolution_node
from agent.state import TextToSQLState
from agent.tools import lookup_semantic_term, retrieve_schema_chunks  # noqa: F401

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover

    def load_dotenv(_path: Path | None = None) -> None:
        return


def _semantic_resolution(state: TextToSQLState) -> dict[str, Any]:
    """Adapter: the underlying node takes a query string; LangGraph passes state."""
    return semantic_resolution_node(state["original_query"])


def _clarification(state: TextToSQLState) -> dict[str, Any]:
    """Adapter: expose clarification request to callers."""
    return clarification_node(state)


def _schema_retrieval(state: TextToSQLState) -> dict[str, Any]:
    """Adapter: schema RAG runs only when fallback is signaled."""
    return schema_retrieval_node(state)


def _route_after_semantic(state: TextToSQLState) -> str:
    router = state.get("router") or {}
    if router.get("needs_clarification"):
        return "clarification"
    if router.get("fallback_used"):
        return "schema_retrieval"
    return END


def build_graph():
    """Build and compile the agent graph.

    Current topology (will grow as nodes land)::

        START -> semantic_resolution -> (clarification? | schema_retrieval?) -> END

    Next nodes to add (per the design docs): clarification (HITL),
    schema_rag_fallback, sql_generation, sql_validation_sqlglot,
    execution, taxonomy_correction, pii_masking, synthesis.
    """
    builder = StateGraph(TextToSQLState)
    builder.add_node("semantic_resolution", _semantic_resolution)
    builder.add_node("clarification", _clarification)
    builder.add_node("schema_retrieval", _schema_retrieval)
    builder.set_entry_point("semantic_resolution")
    builder.add_conditional_edges("semantic_resolution", _route_after_semantic)
    builder.add_edge("clarification", END)
    builder.add_edge("schema_retrieval", END)

    # When langchain_core + an LLM are wired in:
    #     from langchain_core.tools import tool
    #     sql_llm = sql_llm.bind_tools([tool(lookup_semantic_term), tool(retrieve_schema_chunks)])

    return builder.compile()


if __name__ == "__main__":
    load_dotenv(REPO_ROOT / ".env")
    graph = build_graph()
    out = graph.invoke(
        # {"original_query": "Show top 5 customer countries by revenue in 1997, shipped orders only"}
        {"original_query": "how much did me make?"}
    )
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
