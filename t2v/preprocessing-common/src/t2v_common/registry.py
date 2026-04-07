"""ChromaDB registry read/write for t2v collection metadata."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class RegistryEntry:
    collection_name: str
    display_name: str
    description: str
    schema: str  # "biblical" or "generic"
    item_count: int


def write_registry_entry(chromadb_config, entry: RegistryEntry) -> None:
    """Upsert a collection entry into the _t2v_registry collection. Safe to re-run."""
    import chromadb

    client = chromadb.PersistentClient(path=str(chromadb_config.persist_dir))
    registry = client.get_or_create_collection(
        name="t2v.registry",
        metadata={"hnsw:space": "cosine"},
    )
    registry.upsert(
        ids=[entry.collection_name],
        documents=[entry.description],
        metadatas=[
            {
                "display_name": entry.display_name,
                "description": entry.description,
                "schema": entry.schema,
                "item_count": entry.item_count,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
