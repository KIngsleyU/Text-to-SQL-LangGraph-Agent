"""Shared loaders for Step 1 artifacts used by agent nodes and tools.

Currently only ``semantic_terms.northwind.json`` is exposed. As more Step 1
artifacts (vector indexes, sensitive-data tags, value retrieval cache, etc.)
come online they should be added here so every node/tool has one canonical
loader path with caching.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "data" / "artifacts"

SEMANTIC_TERMS_PATH = ARTIFACTS_DIR / "semantic_terms.northwind.json"


@lru_cache(maxsize=1)
def load_semantic_terms() -> dict[str, Any]:
    """Load and cache the Northwind ``semantic_terms`` artifact.

    Cached so the JSON is read at most once per process. After regenerating
    the artifact during a long-running session, call
    ``load_semantic_terms.cache_clear()`` to force a re-read.
    """
    return json.loads(SEMANTIC_TERMS_PATH.read_text(encoding="utf-8"))
