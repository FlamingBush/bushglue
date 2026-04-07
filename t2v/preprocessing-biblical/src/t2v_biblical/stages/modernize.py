import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from t2v_common.config import Config
from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted, ValidationError
from t2v_common.llm import LLMClient

logger = logging.getLogger(__name__)

WORK_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
    "bible_verse": "TEXT",
    "text": "TEXT",
}

RESULTS_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
    "bible_verse": "TEXT",
    "original_text": "TEXT",
    "modern_text": "TEXT",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "modern_text": {"type": "string"},
    },
    "required": ["modern_text"],
}


def _load_selected_verses(config: Config) -> list[dict]:
    """Load selected verses from Stage 1's isolate.db results."""
    isolate_db_path = config.pipeline.output_dir / "isolate.db"
    if not isolate_db_path.exists():
        raise FileNotFoundError(
            f"Stage 1 database not found: {isolate_db_path}. Run Stage 1 first."
        )

    isolate_queue = WorkQueue(
        isolate_db_path,
        work_schema={"verse_id": "TEXT PRIMARY KEY"},
        results_schema={
            "verse_id": "TEXT PRIMARY KEY",
            "book_id": "TEXT",
            "bible_verse": "TEXT",
            "text": "TEXT",
        },
    )

    results = isolate_queue.fetch_all_results()
    isolate_queue.close()

    if not results:
        raise RuntimeError("Stage 1 has no selected verses. Run Stage 1 first.")

    logger.info("Loaded %d selected verses from Stage 1", len(results))
    return results


def _process_verse(
    verse: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    prompt_template: str,
) -> dict | None:
    """Process a single verse through the LLM. Returns result dict or None on failure."""
    prompt = prompt_template.format(
        verse_text=verse["text"],
        verse_reference=verse["bible_verse"],
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
            "Skipping verse %s after retries exhausted: %s",
            verse["verse_id"],
            e,
        )
        return None

    return {
        "verse_id": verse["verse_id"],
        "bible_verse": verse["bible_verse"],
        "original_text": verse["text"],
        "modern_text": result["modern_text"],
    }


def run(config: Config) -> None:
    """Stage 2: Convert selected verses to modern English."""
    db_path = config.pipeline.output_dir / "modernize.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    # Check if already complete
    selected_verses = _load_selected_verses(config)
    if queue.results_count() >= len(selected_verses):
        logger.info(
            "Stage 2 already complete: %d verses modernized", queue.results_count()
        )
        return

    # Populate work queue from Stage 1 results if needed
    work_items = [
        {
            "verse_id": v["verse_id"],
            "bible_verse": v["bible_verse"],
            "text": v["text"],
        }
        for v in selected_verses
    ]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 2 already complete: %d verses modernized", queue.results_count()
        )
        return

    # Set up LLM client and error tracker
    llm = LLMClient(config.preprocessing_llm)
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

        # Process all items in the batch concurrently
        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            future_to_verse = {
                executor.submit(
                    _process_verse, verse, llm, tracker, prompt_template
                ): verse
                for verse in batch
            }
            for future in as_completed(future_to_verse):
                verse = future_to_verse[future]
                try:
                    results[verse["verse_id"]] = future.result()
                except Exception as e:
                    logger.error(
                        "Unexpected error processing verse %s: %s",
                        verse["verse_id"],
                        e,
                    )
                    results[verse["verse_id"]] = None

        # Commit results to DB sequentially (SQLite is single-writer)
        for verse in batch:
            result = results.get(verse["verse_id"])
            if result is None:
                continue

            queue.complete("verse_id", verse["verse_id"], result)

            logger.info(
                "Modernized %d/%d verses (%d remaining)",
                queue.results_count(),
                len(selected_verses),
                queue.work_remaining(),
            )

    logger.info("Stage 2 complete: %d verses modernized", queue.results_count())
    queue.close()
