import csv
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from t2v_common.config import Config
from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted, ValidationError
from t2v_common.llm import LLMClient

logger = logging.getLogger(__name__)

VERSE_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
    "book_id": "TEXT",
    "bible_verse": "TEXT",
    "text": "TEXT",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verse_id": {"type": "string"},
    },
    "required": ["verse_id"],
}


def _load_csv(config: Config) -> list[dict]:
    """Load bible verses from CSV file."""
    csv_path = config.pipeline.csv_path
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    verses = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            verses.append(
                {
                    "verse_id": row["verse_id"],
                    "book_id": row["book_id"],
                    "bible_verse": row["bible_verse"],
                    "text": row["text"],
                }
            )
    logger.info("Loaded %d verses from %s", len(verses), csv_path)
    return verses


def _format_verses_block(verses: list[dict]) -> str:
    """Format verses for the prompt."""
    lines = []
    for v in verses:
        lines.append(f"[{v['verse_id']}] ({v['bible_verse']}): {v['text']}")
    return "\n".join(lines)


def _process_group(
    group: list[dict],
    llm: LLMClient,
    tracker: ErrorTracker,
    prompt_template: str,
) -> dict | None:
    """Process a single sample group through the LLM.

    Returns the selected verse dict, or None on failure.
    """
    candidate_ids = {v["verse_id"] for v in group}
    verses_block = _format_verses_block(group)
    prompt = prompt_template.format(
        num_verses=len(group),
        verses_block=verses_block,
    )

    def do_generate():
        result = llm.generate(prompt, RESPONSE_SCHEMA)
        selected_id = result.get("verse_id")
        if selected_id not in candidate_ids:
            raise ValidationError(
                f"LLM selected '{selected_id}' which is not in candidates: {candidate_ids}"
            )
        return result

    try:
        result = tracker.retry(do_generate)
    except RetriesExhausted as e:
        logger.error("Skipping group after retries exhausted: %s", e)
        return None

    selected_id = result["verse_id"]
    return next(v for v in group if v["verse_id"] == selected_id)


def run(config: Config) -> None:
    """Stage 1: Isolate interesting/orthogonal verses."""
    db_path = config.pipeline.output_dir / "isolate.db"
    queue = WorkQueue(db_path, VERSE_SCHEMA, VERSE_SCHEMA)

    # Check if already complete
    target = config.pipeline.num_verses_to_select
    if queue.results_count() >= target:
        logger.info(
            "Stage 1 already complete: %d verses selected", queue.results_count()
        )
        return

    # Populate work queue from CSV if needed
    verses = _load_csv(config)
    queue.populate(verses)

    # Set up LLM client and error tracker
    llm = LLMClient(config.preprocessing_llm)
    tracker = ErrorTracker(config.error_handling)

    if not config.prompts.isolate:
        raise RuntimeError("No isolate prompt template found in prompts/isolate.txt")
    prompt_template = config.prompts.isolate[0]

    sample_size = config.pipeline.isolation_sample_size
    batch_size = config.pipeline.batch_size

    while queue.results_count() < target:
        remaining = queue.work_remaining()
        if remaining < sample_size:
            logger.warning(
                "Only %d verses remaining in pool, need %d per sample. Stopping.",
                remaining,
                sample_size,
            )
            break

        # How many groups we still need, capped by batch_size
        groups_needed = min(batch_size, target - queue.results_count())

        # How many non-overlapping groups we can actually form
        max_groups = remaining // sample_size
        num_groups = min(groups_needed, max_groups)
        if num_groups == 0:
            logger.warning(
                "Only %d verses remaining, cannot form a full sample of %d. Stopping.",
                remaining,
                sample_size,
            )
            break

        # Sample all needed verses at once, then split into non-overlapping groups
        all_candidates = queue.fetch_random(num_groups * sample_size)
        groups = [
            all_candidates[i * sample_size : (i + 1) * sample_size]
            for i in range(num_groups)
        ]

        # Process all groups concurrently
        group_results: list[tuple[int, dict | None]] = []
        with ThreadPoolExecutor(max_workers=num_groups) as executor:
            future_to_idx = {
                executor.submit(
                    _process_group, group, llm, tracker, prompt_template
                ): idx
                for idx, group in enumerate(groups)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    group_results.append((idx, future.result()))
                except Exception as e:
                    logger.error("Unexpected error processing group %d: %s", idx, e)
                    group_results.append((idx, None))

        # Sort by group index to process in order
        group_results.sort(key=lambda x: x[0])

        # Commit results to DB sequentially (SQLite is single-writer)
        for idx, selected_verse in group_results:
            group = groups[idx]
            all_ids = [v["verse_id"] for v in group]

            if selected_verse is not None:
                # Store selected verse in results and remove from work
                queue.complete("verse_id", selected_verse["verse_id"], selected_verse)
                # Remove the other candidates from work too
                other_ids = [
                    vid for vid in all_ids if vid != selected_verse["verse_id"]
                ]
                queue.remove_batch("verse_id", other_ids)
            else:
                # Failed group — leave candidates in work queue for future rounds
                logger.warning("Group %d failed, leaving candidates in pool", idx)
                continue

            selected_count = queue.results_count()
            logger.info(
                "Selected %d/%d verses (%d remaining in pool)",
                selected_count,
                target,
                queue.work_remaining(),
            )

    logger.info("Stage 1 complete: %d verses selected", queue.results_count())
    queue.close()
