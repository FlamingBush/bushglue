import argparse
import logging
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from t2v_common.config import (
    ChromaDBConfig,
    CollectionConfig,
    ErrorHandlingConfig,
    LLMEndpointConfig,
    load_collection_config,
)
from t2v_common.registry import RegistryEntry, write_registry_entry
from t2v_generic.stages import embed, ingest, modernize, questionize

logger = logging.getLogger("t2v")


@dataclass
class PipelineConfig:
    batch_size: int
    output_dir: Path
    input_csv: Path
    modernize_enabled: bool
    num_questions_per_item: int


@dataclass
class PromptsConfig:
    modernize: list[str]
    questionize: list[str]


@dataclass
class Config:
    llm_preprocessing: LLMEndpointConfig
    llm_embedding: LLMEndpointConfig
    pipeline: PipelineConfig
    chromadb: ChromaDBConfig
    error_handling: ErrorHandlingConfig
    prompts: PromptsConfig
    collection: CollectionConfig


def _load_prompt_file(path: Path) -> list[str]:
    """Load a prompt template file, splitting on '---' separators."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    sections = text.split("\n---\n")
    return [section.strip() for section in sections if section.strip()]


def load_config(config_path: str | Path) -> Config:
    """Load and validate config from a TOML file."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    base_dir = config_path.parent.resolve()

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    def resolve(p: str) -> Path:
        return (base_dir / Path(p).expanduser()).resolve()

    def load_llm_endpoint(section: dict) -> LLMEndpointConfig:
        api_key = ""
        api_key_file = section.get("api_key_file", "")
        if api_key_file:
            key_path = resolve(api_key_file)
            if not key_path.exists():
                raise FileNotFoundError(f"API key file not found: {key_path}")
            api_key = key_path.read_text(encoding="utf-8").strip()
        return LLMEndpointConfig(
            api_type=section["api_type"],
            endpoint=section["endpoint"],
            api_key=api_key,
            model=section["model"],
            max_requests_per_minute=section.get("max_requests_per_minute", 0),
        )

    llm_preprocessing = load_llm_endpoint(raw["llm"]["preprocessing"])
    llm_embedding = load_llm_endpoint(raw["llm"]["embedding"])

    pipeline_raw = raw["pipeline"]
    pipeline = PipelineConfig(
        batch_size=pipeline_raw["batch_size"],
        output_dir=resolve(pipeline_raw["output_dir"]),
        input_csv=resolve(pipeline_raw["input_csv"]),
        modernize_enabled=pipeline_raw["modernize_enabled"],
        num_questions_per_item=pipeline_raw["num_questions_per_item"],
    )

    chromadb_raw = raw["chromadb"]
    chromadb = ChromaDBConfig(
        persist_dir=resolve(chromadb_raw["persist_dir"]),
        server_host=chromadb_raw["server_host"],
        server_port=chromadb_raw["server_port"],
        collection_name=raw["collection"]["name"],
    )

    error_handling = ErrorHandlingConfig(**raw["error_handling"])

    prompts_raw = raw["prompts"]
    prompts = PromptsConfig(
        modernize=_load_prompt_file(resolve(prompts_raw["modernize"])),
        questionize=_load_prompt_file(resolve(prompts_raw["questionize"])),
    )

    collection = load_collection_config(raw)

    return Config(
        llm_preprocessing=llm_preprocessing,
        llm_embedding=llm_embedding,
        pipeline=pipeline,
        chromadb=chromadb,
        error_handling=error_handling,
        prompts=prompts,
        collection=collection,
    )


STAGES = [
    ("1. Ingest snippets", ingest.run),
    ("2. Modernize snippets", modernize.run),
    ("3. Generate snippet questions", questionize.run),
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

    logger.info("T2V generic preprocessing pipeline starting")
    logger.info("Output directory: %s", config.pipeline.output_dir)

    for stage_name, stage_fn in STAGES:
        logger.info("--- Starting stage: %s ---", stage_name)
        stage_fn(config)
        logger.info("--- Completed stage: %s ---", stage_name)

    logger.info("All stages complete")


def cmd_register(args: argparse.Namespace) -> None:
    config = load_config(args.config)

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
    parser = argparse.ArgumentParser(description="T2V generic preprocessing pipeline")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run all pipeline stages")
    run_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )

    register_parser = subparsers.add_parser(
        "register", help="Register collection metadata in t2v.registry"
    )
    register_parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )

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
        if not hasattr(args, "config"):
            args.config = "config.toml"
        cmd_run(args)


if __name__ == "__main__":
    main()
