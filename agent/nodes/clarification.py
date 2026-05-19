"""LangGraph node: surface clarification requests from semantic routing."""

from __future__ import annotations

from typing import Any

from agent.state import TextToSQLState


def clarification_node(state: TextToSQLState) -> dict[str, Any]:
    """Propagate clarification signals into top-level state fields."""
    router = state.get("router") or {}
    needs = bool(router.get("needs_clarification"))
    question = router.get("clarification_question")
    return {
        "needs_clarification": needs,
        "clarification_question": question,
    }
