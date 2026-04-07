# Text-to-Verse
## Submit a question and receive a verse.
T2V uses a local LLM to retrieve a verse based on the question.

## Constraints
* The final product needs to be able to run on a the ODROID M2, using its NPU. This can be done using a special version of OpenVINO.
* All preprocessing steps can be done on a regular desktop computer.
* Response time should be minimized.
* Relevancy should be high.
* System memory is limited to 8GB.


## Challenges
* The bible contains 37774 verses, which is a large amount to search through.
* The bible is not textually similar to modern speech.
* The bible does not directly address modern topics, or conditions.

## Proposed Solution

Let's do the heavy lifting ahead of time. Instead of using a decoder model at run time, we can use an encoder. They are much faster and require less memory. We can have a list of verses along with embeddings, which are vectors that represent the meaning of the verse. We can use these embeddings to find verses that are similar to the query. In order to make the query more accurate, we can embed the verses as queries using modern language. That is to make the embeddings more similar to the query, we turn the verses into questions using modern language. For each verse, we will ask an LLM to modernize the verse. Then we will ask an LLM to generate several questions that the modern verse could answer. Those questions then get encoded into embedding vectors and stored with the original verse. Then we can use the same embedding model to encode the query and find the most similar verses.

### Setup
* Download a CSV file containing the bible verses from https://github.com/JaniTomati/bible-verse-data , via git clone git@github.com:JaniTomati/bible-verse-data.git .
* Install ollama.
* In ollama pull the preprocessing model (`gpt-oss:120b`), and the embedding model (`qwen3-embedding:0.6b`).
* Using [ollama qwen3-embedding:0.6b](https://ollama.com/library/qwen3-embedding:0.6b) for embedding.
* Using https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-fp16-ov for embedding on the ODROID M2.
  * Or maybe https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-int8-ov if fp16 is too slow.
* Make sure the ollama batch size is set to a reasonable value (e.g., 8)
* We can use SQLite DB to store intermediate artifacts, so that we can resume the process from where it left off if there is a crash or interruption.
* The preprocessing scripts will live in the `preprocessing` directory. We should use python3 best practices and use a virtual environment setup with `uv`.
* The final deployable binary will be written in Rust.

### Preprocessing
Each step is done in a separate script which produces its own intermediate output. All steps are computationally intensive, so we want to be able to resume them from where they left off. In order to be resistant to crashes and interruptions, we can use SQLite DBs to store intermediate artifacts. The first step of each script is to open the SQLite DB if it already exists, or create it if it doesn't. If it does not exist we create a table with all of the work we will need to do, and an empty table for the results. Each time we finish some work we will write the results to the results table and then remove it from the work table. When we start the process if the DB already exists and has work, we can resume the process from where it left off by fetching the next batch of work from the DB.

#### Configuration
In order to keep the configuration in sync between stages, there will be a common TOML configuration file used for all of the preprocessing stages scripts.

* LLM endpoints are configured independently for preprocessing (text generation) and embedding:
  * Each has its own `api_type` (`"ollama"` or `"openai"`), `endpoint`, `api_key_file`, and `model`.
  * This allows using different providers per step (e.g., Ollama for embeddings, OpenAI-compatible for generation).
  * API keys are stored in separate files referenced by `api_key_file`, not in the config directly.
  * When using OpenAI-compatible APIs, requests are issued in parallel. Ollama requests are sequential (Ollama handles its own batching).
* The output folder is a directory where the intermediate artifacts will be stored.
* Each stage lists the required configuration inputs.
* Prompt file templates
  * Each prompt file is a text file containing one or more prompt templates using Python f-string format.
  * Multiple prompts within a single file are separated by a line containing only `---` (triple dash).
  * Leading and trailing whitespace in each prompt section is trimmed.
  * Each stage has its own prompt template file (isolate, modernize, questionize).
  * A `preprocessing/prompts/README.md` documents the available substitution variables for each template.

#### Error Handling and Logging
All preprocessing stages use a shared error tracking module (`common/error_tracker.py`) that provides:

* **Separate retry counters per error class**:
  * **Network errors**: connection failures, timeouts, HTTP 5xx responses. Retried with exponential backoff.
  * **Validation errors**: LLM output that fails format or content validation (e.g., selected verse ID not in candidates, malformed JSON). Retried by re-prompting.
  * Each error class has its own configurable max retry count. When max retries for an error class are exhausted, the work item is skipped (left in the work queue for the next run).
* **Logging**:
  * All errors are logged to a log file in the output directory.
  * On each error, a running total of errors by class is displayed to the console (e.g., `[errors] network: 3 | validation: 7`).
  * Progress messages (items completed, items remaining) are logged at INFO level.
* **DB write failures** are treated as fatal — log and exit immediately.

#### LLM Output Format
All non-embedding LLM stages (isolate, modernize, questionize) must use JSON output mode via tool-calling / structured output. This ensures parseable, deterministic output from the LLM. The LLM client must assert that responses are valid JSON before passing them to the stage logic. Invalid JSON is treated as a validation error and retried.

#### Main script
There should be a main script that orchestrates the entire process. It should handle the initialization of the SQLite DB, the execution of each preprocessing step, and the final output generation. It should be able to resume from where it left off if there is a crash or interruption.

#### Isolating interesting verses
The point of this stage is to select the most interesting and orthogonal verses from the bible. Orthogonal verses are those that are not similar to each other in terms of content, style, or theme. For an initial approach, we can try to ask an LLM (`gpt-oss:120b`) to evaluate a list of random verses based on their content, style, and theme. We can give it X verses and ask it which one is the most fits our criteria. In an ideal world we would use a more sophisticated approach.

*NOTE* The inputs to this stage are the CSV file containing the bible verses, the preprocessing model, the batch size, the number of verses to select, the output folder, and a file containing the prompt template.
* Use `gpt-oss:120b` to isolate N (1000?) interesting verses. This can be done using batch processing.
  * Select X (9?) verses at random from the list of verse ids. Remove those ids from the list of verse ids to prevent duplication.
  * Prompt the LLM to select the most orthogonal verse from the verses.
  * Ensure that the selected verse is one of the ones selected in the previous step. If not repeat the same prompt.
  * Continue until we have N (1000?) verses.
*NOTE* This produces an intermediate artifact, which is an sqlite database containing a filtered version of the original CSV file.

#### Converting to modern english
*NOTE* The inputs to this stage are the database file containing the filtered bible verses, the preprocessing model, the output folder, the batch size, and a file containing the prompt template.
* Use `gpt-oss:120b` to convert the text of the verses into modern english. This can be done using batch processing.
  * For each verse in the database file it will be run with each prompt in the prompts file, and the output will be stored in a new table in the database. This table will contain the original verse ID, and the new modern english text.
  * We need to use an instruct capable model which can produce output free of surrounding text.
*NOTE* This produces an intermediate artifact, which is a SQLite DB extension of the filtered bible verses file, containing the tables from the previous steps and the modern english text in a new table.

#### Converting to verse-questions
The point of this stage is transform the a verse into a question. This, in theory, increases its similarity to the user's query, and makes it easier to find relevant verses.
*NOTE* The inputs to this stage are the SQLite database file containing the modern english text, the preprocessing model, the batch size, the output folder, and a file containing the prompt template.
* Use `gpt-oss:120b` to convert the modern english verse into a series of verse-questions. This can be done using batch processing.
  * For each verse in the database file it will be run with each prompt in the prompts file, and the output will be stored in a new table in the database. This table will contain the original verse ID, and the new verse-questions text.
  * We need to use an instruct capable model which can produce output free of surrounding text.
*NOTE* This produces an intermediate artifact, which is an SQLite database file, containing the tables from the previous steps and verse-questions in a new table.

#### Creating the vector database
*NOTE* The inputs to this stage are the SQLite database file containing the verse-questions, the embedding model, the batch size, the output folder.
* Use `qwen3-embedding:0.6b` to convert the verse-questions into vectors. This can be done using batch processing.
* Store the vectors in a local ChromaDB persistent database file (using the ChromaDB Python library directly, no server needed).
*NOTE* The final artifact produced by the preprocessing stage is a ChromaDB persistent database directory that can be deployed to the ODROID.

### Deployment
* A launch script (`scripts/launch_chromadb.sh` or similar) starts the ChromaDB server, loading the persistent database directory produced by preprocessing.
* The ChromaDB server must be running before the Rust binary can accept queries.

### Live Processing

The Rust binary (`src/`) implements the query engine and HTTP server.

#### Query Flow

1. **Embed** the user question into a vector via Ollama (`qwen3-embedding:0.6b`).
2. **Search ChromaDB** for the top `--top-n` (default 20) most similar verse-questions.
3. **Deduplicate** results by `verse_id` (multiple questions can map to the same verse).
4. **Filter** recently returned verses (in-memory recency tracker, up to 100 entries, 1-hour TTL).
5. **Select candidates for reranking**: take the top `--rerank-n` (default 4) from the filtered list. If all results were recent, fall back to the top `rerank_n` from the full deduplicated list.
6. **Rerank** the candidates using an LLM (via Ollama `/api/chat`). All candidates are scored in a single batch call. Raw scores are normalized to a 0–10 range and results are sorted by score descending.
7. **Sample** one verse using temperature-based sampling over the normalized rerank scores (higher score = more likely to be chosen).
8. **Record** the chosen verse in the recency tracker and return it.

#### Reranking

Reranking uses a configurable list of LLM models (default: `qwen3:4b,qwen3:1.7b`). The first model in the list is the default; clients can override the model per request. The reranking prompt sends all candidate verses in a single call and receives an array of scores, one per verse.

#### HTTP API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/query` | JSON body: `{"question": "...", "rerank": true\|false, "rerank_model": "..."}`. Returns verse with timing metrics. |
| `GET` | `/models` | Returns `{"rerank_models": [...]}` — the configured reranking model list. |
| `GET` | `/health` | Liveness probe. |
| `GET` | `/chat` | Embedded HTML chat interface for manual testing. |

#### Response Fields

The `/query` response includes:
* `verse_id`, `bible_verse`, `original_text`, `modern_text`, `matched_question`, `distance`
* `rerank_score` (normalized 0–10), `rerank_model` (omitted if reranking was skipped)
* `gen_q_embed_ms` — time to embed the question
* `embedding_db_lookup_ms` — time for ChromaDB similarity search
* `embedding_reranking_ms` — time for LLM reranking (omitted if reranking was skipped)
* `all_results` — the other candidates considered (excluding the chosen verse)
* `elapsed_ms` — total request time

#### CLI Flags (key ones)

| Flag | Default | Description |
|------|---------|-------------|
| `--top-n` | 20 | Results fetched from ChromaDB |
| `--rerank-n` | 4 | Candidates passed to the reranker |
| `--rerank-models` | `qwen3:4b,qwen3:1.7b` | Comma-separated reranking models |
| `--disable-rerank` | — | Skip reranking entirely |
| `--temperature` | 1.0 | Sampling temperature |
| `--max-recent` | 100 | Max verses in recency tracker |
| `--recent-ttl-secs` | 3600 | Recency TTL in seconds |
