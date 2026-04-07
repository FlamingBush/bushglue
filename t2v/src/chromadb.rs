use anyhow::{Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Lightweight async client for the ChromaDB v2 REST API.
///
/// Reuses a single `reqwest::Client` for connection pooling.
/// The collection is treated as read-only at runtime — no deletes,
/// upserts, or snapshots.
pub struct ChromaManager {
    client: Client,
    base: String,
    /// Map from collection name → resolved UUID
    collection_ids: HashMap<String, String>,
}

// ── Public types ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct QueryHit {
    pub id: String,
    pub distance: f32,
    pub document: String,
    pub metadata: HashMap<String, serde_json::Value>,
}

/// An entry from the t2v.registry collection.
#[derive(Debug, Clone)]
pub struct RegistryEntry {
    pub name: String,
    pub display_name: String,
    pub description: String,
    pub schema: String,
}

// ── Wire types (serde) ──────────────────────────────────────────────────

#[derive(Deserialize)]
struct CollectionInfo {
    id: String,
}

#[derive(Serialize)]
struct QueryBody {
    query_embeddings: Vec<Vec<f32>>,
    n_results: u32,
    include: Vec<String>,
}

#[derive(Deserialize)]
struct QueryResponse {
    ids: Vec<Vec<String>>,
    distances: Option<Vec<Vec<f32>>>,
    documents: Option<Vec<Vec<Option<String>>>>,
    metadatas: Option<Vec<Vec<Option<HashMap<String, serde_json::Value>>>>>,
}

#[derive(Serialize)]
struct GetBody {
    include: Vec<String>,
}

#[derive(Deserialize)]
struct GetResponse {
    ids: Vec<String>,
    documents: Option<Vec<Option<String>>>,
    metadatas: Option<Vec<Option<HashMap<String, serde_json::Value>>>>,
}

// ── Implementation ──────────────────────────────────────────────────────

impl ChromaManager {
    const TENANT: &str = "default_tenant";
    const DATABASE: &str = "default_database";
    const REGISTRY_COLLECTION: &str = "t2v.registry";

    /// Resolve the UUID for a single named collection.
    async fn resolve_collection(client: &Client, base: &str, collection_name: &str) -> Result<String> {
        let url = format!(
            "{}/api/v2/tenants/{}/databases/{}/collections/{}",
            base,
            Self::TENANT,
            Self::DATABASE,
            collection_name,
        );

        let resp = client
            .get(&url)
            .send()
            .await
            .context("failed to reach ChromaDB server")?;

        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("ChromaDB collection lookup failed ({status}): {text}");
        }

        let info: CollectionInfo = resp
            .json()
            .await
            .context("failed to parse collection info")?;

        Ok(info.id)
    }

    /// Read all entries from the t2v.registry collection.
    /// Returns Ok(vec![]) if the collection doesn't exist (404).
    pub async fn read_registry(base_url: &str) -> Result<Vec<RegistryEntry>> {
        let client = Client::new();
        let base = base_url.trim_end_matches('/').to_string();

        // First, try to resolve the registry collection UUID.
        let url = format!(
            "{}/api/v2/tenants/{}/databases/{}/collections/{}",
            base,
            Self::TENANT,
            Self::DATABASE,
            Self::REGISTRY_COLLECTION,
        );

        let resp = client
            .get(&url)
            .send()
            .await
            .context("failed to reach ChromaDB server when checking registry")?;

        let status = resp.status();
        if status == 404 || status.as_u16() == 404 {
            tracing::info!("t2v.registry collection not found, skipping registry");
            return Ok(vec![]);
        }

        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            tracing::warn!("Registry collection lookup failed ({status}): {text}, using default collections");
            return Ok(vec![]);
        }

        let info: CollectionInfo = resp
            .json()
            .await
            .context("failed to parse registry collection info")?;

        let registry_uuid = info.id;

        // Now fetch all items from the registry collection.
        let get_url = format!(
            "{}/api/v2/tenants/{}/databases/{}/collections/{}/get",
            base,
            Self::TENANT,
            Self::DATABASE,
            registry_uuid,
        );

        let get_body = GetBody {
            include: vec!["documents".into(), "metadatas".into()],
        };

        let get_resp = client
            .post(&get_url)
            .json(&get_body)
            .send()
            .await
            .context("failed to fetch registry items")?;

        let get_status = get_resp.status();
        if !get_status.is_success() {
            let text = get_resp.text().await.unwrap_or_default();
            tracing::warn!("Registry get failed ({get_status}): {text}, using default collections");
            return Ok(vec![]);
        }

        let parsed: GetResponse = get_resp
            .json()
            .await
            .context("failed to parse registry get response")?;

        let metadatas = parsed.metadatas.unwrap_or_default();

        let mut entries = Vec::new();
        for (i, id) in parsed.ids.iter().enumerate() {
            let meta = metadatas.get(i).and_then(|m| m.as_ref()).cloned().unwrap_or_default();

            let get_str = |key: &str| -> String {
                meta.get(key)
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string()
            };

            let name = get_str("name");
            // Fall back to id if name metadata not set
            let name = if name.is_empty() { id.clone() } else { name };

            entries.push(RegistryEntry {
                name,
                display_name: get_str("display_name"),
                description: get_str("description"),
                schema: get_str("schema"),
            });
        }

        tracing::info!(count = entries.len(), "loaded registry entries");
        Ok(entries)
    }

    /// Connect to a ChromaDB server and resolve the named collections.
    pub async fn connect(base_url: &str, names: &[String]) -> Result<Self> {
        let client = Client::new();
        let base = base_url.trim_end_matches('/').to_string();

        let mut collection_ids = HashMap::new();

        for name in names {
            let id = Self::resolve_collection(&client, &base, name)
                .await
                .with_context(|| format!("failed to resolve collection '{name}'"))?;

            // Count items for logging
            let count_url = format!(
                "{}/api/v2/tenants/{}/databases/{}/collections/{}/count",
                base,
                Self::TENANT,
                Self::DATABASE,
                id,
            );
            let count: u32 = client
                .get(&count_url)
                .send()
                .await
                .and_then(|r| r.error_for_status())
                .ok()
                .and_then(|r| futures::executor::block_on(r.json()).ok())
                .unwrap_or(0);

            tracing::info!(
                collection = %name,
                id = %id,
                documents = count,
                "connected to ChromaDB collection",
            );

            collection_ids.insert(name.clone(), id);
        }

        Ok(Self {
            client,
            base,
            collection_ids,
        })
    }

    /// Nearest-neighbour search on a specific named collection.
    pub async fn query(&self, collection_name: &str, embedding: &[f32], n: u32) -> Result<Vec<QueryHit>> {
        let collection_id = self.collection_ids.get(collection_name)
            .with_context(|| format!("unknown collection '{collection_name}'"))?;

        let body = QueryBody {
            query_embeddings: vec![embedding.to_vec()],
            n_results: n,
            include: vec!["distances".into(), "documents".into(), "metadatas".into()],
        };

        let url = self.collection_action_url(collection_id, "query");
        let resp = self
            .client
            .post(&url)
            .json(&body)
            .send()
            .await
            .context("ChromaDB query request failed")?;

        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            anyhow::bail!("ChromaDB query error ({status}): {text}");
        }

        let parsed: QueryResponse = resp
            .json()
            .await
            .context("failed to parse ChromaDB query response")?;

        let ids = parsed.ids.into_iter().next().unwrap_or_default();
        let distances = parsed
            .distances
            .and_then(|d| d.into_iter().next())
            .unwrap_or_default();
        let documents = parsed
            .documents
            .and_then(|d| d.into_iter().next())
            .unwrap_or_default();
        let metadatas = parsed
            .metadatas
            .and_then(|m| m.into_iter().next())
            .unwrap_or_default();

        let hits = ids
            .into_iter()
            .enumerate()
            .map(|(i, id)| QueryHit {
                id,
                distance: distances.get(i).copied().unwrap_or(f32::MAX),
                document: documents.get(i).and_then(|d| d.clone()).unwrap_or_default(),
                metadata: metadatas.get(i).and_then(|m| m.clone()).unwrap_or_default(),
            })
            .collect();

        Ok(hits)
    }

    // ── Helpers ─────────────────────────────────────────────────────────

    fn collection_action_url(&self, collection_id: &str, action: &str) -> String {
        format!(
            "{}/api/v2/tenants/{}/databases/{}/collections/{}/{}",
            self.base,
            Self::TENANT,
            Self::DATABASE,
            collection_id,
            action,
        )
    }
}
