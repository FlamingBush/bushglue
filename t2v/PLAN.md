# Text-to-Verse Implementation Plan

## Overview

Two major phases: **Preprocessing** (Python, on a desktop) and **Live Processing** (Rust, on the ODROID M2 with NPU).

Preprocessing uses `gpt-oss:120b` via Ollama for text generation and `qwen3-embedding:0.6b` for embeddings. Live processing uses the Qwen3-Embedding-0.6B model (via OpenVINO) to match user queries against pre-computed embeddings stored in ChromaDB.

---

## Project Structure

```
text-to-verse/
  PROJECT.md
  PLAN.md
  preprocessing/              # Python (uv venv)
    pyproject.toml
    config.toml               # Shared TOML config for all stages
    prompts/
      README.md               # Documents available substitution variables
      isolate.txt             # Prompt template for selecting orthogonal verses
      modernize.txt           # Prompt template for modernizing verses
      questionize.txt         # Prompt template for generating verse-questions
    src/
      common/
        db.py                 # SQLite work queue pattern
        llm.py                # Ollama/OpenAI API client (JSON/tool-calling output)
        config.py             # TOML config loader
        error_tracker.py      # Error tracking, retry logic, logging
      stages/
        01_isolate.py         # Isolate interesting/orthogonal verses
        02_modernize.py       # Convert verses to modern english
        03_questionize.py     # Convert modern verses to questions
        04_embed.py           # Generate embeddings and store in ChromaDB
      main.py                 # Orchestrator — runs all stages in sequence
  scripts/
    launch_chromadb.sh          # Starts ChromaDB server with the persistent DB
  src/                        # Rust — final deployable binary
    main.rs
  Cargo.toml
```

---

## Phase 0: Setup

- [ ] Set up Python project with `uv` in `preprocessing/`
- [ ] Create `config.toml` with shared configuration
- [ ] Clone bible verse data: `git clone git@github.com:JaniTomati/bible-verse-data.git`
- [ ] Install Ollama, pull `gpt-oss:120b` and `qwen3-embedding:0.6b`
- [ ] Configure Ollama batch size
- [ ] Draft prompt templates in `prompts/`
- [ ] Create `prompts/README.md` documenting substitution variables

### config.toml structure

```toml
[llm]
endpoint = "http://localhost:11434"   # Ollama or OpenAI-compatible
api_key = ""                          # Optional, for OpenAI-compatible endpoints
preprocessing_model = "gpt-oss:120b"
embedding_model = "qwen3-embedding:0.6b"

[pipeline]
batch_size = 8
output_dir = "output"
csv_path = "bible-verse-data/bible_verses.csv"
num_verses_to_select = 1000          # N for isolation stage
isolation_sample_size = 9            # X verses per isolation round
num_questions_per_verse = 3          # Y questions per verse

[chromadb]
persist_dir = "output/chromadb"       # Local persistent DB directory (preprocessing)
server_host = "localhost"             # ChromaDB server host (production)
server_port = 8000                    # ChromaDB server port (production)
collection_name = "verse_embeddings"

[error_handling]
max_network_retries = 5
max_validation_retries = 3
retry_base_delay_seconds = 2.0
max_retry_delay_seconds = 30.0
log_file = "preprocessing.log"        # Written to output_dir

[prompts]
isolate = "prompts/isolate.txt"
modernize = "prompts/modernize.txt"
questionize = "prompts/questionize.txt"
```

---

## Phase 1: Python Preprocessing Infrastructure

### 1.1 SQLite Work Queue (`common/db.py`)
- [ ] Reusable work queue pattern:
  - Initialize DB with work table + results table
  - Populate work items (idempotent — skip if already populated)
  - Fetch next batch from work table
  - Complete item: write result + remove from work in a single transaction
  - Check if all work is done
- [ ] All operations use transactions for crash safety

### 1.2 LLM Client (`common/llm.py`)
- [ ] Support Ollama API (`/api/chat` with tool-calling for generation, `/api/embeddings` for embeddings)
- [ ] Support OpenAI-compatible API (`/v1/chat/completions` with tool-calling, `/v1/embeddings`)
- [ ] All non-embedding calls use JSON output via tool-calling / structured output
- [ ] Assert valid JSON response before returning to caller; invalid JSON raises a validation error
- [ ] Configurable via `config.toml`
- [ ] Delegates retry logic to `error_tracker`

### 1.3 Error Tracker (`common/error_tracker.py`)
- [ ] Separate retry counters per error class:
  - **Network errors**: connection failures, timeouts, HTTP 5xx. Exponential backoff.
  - **Validation errors**: malformed JSON, invalid LLM output content. Immediate re-prompt.
- [ ] Configurable max retries per class (from `config.toml`)
- [ ] When max retries exhausted for a work item, skip it (leave in work queue for next run)
- [ ] Log every error to a log file in the output directory
- [ ] On each error, print a running total of errors by class to console (e.g., `[errors] network: 3 | validation: 7`)
- [ ] DB write failures are fatal — log and exit immediately

### 1.4 Config Loader (`common/config.py`)
- [ ] Load and validate `config.toml`
- [ ] Provide typed access to configuration values
- [ ] Load prompt templates from files using Python f-string format
- [ ] Split multiple prompts per file on `---` separator lines; trim whitespace from each section

---

## Phase 2: Preprocessing Pipeline Stages

### 2.1 Stage 1 — Isolate Interesting Verses (`stages/01_isolate.py`)
- [ ] Input: CSV file, `gpt-oss:120b` (JSON output via tool-calling), batch size, N (num verses), prompt template
- [ ] Parse CSV into verse records
- [ ] Init SQLite DB with all verse IDs as work
- [ ] Loop until N verses selected:
  - Sample X random verse IDs from remaining work
  - Prompt LLM to pick the most interesting/orthogonal verse
  - Validate response; retry on invalid output
  - Store selected verse in results, remove all X from work
- [ ] Output: SQLite DB with `selected_verses` table

### 2.2 Stage 2 — Modernize Verses (`stages/02_modernize.py`)
- [ ] Input: SQLite DB (from stage 1), `gpt-oss:120b` (JSON output via tool-calling), prompt template
- [ ] For each selected verse, run prompt to convert to modern english
- [ ] Store in `modern_verses` table (verse_id, modern_text)
- [ ] Work queue pattern for resumability

### 2.3 Stage 3 — Generate Verse-Questions (`stages/03_questionize.py`)
- [ ] Input: SQLite DB (from stage 2), `gpt-oss:120b` (JSON output via tool-calling), prompt template
- [ ] For each modern verse, generate questions the verse could answer
- [ ] Store in `verse_questions` table (verse_id, questions_text)
- [ ] Work queue pattern for resumability

### 2.4 Stage 4 — Embed and Store in ChromaDB (`stages/04_embed.py`)
- [ ] Input: SQLite DB (from stage 3), `qwen3-embedding:0.6b`
- [ ] For each verse-question, generate embedding vector via Ollama
- [ ] Store vectors in a local ChromaDB persistent directory (using ChromaDB Python library directly, no server)
- [ ] Original verse_id stored as metadata
- [ ] Output: ChromaDB persistent directory deployable to ODROID

### 2.5 Orchestrator (`main.py`)
- [ ] Run stages 1–4 sequentially
- [ ] Each stage checks if its work is already complete before starting
- [ ] Progress reporting

---

## Phase 3: Rust Live Processing Binary

- [x] Connect to ChromaDB server (custom HTTP client, `src/chromadb.rs`)
- [x] **CLI one-shot mode**: accept a question, return a verse, exit
- [x] **HTTP server mode** (Axum): `POST /query`, `GET /models`, `GET /health`, `GET /chat`
- [x] Encode question via Ollama (`qwen3-embedding:0.6b`) — `src/embedder.rs`
- [x] Semantic search via ChromaDB: fetch top `--top-n` (default 20) verse-questions
- [x] Deduplicate results by `verse_id` before reranking
- [x] **Recency tracking**: in-memory deque, up to 100 entries, 1-hour TTL; filtering happens before reranking so the reranker only sees eligible candidates
- [x] **LLM reranking** (`src/reranker.rs`): single batch call to Ollama, configurable model list (`--rerank-models`), scores normalized to 0–10 range
- [x] Temperature-based sampling over normalized rerank scores
- [x] Per-request timing: `gen_q_embed_ms`, `embedding_db_lookup_ms`, `embedding_reranking_ms`
- [x] **Embedded chat UI** (`src/chat.html`): model selector dropdown, rerank toggle, JSON inspector
- [x] `GET /models` endpoint exposes configured reranking model list to UI/clients
- [ ] Encode question using OpenVINO (NPU-accelerated) for ODROID M2 deployment — currently uses Ollama

---

## Phase 4: ODROID M2 Deployment

- [ ] Create `scripts/launch_chromadb.sh` to start ChromaDB server loading the persistent DB directory
- [ ] Integrate OpenVINO Qwen3-Embedding-0.6B (fp16 or int8 depending on performance)
  - fp16: https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-fp16-ov
  - int8: https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-int8-ov
- [ ] Deploy ChromaDB persistent directory + launch script to ODROID
- [ ] Optimize memory to fit within 8GB
- [ ] Package as standalone binary
- [ ] Minimize response time

---

## Immediate Next Steps

1. Initialize `preprocessing/` with `uv` and `pyproject.toml`
2. Create `config.toml` skeleton
3. Create `prompts/README.md` and draft prompt templates
4. Implement `common/error_tracker.py` (error classes, retry logic, logging)
5. Implement `common/db.py` (work queue pattern)
6. Implement `common/llm.py` (Ollama API client with JSON/tool-calling output)
7. Implement Stage 1 (isolate verses) as proof of concept
