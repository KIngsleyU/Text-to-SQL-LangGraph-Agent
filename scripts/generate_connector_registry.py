#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Generate the Step 1 **connector registry** artifact from environment configuration.

================================================================================
Why this script exists (maps to system_design_docs/step1)
================================================================================

The Execution Template requires artifact **``connector_registry_config``**:
  source name -> dialect, connection profile, read-only policy, timeout policy.

LangGraph / SQLAlchemy code should **not** hard-code hosts and passwords. This file
produces a **single JSON deliverable** that:

  * Identifies each logical data source (e.g. ``neon_text2sql_primary``).
  * Declares **PostgreSQL dialect** and **default schema** for Text-to-SQL grounding.
  * Points credentials at **environment variables** (never writes secrets to disk).
  * Records **governance knobs**: read-only intent, statement timeout, row cap,
    allowed schemas (allowlist), matching Step 1 §6 Security baseline.

You typically run this after ``export_metadata_catalog.py`` so both artifacts share
the same ``NEON_TEXT2SQL_URL`` and schema assumptions.

================================================================================
Security
================================================================================

**Passwords must not appear in committed JSON.** We parse ``NEON_TEXT2SQL_URL`` only to
extract non-secret fields (host, port, database, user, sslmode). The password is
referenced only indirectly via ``connection.uri_env_var``.

================================================================================
Usage
================================================================================

  python scripts/generate_connector_registry.py

  # Redact host/db/user from the JSON (keep only uri_env_var + policy) for public repos:
  python scripts/generate_connector_registry.py --redact

  # Optional overrides:
  DEFAULT_SCHEMA=northwind python scripts/generate_connector_registry.py

Environment:

  NEON_TEXT2SQL_URL   Required. PostgreSQL URI (postgresql://... or postgres://...).

Optional environment:

  CONNECTOR_REGISTRY_PATH     Output path (default: data/artifacts/connector_registry.json)
  DEFAULT_SCHEMA              Default search schema for this project (default: northwind)
  STATEMENT_TIMEOUT_MS        e.g. 30000
  MAX_ROWS_RETURNED           e.g. 5000
  ALLOWED_SCHEMAS             Comma-separated, default: northwind

See also: ``export_metadata_catalog.py`` for the companion ``metadata_catalog`` artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Defaults aligned with Step 1 governance / Neon typical usage
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT = Path("data") / "artifacts" / "connector_registry.json"
SPEC_VERSION = "1.0"


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format (for snapshot discipline)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_db_url() -> str:
    """Load ``NEON_TEXT2SQL_URL`` from .env / environment."""
    load_dotenv()
    url = os.getenv("NEON_TEXT2SQL_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Missing NEON_TEXT2SQL_URL. Set it in .env or the environment."
        )
    if not (url.startswith("postgresql://") or url.startswith("postgres://")):
        raise RuntimeError(
            "NEON_TEXT2SQL_URL must be a PostgreSQL URI (postgresql:// or postgres://)."
        )
    return url


def _parse_postgres_url(url: str) -> dict[str, Any]:
    """
    Parse a PostgreSQL connection URI into a structured, non-secret dict.

    Password is **never** returned; callers must use the env var for auth.

    Supports standard libpq-style URIs, including query params like sslmode=require.
    """
    parsed = urlparse(url)
    # Username/password in netloc: user:pass@host:port
    username = unquote(parsed.username) if parsed.username else None
    hostname = parsed.hostname
    port = parsed.port or 5432
    database = (parsed.path or "").lstrip("/") or None

    q = parse_qs(parsed.query)
    sslmode_list = q.get("sslmode") or q.get("ssl-mode")
    sslmode = sslmode_list[0] if sslmode_list else None

    return {
        "host": hostname,
        "port": port,
        "database": database,
        "username": username,
        "sslmode": sslmode,
        "query_params": {k: v[0] if len(v) == 1 else v for k, v in q.items()},
    }


def _try_git_sha() -> str | None:
    """Best-effort short git SHA for traceability (optional)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def parse_cli() -> argparse.Namespace:
    """Parse optional ``--redact`` for safer commits to public repositories."""
    p = argparse.ArgumentParser(description="Generate data/artifacts/connector_registry.json")
    p.add_argument(
        "--redact",
        action="store_true",
        help="Omit parsed host, port, database, username from output; "
        "keep uri_env_var and governance only.",
    )
    return p.parse_args()


def build_registry(*, redact_parsed_fields: bool = False) -> dict[str, Any]:
    """
    Build the full connector registry document.

    This structure is stable for Step 2: execution nodes can load this JSON and
    resolve ``uri_env_var`` at runtime (never embed passwords in repo files).
    """
    db_url = _load_db_url()
    parsed = _parse_postgres_url(db_url)

    default_schema = os.getenv("DEFAULT_SCHEMA", "northwind").strip()
    timeout_ms = int(os.getenv("STATEMENT_TIMEOUT_MS", "30000"))
    max_rows = int(os.getenv("MAX_ROWS_RETURNED", "5000"))
    allowed_raw = os.getenv("ALLOWED_SCHEMAS", default_schema).strip()
    allowed_schemas = [s.strip() for s in allowed_raw.split(",") if s.strip()]

    source_id = os.getenv("CONNECTOR_SOURCE_ID", "neon_text2sql_primary").strip()

    connection_block: dict[str, Any] = {
        "uri_env_var": "NEON_TEXT2SQL_URL",
        "password_storage": "embedded_in_uri_env_var_only",
    }
    if not redact_parsed_fields:
        connection_block.update(
            {
                "host": parsed["host"],
                "port": parsed["port"],
                "database": parsed["database"],
                "username": parsed["username"],
                "sslmode": parsed["sslmode"],
            }
        )
    else:
        connection_block["parsed_fields_redacted"] = True
        connection_block["redaction_note"] = (
            "host/port/database/username omitted; resolve via NEON_TEXT2SQL_URL at runtime."
        )

    return {
        "spec_version": SPEC_VERSION,
        "generated_at": _utc_now_iso(),
        "git_commit_short": _try_git_sha(),
        "documentation": (
            "Step 1 connector registry: dialect + policy + env-based credentials. "
            "Secrets live only in NEON_TEXT2SQL_URL (or split env vars in future)."
        ),
        "sources": [
            {
                "id": source_id,
                "kind": "postgresql",
                "dialect": "postgresql",
                "engine_hint": "sqlalchemy + psycopg (async: asyncpg)",
                "connection": connection_block,
                "routing": {
                    "default_schema": default_schema,
                    "allowed_schemas": allowed_schemas,
                    "search_path_recommendation": allowed_schemas + ["public"],
                },
                "governance": {
                    "read_only_intent": True,
                    "statement_timeout_ms": timeout_ms,
                    "max_rows_returned": max_rows,
                    "blocked_statement_classes": [
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "DROP",
                        "TRUNCATE",
                        "ALTER",
                        "CREATE",
                        "GRANT",
                        "REVOKE",
                    ],
                    "notes": (
                        "Enforce read-only at the DB role level in Neon; this JSON expresses "
                        "application-side policy for the agent pipeline."
                    ),
                },
            }
        ],
    }


def main() -> int:
    """Write ``connector_registry.json`` and print the output path."""
    try:
        cli = parse_cli()
        doc = build_registry(redact_parsed_fields=cli.redact)
        out_path = Path(
            os.getenv("CONNECTOR_REGISTRY_PATH", str(DEFAULT_OUTPUT))
        ).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Connector registry written: {out_path}")
        print(f"  sources: {len(doc['sources'])}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI convenience
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
