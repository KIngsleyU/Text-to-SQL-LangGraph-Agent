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

from agent.nodes import semantic_resolution_node
from agent.state import TextToSQLState
from agent.tools import lookup_semantic_term  # noqa: F401  (bound to the LLM later)


def _semantic_resolution(state: TextToSQLState) -> dict[str, Any]:
    """Adapter: the underlying node takes a query string; LangGraph passes state."""
    return semantic_resolution_node(state["original_query"])


def build_graph():
    """Build and compile the agent graph.

    Current topology (will grow as nodes land)::

        START -> semantic_resolution -> END

    Next nodes to add (per the design docs): clarification (HITL),
    schema_rag_fallback, sql_generation, sql_validation_sqlglot,
    execution, taxonomy_correction, pii_masking, synthesis.
    """
    builder = StateGraph(TextToSQLState)
    builder.add_node("semantic_resolution", _semantic_resolution)
    builder.set_entry_point("semantic_resolution")
    builder.add_edge("semantic_resolution", END)

    # When langchain_core + an LLM are wired in:
    #     from langchain_core.tools import tool
    #     sql_llm = sql_llm.bind_tools([tool(lookup_semantic_term)])

    return builder.compile()


if __name__ == "__main__":
    graph = build_graph()
    out = graph.invoke(
        {"original_query": "Show top 5 customer countries by revenue in 1997, shipped orders only"}
    )
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
