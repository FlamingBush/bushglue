import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted, ValidationError
from t2v_common.llm import LLMClient

logger = logging.getLogger(__name__)

WORK_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
    "text": "TEXT",
    "description": "TEXT",
    "source_description": "TEXT",
}

RESULTS_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
    "text": "TEXT",
    "description": "TEXT",
    "source_description": "TEXT",
    "modern_text": "TEXT",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "modern_text": {"type": "string"},
    },
    "required": ["modern_text"],
}


def _load_ingested_snippets(config) -> list[dict]:
    """Load ingested snippets from Stage 1's ingest.db results."""
    ingest_db_path = config.pipeline.output_dir / "ingest.db"
    if not ingest_db_path.exists():
        raise FileNotFoundError(
            f"Stage 1 database not found: {ingest_db_path}. Run Stage 1 first."
        )

    ingest_queue = WorkQueue(
        ingest_db_path,
        work_schema=WORK_SCHEMA,
        results_schema={
            "snippet_id": "TEXT PRIMARY KEY",
            "text": "TEXT",
            "description": "TEXT",
            "source_description": "TEXT",
        },
    )

    results = ingest_queue.fetch_all_results()
    ingest_queue.close()

    if not results:
        raise RuntimeError("Stage 1 has no ingested snippets. Run Stage 1 first.")

    logger.info("Loaded %d snippets from Stage 1", len(results))
    return results


def _process_snippet(
    snippet: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    prompt_template: str,
) -> dict | None:
    """Process a single snippet through the LLM. Returns result dict or None on failure."""
    prompt = prompt_template.format(
        item_description=snippet["description"],
        item_text=snippet["text"],
    )

    def do_generate():
        result = llm.generate(prompt, RESPONSE_SCHEMA)
        modern_text = result.get("modern_text")
        if not modern_text or not isinstance(modern_text, str):
            raise ValidationError(f"LLM returned invalid modern_text: {result}")
        return result

    try:
        result = tracker.retry(do_generate)
    except RetriesExhausted as e:
        logger.error(
            "Skipping snippet %s after retries exhausted: %s",
            snippet["snippet_id"],
            e,
        )
        return None

    return {
        "snippet_id": snippet["snippet_id"],
        "text": snippet["text"],
        "description": snippet["description"],
        "source_description": snippet["source_description"],
        "modern_text": result["modern_text"],
    }


def run(config) -> None:
    """Stage 2: Convert snippets to modern English (or pass through if disabled)."""
    db_path = config.pipeline.output_dir / "modernize.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    snippets = _load_ingested_snippets(config)

    # Check if already complete
    if queue.results_count() >= len(snippets):
        logger.info(
            "Stage 2 already complete: %d snippets modernized", queue.results_count()
        )
        return

    if not config.pipeline.modernize_enabled:
        # Pass-through: copy original text as modern_text without LLM call
        logger.info(
            "Modernize disabled: copying original text as modern_text for %d snippets",
            len(snippets),
        )
        existing_results = {r["snippet_id"] for r in queue.fetch_all_results()}
        for snippet in snippets:
            if snippet["snippet_id"] not in existing_results:
                queue.complete(
                    "snippet_id",
                    snippet["snippet_id"],
                    {
                        "snippet_id": snippet["snippet_id"],
                        "text": snippet["text"],
                        "description": snippet["description"],
                        "source_description": snippet["source_description"],
                        "modern_text": snippet["text"],
                    },
                )
        logger.info("Stage 2 complete: %d snippets passed through", queue.results_count())
        queue.close()
        return

    # Populate work queue from Stage 1 results if needed
    work_items = [
        {
            "snippet_id": s["snippet_id"],
            "text": s["text"],
            "description": s["description"],
            "source_description": s["source_description"],
        }
        for s in snippets
    ]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 2 already complete: %d snippets modernized", queue.results_count()
        )
        return

    # Set up LLM client and error tracker
    llm = LLMClient(config.llm_preprocessing)
    tracker = ErrorTracker(config.error_handling)

    if not config.prompts.modernize:
        raise RuntimeError(
            "No modernize prompt template found in prompts/modernize.txt"
        )
    prompt_template = config.prompts.modernize[0]

    batch_size = config.pipeline.batch_size

    while not queue.is_complete():
        batch = queue.fetch_batch(batch_size)
        if not batch:
            break

        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            future_to_snippet = {
                executor.submit(
                    _process_snippet, snippet, llm, tracker, prompt_template
                ): snippet
                for snippet in batch
            }
            for future in as_completed(future_to_snippet):
                snippet = future_to_snippet[future]
                try:
                    results[snippet["snippet_id"]] = future.result()
                except Exception as e:
                    logger.error(
                        "Unexpected error processing snippet %s: %s",
                        snippet["snippet_id"],
                        e,
                    )
                    results[snippet["snippet_id"]] = None

        for snippet in batch:
            result = results.get(snippet["snippet_id"])
            if result is None:
                continue

            queue.complete("snippet_id", snippet["snippet_id"], result)

            logger.info(
                "Modernized %d/%d snippets (%d remaining)",
                queue.results_count(),
                len(snippets),
                queue.work_remaining(),
            )

    logger.info("Stage 2 complete: %d snippets modernized", queue.results_count())
    queue.close()
