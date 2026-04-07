import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMEndpointConfig:
    api_type: str  # "ollama" or "openai"
    endpoint: str
    api_key: str  # Resolved at load time from api_key_file
    model: str
    max_requests_per_minute: int  # 0 = no limit


@dataclass
class PipelineConfig:
    batch_size: int
    output_dir: Path
    csv_path: Path
    num_verses_to_select: int
    isolation_sample_size: int
    num_questions_per_verse: int


@dataclass
class ChromaDBConfig:
    persist_dir: Path
    server_host: str
    server_port: int
    collection_name: str


@dataclass
class ErrorHandlingConfig:
    max_network_retries: int
    max_validation_retries: int
    retry_base_delay_seconds: float
    max_retry_delay_seconds: float
    log_file: str


@dataclass
class PromptsConfig:
    isolate: list[str] = field(default_factory=list)
    modernize: list[str] = field(default_factory=list)
    questionize: list[str] = field(default_factory=list)


@dataclass
class CollectionConfig:
    name: str
    display_name: str
    description: str
    schema: str  # "biblical" or "generic"


@dataclass
class Config:
    preprocessing_llm: LLMEndpointConfig
    embedding_llm: LLMEndpointConfig
    pipeline: PipelineConfig
    chromadb: ChromaDBConfig
    error_handling: ErrorHandlingConfig
    prompts: PromptsConfig
    collection: CollectionConfig
    base_dir: Path


def load_collection_config(config_dict: dict) -> CollectionConfig:
    """Load CollectionConfig from a parsed TOML config dict."""
    section = config_dict["collection"]
    return CollectionConfig(
        name=section["name"],
        display_name=section["display_name"],
        description=section["description"],
        schema=section["schema"],
    )


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

    preprocessing_llm = load_llm_endpoint(raw["llm"]["preprocessing"])
    embedding_llm = load_llm_endpoint(raw["llm"]["embedding"])

    pipeline_raw = raw["pipeline"]
    pipeline = PipelineConfig(
        batch_size=pipeline_raw["batch_size"],
        output_dir=resolve(pipeline_raw["output_dir"]),
        csv_path=resolve(pipeline_raw["csv_path"]),
        num_verses_to_select=pipeline_raw["num_verses_to_select"],
        isolation_sample_size=pipeline_raw["isolation_sample_size"],
        num_questions_per_verse=pipeline_raw["num_questions_per_verse"],
    )

    chromadb_raw = raw["chromadb"]
    chromadb = ChromaDBConfig(
        persist_dir=resolve(chromadb_raw["persist_dir"]),
        server_host=chromadb_raw["server_host"],
        server_port=chromadb_raw["server_port"],
        collection_name=chromadb_raw["collection_name"],
    )

    error_handling = ErrorHandlingConfig(**raw["error_handling"])

    prompts_raw = raw["prompts"]
    prompts = PromptsConfig(
        isolate=_load_prompt_file(resolve(prompts_raw["isolate"])),
        modernize=_load_prompt_file(resolve(prompts_raw["modernize"])),
        questionize=_load_prompt_file(resolve(prompts_raw["questionize"])),
    )

    collection = load_collection_config(raw)

    return Config(
        preprocessing_llm=preprocessing_llm,
        embedding_llm=embedding_llm,
        pipeline=pipeline,
        chromadb=chromadb,
        error_handling=error_handling,
        prompts=prompts,
        collection=collection,
        base_dir=base_dir,
    )
