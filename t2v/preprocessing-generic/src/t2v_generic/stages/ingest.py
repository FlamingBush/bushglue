import csv
import logging

from t2v_common.db import WorkQueue

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
}


def run(config) -> dict:
    """Stage 1: Ingest snippets from CSV into the work queue."""
    db_path = config.pipeline.output_dir / "ingest.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    input_csv = config.pipeline.input_csv
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    snippets = []
    with open(input_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            snippets.append(
                {
                    "snippet_id": row["id"],
                    "text": row["text"],
                    "description": row["description"],
                    "source_description": row["source_description"],
                }
            )

    logger.info("Loaded %d snippets from %s", len(snippets), input_csv)

    # Populate work queue (idempotent)
    queue.populate(snippets)

    # Copy all work items directly to results (no LLM processing in this stage)
    existing_results = {r["snippet_id"] for r in queue.fetch_all_results()}
    added = 0
    for snippet in snippets:
        if snippet["snippet_id"] not in existing_results:
            queue.complete("snippet_id", snippet["snippet_id"], snippet)
            added += 1

    logger.info(
        "Stage 1 complete: %d snippets ingested (%d new)", len(snippets), added
    )
    queue.close()

    return {"items_ingested": len(snippets)}
