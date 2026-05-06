# Step 1: Data and RAG Preparation Pipeline (Execution Template)

## Objective
Build a production-ready metadata and retrieval foundation for the Text-to-SQL LangGraph agent before SQL generation/orchestration work begins.

This step ensures the agent has deterministic schema understanding, value grounding, and connectivity abstractions, so downstream nodes avoid hallucinated joins, invalid filters, and dialect mistakes.

## Scope
- In scope:
  - Metadata ingestion and enrichment
  - Semantic ontology layer bootstrap
  - Schema RAG and pruning pipeline
  - Value/content retrieval pipeline
  - Connector registry and dialect detection setup
  - Security baseline for read-only access and sensitive data tagging
- Out of scope:
  - Full LangGraph orchestration graph
  - SQL generation/correction nodes
  - Final user-facing answer synthesis

## Inputs
- Source databases and/or warehouse endpoints
- Read-only credentials per source
- Existing business glossary or KPI definitions (if available)
- Table/column comments and data dictionaries
- Environment config (dev/staging/prod)

## Required Artifacts (Step 1 Outputs)
1. `metadata_catalog`
   - Tables, columns, data types, PK/FK, comments, sample values, row-count stats
2. `semantic_terms` mapping
   - Business term -> metric logic/table-column lineage/default filters
3. `schema_vector_index`
   - Embeddings index for schema/table retrieval
4. `value_vector_index`
   - Embeddings index for categorical values and key text-normalized literals
5. `connector_registry_config`
   - Source name -> dialect, connection profile, read-only policy, timeout policy
6. `sensitive_data_tags`
   - Column-level sensitivity labels (PII/PCI/PHI/etc.) for downstream masking policy
7. `retrieval_eval_report`
   - Retrieval quality metrics and benchmark queries for schema/value recall

## Implementation Plan

### 1) Metadata Ingestion and Schema Enrichment
Use connector introspection to extract enriched metadata beyond raw DDL.

Tasks:
- Pull table/column/type metadata for each source.
- Capture explicit PK/FK relationships and schema-level comments.
- Store representative sample values (bounded, safe sample size).
- Normalize naming variants (snake_case, camelCase, abbreviations).
- Persist metadata into a canonical catalog format (JSON/YAML or DB table set).

Acceptance criteria:
- At least 95% of target tables/columns are represented in catalog.
- PK/FK relationships are fully materialized for supported sources.
- Catalog entries include source, schema, table, column, type, description.

### 2) Semantic Ontology Layer
Map business language to deterministic technical logic.

Tasks:
- Define initial ontology for highest-priority business terms:
  - Example: "active customer", "gross revenue", "churned account"
- Attach each term to:
  - metric expression
  - required dimensions
  - default filters/time windows
  - source lineage (table/column)
- If available, integrate governed layer (dbt MetricFlow/Cube/dotML).
- For uncovered terms, define fallback to schema+RAG retrieval.

Acceptance criteria:
- Top 20 business terms documented with deterministic mappings.
- KPI terms have owner and definition version.
- Fallback behavior is explicitly defined when semantic coverage is missing.

### 3) Advanced Retrieval and Schema Pruning (RAG)
Implement coarse-to-fine retrieval to avoid prompt saturation.

Tasks:
- Build schema embeddings for table-level and column-level units.
- Add metadata filters before semantic search:
  - domain, source, schema, freshness tier
- Retrieve top-k relevant tables/columns per question.
- Assemble a compact "retrieved sub-schema" payload for downstream SQL generation.
- Add support for LinkAlign-style iterative narrowing where practical.

Acceptance criteria:
- Retrieval pipeline supports coarse filter + fine semantic ranking.
- Top-k retrieval latency meets target (e.g., < 500 ms on dev corpus).
- Prompt payload reduced significantly versus full schema dump.

### 4) Database Content and Cell Value Retrieval
Ground literals and WHERE clause values using verified data.

Tasks:
- Build value index for high-impact categorical/text columns.
- Include normalization map for aliases/synonyms:
  - "Texas" -> "TX", "United States" -> "US", etc.
- Implement `ValueRetrieval` component:
  - detect likely entities/filters in user query
  - fetch candidate canonical values with confidence score
- Return candidate values with provenance (source table/column).

Acceptance criteria:
- Retrieval resolves known format mismatches in validation set.
- Value precision on benchmark entity set meets threshold (define target).
- Returned values include confidence and source provenance.

### 5) Universal Connectivity Abstraction
Prepare execution-layer plumbing without full execution orchestration.

Tasks:
- Implement connector registry abstraction (SQLAlchemy or Ibis).
- Register each source with:
  - dialect
  - read-only credential profile
  - query timeout defaults
  - max row return policy
- Add dialect metadata for downstream prompt injection/transpilation.
- Optionally expose schema/value tools through MCP-compatible interfaces.

Acceptance criteria:
- Every source can be introspected through one unified interface.
- Dialect is correctly detected/stored for each registered source.
- Read-only permissions are validated by connection test.

### 6) Security and Governance Baseline (Start in Step 1)
Shift safety-left before query generation exists.

Tasks:
- Verify database roles are read-only and schema-scoped.
- Define allowlist of queryable schemas/tables.
- Tag sensitive columns (PII/PCI/PHI) in metadata catalog.
- Record policy rules for downstream:
  - max rows
  - timeout ceilings
  - blocked operations classes

Acceptance criteria:
- Read-only access verified for all configured sources.
- Sensitive columns are tagged and exportable in policy format.
- Governance policy document exists and is versioned.

### 7) Data Freshness, Versioning, and Re-index Policy
Prevent stale retrieval and brittle behavior after schema change.

Tasks:
- Assign version IDs to metadata snapshots.
- Define re-index triggers:
  - schema change
  - new tables
  - renamed columns
  - periodic refresh window
- Maintain change log between catalog versions.
- Support rollback to prior index snapshot if refresh fails.

Acceptance criteria:
- Every retrieval index is traceable to a metadata version.
- Re-index policy is automated or scriptable.
- Schema drift is detectable and reportable.

## Validation and Test Plan

### Retrieval quality checks
- Schema retrieval recall@k on curated benchmark prompts.
- Column retrieval precision@k for filter and join-heavy prompts.
- Value retrieval accuracy for alias/format normalization cases.

### Operational checks
- Index build time and index size tracked per source.
- Retrieval latency p50/p95 measured.
- Connector health check and timeout enforcement validated.

### Safety checks
- Read-only permission tests for each connector.
- Sensitive column tagging coverage report.
- Blocklist/allowlist policy integrity tests.

## Done Criteria (Step 1 Exit Gate)
Step 1 is complete only when all are true:
- Required artifacts are generated and versioned.
- Retrieval quality passes defined thresholds.
- Connector registry works across target sources in read-only mode.
- Sensitive data tagging and governance baseline are in place.
- Re-index/versioning workflow is documented and testable.
- A short handoff document exists for Step 2 (LangGraph node integration).

## Recommended Starter Stack (Default)
Use this unless there is a strong reason to diverge:
- Connectivity: SQLAlchemy registry first (optional Ibis extension later)
- Embeddings + vector index: OpenAI embeddings + FAISS
- Metadata store: Postgres (or equivalent durable store)
- Optional graph enrichment: Neo4j (phase-in after core retrieval is stable)
- Observability seed: retrieval logs + evaluation artifacts persisted from day 1

## Handoff to Step 2
Step 2 should consume these Step 1 outputs directly:
- `metadata_catalog` -> schema retrieval node
- `semantic_terms` -> semantic resolution node
- `schema_vector_index` and `value_vector_index` -> retrieval nodes
- `connector_registry_config` -> execution node plumbing
- `sensitive_data_tags` -> PII masking/output safety nodes

This keeps Step 2 focused on orchestration and reasoning, not data foundation rework.

