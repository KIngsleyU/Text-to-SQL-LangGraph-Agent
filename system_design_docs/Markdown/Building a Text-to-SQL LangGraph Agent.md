# **Enterprise Architecture for Agentic Text-to-SQL: A Comprehensive LangGraph Framework**

The paradigm of translating natural language into Structured Query Language (Text-to-SQL) has transitioned from isolated, zero-shot generative models to sophisticated, multi-agent orchestrations. Early monolithic approaches—which injected entire database Data Definition Language (DDL) schemas into a Large Language Model (LLM) context window—suffered from severe structural failures in enterprise environments. These failures included context window saturation, reasoning drift across complex multi-hop joins, and an inability to recover from syntactical or logical errors.1 A single generation pass forces a model to perform intent understanding, logic planning, schema mapping, and syntax generation simultaneously, which frequently results in logic conflation or hallucinated relationships between tables.1  
To achieve production-grade reliability across complex, multi-database environments, a robust agentic architecture is required. This report synthesizes a unified, enterprise-ready master plan for building a state-of-the-art Text-to-SQL solution using LangGraph. The framework merges core state management, dynamic dialect routing, human-in-the-loop (HITL) ambiguity resolution, advanced semantic schema linking, taxonomy-guided error correction, and strict security governance. Furthermore, it details the deployment strategies necessary for universal connectivity—enabling the agent to interface seamlessly with local databases, cloud data warehouses, and distributed data lakes.2

## **Establishing System Scope, Trust Boundaries, and Evaluation Metrics**

Before engineering the graph architecture, the operational scope and success criteria of the agent must be defined. Modern enterprise queries often span databases containing over 1,000 columns with intricate nesting and cross-domain relationships, as evidenced by benchmarks like Spider 2.0.3

### **Operational Scope and Execution Boundaries**

The initial deployment of an enterprise Text-to-SQL agent must operate under a principle of least privilege, enforcing strict read-only analytics. The agent must be explicitly barred from executing destructive statements. The connectivity scope progresses in three distinct phases to ensure scalability and governance. The first phase establishes baseline capabilities on single relational systems. The second phase introduces multi-database routing, expanding to cloud data warehouses that require dialect-aware translation. The final phase addresses data lakes and lakehouse architectures. Raw object storage cannot be queried directly as a relational database; therefore, the agent must route queries through distributed compute engines, such as AWS Athena or Trino, to interact with the underlying data lake.2

### **Defining Multidimensional Success Metrics**

Standard exact-match metrics are insufficient for evaluating agentic SQL. Evaluation must encompass a multidimensional matrix inspired by the BIRD-SQL, Spider 2.0, and MultiSpider 2.0 benchmarks.6 Enterprise text-to-SQL is not merely a parsing problem; it is an execution-risk problem that demands rigorous benchmarking \[Plan 2\].

| Evaluation Metric | Description | Benchmark Origin |
| :---- | :---- | :---- |
| **Execution Accuracy (EX)** | Measures if the generated SQL returns the exact same result set as the gold standard query, evaluating functional correctness rather than string similarity. | Spider / BIRD |
| **Valid Efficiency Score (VES)** | Evaluates the computational efficiency of the generated query against the gold standard, penalizing queries that require excessive scanning or suboptimal joins. | BIRD-SQL |
| **Clarification Rate** | The frequency with which the agent correctly identifies underspecified queries and triggers a Human-in-the-Loop (HITL) intervention. | Agentic Operations |
| **Self-Correction Rate** | The percentage of initial SQL generation failures that are successfully resolved by the agent's internal error-handling and revision loops. | SQL-of-Thought |
| **Dialect Robustness** | The ability of the agent to generate syntactically flawless queries across diverse SQL dialects (e.g., T-SQL, Snowflake, BigQuery) from a single canonical intent. | Spider 2.0 |
| **Multilingual Competence** | The capacity of the agent to map non-English natural language requests to complex SQL without losing structural fidelity or intrinsic reasoning logic. | MultiSpider 2.0 |

State-of-the-art models evaluated on intrinsic reasoning alone often fail when exposed to the linguistic and dialectal variability present in enterprise environments. For example, on the MultiSpider 2.0 benchmark, leading models drop to a mere four percent execution accuracy when lacking collaborative agentic refinement, emphasizing the necessity of an iterative, multi-agent architecture.8

## **Core LangGraph Architecture and State Management**

LangGraph provides the orchestration layer necessary to convert a linear LLM chain into a cyclical, reasoning-driven graph.11 The architecture relies on three primary abstractions: State, Nodes, and Conditional Edges.13 The state acts as the shared memory across the agentic workflow, maintaining the conversation history, retrieved schemas, execution results, and error logs \[Plan 2\].

### **Designing the Shared State Schema**

In LangGraph, state is managed dynamically across "super-steps," which are batches of concurrent node executions.14 Following each super-step, reducers merge updates into the state, which is then checkpointed. While Pydantic models offer strict validation, LangGraph documentation and industry best practices strongly recommend utilizing Python's TypedDict combined with specific reducer functions, such as operator.add, to manage state transitions efficiently.15 This approach avoids silent data loss during partial updates and ensures consistency across complex multi-agent interactions.  
The following code details the comprehensive state schema required for an enterprise Text-to-SQL agent, incorporating the necessary fields for schema linking, dialect routing, and error correction.

Python

from typing import Annotated, TypedDict, List, Optional, Dict, Any  
from operator import add  
from langgraph.graph.message import add\_messages  
from langchain\_core.messages import BaseMessage

class TextToSQLState(TypedDict):  
    """  
    Central state object for the LangGraph multi-agent workflow.  
    Maintains all context required for schema linking, SQL generation,  
    execution, and error correction.  
    """  
    \# Conversation history and user inputs  
    messages: Annotated, add\_messages\]  
    original\_query: str  
      
    \# Metadata and Semantic Layer  
    target\_dialect: str  
    target\_database: str  
    retrieved\_schema: str  
    retrieved\_values: Dict\[str, List\[str\]\]  
      
    \# Generation and Execution Pipeline  
    candidate\_sql: Optional\[str\]  
    logical\_plan: Optional\[str\]  
    execution\_result: Optional\]\]  
      
    \# Error Handling and Correction  
    error\_history: Annotated\[List\[str\], add\]  
    correction\_attempts: int  
      
    \# Human-in-the-loop (HITL) Status  
    needs\_clarification: bool  
    clarification\_question: Optional\[str\]

### **Concurrency, Persistence, and State Migration**

LangGraph differentiates between short-term memory, which persists state through a single graph invocation or thread, and long-term memory, which persists across sessions.15 To support enterprise reliability and HITL workflows, thread-scoped state checkpointing is mandatory. This mechanism allows the graph to pause execution, await user clarification, and resume flawlessly from the exact point of interruption.17  
Utilizing PostgresSaver from the langgraph-checkpoint-postgres library ensures production-grade, fault-tolerant state persistence.19 Because checkpoint data grows linearly with usage as each state transition is recorded, operational best practices dictate treating checkpoints as operational logs rather than permanent storage. Deploying background jobs to aggressively prune older thread states prevents database bloat while maintaining necessary short-term continuity.21

Python

from langgraph.checkpoint.postgres import PostgresSaver  
from psycopg\_pool import ConnectionPool

def initialize\_checkpointer(db\_uri: str) \-\> PostgresSaver:  
    """  
    Initializes a highly concurrent PostgreSQL connection pool   
    to manage LangGraph state persistence.  
    """  
    pool \= ConnectionPool(conninfo=db\_uri, max\_size=20)  
    with pool.connection() as conn:  
        saver \= PostgresSaver(conn)  
        saver.setup()   
        return saver

Enterprise databases are subject to continuous evolution. Columns are renamed, and tables are deprecated. This introduces complexities in persisted agent states. If a graph is paused for HITL clarification and the underlying database schema changes before resumption, utilizing the older checkpoint may cause generation failures. Best practices mandate implementing schema versioning and lazy online migration. When loading an older checkpoint, the system should verify if the expected structure matches the current application schema.22

## **Metadata Ingestion and the Semantic Ontology Layer**

Feeding an LLM raw database schemas guarantees hallucinations when operating at scale. A Text-to-SQL agent can only be as accurate as its understanding of the underlying database. If the model does not clearly comprehend table existence, column nomenclature, or relational keys, it will resort to guessing.23 To mitigate reasoning drift, the system must bridge the semantic gap between abstract business language and raw relational artifacts.

### **Evolving Beyond Raw DDL with Semantic Layers**

Modern architectures deploy a semantic layer to map business terms directly to technical logic, providing the LLM with a highly curated ontology.4 For example, a query asking for "active customers" contains embedded business logic. Standard schema-linking forces the LLM to deduce the definition of "active," which often leads to inaccurate filtering.  
Constructing a semantic layer utilizing a property graph allows the agent to reason over relationships rather than raw DDL. The graph models entities such as Database, Schema, Table, and Column as nodes. Crucially, it maps these nodes via both explicit foreign keys (EXPLICIT\_FK\_TO) and implicit relationships (IMPLICIT\_RELATION\_TO) derived through vector similarity search.25 This empowers the agent to navigate massive enterprise schemas using multi-hop reasoning without hallucinating nonexistent joins \[Plan 1\].  
When integrating with mature semantic frameworks like dbt or Cube, the strategy shifts toward deterministic generation. For mission-critical key performance indicators (KPIs), the agent delegates query generation to the semantic engine's API (such as dbt's MetricFlow). Because the logic is codified within the semantic layer, the engine ensures that if the LLM successfully identifies the correct metric and dimension, the resulting query is guaranteed to be 100% accurate.4

| Schema Delivery Method | Analytical Capabilities | Primary Failure Modes |
| :---- | :---- | :---- |
| **Raw DDL Injection** | Simple, single-table retrieval. Ad hoc exploration. | Context window saturation; hallucinated joins; logic conflation. |
| **Vector RAG Retrieval** | Scalable across thousands of tables; dynamic context. | Missed implicit relationships; incomplete schema retrieval. |
| **Property Graph Ontology** | Multi-hop reasoning; entity resolution; complex joins. | High maintenance overhead; requires extensive metadata modeling. |
| **Governed Semantic Layer** | Deterministic KPI reporting; 100% execution accuracy. | Inflexible for unmodeled, highly ad-hoc exploratory questions. |

The recommended architecture implements an integrated workflow: the agent first checks if the semantic layer can answer a question. If coverage is unavailable, the agent falls back to raw Text-to-SQL augmented by vector retrieval, ensuring both accuracy for KPIs and flexibility for discovery.4

### **Database Content and Cell Value Retrieval**

A secondary failure mode occurs during the generation of WHERE clauses. An LLM may deduce the correct column but hypothesize the incorrect cell value format. For example, it might generate WHERE state \= 'Texas', whereas the database strictly stores the value as 'TX'. To resolve this discrepancy, the agent must perform instance or value retrieval.27  
This challenge is modeled as a Retrieval-Augmented Generation (RAG) problem. A vector database indexes categorical column values alongside database comments. Before SQL generation occurs, a ValueRetrievalNode identifies named entities within the user's prompt and fetches the exact database string formats, injecting these verified values into the LLM's context window.2

## **Universal Database Connectivity and Dialect Abstraction**

An enterprise agent must not be hardcoded to a single database. It requires a dynamic connectivity abstraction layer capable of routing queries to local environments, cloud warehouses, and distributed compute engines.

### **The SQLAlchemy Connector Registry Pattern**

To manage multiple underlying systems, the architecture employs a Connector Registry built upon SQLAlchemy. SQLAlchemy provides a uniform Object-Relational Mapping (ORM) and Core SQL expression layer capable of interacting with diverse database drivers, such as psycopg2 for PostgreSQL, pymysql for MySQL, or snowflake-sqlalchemy for Snowflake.32 This registry pattern abstracts connection pooling and dialect configuration away from the core agent logic.

Python

import sqlalchemy  
from typing import Dict, Any, List

class DatabaseConnectorRegistry:  
    """  
    A registry pattern to manage multiple database connections dynamically,  
    providing a unified interface for the LangGraph agent to execute SQL.  
    """  
    def \_\_init\_\_(self):  
        self.\_engines: Dict\[str, sqlalchemy.Engine\] \= {}  
        self.\_dialects: Dict\[str, str\] \= {}

    def register\_database(self, name: str, connection\_uri: str, dialect: str) \-\> None:  
        """  
        Registers a new database engine using SQLAlchemy.  
        Example URI: 'postgresql+psycopg2://user:pass@host/dbname'  
        """  
        engine \= sqlalchemy.create\_engine(connection\_uri, pool\_pre\_ping=True)  
        self.\_engines\[name\] \= engine  
        self.\_dialects\[name\] \= dialect

    def get\_engine(self, name: str) \-\> sqlalchemy.Engine:  
        """Retrieves an active engine instance by name."""  
        if name not in self.\_engines:  
            raise ValueError(f"Database '{name}' is not registered in the system.")  
        return self.\_engines\[name\]

    def get\_dialect(self, name: str) \-\> str:  
        """Retrieves the SQL dialect for a registered database."""  
        return self.\_dialects.get(name, "ansi")

    def execute\_read\_only(self, name: str, sql: str, timeout: int \= 30\) \-\> List\]:  
        """  
        Executes a query safely, strictly fetching results.  
        Includes timeout enforcement to prevent runaway queries.  
        """  
        engine \= self.get\_engine(name)  
        with engine.connect() as connection:  
            \# Set statement timeout for PostgreSQL as an example of execution safety  
            if self.\_dialects.get(name) \== "postgresql":  
                connection.execute(sqlalchemy.text(f"SET statement\_timeout \= {timeout \* 1000}"))  
              
            result \= connection.execute(sqlalchemy.text(sql))  
            return \[dict(row.\_mapping) for row in result\]

### **The Model Context Protocol (MCP) Integration**

While the SQLAlchemy registry functions internally, exposing these connections safely to distributed LLM agents is optimally achieved through the Model Context Protocol (MCP).36 MCP standardizes how models access data and APIs, decoupling context provisioning from the LLM interaction logic.38  
By constructing an MCP Server that wraps the DatabaseConnectorRegistry, the system provides a unified HTTP or stdio interface. The protocol separates capabilities into Resources (static data like schemas to be loaded into context), Tools (executable functions like querying the database), and Prompts (reusable templates).38 The agent interacts with the MCP server to request schema descriptions or execute strictly validated queries, completely abstracting the underlying database architecture from the LangGraph execution flow.40

## **The Human-in-the-Loop (HITL) Clarification Pipeline**

A critical flaw in standard Text-to-SQL systems is the propensity for models to hallucinate assumptions when faced with underspecified queries \[Plan 1, Plan 2\]. If a user asks, "Show me the top customers," the metric defining "top" (e.g., revenue, transaction count, tenure) is completely ambiguous.  
The LangGraph architecture enforces a strict Human-in-the-Loop (HITL) checkpoint to intercept these scenarios. An IntentClassificationNode evaluates the query against the retrieved semantic schema. If ambiguity is detected, the graph sets the state variable needs\_clarification \= True and interrupts execution.12

Python

from langchain\_core.messages import AIMessage

def intent\_and\_ambiguity\_node(state: TextToSQLState) \-\> TextToSQLState:  
    """  
    Evaluates the user query against the retrieved schema. If necessary parameters  
    are missing, formulates a precise clarification question.  
    """  
    query \= state\["original\_query"\]  
    schema \= state\["retrieved\_schema"\]  
      
    \# LLM invocation to detect ambiguity and identify missing dimensions  
    is\_ambiguous, missing\_context \= detect\_ambiguity\_llm(query, schema)  
      
    if is\_ambiguous:  
        clarification \= (  
            f"Your request for '{query}' is ambiguous. Specifically, it lacks definition "  
            f"around {missing\_context}. Could you clarify the time window and metric definition?"  
        )  
        return {  
            "needs\_clarification": True,  
            "clarification\_question": clarification,  
            "messages": \[AIMessage(content=clarification)\]  
        }  
          
    return {"needs\_clarification": False}

def route\_after\_intent(state: TextToSQLState) \-\> str:  
    """Conditional edge router determining if human intervention is required."""  
    if state\["needs\_clarification"\]:  
        return "human\_clarification"  
    return "schema\_linking"

LangGraph interrupts are specifically designed to pause execution for external input. Because the entire thread history is serialized into the PostgreSQL checkpointer, the system safely rests until the user provides the defining parameters, at which point the graph resumes execution without losing semantic context.17

## **SQL Generation and Advanced Schema Linking**

The core of the multi-agent reasoning pipeline is the SQL generation and validation loop. This follows the principles of the "SQL-of-Thought" framework, which decomposes the task into subproblem identification, Chain-of-Thought (CoT) query planning, and taxonomy-guided error modification.42

### **The LinkAlign Methodology**

Schema linking—the process of mapping natural language tokens to specific tables and columns—is a primary bottleneck in text-to-SQL translation. The LinkAlign framework addresses this challenge in large-scale scenarios by employing multi-round semantic enhanced retrieval and multi-agent debate.43  
Instead of a single LLM attempting to link the schema, the system isolates irrelevant information through iterative filtering. The agent extracts the precise tables and columns required, constructing a dense, highly relevant sub-schema. This rigorous extraction emulation, powered by reasoning-enhanced prompting techniques, scales schema linking to complex, redundant databases and drastically reduces the hallucination of foreign key joins.45

### **Dialect-Aware Translation and Query Planning**

Once the schema and cell values are linked, the SQLGenerationNode drafts a logical plan before generating the final query string. A critical element here is dialect injection. The routing module detects the target environment (e.g., PostgreSQL, Trino, Snowflake) and dynamically injects the correct SQL dialect rules into the prompt. This includes specific instructions regarding function mappings, quoting rules, date handling operations, and limit syntax \[Plan 2\]. Spanning diverse SQL dialects requires the model to generate in one canonical form and seamlessly adapt to the target engine, a necessity proven by the multi-dialect workflows in Spider 2.0.1

## **Validation, Execution, and Taxonomy-Guided Correction**

Before executing a query against a live database, the system must validate the Abstract Syntax Tree (AST). Executing malformed queries against data warehouses wastes valuable compute resources, incurs network latency, and poses severe security risks.

### **Offline AST Validation via SQLGlot**

The architecture integrates SQLGlot, an advanced, pure-Python SQL parser, transpiler, and optimizer.46 SQLGlot analyzes the LLM's output without requiring database instantiation. It is capable of detecting a variety of syntax errors, such as unbalanced parentheses, incorrect usage of reserved keywords, and dialect incompatibilities.46  
Furthermore, SQLGlot acts as a security sentry. By traversing the expression tree, the system confirms that the AST is exclusively a SELECT statement, blocking any destructive operations from ever reaching the database connector.48

Python

import sqlglot  
from sqlglot.errors import ParseError

def validate\_and\_transpile\_sql(raw\_sql: str, target\_dialect: str) \-\> dict:  
    """  
    Validates SQL syntax, transpiles to the target dialect, and enforces  
    read-only sandboxing using SQLGlot AST analysis.  
    """  
    try:  
        \# Transpile the raw string into the target dialect  
        transpiled\_sql \= sqlglot.transpile(raw\_sql, write=target\_dialect)  
          
        \# Parse into an AST to evaluate operation types  
        parsed\_ast \= sqlglot.parse\_one(transpiled\_sql, read=target\_dialect)  
          
        \# Security Guardrail: Reject non-SELECT operations  
        if not isinstance(parsed\_ast, sqlglot.exp.Select):  
            return {  
                "valid": False,   
                "error": "SECURITY VIOLATION: Query contains non-SELECT operations."  
            }  
              
        return {"valid": True, "sql": transpiled\_sql}  
          
    except ParseError as e:  
        return {"valid": False, "error": f"AST Syntax Error: {str(e)}"}

### **Taxonomy-Guided Dynamic Error Modification**

If SQLGlot validation fails, or if the database execution engine returns a logical error (such as an ambiguous column reference or a type mismatch during a join), the graph routes the state to the CorrectionNode.  
Traditional systems rely on static, execution-based correction loops that blindly feed errors back to the model. In contrast, the SQL-of-Thought architecture introduces taxonomy-guided dynamic error modification informed by in-context learning.42 A dedicated classification agent categorizes the failure into a fine-grained error taxonomy, such as syntax-malformed, schema-missing, join-missing-table, filter-type-mismatch, or agg-missing-group-by.51  
The LLM is then provided with the original question, the candidate SQL, the precise taxonomy classification, the execution error message, and a CoT prompt directing it to formulate a correction plan before regenerating the SQL.1

Python

def execution\_and\_correction\_node(state: TextToSQLState) \-\> TextToSQLState:  
    """  
    Attempts to validate and execute the SQL. If an error occurs,   
    classifies the error and updates the state for taxonomy-guided correction.  
    """  
    sql \= state\["candidate\_sql"\]  
    dialect \= state\["target\_dialect"\]  
    db\_name \= state\["target\_database"\]  
      
    \# 1\. Offline AST Validation  
    validation \= validate\_and\_transpile\_sql(sql, dialect)  
    if not validation\["valid"\]:  
        return {  
            "error\_history": \[validation\["error"\]\],  
            "correction\_attempts": state\["correction\_attempts"\] \+ 1  
        }  
          
    \# 2\. Live Database Execution  
    try:  
        \# Assumes registry is instantiated globally or passed via context  
        results \= db\_registry.execute\_read\_only(db\_name, validation\["sql"\])  
        return {"execution\_result": results}  
          
    except Exception as e:  
        error\_msg \= str(e)  
        \# In a full deployment, this invokes the taxonomy classification LLM  
        taxonomy\_class \= classify\_error\_taxonomy(error\_msg, sql)  
        detailed\_error \= f"\[{taxonomy\_class}\] {error\_msg}"  
          
        return {  
            "error\_history": \[detailed\_error\],  
            "correction\_attempts": state\["correction\_attempts"\] \+ 1  
        }

## **Security, Data Privacy, and PII Masking**

Enterprise deployment of LLM agents interacting with production data necessitates uncompromising security protocols. This is particularly vital concerning Personally Identifiable Information (PII) and regulatory compliance frameworks such as the European Union's GDPR or Canada's Personal Information Protection and Electronic Documents Act (PIPEDA).52

### **PIPEDA Compliance and Local Model Routing**

Under PIPEDA, organizations operate under a consent-based privacy regime. They must obtain explicit or implied consent for data collection and deploy robust internal policies to safeguard sensitive information against unauthorized disclosure.52 Passing raw database execution results containing PII to a hosted, public LLM API (such as OpenAI or Anthropic) for result summarization violates data residency mandates and introduces severe privacy risks, as LLMs operate as non-deterministic pattern matchers capable of memorizing and regurgitating sensitive data.55  
To maintain strict compliance, the architecture supports two primary mitigations:

1. **Local LLM Execution Integration**: Utilizing execution engines like Ollama to run Small Language Models (SLMs) such as Llama-3 or Mistral entirely on-premises or within a secure virtual private cloud (VPC). This guarantees that sensitive corporate data never traverses the public internet.57  
2. **Dynamic PII Masking Pipelines**: When frontier public LLMs must be leveraged for superior reasoning and multi-hop query planning, the execution results must be sanitized *before* they leave the secure environment.60

### **Implementing the PII Redaction Node**

The framework implements a sanitization layer immediately following successful database execution and prior to the final synthesis node. Utilizing frameworks similar to LlamaIndex's PIINodePostprocessor or custom implementations leveraging Microsoft Presidio, the system scans the raw execution payload. It detects entity patterns—such as Social Security Numbers, email addresses, healthcare information, and credit card digits—and replaces them with format-preserving, reversible tokens or static masks.61

Python

import re

def pii\_masking\_node(state: TextToSQLState) \-\> TextToSQLState:  
    """  
    Scans the execution\_result for PII and redacts it before sending  
    the payload to the public LLM for final natural language synthesis.  
    """  
    raw\_results \= state\["execution\_result"\]  
    if not raw\_results:  
        return state  
          
    sanitized\_results \=  
      
    \# Simplified regex for demonstration; production utilizes advanced NER  
    email\_pattern \= re.compile(r"^\[a-zA-Z0-9\_.+-\]+@\[a-zA-Z0-9-\]+\\.\[a-zA-Z0-9-.\]+$")  
      
    for row in raw\_results:  
        sanitized\_row \= {}  
        for key, value in row.items():  
            str\_val \= str(value)  
            \# Check column names or value patterns for sensitivity  
            if "email" in key.lower() or email\_pattern.match(str\_val):  
                sanitized\_row\[key\] \= ""  
            elif "ssn" in key.lower():  
                sanitized\_row\[key\] \= ""  
            else:  
                sanitized\_row\[key\] \= value  
        sanitized\_results.append(sanitized\_row)  
          
    return {"execution\_result": sanitized\_results}

Only the sanitized\_results are transmitted to the final SynthesisNode, where the LLM drafts a human-readable answer, fully isolating the proprietary data product from external exposure.62

## **Evaluation Harness and Operational Observability**

A text-to-SQL agent deployed without rigorous evaluation and deep observability is an operational liability. Industry surveys continuously emphasize that enterprise evaluation must go beyond classic single-turn tasks \[Plan 2\].

### **Formal Benchmarking Strategies**

The evaluation harness must be modeled against stringent academic benchmarks. While Spider measured baseline capability, the introduction of Spider 2.0 demands evaluation on massive enterprise schemas containing thousands of columns, requiring models to process extremely long contexts, perform intricate reasoning, and generate queries exceeding 100 lines.3  
Simultaneously, the framework must integrate BIRD-SQL benchmarking methodologies, measuring both execution accuracy and the Valid Efficiency Score (VES). It is not enough for an agent to retrieve the correct answer if the generated SQL requires scanning an entire multi-terabyte data warehouse due to a missed partition key.9 Regression tests must be continuously executed against private enterprise-style test sets to account for schema changes, renamed columns, and warehouse-specific edge cases \[Plan 2\].

### **Tracing, Telemetry, and Token Budgeting**

Due to the complex, cyclical nature of a multi-agent LangGraph system, standard application logging is inadequate. Integrating comprehensive telemetry tools, such as LangSmith, is mandatory for tracing node transitions, visualizing multi-step reasoning, tracking retrieval hit rates, and debugging execution failures in real-time.13  
Observability metrics must tightly monitor token budgets. Injecting massive schemas into context windows incurs prohibitive latency and financial costs. A dynamic routing policy must be implemented: the system prunes schemas using dense vector retrieval before context window insertion, and utilizes smaller, highly efficient models (like an 8B parameter local model) for simpler tasks such as intent classification, routing, or basic content retrieval. Larger, frontier models are strictly reserved for complex, multi-hop CoT SQL generation \[Plan 1\].

## **Complete Graph Orchestration and Deployment**

The final architectural phase unites the autonomous nodes, memory persistence, and connectivity abstractions into a cohesive, observable LangGraph application.  
The nodes are wired using LangGraph's StateGraph, mapping the exact flow of execution and implementing the conditional routing logic that dictates when to clarify, when to generate, when to execute, and when to retry based on the SQL-of-Thought taxonomy.

Python

from langgraph.graph import StateGraph, END

def route\_after\_execution(state: TextToSQLState) \-\> str:  
    """  
    Evaluates the execution outcome. Routes to PII masking on success,  
    routes to synthesis on terminal failure, or loops back to generation  
    for taxonomy-guided correction.  
    """  
    if state\["execution\_result"\] is not None:  
        return "pii\_masking"  
          
    if state\["correction\_attempts"\] \>= 3:  
        \# Terminal failure after max retries; fail gracefully  
        return "synthesis"   
          
    \# Trigger SQL-of-Thought correction loop  
    return "sql\_generation"

\# 1\. Initialize Graph with Shared State  
builder \= StateGraph(TextToSQLState)

\# 2\. Register Processing Nodes  
builder.add\_node("intent", intent\_and\_ambiguity\_node)  
\# HITL Node assumes external interruption, updates state upon resumption  
builder.add\_node("human\_clarification", lambda state: state)   
builder.add\_node("schema\_linking", schema\_linking\_node) \# Implements LinkAlign  
builder.add\_node("sql\_generation", sql\_generation\_node) \# Implements CoT and Dialect Rules  
builder.add\_node("execution\_correction", execution\_and\_correction\_node)  
builder.add\_node("pii\_masking", pii\_masking\_node)  
builder.add\_node("synthesis", synthesis\_node)

\# 3\. Define Entry Point and Edges  
builder.set\_entry\_point("intent")

\# Conditional routing based on ambiguity detection  
builder.add\_conditional\_edges("intent", route\_after\_intent)  
builder.add\_edge("human\_clarification", "intent") \# Re-evaluate intent after human input

\# Linear generation flow  
builder.add\_edge("schema\_linking", "sql\_generation")  
builder.add\_edge("sql\_generation", "execution\_correction")

\# Conditional routing for execution success or error correction  
builder.add\_conditional\_edges("execution\_correction", route\_after\_execution)

\# Finalization pipeline  
builder.add\_edge("pii\_masking", "synthesis")  
builder.add\_edge("synthesis", END)

\# 4\. Compile the Graph with Persistence and Interrupts  
checkpointer \= initialize\_checkpointer(DB\_URI)  
agentic\_sql\_graph \= builder.compile(  
    checkpointer=checkpointer,  
    interrupt\_before=\["human\_clarification"\] \# Pauses execution for HITL  
)

This compilation yields a production-ready computational graph. By integrating LangGraph interrupts, the state pauses seamlessly, persisting all dialogue history and linked schema variables to the PostgreSQL checkpointer. Once the application layer captures the user's clarification, the thread resumes, proceeding through the LinkAlign schema extraction and dialect-aware generation nodes without duplicating compute efforts.

## **Synthesis and Strategic Recommendations**

The transition from rudimentary text-to-SQL generation to an enterprise-grade agentic AI solution requires a profound architectural paradigm shift. It demands moving beyond simple prompts and embracing a rigorous software engineering mindset tailored for deterministic data pipelines. By leveraging the LangGraph framework, this architecture achieves stateful, multi-turn reasoning that accurately mirrors the iterative workflow of a human data analyst.  
The integration of a Semantic Ontology Layer and Database Value Retrieval ensures the LLM is equipped with accurate, deterministic context, eliminating the reasoning drift inherent in zero-shot raw DDL injection. The SQLAlchemy Connector Registry and the Model Context Protocol (MCP) successfully abstract away the complexities of interacting with diverse local and cloud environments, bridging the gap between relational databases and massive data lakes.  
Finally, the inclusion of strict Human-in-the-Loop checkpoints, offline AST validation via SQLGlot, and dynamic PII masking guarantees that the system is not only highly accurate—capable of conquering the rigorous evaluations of Spider 2.0 and BIRD-SQL—but also secure, trustworthy, and compliant with stringent global data privacy regulations. This orchestration represents the state-of-the-art in scalable, agentic data access.

#### **Works cited**

1. Architecting State-of-the-Art Text-to-SQL Agents for Enterprise Complexity \- Towards AI, accessed April 28, 2026, [https://pub.towardsai.net/architecting-state-of-the-art-text-to-sql-agents-for-enterprise-complexity-629c5c5197b8](https://pub.towardsai.net/architecting-state-of-the-art-text-to-sql-agents-for-enterprise-complexity-629c5c5197b8)  
2. Techniques for improving text-to-SQL | Google Cloud Blog, accessed April 28, 2026, [https://cloud.google.com/blog/products/databases/techniques-for-improving-text-to-sql](https://cloud.google.com/blog/products/databases/techniques-for-improving-text-to-sql)  
3. Spider 2.0, accessed April 28, 2026, [https://spider2-sql.github.io/](https://spider2-sql.github.io/)  
4. Semantic Layer vs. Text-to-SQL: 2026 Benchmark Update | dbt ..., accessed April 28, 2026, [https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)  
5. Multilingual Text-to-SQL: Benchmarking the Limits of Language Models with Collaborative Language Agents \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2509.24405v1](https://arxiv.org/html/2509.24405v1)  
6. \[2411.07763\] Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows \- arXiv, accessed April 28, 2026, [https://arxiv.org/abs/2411.07763](https://arxiv.org/abs/2411.07763)  
7. Why AI Agents Need a New Database Abstraction | by Li Shen | Mar, 2026 \- Medium, accessed April 28, 2026, [https://medium.com/@shenli3514/why-ai-agents-need-a-new-database-abstraction-88830244f3aa](https://medium.com/@shenli3514/why-ai-agents-need-a-new-database-abstraction-88830244f3aa)  
8. \[2509.24405\] Multilingual Text-to-SQL: Benchmarking the Limits of Language Models with Collaborative Language Agents \- arXiv, accessed April 28, 2026, [https://arxiv.org/abs/2509.24405](https://arxiv.org/abs/2509.24405)  
9. Bird SQL, accessed April 28, 2026, [https://bird-bench.github.io/](https://bird-bench.github.io/)  
10. Understanding the Effects of Noise in Text-to-SQL: An Examination of the BIRD-Bench Benchmark \- ACL Anthology, accessed April 28, 2026, [https://aclanthology.org/2024.acl-short.34/](https://aclanthology.org/2024.acl-short.34/)  
11. Text-to-SQL Was Just Step 1: Building an “Agentic” Data Analyst with LangGraph \- Medium, accessed April 28, 2026, [https://medium.com/@kapildevkhatik2/text-to-sql-was-just-step-1-building-an-agentic-data-analyst-with-langgraph-2bd9d2773d1c](https://medium.com/@kapildevkhatik2/text-to-sql-was-just-step-1-building-an-agentic-data-analyst-with-langgraph-2bd9d2773d1c)  
12. Build a custom SQL agent \- Docs by LangChain, accessed April 28, 2026, [https://docs.langchain.com/oss/python/langgraph/sql-agent](https://docs.langchain.com/oss/python/langgraph/sql-agent)  
13. LangGraph: Agent Orchestration Framework for Reliable AI Agents \- LangChain, accessed April 28, 2026, [https://www.langchain.com/langgraph](https://www.langchain.com/langgraph)  
14. Part 1: How LangGraph Manages State for Multi-Agent Workflows (Best Practices) \- Medium, accessed April 28, 2026, [https://medium.com/@bharatraj1918/langgraph-state-management-part-1-how-langgraph-manages-state-for-multi-agent-workflows-da64d352c43b](https://medium.com/@bharatraj1918/langgraph-state-management-part-1-how-langgraph-manages-state-for-multi-agent-workflows-da64d352c43b)  
15. The Architecture of Agent Memory: How LangGraph Really Works \- DEV Community, accessed April 28, 2026, [https://dev.to/sreeni5018/the-architecture-of-agent-memory-how-langgraph-really-works-59ne](https://dev.to/sreeni5018/the-architecture-of-agent-memory-how-langgraph-really-works-59ne)  
16. Mastering LangGraph State Management in 2025 \- Sparkco, accessed April 28, 2026, [https://sparkco.ai/blog/mastering-langgraph-state-management-in-2025](https://sparkco.ai/blog/mastering-langgraph-state-management-in-2025)  
17. State Management \- LangGraph \- Mintlify, accessed April 28, 2026, [https://mintlify.com/langchain-ai/langgraph/concepts/state](https://mintlify.com/langchain-ai/langgraph/concepts/state)  
18. Persistence \- Docs by LangChain, accessed April 28, 2026, [https://docs.langchain.com/oss/python/langgraph/persistence](https://docs.langchain.com/oss/python/langgraph/persistence)  
19. Mastering LangGraph Checkpointing: Best Practices for 2025 \- Sparkco, accessed April 28, 2026, [https://sparkco.ai/blog/mastering-langgraph-checkpointing-best-practices-for-2025](https://sparkco.ai/blog/mastering-langgraph-checkpointing-best-practices-for-2025)  
20. langgraph.checkpoint.postgres \- LangChain Reference Docs, accessed April 28, 2026, [https://reference.langchain.com/python/langgraph.checkpoint.postgres](https://reference.langchain.com/python/langgraph.checkpoint.postgres)  
21. Best practice for managing LangGraph Postgres checkpoints for short-term memory in production? : r/LangChain \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/LangChain/comments/1qna46j/best\_practice\_for\_managing\_langgraph\_postgres/](https://www.reddit.com/r/LangChain/comments/1qna46j/best_practice_for_managing_langgraph_postgres/)  
22. Feature Request: Support for State Schema Versioning & Migration in LangGraph.js · Issue \#536 · langchain-ai/langgraphjs \- GitHub, accessed April 28, 2026, [https://github.com/langchain-ai/langgraphjs/issues/536](https://github.com/langchain-ai/langgraphjs/issues/536)  
23. Best Practices for Building Robust Text-to-SQL Agents | by EzInsights AI | Medium, accessed April 28, 2026, [https://medium.com/@ezinsightsai/best-practices-for-building-robust-text-to-sql-agents-f81d4c4ea6b3](https://medium.com/@ezinsightsai/best-practices-for-building-robust-text-to-sql-agents-f81d4c4ea6b3)  
24. Semantic Layers are the missing piece for AI-Enabled Analytics \- Cube Blog, accessed April 28, 2026, [https://cube.dev/blog/semantic-layers-the-missing-piece-for-ai-enabled-analytics](https://cube.dev/blog/semantic-layers-the-missing-piece-for-ai-enabled-analytics)  
25. I built a Python tool to create a semantic layer over SQL for LLMs using a Knowledge Graph. Is this a useful approach? : r/dataengineering \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/dataengineering/comments/1n81kxy/i\_built\_a\_python\_tool\_to\_create\_a\_semantic\_layer/](https://www.reddit.com/r/dataengineering/comments/1n81kxy/i_built_a_python_tool_to_create_a_semantic_layer/)  
26. Semantic Layer as the Data Interface for LLMs \- dbt Labs, accessed April 28, 2026, [https://www.getdbt.com/blog/semantic-layer-as-the-data-interface-for-llms](https://www.getdbt.com/blog/semantic-layer-as-the-data-interface-for-llms)  
27. LinkAlign: Scalable Schema Linking for Real-World Large-Scale Multi-Database Text-to-SQL \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2503.18596v2](https://arxiv.org/html/2503.18596v2)  
28. Build a robust text-to-SQL solution generating complex queries, self-correcting, and querying diverse data sources \- AWS, accessed April 28, 2026, [https://aws.amazon.com/blogs/machine-learning/build-a-robust-text-to-sql-solution-generating-complex-queries-self-correcting-and-querying-diverse-data-sources/](https://aws.amazon.com/blogs/machine-learning/build-a-robust-text-to-sql-solution-generating-complex-queries-self-correcting-and-querying-diverse-data-sources/)  
29. RB-SQL: A Retrieval-based LLM Framework for Text-to-SQL \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2407.08273v1](https://arxiv.org/html/2407.08273v1)  
30. Architectural Patterns for Text-to-SQL: Leveraging LLMs for Enhanced BigQuery Interactions | by Arun Shankar | Google Cloud \- Medium, accessed April 28, 2026, [https://medium.com/google-cloud/architectural-patterns-for-text-to-sql-leveraging-llms-for-enhanced-bigquery-interactions-59756a749e15](https://medium.com/google-cloud/architectural-patterns-for-text-to-sql-leveraging-llms-for-enhanced-bigquery-interactions-59756a749e15)  
31. Optimal RAG for text-2-sql : r/LangChain \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/LangChain/comments/1e5pe1a/optimal\_rag\_for\_text2sql/](https://www.reddit.com/r/LangChain/comments/1e5pe1a/optimal_rag_for_text2sql/)  
32. Ultimate guide to SQLAlchemy library in python \- Deepnote, accessed April 28, 2026, [https://deepnote.com/blog/ultimate-guide-to-sqlalchemy-library-in-python](https://deepnote.com/blog/ultimate-guide-to-sqlalchemy-library-in-python)  
33. sqlalchemy/sqlalchemy: The Database Toolkit for Python \- GitHub, accessed April 28, 2026, [https://github.com/sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy)  
34. Connecting to SQL Database using SQLAlchemy in Python \- GeeksforGeeks, accessed April 28, 2026, [https://www.geeksforgeeks.org/python/connecting-to-sql-database-using-sqlalchemy-in-python/](https://www.geeksforgeeks.org/python/connecting-to-sql-database-using-sqlalchemy-in-python/)  
35. Engine Configuration — SQLAlchemy 2.1 Documentation, accessed April 28, 2026, [http://docs.sqlalchemy.org/en/latest/core/engines.html](http://docs.sqlalchemy.org/en/latest/core/engines.html)  
36. The Three Abstractions That Make AI Agents Real \- Vivek Haldar, accessed April 28, 2026, [https://vivekhaldar.com/articles/the-three-abstractions-that-make-ai-agents-real/](https://vivekhaldar.com/articles/the-three-abstractions-that-make-ai-agents-real/)  
37. ToolRegistry: A Protocol-Agnostic Tool Management Library for Function-Calling LLMs, accessed April 28, 2026, [https://arxiv.org/html/2507.10593v1](https://arxiv.org/html/2507.10593v1)  
38. modelcontextprotocol/python-sdk: The official Python SDK for Model Context Protocol servers and clients \- GitHub, accessed April 28, 2026, [https://github.com/modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)  
39. Practical Guide to MCP (Model Context Protocol) in Python \- DEV Community, accessed April 28, 2026, [https://dev.to/m\_sea\_bass/practical-guide-to-mcp-model-context-protocol-in-python-ijd](https://dev.to/m_sea_bass/practical-guide-to-mcp-model-context-protocol-in-python-ijd)  
40. Building an MCP Server for Databases: A Simple Guide for Beginners \- Medium, accessed April 28, 2026, [https://medium.com/@ambhargava.cts/building-an-mcp-server-for-databases-a-simple-guide-for-beginners-859ba77bc4c9](https://medium.com/@ambhargava.cts/building-an-mcp-server-for-databases-a-simple-guide-for-beginners-859ba77bc4c9)  
41. I Built a Model Context Protocol (MCP) Server to Let LLMs Insert & Query PostgreSQL Using Just Natur : r/Python \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/Python/comments/1klj6h8/i\_built\_a\_model\_context\_protocol\_mcp\_server\_to/](https://www.reddit.com/r/Python/comments/1klj6h8/i_built_a_model_context_protocol_mcp_server_to/)  
42. SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2509.00581v1](https://arxiv.org/html/2509.00581v1)  
43. \[EMNLP 2025 Main\] LinkAlign: Scalable Schema Linking for Real-World Large-Scale Multi-Database Text-to-SQL \- GitHub, accessed April 28, 2026, [https://github.com/Satissss/LinkAlign](https://github.com/Satissss/LinkAlign)  
44. LinkAlign: Scalable Schema Linking for Real-World Large-Scale Multi-Database Text-to-SQL \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2503.18596v1](https://arxiv.org/html/2503.18596v1)  
45. LinkAlign: Scalable Schema Linking for Real-World Large-Scale Multi-Database Text-to-SQL \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2503.18596v3](https://arxiv.org/html/2503.18596v3)  
46. tobymao/sqlglot: Python SQL Parser and Transpiler \- GitHub, accessed April 28, 2026, [https://github.com/tobymao/sqlglot](https://github.com/tobymao/sqlglot)  
47. SQL Parsing using SQLGlot. Structured query language (SQL) has… | by Anup Kumar Ray | Medium, accessed April 28, 2026, [https://medium.com/@anupkumarray/sql-parsing-using-sqlglot-ad8a3c7fac59](https://medium.com/@anupkumarray/sql-parsing-using-sqlglot-ad8a3c7fac59)  
48. Testing SQL with Python and SQLGlot \- Emil Sadek, accessed April 28, 2026, [https://emilsadek.com/blog/testing-sql/](https://emilsadek.com/blog/testing-sql/)  
49. Validating a query against a schema in Python without instantiating? \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/dataengineering/comments/1kdokwn/validating\_a\_query\_against\_a\_schema\_in\_python/](https://www.reddit.com/r/dataengineering/comments/1kdokwn/validating_a_query_against_a_schema_in_python/)  
50. SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction \- NeurIPS 2026, accessed April 28, 2026, [https://neurips.cc/virtual/2025/131690](https://neurips.cc/virtual/2025/131690)  
51. SQL-of-Thought: Structured Text-to-SQL Reasoning \- Emergent Mind, accessed April 28, 2026, [https://www.emergentmind.com/topics/sql-of-thought](https://www.emergentmind.com/topics/sql-of-thought)  
52. The Ultimate Guide to PIPEDA Compliance | Blog \- OneTrust, accessed April 28, 2026, [https://www.onetrust.com/blog/the-ultimate-guide-to-pipeda-compliance/](https://www.onetrust.com/blog/the-ultimate-guide-to-pipeda-compliance/)  
53. PIPEDA & CPPA: How the Canadian privacy laws impact your analytics \[Updated\], accessed April 28, 2026, [https://piwik.pro/blog/pipeda-analytics/](https://piwik.pro/blog/pipeda-analytics/)  
54. Canada in focus: Data protection and AI in Canada | ReedSmith, accessed April 28, 2026, [https://www.reedsmith.com/our-insights/blogs/viewpoints/102lo57/canada-in-focus-data-protection-and-ai-in-canada/](https://www.reedsmith.com/our-insights/blogs/viewpoints/102lo57/canada-in-focus-data-protection-and-ai-in-canada/)  
55. PII Sanitization Needed for LLMs and Agentic AI is Now Easier to Build | Kong Inc., accessed April 28, 2026, [https://konghq.com/blog/enterprise/building-pii-sanitization-for-llms-and-agentic-ai](https://konghq.com/blog/enterprise/building-pii-sanitization-for-llms-and-agentic-ai)  
56. MaskSQL: Safeguarding Privacy for LLM-Based Text-to-SQL via Abstraction \- arXiv, accessed April 28, 2026, [https://arxiv.org/html/2509.23459v2](https://arxiv.org/html/2509.23459v2)  
57. Chat With Your Database\! Build a Local SQL AI Agent to Query Databases (LangChain & Ollama) \- YouTube, accessed April 28, 2026, [https://www.youtube.com/watch?v=ay\_sYadoxgk](https://www.youtube.com/watch?v=ay_sYadoxgk)  
58. Smart SQL Agents with Local LLMs: Cross-Database Text-to-SQL in Production \- Medium, accessed April 28, 2026, [https://medium.com/@bravekjh/smart-sql-agents-with-local-llms-cross-database-text-to-sql-in-production-a94bc2a552cb](https://medium.com/@bravekjh/smart-sql-agents-with-local-llms-cross-database-text-to-sql-in-production-a94bc2a552cb)  
59. Local Text to SQL (MySQL) Query LLM App with LLAMA 3.2, Ollama and Langchain, accessed April 28, 2026, [https://www.youtube.com/watch?v=2JyeYMHwK5s](https://www.youtube.com/watch?v=2JyeYMHwK5s)  
60. LLM Data Masking: Silver Bullet or Double-Edged Sword? \- Salesforce, accessed April 28, 2026, [https://www.salesforce.com/blog/llm-data-masking/](https://www.salesforce.com/blog/llm-data-masking/)  
61. Protecting Sensitive and PII information in RAG with Elasticsearch and LlamaIndex, accessed April 28, 2026, [https://www.elastic.co/search-labs/blog/rag-security-masking-pii](https://www.elastic.co/search-labs/blog/rag-security-masking-pii)  
62. How do you handle PII or sensitive data when routing through LLM agents or plugin-based workflows? \- Reddit, accessed April 28, 2026, [https://www.reddit.com/r/LLM/comments/1naukel/how\_do\_you\_handle\_pii\_or\_sensitive\_data\_when/](https://www.reddit.com/r/LLM/comments/1naukel/how_do_you_handle_pii_or_sensitive_data_when/)  
63. bird-bench/BIRD-CRITIC-1: \[NeurIPS 2025 Main\] SWE-SQL: Illuminating LLM Pathways to Solve User SQL Issues in Real-World Applications \- GitHub, accessed April 28, 2026, [https://github.com/bird-bench/BIRD-CRITIC-1](https://github.com/bird-bench/BIRD-CRITIC-1)