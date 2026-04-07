import csv
import logging

import chromadb
from chromadb.api.types import Embedding, Metadata

from t2v_common.config import Config
from t2v_common.db import WorkQueue
from t2v_common.error_tracker import ErrorTracker, RetriesExhausted
from t2v_common.llm import LLMClient
from t2v_common.registry import RegistryEntry, write_registry_entry

logger = logging.getLogger(__name__)

WORK_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
}

RESULTS_SCHEMA = {
    "verse_id": "TEXT PRIMARY KEY",
    "num_questions_embedded": "INTEGER",
}


def _load_book_info(config: Config) -> dict[str, dict]:
    """Load book_title and testament_title from the source CSV, keyed by verse_id."""
    csv_path = config.pipeline.csv_path
    if not csv_path.exists():
        logger.warning("Source CSV not found at %s, book/testament will be empty", csv_path)
        return {}
    info: dict[str, dict] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            info[row["verse_id"]] = {
                "book_title": row["book_title"].replace("_", " ").title(),
                "testament_title": row["testament_title"].replace("_", " ").title(),
            }
    return info


def _load_verse_data(config: Config) -> dict[str, dict]:
    """Load all verse data from Stage 2 (modernize) and Stage 3 (questionize).

    Returns a dict keyed by verse_id with keys:
        bible_verse, original_text, modern_text, questions (list[str]),
        book_title, testament_title
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
        work_schema={"verse_id": "TEXT PRIMARY KEY"},
        results_schema={
            "verse_id": "TEXT PRIMARY KEY",
            "bible_verse": "TEXT",
            "original_text": "TEXT",
            "modern_text": "TEXT",
        },
    )
    modern_results = modernize_queue.fetch_all_results()
    modernize_queue.close()

    questionize_queue = WorkQueue(
        questionize_db_path,
        work_schema={"verse_id": "TEXT PRIMARY KEY"},
        results_schema={
            "verse_id": "TEXT PRIMARY KEY",
            "bible_verse": "TEXT",
            "modern_text": "TEXT",
            "questions": "TEXT",
        },
    )
    question_results = questionize_queue.fetch_all_results()
    questionize_queue.close()

    if not modern_results:
        raise RuntimeError("Stage 2 has no modernized verses. Run Stage 2 first.")
    if not question_results:
        raise RuntimeError("Stage 3 has no questionized verses. Run Stage 3 first.")

    # Index modern results by verse_id
    modern_by_id = {r["verse_id"]: r for r in modern_results}

    # Load book/testament info from source CSV
    book_info = _load_book_info(config)

    # Build combined data
    verse_data: dict[str, dict] = {}
    for qr in question_results:
        vid = qr["verse_id"]
        mr = modern_by_id.get(vid)
        if mr is None:
            logger.warning(
                "Verse %s in questionize but not in modernize, skipping", vid
            )
            continue

        questions = [q.strip() for q in qr["questions"].split("\n") if q.strip()]
        if not questions:
            logger.warning("Verse %s has no questions, skipping", vid)
            continue

        bi = book_info.get(vid, {})
        verse_data[vid] = {
            "bible_verse": mr["bible_verse"],
            "original_text": mr["original_text"],
            "modern_text": mr["modern_text"],
            "questions": questions,
            "book_title": bi.get("book_title", ""),
            "testament_title": bi.get("testament_title", ""),
        }

    logger.info("Loaded data for %d verses from Stages 2 and 3", len(verse_data))
    return verse_data


def _process_verse(
    verse_id: str,
    verse_data: dict,
    llm: LLMClient,
    tracker: ErrorTracker,
    collection: chromadb.Collection,
) -> int | None:
    """Embed all questions for a verse and upsert into ChromaDB.

    Returns the number of questions embedded, or None on failure.
    """
    questions = verse_data["questions"]
    ids: list[str] = []
    embeddings: list[Embedding] = []
    documents: list[str] = []
    metadatas: list[Metadata] = []

    for i, question in enumerate(questions):
        doc_id = f"{verse_id}_q{i}"

        def do_embed(text=question):
            return llm.embed(text)

        try:
            embedding = tracker.retry(do_embed)
        except RetriesExhausted as e:
            logger.error(
                "Skipping question %d for verse %s after retries exhausted: %s",
                i,
                verse_id,
                e,
            )
            return None

        ids.append(doc_id)
        embeddings.append(embedding)
        documents.append(question)
        metadatas.append(
            {
                "verse_id": verse_id,
                "bible_verse": verse_data["bible_verse"],
                "original_text": verse_data["original_text"],
                "modern_text": verse_data["modern_text"],
                "book_title": verse_data["book_title"],
                "testament_title": verse_data["testament_title"],
            },
        )

    # Upsert all questions for this verse at once (safe on resume)
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )

    return len(questions)


def run(config: Config) -> None:
    """Stage 4: Generate embeddings and store in ChromaDB."""
    db_path = config.pipeline.output_dir / "embed.db"
    queue = WorkQueue(db_path, WORK_SCHEMA, RESULTS_SCHEMA)

    # Load all verse data
    verse_data = _load_verse_data(config)

    # Check if already complete
    if queue.results_count() >= len(verse_data):
        logger.info(
            "Stage 4 already complete: %d verses embedded", queue.results_count()
        )
        return

    # Populate work queue if needed
    work_items = [{"verse_id": vid} for vid in verse_data]
    queue.populate(work_items)

    if queue.is_complete():
        logger.info(
            "Stage 4 already complete: %d verses embedded", queue.results_count()
        )
        return

    # Set up embedding LLM client and error tracker
    llm = LLMClient(config.embedding_llm)
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

    total = len(verse_data)

    while not queue.is_complete():
        batch = queue.fetch_batch(config.pipeline.batch_size)
        if not batch:
            break

        for item in batch:
            vid = item["verse_id"]
            vdata = verse_data.get(vid)
            if vdata is None:
                logger.warning("Verse %s not found in loaded data, skipping", vid)
                continue

            num_embedded = _process_verse(vid, vdata, llm, tracker, collection)

            if num_embedded is None:
                logger.error("Skipping verse %s due to embedding failure", vid)
                continue

            queue.complete(
                "verse_id",
                vid,
                {"verse_id": vid, "num_questions_embedded": num_embedded},
            )

            logger.info(
                "Embedded %d/%d verses (%d questions) (%d remaining)",
                queue.results_count(),
                total,
                num_embedded,
                queue.work_remaining(),
            )

    logger.info(
        "Stage 4 complete: %d verses embedded, %d total documents in ChromaDB",
        queue.results_count(),
        collection.count(),
    )
    queue.close()

    entry = RegistryEntry(
        collection_name=config.collection.name,
        display_name=config.collection.display_name,
        description=config.collection.description,
        schema=config.collection.schema,
        item_count=len(verse_data),
    )
    write_registry_entry(config.chromadb, entry)
    logger.info("Registered collection '%s' in t2v.registry", config.collection.name)
