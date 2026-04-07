import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from t2v_common.config import load_config
from t2v_common.registry import RegistryEntry, write_registry_entry
from t2v_biblical.stages import embed, isolate, modernize, questionize

logger = logging.getLogger("t2v")

STAGES = [
    ("1. Isolate interesting verses", isolate.run),
    ("2. Modernize verses", modernize.run),
    ("3. Generate verse-questions", questionize.run),
    ("4. Embed and store in ChromaDB", embed.run),
]


def setup_logging(output_dir: Path, log_filename: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / log_filename

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    root.addHandler(console)
    root.addHandler(file_handler)


def _get_questionize_item_count(output_dir: Path) -> int:
    """Return the number of results in questionize.db, or 0 if unavailable."""
    db_path = output_dir / "questionize.db"
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT COUNT(*) FROM results")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    setup_logging(config.pipeline.output_dir, config.error_handling.log_file)

    logger.info("T2V preprocessing pipeline starting")
    logger.info("Output directory: %s", config.pipeline.output_dir)

    for stage_name, stage_fn in STAGES:
        logger.info("--- Starting stage: %s ---", stage_name)
        stage_fn(config)
        logger.info("--- Completed stage: %s ---", stage_name)

    logger.info("All stages complete")


def cmd_register(args: argparse.Namespace) -> None:
    config = load_config(args.config)

    # Set up minimal logging to stdout
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    item_count = _get_questionize_item_count(config.pipeline.output_dir)
    logger.info(
        "Registering collection '%s' with item_count=%d",
        config.collection.name,
        item_count,
    )

    entry = RegistryEntry(
        collection_name=config.collection.name,
        display_name=config.collection.display_name,
        description=config.collection.description,
        schema=config.collection.schema,
        item_count=item_count,
    )
    write_registry_entry(config.chromadb, entry)
    logger.info(
        "Successfully registered collection '%s' in t2v.registry at %s",
        config.collection.name,
        config.chromadb.persist_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="T2V preprocessing pipeline")
    subparsers = parser.add_subparsers(dest="command")

    # Default "run" subcommand (runs all pipeline stages)
    run_parser = subparsers.add_parser("run", help="Run all pipeline stages")
    run_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )

    # "register" subcommand (writes collection metadata to _t2v_registry)
    register_parser = subparsers.add_parser(
        "register", help="Register collection metadata in _t2v_registry"
    )
    register_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )

    # Support legacy invocation with no subcommand (treat as "run")
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Path to config.toml (default: config.toml) — legacy, use 'run' subcommand",
    )

    args = parser.parse_args()

    if args.command == "register":
        cmd_register(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        # Legacy: no subcommand given — treat as pipeline run
        if not hasattr(args, "config"):
            args.config = "config.toml"
        cmd_run(args)


if __name__ == "__main__":
    main()
