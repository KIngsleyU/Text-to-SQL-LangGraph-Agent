"""Smoke test for the semantic ontology entry point.

Exercises both adapters over the same resolver core:

* ``agent.nodes.semantic_resolution_node`` — runs deterministically inside
  the LangGraph, returns a state delta (router + slim prompt payload).
* ``agent.tools.lookup_semantic_term`` — LLM-callable; takes a phrase and
  returns a JSON string suitable for the LLM's working memory.

Run from the repo root::

    python agent/test.py
    python -m agent.test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.nodes import semantic_resolution_node
from agent.tools import lookup_semantic_term


def _print(label: str, payload: Any) -> None:
    print(label)
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            print(payload)
        else:
            print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print("-" * 88)


if __name__ == "__main__":
    queries = [
        "Show top 5 customer countries by revenue in 1997, shipped orders only",
        "Total freight by shipper company",
        "Number of orders by employee in 1997",
        "Revenue by category, exclude discontinued products",
        "How many active customers placed orders",
        "Sales by country",
        "Average line discount by supplier",
        "Stock units value at list price for non-discontinued products",
    ]
    query = queries[5]

    _print(f"NODE result for: {query!r}", semantic_resolution_node(query))
    _print("TOOL result for phrase: 'active customers'", lookup_semantic_term("active customers"))
