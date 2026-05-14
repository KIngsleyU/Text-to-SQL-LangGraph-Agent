"""Shared loaders for Step 1 artifacts used by agent nodes and tools.

``semantic_terms`` and ``schema_vector_index`` manifests are exposed here so
nodes/tools share one canonical loader path with caching.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "data" / "artifacts"

SEMANTIC_TERMS_PATH = ARTIFACTS_DIR / "semantic_terms.northwind.json"


def schema_vector_manifest_path(schema: str = "northwind") -> Path:
    """Path to ``schema_vector_index_manifest.<schema>.json`` (Step 1 Pinecone binding)."""
    return ARTIFACTS_DIR / f"schema_vector_index_manifest.{schema}.json"


@lru_cache(maxsize=8)
def load_schema_vector_manifest(schema: str = "northwind") -> dict[str, Any]:
    """Load manifest written by ``scripts/build_schema_index.py``.

    Cached per schema. After regenerating the manifest, call
    ``load_schema_vector_manifest.cache_clear()`` or restart the process.
    """
    path = schema_vector_manifest_path(schema)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {path.name}; run: python scripts/build_schema_index.py --schema {schema}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_semantic_terms() -> dict[str, Any]:
    """Load and cache the Northwind ``semantic_terms`` artifact.

    Cached so the JSON is read at most once per process. After regenerating
    the artifact during a long-running session, call
    ``load_semantic_terms.cache_clear()`` to force a re-read.
    """
    return json.loads(SEMANTIC_TERMS_PATH.read_text(encoding="utf-8"))
