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
    "modern_text": "TEXT",
}

RESULTS_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
    "bible_verse": "TEXT",
    "modern_text": "TEXT",
    "questions": "TEXT",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["questions"],
}


def _load_modern_verses(config: Config) -> list[dict]:
    """Load modernized verses from Stage 2's modernize.db results."""
    modernize_db_path = config.pipeline.output_dir / "modernize.db"
    if not modernize_db_path.exists():
        raise FileNotFoundError(
            f"Stage 2 database not found: {modernize_db_path}. Run Stage 2 first."
        )

    modernize_queue = WorkQueue(
        modernize_db_path,
        work_schema={"verse_id": "TEXT PRIMARY KEY"},
        results_schema={
            "verse_id": "TEXT PRIMARY KEY",
            "bible_verse": "TEXT",
            "original_text": "TEXT",
            "modern_text": "TEXT",
        },
    )

    results = modernize_queue.fetch_all_results()
    modernize_queue.close()

    if not results:
        raise RuntimeError("Stage 2 has no modernized verses. Run Stage 2 first.")

    logger.info("Loaded %d modernized verses from Stage 2", len(results))
    return results


def _process_verse(
    verse: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    prompt_template: str,
    num_questions: int,
) -> dict | None:
    """Process a single verse through the LLM. Returns result dict or None on failure."""
    prompt = prompt_template.format(
        modern_text=verse["modern_text"],
        verse_reference=verse["bible_verse"],
        num_questions=num_questions,
    )

    def do_generate():
        result = llm.generate(prompt, RESPONSE_SCHEMA)
        questions = result.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValidationError(f"LLM returned invalid questions: {result}")
        for i, q in enumerate(questions):
            if not isinstance(q, str) or not q.strip():
                raise ValidationError(f"Question {i} is not a non-empty string: {q!r}")
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
        "modern_text": verse["modern_text"],
        "questions": "\n".join(result["questions"]),
    }


def run(config: Config) -> None:
    """Stage 3: Generate questions for each modernized verse."""
    db_path = config.pipeline.output_dir / "questionize.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    # Check if already complete
    modern_verses = _load_modern_verses(config)
    if queue.results_count() >= len(modern_verses):
        logger.info(
            "Stage 3 already complete: %d verses questionized",
            queue.results_count(),
        )
        return

    # Populate work queue from Stage 2 results if needed
    work_items = [
        {
            "verse_id": v["verse_id"],
            "bible_verse": v["bible_verse"],
            "modern_text": v["modern_text"],
        }
        for v in modern_verses
    ]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 3 already complete: %d verses questionized",
            queue.results_count(),
        )
        return

    # Set up LLM client and error tracker
    llm = LLMClient(config.preprocessing_llm)
    tracker = ErrorTracker(config.error_handling)

    if not config.prompts.questionize:
        raise RuntimeError(
            "No questionize prompt template found in prompts/questionize.txt"
        )
    prompt_template = config.prompts.questionize[0]

    batch_size = config.pipeline.batch_size
    num_questions = config.pipeline.num_questions_per_verse

    while not queue.is_complete():
        batch = queue.fetch_batch(batch_size)
        if not batch:
            break

        # Process all items in the batch concurrently
        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            future_to_verse = {
                executor.submit(
                    _process_verse, verse, llm, tracker, prompt_template, num_questions
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
                "Questionized %d/%d verses (%d remaining)",
                queue.results_count(),
                len(modern_verses),
                queue.work_remaining(),
            )

    logger.info("Stage 3 complete: %d verses questionized", queue.results_count())
    queue.close()
