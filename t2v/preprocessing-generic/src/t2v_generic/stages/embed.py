import logging

import chromadb
from chromadb.api.types import Embedding, Metadata

from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted
from t2v_common.llm import LLMClient
from t2v_common.registry import RegistryEntry, write_registry_entry

logger = logging.getLogger(__name__)

WORK_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
}

RESULTS_SCHEMA = {
    "snippet_id": "TEXT PRIMARY KEY",
    "num_questions_embedded": "INTEGER",
}


def _load_snippet_data(config) -> dict[str, dict]:
    """Load all snippet data from Stage 2 (modernize) and Stage 3 (questionize).

    Returns a dict keyed by snippet_id with keys:
        text, description, source_description, modern_text, questions (list[str])
    """
    modernize_db_path = config.pipeline.output_dir / "modernize.db"
    if not modernize_db_path.exists():
        raise FileNotFoundError(
            f"Stage 2 database not found: {modernize_db_path}. Run Stage 2 first."
        )

    questionize_db_path = config.pipeline.output_dir / "questionize.db"
    if not questionize_db_path.exists():
        raise FileNotFoundError(
            f"Stage 3 database not found: {questionize_db_path}. Run Stage 3 first."
        )

    modernize_queue = WorkQueue(
        modernize_db_path,
        work_schema={"snippet_id": "TEXT PRIMARY KEY"},
        results_schema={
            "snippet_id": "TEXT PRIMARY KEY",
            "text": "TEXT",
            "description": "TEXT",
            "source_description": "TEXT",
            "modern_text": "TEXT",
        },
    )
    modern_results = modernize_queue.fetch_all_results()
    modernize_queue.close()

    questionize_queue = WorkQueue(
        questionize_db_path,
        work_schema={"snippet_id": "TEXT PRIMARY KEY"},
        results_schema={
            "snippet_id": "TEXT PRIMARY KEY",
            "description": "TEXT",
            "source_description": "TEXT",
            "modern_text": "TEXT",
            "questions": "TEXT",
        },
    )
    question_results = questionize_queue.fetch_all_results()
    questionize_queue.close()

    if not modern_results:
        raise RuntimeError("Stage 2 has no modernized snippets. Run Stage 2 first.")
    if not question_results:
        raise RuntimeError("Stage 3 has no questionized snippets. Run Stage 3 first.")

    # Index modern results by snippet_id
    modern_by_id = {r["snippet_id"]: r for r in modern_results}

    # Build combined data
    snippet_data: dict[str, dict] = {}
    for qr in question_results:
        sid = qr["snippet_id"]
        mr = modern_by_id.get(sid)
        if mr is None:
            logger.warning(
                "Snippet %s in questionize but not in modernize, skipping", sid
            )
            continue

        questions = [q.strip() for q in qr["questions"].split("\n") if q.strip()]
        if not questions:
            logger.warning("Snippet %s has no questions, skipping", sid)
            continue

        snippet_data[sid] = {
            "text": mr["text"],
            "description": mr["description"],
            "source_description": mr["source_description"],
            "modern_text": mr["modern_text"],
            "questions": questions,
        }

    logger.info("Loaded data for %d snippets from Stages 2 and 3", len(snippet_data))
    return snippet_data


def _process_snippet(
    snippet_id: str,
    snippet_data: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    collection: chromadb.Collection,
) -> int | None:
    """Embed all questions for a snippet and upsert into ChromaDB.

    Returns the number of questions embedded, or None on failure.
    """
    questions = snippet_data["questions"]
    ids: list[str] = []
    embeddings: list[Embedding] = []
    documents: list[str] = []
    metadatas: list[Metadata] = []

    for i, question in enumerate(questions):
        doc_id = f"{snippet_id}_q{i}"

        def do_embed(text=question):
            return llm.embed(text)

        try:
            embedding = tracker.retry(do_embed)
        except RetriesExhausted as e:
            logger.error(
                "Skipping question %d for snippet %s after retries exhausted: %s",
                i,
                snippet_id,
                e,
            )
            return None

        ids.append(doc_id)
        embeddings.append(embedding)
        documents.append(question)
        metadatas.append(
            {
                "snippet_id": snippet_id,
                "text": snippet_data["text"],
                "description": snippet_data["description"],
                "source_description": snippet_data["source_description"],
                "modern_text": snippet_data["modern_text"],
            },
        )

    # Upsert all questions for this snippet at once (safe on resume)
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )

    return len(questions)


def run(config) -> None:
    """Stage 4: Generate embeddings and store in ChromaDB."""
    db_path = config.pipeline.output_dir / "embed.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    # Load all snippet data
    snippet_data = _load_snippet_data(config)

    # Check if already complete
    if queue.results_count() >= len(snippet_data):
        logger.info(
            "Stage 4 already complete: %d snippets embedded", queue.results_count()
        )
        return

    # Populate work queue if needed
    work_items = [{"snippet_id": sid} for sid in snippet_data]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 4 already complete: %d snippets embedded", queue.results_count()
        )
        return

    # Set up embedding LLM client and error tracker
    llm = LLMClient(config.llm_embedding)
    tracker = ErrorTracker(config.error_handling)

    # Set up ChromaDB persistent client
    persist_dir = str(config.chromadb.persist_dir)
    client = chromadb.PersistentClient(path=persist_dir)
    collection = client.get_or_create_collection(
        name=config.chromadb.collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info(
        "ChromaDB collection '%s' at %s (existing docs: %d)",
        config.chromadb.collection_name,
        persist_dir,
        collection.count(),
    )

    total = len(snippet_data)

    while not queue.is_complete():
        batch = queue.fetch_batch(config.pipeline.batch_size)
        if not batch:
            break

        for item in batch:
            sid = item["snippet_id"]
            sdata = snippet_data.get(sid)
            if sdata is None:
                logger.warning("Snippet %s not found in loaded data, skipping", sid)
                continue

            num_embedded = _process_snippet(sid, sdata, llm, tracker, collection)

            if num_embedded is None:
                logger.error("Skipping snippet %s due to embedding failure", sid)
                continue

            queue.complete(
                "snippet_id",
                sid,
                {"snippet_id": sid, "num_questions_embedded": num_embedded},
            )

            logger.info(
                "Embedded %d/%d snippets (%d questions) (%d remaining)",
                queue.results_count(),
                total,
                num_embedded,
                queue.work_remaining(),
            )

    logger.info(
        "Stage 4 complete: %d snippets embedded, %d total documents in ChromaDB",
        queue.results_count(),
        collection.count(),
    )
    queue.close()

    entry = RegistryEntry(
        collection_name=config.collection.name,
        display_name=config.collection.display_name,
        description=config.collection.description,
        schema=config.collection.schema,
        item_count=len(snippet_data),
    )
    write_registry_entry(config.chromadb, entry)
    logger.info("Registered collection '%s' in t2v.registry", config.collection.name)
