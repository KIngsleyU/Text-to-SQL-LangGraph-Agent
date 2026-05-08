#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Resolve a natural-language query against the **semantic_terms** ontology.

Implements the three pipeline stages from the Step 1 design notes:

    1. **Detect candidate terms** by matching the query against each term's
       ``aliases`` and ``canonical_label`` (whole-phrase preferred, then
       content-word overlap).
    2. **Disambiguate** when multiple terms compete (customer vs. ship
       country, order date vs. shipped date, etc.) using small intent-cue
       rules. If still tied, mark ``needs_clarification`` so the agent can
       ask a follow-up question.
    3. **Assemble a semantic context package**: extract the SQL fragments
       (``postgres_aggregate_sql`` / ``postgres_expression`` /
       ``postgres_predicate``), lineage, ``agent_join_notes``, and the
       relevant subset of ``foreign_key_paths`` for safe joins. When nothing
       matches, surface ``fallback_policy``.

Artifact produced by ``scripts/generate_semantic_terms.py``::

    data/artifacts/semantic_terms.<schema>.json

Usage::

    python scripts/resolve_semantic_context.py "Top 5 countries by revenue in 1997"
    python scripts/resolve_semantic_context.py --demo
    python scripts/resolve_semantic_context.py "Sales by country" --compact
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT = REPO_ROOT / "data" / "artifacts" / "semantic_terms.northwind.json"

# Filler words; ignored when scoring partial content-word overlap.
STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "by", "for", "to", "and", "or",
    "with", "from", "into", "is", "are", "was", "were",
})

WORD_RE = re.compile(r"[a-z0-9]+")

# Score weights — higher is a stronger lexical match against an alias/label.
_SCORE_WHOLE_PHRASE_BASE = 10  # + word_count
_SCORE_ALL_CONTENT_BASE = 5    # + matched_content_words
_SCORE_PARTIAL_BASE = 2        # + matched_content_words (>= 50% coverage)


# Intent-cue rules: when *competitors* both match the query, prefer the
# competitor whose cue phrase appears in the query. If no cue fires, both
# stay and the resolver records a clarification question.
DISAMBIGUATION_RULES: list[dict[str, Any]] = [
    {
        "competitors": {"dimension.customer_country", "dimension.ship_country"},
        "cues": {
            "dimension.ship_country": [
                "shipped to", "ship-to", "ship to", "destination",
                "shipping country", "delivered to", "ship country",
            ],
            "dimension.customer_country": [
                "customer country", "customer's country", "buyer country",
                "buyers", "customers in", "by customer country",
                "customer countries", "customer nation",
            ],
        },
    },
    {
        "competitors": {
            "dimension.order_date",
            "dimension.shipped_date",
            "dimension.required_date",
        },
        "cues": {
            "dimension.shipped_date": [
                "shipped on", "ship date", "shipped date", "date shipped",
            ],
            "dimension.required_date": [
                "required by", "due date", "needed by", "required date",
            ],
            "dimension.order_date": [
                "placed on", "ordered on", "order date", "order day",
                "when ordered",
            ],
        },
    },
    {
        "competitors": {"metric.distinct_order_count", "metric.order_line_count"},
        "cues": {
            "metric.order_line_count": [
                "line items", "order lines", "detail rows", "line item count",
            ],
            "metric.distinct_order_count": [
                "number of orders", "distinct orders", "orders placed",
                "order count",
            ],
        },
    },
]


def _normalize_query(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokens(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def _whole_phrase_in(phrase_norm: str, query_norm: str) -> bool:
    if not phrase_norm:
        return False
    pattern = r"\b" + re.escape(phrase_norm) + r"\b"
    return re.search(pattern, query_norm) is not None


def _phrase_score(
    phrase: str, query_norm: str, query_tokens: set[str]
) -> tuple[int, str]:
    phrase_clean = re.sub(r"[^a-z0-9 ]+", " ", phrase.lower())
    phrase_clean = re.sub(r"\s+", " ", phrase_clean).strip()
    if not phrase_clean:
        return 0, ""

    if _whole_phrase_in(phrase_clean, query_norm):
        return _SCORE_WHOLE_PHRASE_BASE + len(phrase_clean.split()), phrase_clean

    words = phrase_clean.split()
    content = [w for w in words if w not in STOPWORDS and len(w) >= 3]
    if not content:
        return 0, ""

    matched = [w for w in content if w in query_tokens]
    if not matched:
        return 0, ""

    if len(matched) == len(content):
        return _SCORE_ALL_CONTENT_BASE + len(content), " ".join(matched)

    if len(matched) / len(content) >= 0.5:
        return _SCORE_PARTIAL_BASE + len(matched), " ".join(matched)

    return 0, ""


def _score_term(
    term: dict[str, Any], query_norm: str, query_tokens: set[str]
) -> tuple[int, list[str]]:
    candidates = [term.get("canonical_label", "")] + list(term.get("aliases") or [])
    best_score = 0
    best_via: list[str] = []
    for phrase in candidates:
        if not phrase:
            continue
        score, matched = _phrase_score(phrase, query_norm, query_tokens)
        if score > best_score:
            best_score = score
            best_via = [phrase]
        elif score == best_score and score > 0 and phrase not in best_via:
            best_via.append(phrase)
    return best_score, best_via


def _fragment_for(term: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("postgres_aggregate_sql", "postgres_expression", "postgres_predicate"):
        if term.get(key):
            return key, term[key]
    return None, None


def _detect_candidates(
    terms: list[dict[str, Any]], query_norm: str
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query_norm)
    out: list[dict[str, Any]] = []
    for term in terms:
        score, via = _score_term(term, query_norm, query_tokens)
        if score > 0:
            out.append({"term": term, "score": score, "matched_via": via})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def _apply_disambiguation(
    candidates: list[dict[str, Any]], query_norm: str
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]], list[str]]:
    by_id = {c["term"]["id"]: c for c in candidates}
    removed: set[str] = set()
    notes: list[str] = []
    clar_questions: list[dict[str, Any]] = []
    needs_clar = False

    for rule in DISAMBIGUATION_RULES:
        present = [
            tid for tid in rule["competitors"]
            if tid in by_id and tid not in removed
        ]
        if len(present) < 2:
            continue

        cue_hits = {
            tid: sum(
                1 for cue in rule["cues"].get(tid, [])
                if _whole_phrase_in(cue.lower(), query_norm)
            )
            for tid in present
        }
        max_hits = max(cue_hits.values())

        if max_hits == 0:
            needs_clar = True
            labels = [by_id[t]["term"]["canonical_label"] for t in present]
            clar_questions.append({
                "competitor_term_ids": present,
                "competitor_labels": labels,
                "question": (
                    f"Did you mean {' or '.join(labels)}? "
                    "Please clarify which one applies."
                ),
            })
            notes.append(
                f"Ambiguous match across {present}; awaiting clarification."
            )
            continue

        winners = [tid for tid, h in cue_hits.items() if h == max_hits]
        losers = [tid for tid in present if tid not in winners]
        for tid in losers:
            removed.add(tid)
        notes.append(
            f"Disambiguated {present} -> kept {winners} via intent cues."
        )

    final = [c for c in candidates if c["term"]["id"] not in removed]
    return final, needs_clar, clar_questions, notes


def _collect_join_subgraph(
    matched_tables: set[str], fk_paths: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fk in fk_paths:
        if fk.get("from_table") in matched_tables or fk.get("to_table") in matched_tables:
            out.append(fk)
    return out


def _assemble_context(
    query: str,
    artifact: dict[str, Any],
    candidates: list[dict[str, Any]],
    needs_clarification: bool,
    clar_questions: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    matched_terms_out: list[dict[str, Any]] = []
    matched_tables: set[str] = set()

    for c in candidates:
        term = c["term"]
        frag_role, frag_sql = _fragment_for(term)
        for link in term.get("lineage") or []:
            tbl = link.get("table")
            if tbl:
                matched_tables.add(tbl)
        if term.get("primary_fact_table"):
            matched_tables.add(term["primary_fact_table"])
        matched_terms_out.append({
            "id": term["id"],
            "kind": term["kind"],
            "canonical_label": term["canonical_label"],
            "score": c["score"],
            "matched_via": c["matched_via"],
            "fragment_role": frag_role,
            "sql_fragment": frag_sql,
            "default_grain": term.get("default_grain"),
            "primary_fact_table": term.get("primary_fact_table"),
            "lineage": term.get("lineage") or [],
            "agent_join_notes": term.get("agent_join_notes"),
        })

    fk_paths = artifact.get("foreign_key_paths") or []
    join_paths = _collect_join_subgraph(matched_tables, fk_paths)

    fallback_used = not matched_terms_out
    if fallback_used:
        notes.append(
            "No semantic_term matched; downstream should use fallback_policy "
            "(schema+RAG retrieval and value retrieval)."
        )

    return {
        "query": query,
        "ontology_version": artifact.get("ontology_version"),
        "schema": artifact.get("schema"),
        "catalog_binding": artifact.get("catalog_binding"),
        "matched_terms": matched_terms_out,
        "joinable_tables": sorted(matched_tables),
        "join_paths": join_paths,
        "needs_clarification": needs_clarification,
        "clarification_questions": clar_questions,
        "fallback_used": fallback_used,
        "fallback_policy": artifact.get("fallback_policy") if fallback_used else None,
        "notes": notes,
    }


def resolve(query: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """End-to-end: detect → disambiguate → assemble semantic context."""
    query_norm = _normalize_query(query)
    candidates = _detect_candidates(artifact.get("terms") or [], query_norm)
    final, needs_clar, clar_q, notes = _apply_disambiguation(candidates, query_norm)
    return _assemble_context(query, artifact, final, needs_clar, clar_q, notes)


DEMO_QUERIES: list[str] = [
    "Show top 5 customer countries by revenue in 1997, shipped orders only",
    "Total freight by shipper company",
    "Number of orders by employee in 1997",
    "Revenue by category, exclude discontinued products",
    "How many active customers placed orders",
    "Sales by country",
    "Average line discount by supplier",
    "Stock units value at list price for non-discontinued products",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Resolve a natural-language query against semantic_terms.<schema>.json "
            "and emit a compact context package for the SQL generation node."
        ),
    )
    p.add_argument("query", nargs="?", help="Natural-language query (omit when using --demo).")
    p.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help=f"Path to semantic_terms JSON (default: {DEFAULT_ARTIFACT.relative_to(REPO_ROOT)}).",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="Run a built-in suite of example queries and print each context.",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of the default indented output.",
    )
    return p.parse_args()


def _print_context(ctx: dict[str, Any], compact: bool) -> None:
    if compact:
        print(json.dumps(ctx, ensure_ascii=False, default=str))
    else:
        print(json.dumps(ctx, indent=2, ensure_ascii=False, default=str))


def main() -> None:
    args = parse_args()
    artifact_path = args.artifact or DEFAULT_ARTIFACT
    if not artifact_path.is_file():
        print(f"Artifact not found: {artifact_path}", file=sys.stderr)
        sys.exit(1)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    if args.demo:
        for q in DEMO_QUERIES:
            ctx = resolve(q, artifact)
            print("=" * 88)
            print(f"Query: {q!r}")
            print("-" * 88)
            _print_context(ctx, args.compact)
            print()
        return

    if not args.query:
        print("Provide a query string or pass --demo.", file=sys.stderr)
        sys.exit(2)

    ctx = resolve(args.query, artifact)
    _print_context(ctx, args.compact)


if __name__ == "__main__":
    main()
