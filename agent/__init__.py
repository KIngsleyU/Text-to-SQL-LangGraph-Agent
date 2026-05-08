"""Text-to-SQL LangGraph agent package.

Importing anything from ``agent.*`` first ensures the repo root is on
``sys.path`` so that ``scripts.resolve_semantic_context`` (and other
top-level modules) can be imported by nodes/tools without each one
re-bootstrapping the path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
