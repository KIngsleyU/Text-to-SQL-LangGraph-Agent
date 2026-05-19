# agent/state.py
from __future__ import annotations
from typing import Annotated, Any, TypedDict
from operator import add

class TextToSQLState(TypedDict, total=False):
    original_query: str
    query: str
    router: dict[str, Any]
    semantic_prompt_payload: dict[str, Any]
    schema_rag_used: bool
    schema_rag_query: str
    schema_rag_hits: list[dict[str, Any]]
    schema_rag_text: str
    needs_clarification: bool
    clarification_question: str
    candidate_sql: str
    execution_result: list[dict[str, Any]]
    error_history: Annotated[list[str], add]
    correction_attempts: int