# Neon Zero-Cost Data Population Plan (Step 1 Aligned)

I’ll quickly re-check both step1 docs, then scan current web sources for free options (Neon fit, public datasets, and ingestion patterns) and give you a concrete zero-cost data population plan that stays consistent with your Step 1 requirements.

Yes — I scanned current sources and checked against your step1 docs.  
Your Step 1 direction is consistent with best practice, and Neon is a strong choice for $0 right now.

## Neon Fit (No Budget)

Neon Free is good for your current phase because it gives you:

- PostgreSQL-compatible DB (perfect for SQLAlchemy/LangGraph prototyping)
- Branching (great for testing retrieval/index experiments safely)
- Autosuspend/scale-to-zero (keeps cost at zero)
- Enough compute/storage for early Text-to-SQL development

Caveats to design around:

- Free limits are tight (compute hours, storage, egress), so keep seed data small-medium
- Not ideal yet for heavy benchmark-scale loads (full TPC-H large scale, very large embeddings, etc.)

This aligns with your step1 emphasis on metadata first, retrieval quality, and read-only safety.

## Web-Validated Consistency With Step 1

Your Step 1 docs match what industry docs recommend:

- Metadata enrichment over raw DDL dump
- Semantic layer/term mapping (dbt Semantic Layer / MetricFlow style)
- Coarse-to-fine retrieval and schema pruning
- Value retrieval for literal normalization (Texas -> TX)
- Connector abstraction (SQLAlchemy/Ibis) + dialect awareness
- Governance/safety baseline from day one

So you are not off-track — you are on the right architecture.

## Best Free Data Options To Populate Neon (Now)

Use one of these immediately:

- Northwind (Postgres SQL dump): quickest multi-table relational starter (customers/orders/products/order_details).
- Synthea (synthetic healthcare CSVs): very rich relational graph with no real PII risk; excellent for complex joins.
- TPC-H dbgen (small scale factor): great for benchmark-style SQL and eval harness testing.

Suggested order:

1. Start with Northwind (fast win, clean relational structure)
2. Add Synthea subset (more realistic complexity + value-retrieval challenges)
3. Add small TPC-H later for performance/eval scenarios

## Concrete Step-1 Population Plan (for your repo)

1. Create Neon project + database
2. Load schema/data
   - Northwind SQL directly into Neon
   - For CSV datasets: create tables, COPY/bulk-load
3. Generate Step-1 artifacts
   - `metadata_catalog`
   - `semantic_terms`
   - `schema_vector_index`
   - `value_vector_index`
   - `connector_registry_config`
   - `sensitive_data_tags`
   - `retrieval_eval_report`
4. Run validation gates
   - schema recall@k
   - value precision on canonical literals
   - read-only enforcement and table allowlist
5. Freeze version snapshot
   - metadata/index version id + change log

If you want, next I can generate:

- a starter SQL schema+seed pack (Northwind-focused), and
- a Step-1 ingestion script layout (`ingest_metadata.py`, `build_schema_index.py`, `build_value_index.py`, `run_retrieval_eval.py`) tailored for Neon + your current project structure.

