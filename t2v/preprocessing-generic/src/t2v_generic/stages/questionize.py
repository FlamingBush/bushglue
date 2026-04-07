import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted, ValidationError
from t2v_common.llm import LLMClient

logger = logging.getLogger(__name__)

WORK_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
    "description": "TEXT",
    "source_description": "TEXT",
    "modern_text": "TEXT",
}

RESULTS_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
    "description": "TEXT",
    "source_description": "TEXT",
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


def _load_modern_snippets(config) -> list[dict]:
    """Load modernized snippets from Stage 2's modernize.db results."""
    modernize_db_path = config.pipeline.output_dir / "modernize.db"
    if not modernize_db_path.exists():
        raise FileNotFoundError(
            f"Stage 2 database not found: {modernize_db_path}. Run Stage 2 first."
        )

    modernize_queue = WorkQueue(
        modernize_db_path,
        work_schema={
            "snippet_id": "TEXT PRIMARY KEY",
            "text": "TEXT",
            "description": "TEXT",
            "source_description": "TEXT",
        },
        results_schema={
            "snippet_id": "TEXT PRIMARY KEY",
            "text": "TEXT",
            "description": "TEXT",
            "source_description": "TEXT",
            "modern_text": "TEXT",
        },
    )

    results = modernize_queue.fetch_all_results()
    modernize_queue.close()

    if not results:
        raise RuntimeError("Stage 2 has no modernized snippets. Run Stage 2 first.")

    logger.info("Loaded %d modernized snippets from Stage 2", len(results))
    return results


def _process_snippet(
    snippet: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    prompt_template: str,
    num_questions: int,
) -> dict | None:
    """Process a single snippet through the LLM. Returns result dict or None on failure."""
    prompt = prompt_template.format(
        item_description=snippet["description"],
        source_description=snippet["source_description"],
        modern_text=snippet["modern_text"],
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
            "Skipping snippet %s after retries exhausted: %s",
            snippet["snippet_id"],
            e,
        )
        return None

    return {
        "snippet_id": snippet["snippet_id"],
        "description": snippet["description"],
        "source_description": snippet["source_description"],
        "modern_text": snippet["modern_text"],
        "questions": "\n".join(result["questions"]),
    }


def run(config) -> None:
    """Stage 3: Generate questions for each modernized snippet."""
    db_path = config.pipeline.output_dir / "questionize.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    modern_snippets = _load_modern_snippets(config)

    # Check if already complete
    if queue.results_count() >= len(modern_snippets):
        logger.info(
            "Stage 3 already complete: %d snippets questionized",
            queue.results_count(),
        )
        return

    # Populate work queue from Stage 2 results if needed
    work_items = [
        {
            "snippet_id": s["snippet_id"],
            "description": s["description"],
            "source_description": s["source_description"],
            "modern_text": s["modern_text"],
        }
        for s in modern_snippets
    ]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 3 already complete: %d snippets questionized",
            queue.results_count(),
        )
        return

    # Set up LLM client and error tracker
    llm = LLMClient(config.llm_preprocessing)
    tracker = ErrorTracker(config.error_handling)

    if not config.prompts.questionize:
        raise RuntimeError(
            "No questionize prompt template found in prompts/questionize.txt"
        )
    prompt_template = config.prompts.questionize[0]

    batch_size = config.pipeline.batch_size
    num_questions = config.pipeline.num_questions_per_item

    while not queue.is_complete():
        batch = queue.fetch_batch(batch_size)
        if not batch:
            break

        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            future_to_snippet = {
                executor.submit(
                    _process_snippet,
                    snippet,
                    llm,
                    tracker,
                    prompt_template,
                    num_questions,
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
                "Questionized %d/%d snippets (%d remaining)",
                queue.results_count(),
                len(modern_snippets),
                queue.work_remaining(),
            )

    logger.info("Stage 3 complete: %d snippets questionized", queue.results_count())
    queue.close()
