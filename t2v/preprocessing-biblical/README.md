# Preprocessing
This is where all of the preprocessing scripts are located. There is a main script that can be used to preprocess the data start to finish.

## Prerequisites

- Python 3.11+
- A ChromaDB server running locally (default: `localhost:8000`)
- An API key for Claude saved to `~/.claude/burning_bush.txt`

## Setup

From the `preprocessing/` directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

Pipeline settings are in `config.toml`. Key sections:

- `[llm.preprocessing]` — LLM used for text processing stages (isolate, modernize, questionize). Currently configured for Claude Sonnet via the Anthropic API, rate-limited to 45 requests/minute.
- `[llm.embedding]` — LLM used for generating embeddings. Currently configured for a local Ollama model.
- `[pipeline]` — Batch size, output directory, input CSV path, and verse/question counts.
- `[chromadb]` — ChromaDB connection and collection settings.
- `[error_handling]` — Retry limits and delays for network/validation errors.
- `[prompts]` — Paths to prompt template files for each stage.

To use a local Ollama model for preprocessing instead of Claude, comment out the active `[llm.preprocessing]` section and uncomment the Ollama config below it.

## Running the Pipeline

```bash
python -m src.main
```

This runs all four stages in order:

1. **Isolate** — Selects interesting verses from the input CSV.
2. **Modernize** — Rewrites selected verses in modern language.
3. **Questionize** — Generates questions that each verse answers.
4. **Embed** — Generates embeddings and stores them in ChromaDB.

To use a different config file:

```bash
python -m src.main --config config.test.toml
```

Output and logs are written to the directory specified by `pipeline.output_dir` in the config (default: `output/`).
