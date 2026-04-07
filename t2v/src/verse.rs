use anyhow::{Context, Result};
use rand::distr::weighted::WeightedIndex;
use rand::prelude::*;
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

use crate::chromadb::{ChromaManager, QueryHit};
use crate::embedder::Embedder;
use crate::llm_retry::LlmAttempt;
use crate::reranker::Reranker;
use crate::router::Router;
use crate::affect_transformer::AffectTransformer;

// ── Schema and collection config ─────────────────────────────────────────

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CollectionSchema {
    Biblical,
    Generic,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct CollectionConfig {
    pub name: String,
    pub display_name: String,
    pub description: String,
    pub schema: CollectionSchema,
}

// ── Config ───────────────────────────────────────────────────────────────

/// Everything the engine needs to know.
#[derive(Debug, Clone)]
pub struct Config {
    pub chromadb_url: String,
    pub collections: Vec<CollectionConfig>,
    pub ollama_url: String,
    pub embedding_model: String,
    pub top_n: u32,
    pub rerank_n: usize,
    pub temperature: f64,
    pub max_recent: usize,
    pub recent_ttl: Duration,
    pub rerank_ollama_url: String,
    pub rerank_models: Vec<String>,
    pub enable_rerank: bool,
    /// affect name → template string (loaded from --affects-dir at startup)
    pub affects: std::collections::HashMap<String, String>,
    pub affect_models: Vec<String>,
    pub affect_api_url: String,
    pub router_model: Option<String>,
    pub disable_registry: bool,
}

// ── Result types ─────────────────────────────────────────────────────────

/// The result handed back to callers.
#[derive(Debug, Clone, serde::Serialize)]
pub struct QueryResult {
    pub item_id: String,
    pub source: String,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modern_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub book: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub testament: Option<String>,
    pub collection: String,
    pub matched_question: String,
    pub distance: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_model: Option<String>,
    pub gen_q_embed_ms: f64,
    pub embedding_db_lookup_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub embedding_reranking_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub affected_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub affect: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub affect_model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub router_model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub router_raw: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub reranker_attempts: Vec<LlmAttempt>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub router_attempts: Vec<LlmAttempt>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub affect_attempts: Vec<LlmAttempt>,
    pub all_results: Vec<CandidateResult>,
}

/// A candidate result from ChromaDB with optional reranking score.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CandidateResult {
    pub item_id: String,
    pub source: String,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modern_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub book: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub testament: Option<String>,
    pub collection: String,
    pub matched_question: String,
    pub distance: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_score: Option<f64>,
}

/// A recently returned item ID and the time it was returned.
struct RecentEntry {
    item_id: String,
    returned_at: Instant,
}

// ── Engine ────────────────────────────────────────────────────────────────

/// Shared, concurrency-safe query engine.
pub struct Engine {
    embedder: Embedder,
    reranker: Option<Reranker>,
    router: Option<Router>,
    affect_transformer: Option<AffectTransformer>,
    state: Arc<Mutex<State>>,
    config: Config,
}

struct State {
    chroma: ChromaManager,
    /// Per-collection recency trackers.
    recent: HashMap<String, VecDeque<RecentEntry>>,
}

impl Engine {
    /// Connect to ChromaDB + Ollama. No snapshot is cached — the
    /// collection is queried directly on every request.
    pub async fn new(mut config: Config) -> Result<Self> {
        // 1. Try reading the registry to override collections.
        if !config.disable_registry {
            match ChromaManager::read_registry(&config.chromadb_url).await {
                Ok(entries) if !entries.is_empty() => {
                    tracing::info!(count = entries.len(), "using registry collections");
                    config.collections = entries
                        .into_iter()
                        .map(|e| {
                            let schema = match e.schema.as_str() {
                                "generic" => CollectionSchema::Generic,
                                _ => CollectionSchema::Biblical,
                            };
                            CollectionConfig {
                                name: e.name,
                                display_name: e.display_name,
                                description: e.description,
                                schema,
                            }
                        })
                        .collect();
                }
                Ok(_) => {
                    tracing::info!("registry empty, using CLI collections");
                }
                Err(e) => {
                    tracing::warn!(error = %e, "failed to read registry, using CLI collections");
                }
            }
        }

        // 2. Connect ChromaManager to all collection names.
        let collection_names: Vec<String> = config.collections.iter().map(|c| c.name.clone()).collect();
        let chroma = ChromaManager::connect(&config.chromadb_url, &collection_names).await?;

        let embedder = Embedder::new(&config.ollama_url, &config.embedding_model);

        tracing::info!("warming up embedding model ...");
        embedder.warmup().await?;
        tracing::info!("embedding model ready");

        let reranker = if config.enable_rerank {
            tracing::info!("reranking enabled, models: {:?}", config.rerank_models);
            Some(Reranker::new(config.rerank_ollama_url.clone()))
        } else {
            tracing::info!("reranking disabled");
            None
        };

        // 3. Init router if more than one collection.
        let router = if config.collections.len() > 1 {
            let model = config.router_model.clone().unwrap_or_else(|| {
                config.rerank_models.first().cloned().unwrap_or_else(|| "qwen3:4b".to_string())
            });
            tracing::info!(model = %model, "routing enabled (multiple collections)");
            Some(Router::new(config.rerank_ollama_url.clone(), model))
        } else {
            None
        };

        let affect_transformer = if config.affects.is_empty() {
            tracing::info!("no affects loaded");
            None
        } else {
            tracing::info!(affects = ?config.affects.keys().collect::<Vec<_>>(), "affects loaded");
            Some(AffectTransformer::new(config.affect_api_url.clone()))
        };

        Ok(Self {
            embedder,
            reranker,
            router,
            affect_transformer,
            state: Arc::new(Mutex::new(State {
                chroma,
                recent: HashMap::new(),
            })),
            config,
        })
    }

    /// Returns the list of available reranking models.
    pub fn rerank_models(&self) -> &[String] {
        &self.config.rerank_models
    }

    /// Returns the sorted list of available affect names.
    pub fn affect_models(&self) -> &[String] {
        &self.config.affect_models
    }

    pub fn affects(&self) -> Vec<&str> {
        let mut names: Vec<&str> = self.config.affects.keys().map(String::as_str).collect();
        names.sort();
        names
    }

    pub fn collections(&self) -> &[CollectionConfig] {
        &self.config.collections
    }

    /// Embed a question → route to collection → search ChromaDB → deduplicate → rerank (optional) →
    /// filter out recently used items → sample an item → record it → return.
    pub async fn query(
        &self,
        question: &str,
        collection: Option<String>,
        rerank_override: Option<bool>,
        rerank_model: Option<String>,
        affect: Option<String>,
        affect_model: Option<String>,
        router_model: Option<String>,
    ) -> Result<QueryResult> {
        // 1. Determine which collection to use.
        let (collection_name, router_model_used, router_raw, router_attempts) =
            if let Some(name) = collection {
                // Explicit collection requested — validate it exists.
                if !self.config.collections.iter().any(|c| c.name == name) {
                    anyhow::bail!("unknown collection '{name}'");
                }
                (name, None, None, vec![])
            } else if self.config.collections.len() == 1 {
                (self.config.collections[0].name.clone(), None, None, vec![])
            } else if let Some(ref router) = self.router {
                let decision = router
                    .route(question, &self.config.collections, router_model.as_deref())
                    .await?;
                (
                    decision.collection,
                    decision.model_used,
                    decision.raw_response,
                    decision.attempts,
                )
            } else {
                let name = self
                    .config
                    .collections
                    .first()
                    .map(|c| c.name.clone())
                    .context("no collections configured")?;
                (name, None, None, vec![])
            };

        // Find the CollectionConfig for this collection.
        let collection_config = self.config.collections.iter()
            .find(|c| c.name == collection_name)
            .context("collection config not found")?
            .clone();

        // 2. Embed (outside the lock — this is the latency bottleneck).
        let t_embed = Instant::now();
        let embedding = self.embedder.embed(question).await?;
        let gen_q_embed_ms = t_embed.elapsed().as_secs_f64() * 1000.0;

        // 3. Search ChromaDB.
        let state = Arc::clone(&self.state);
        let top_n = self.config.top_n;
        let rerank_n = self.config.rerank_n;
        let temperature = self.config.temperature;
        let max_recent = self.config.max_recent;
        let recent_ttl = self.config.recent_ttl;

        let mut guard = state.lock().await;

        // Evict stale entries from the per-collection recency tracker.
        let col_recent = guard.recent.entry(collection_name.clone()).or_default();
        evict_expired(col_recent, recent_ttl);

        let t_lookup = Instant::now();
        let hits = guard
            .chroma
            .query(&collection_name, &embedding, top_n)
            .await
            .context("semantic search failed")?;
        let embedding_db_lookup_ms = t_lookup.elapsed().as_secs_f64() * 1000.0;

        if hits.is_empty() {
            anyhow::bail!("no results returned from ChromaDB — collection may be empty");
        }

        // 4. Deduplicate by item_id (keep first occurrence).
        let mut seen = std::collections::HashSet::new();
        let mut deduped_hits: Vec<QueryHit> = Vec::new();
        for hit in hits {
            let iid = item_id_from_hit(&hit, &collection_config.schema);
            if seen.insert(iid) {
                deduped_hits.push(hit);
            }
        }

        // 5. Filter out recently used items before reranking.
        let col_recent = guard.recent.entry(collection_name.clone()).or_default();
        let filtered: Vec<QueryHit> = deduped_hits
            .iter()
            .filter(|h| !col_recent.iter().any(|r| r.item_id == item_id_from_hit(h, &collection_config.schema)))
            .cloned()
            .collect();

        // Take up to rerank_n candidates; fall back to all deduped if everything was recent.
        let to_rerank: Vec<QueryHit> = if filtered.is_empty() {
            deduped_hits.into_iter().take(rerank_n).collect()
        } else {
            filtered.into_iter().take(rerank_n).collect()
        };

        // 6. Rerank if enabled (release lock during reranking).
        drop(guard);

        let do_rerank = rerank_override.unwrap_or(self.reranker.is_some());
        let effective_model = rerank_model
            .or_else(|| self.config.rerank_models.first().cloned())
            .unwrap_or_else(|| "qwen3:4b".to_string());
        let mut embedding_reranking_ms: Option<f64> = None;
        let mut reranker_attempts: Vec<LlmAttempt> = vec![];
        let candidates_with_scores: Vec<(QueryHit, Option<f64>)> = if do_rerank {
            let reranker = self.reranker.as_ref()
                .context("reranking requested but server was started with --disable-rerank")?;

            // Prepare item texts for reranking. Use modern_text if non-empty, else text.
            let rerank_inputs: Vec<(String, String)> = to_rerank
                .iter()
                .map(|h| {
                    let candidate = hit_to_candidate(h, &collection_config.schema, &collection_name);
                    let display_text = if candidate.modern_text.as_deref().unwrap_or("").is_empty() {
                        candidate.text.clone()
                    } else {
                        candidate.modern_text.unwrap_or_default()
                    };
                    (candidate.item_id, display_text)
                })
                .collect();

            let t_rerank = Instant::now();
            let (scores_result, attempts) = reranker
                .score_batch(question, &rerank_inputs, &effective_model)
                .await;
            reranker_attempts = attempts;
            let scores = scores_result.context("reranking failed")?;
            embedding_reranking_ms = Some(t_rerank.elapsed().as_secs_f64() * 1000.0);

            // Normalize scores to 0-10 range based on batch min/max.
            let raw_scores: Vec<f64> = scores.iter().map(|(_, s)| *s).collect();
            let min_s = raw_scores.iter().cloned().fold(f64::INFINITY, f64::min);
            let max_s = raw_scores.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let range = max_s - min_s;
            let normalized: Vec<(String, f64)> = scores
                .into_iter()
                .map(|(id, s)| {
                    let norm = if range > 0.0 { (s - min_s) / range * 10.0 } else { 5.0 };
                    (id, norm)
                })
                .collect();

            let score_map: HashMap<String, f64> = normalized.into_iter().collect();

            let mut scored: Vec<(QueryHit, Option<f64>)> = to_rerank
                .into_iter()
                .map(|h| {
                    let iid = item_id_from_hit(&h, &collection_config.schema);
                    let score = score_map.get(&iid).copied();
                    (h, score)
                })
                .collect();

            // Sort by rerank score (descending).
            scored.sort_by(|a, b| {
                let score_a = a.1.unwrap_or(0.0);
                let score_b = b.1.unwrap_or(0.0);
                score_b
                    .partial_cmp(&score_a)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });

            scored
        } else {
            to_rerank.into_iter().map(|h| (h, None)).collect()
        };

        // 7. Sample.
        let chosen_idx = temperature_sample_with_scores(&candidates_with_scores, temperature)?;

        let (chosen_hit, chosen_rerank_score) = &candidates_with_scores[chosen_idx];

        // 8. Build all_results for the response (excluding the chosen item).
        let chosen_item_id = item_id_from_hit(chosen_hit, &collection_config.schema);
        let all_results: Vec<CandidateResult> = candidates_with_scores
            .iter()
            .filter(|(h, _)| item_id_from_hit(h, &collection_config.schema) != chosen_item_id)
            .map(|(h, score)| {
                let mut c = hit_to_candidate(h, &collection_config.schema, &collection_name);
                c.rerank_score = *score;
                c
            })
            .collect();

        // 9. Apply affect transformation if requested (no lock needed).
        let chosen_candidate = hit_to_candidate(chosen_hit, &collection_config.schema, &collection_name);
        let original_text = chosen_candidate.text.clone();
        let (affected_text, used_affect, used_affect_model, affect_attempts) =
            if let Some(ref affect_name) = affect {
                if let Some(template) = self.config.affects.get(affect_name) {
                    if let Some(ref vt) = self.affect_transformer {
                        let effective_affect_model = affect_model
                            .clone()
                            .or_else(|| self.config.affect_models.first().cloned())
                            .unwrap_or_else(|| "qwen3:4b".to_string());
                        let (result, attempts) =
                            vt.apply(template, &original_text, &effective_affect_model).await;
                        match result {
                            Ok(text) => (
                                Some(text),
                                Some(affect_name.clone()),
                                Some(effective_affect_model),
                                attempts,
                            ),
                            Err(e) => {
                                tracing::warn!(
                                    affect = %affect_name,
                                    error = %e,
                                    "affect transformation failed, using original"
                                );
                                (None, None, None, attempts)
                            }
                        }
                    } else {
                        (None, None, None, vec![])
                    }
                } else {
                    tracing::warn!(affect = %affect_name, "unknown affect requested");
                    (None, None, None, vec![])
                }
            } else {
                (None, None, None, vec![])
            };

        // Re-acquire lock to record the chosen item.
        let mut guard = state.lock().await;

        let result = QueryResult {
            item_id: chosen_candidate.item_id.clone(),
            source: chosen_candidate.source,
            text: original_text,
            modern_text: chosen_candidate.modern_text,
            description: chosen_candidate.description,
            book: chosen_candidate.book,
            testament: chosen_candidate.testament,
            collection: collection_name.clone(),
            matched_question: chosen_hit.document.clone(),
            distance: chosen_hit.distance,
            rerank_score: *chosen_rerank_score,
            rerank_model: chosen_rerank_score.map(|_| effective_model.clone()),
            gen_q_embed_ms,
            embedding_db_lookup_ms,
            embedding_reranking_ms,
            affected_text,
            affect: used_affect,
            affect_model: used_affect_model,
            router_model: router_model_used,
            router_raw,
            reranker_attempts,
            router_attempts,
            affect_attempts,
            all_results,
        };

        // Record the chosen item in the recency tracker.
        let col_recent = guard.recent.entry(collection_name.clone()).or_default();
        col_recent.push_back(RecentEntry {
            item_id: result.item_id.clone(),
            returned_at: Instant::now(),
        });

        // Enforce the max-recent cap (drop oldest first).
        while col_recent.len() > max_recent {
            col_recent.pop_front();
        }

        tracing::info!(
            item_id = %result.item_id,
            collection = %collection_name,
            distance = result.distance,
            rerank_score = ?result.rerank_score,
            recent_count = col_recent.len(),
            "item selected",
        );

        Ok(result)
    }
}

// ── Schema-aware mapping ─────────────────────────────────────────────────

/// Map a ChromaDB hit to a CandidateResult based on schema type.
fn hit_to_candidate(hit: &QueryHit, schema: &CollectionSchema, collection_name: &str) -> CandidateResult {
    match schema {
        CollectionSchema::Biblical => {
            let modern = get_metadata_str(&hit.metadata, "modern_text");
            let book = get_metadata_str(&hit.metadata, "book_title");
            let testament = get_metadata_str(&hit.metadata, "testament_title");
            CandidateResult {
                item_id: get_metadata_str(&hit.metadata, "verse_id"),
                source: get_metadata_str(&hit.metadata, "bible_verse"),
                text: get_metadata_str(&hit.metadata, "original_text"),
                modern_text: if modern.is_empty() { None } else { Some(modern) },
                description: None,
                book: if book.is_empty() { None } else { Some(book) },
                testament: if testament.is_empty() { None } else { Some(testament) },
                collection: collection_name.to_string(),
                matched_question: hit.document.clone(),
                distance: hit.distance,
                rerank_score: None,
            }
        }
        CollectionSchema::Generic => {
            let modern = get_metadata_str(&hit.metadata, "modern_text");
            let desc = get_metadata_str(&hit.metadata, "description");
            CandidateResult {
                item_id: get_metadata_str(&hit.metadata, "snippet_id"),
                source: get_metadata_str(&hit.metadata, "source_description"),
                text: get_metadata_str(&hit.metadata, "text"),
                modern_text: if modern.is_empty() { None } else { Some(modern) },
                description: if desc.is_empty() { None } else { Some(desc) },
                book: None,
                testament: None,
                collection: collection_name.to_string(),
                matched_question: hit.document.clone(),
                distance: hit.distance,
                rerank_score: None,
            }
        }
    }
}

/// Extract the item ID from a query hit based on schema.
fn item_id_from_hit(hit: &QueryHit, schema: &CollectionSchema) -> String {
    match schema {
        CollectionSchema::Biblical => get_metadata_str(&hit.metadata, "verse_id"),
        CollectionSchema::Generic => get_metadata_str(&hit.metadata, "snippet_id"),
    }
}

/// Helper to extract a string field from metadata HashMap.
fn get_metadata_str(
    metadata: &std::collections::HashMap<String, serde_json::Value>,
    key: &str,
) -> String {
    metadata
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

// ── free functions ────────────────────────────────────────────────────────

/// Remove entries older than `ttl` from the front of the deque.
fn evict_expired(recent: &mut VecDeque<RecentEntry>, ttl: Duration) {
    let now = Instant::now();
    while recent
        .front()
        .is_some_and(|entry| now.duration_since(entry.returned_at) > ttl)
    {
        recent.pop_front();
    }
}

/// Temperature-based sampling over candidates with optional rerank scores.
/// Returns the index of the chosen candidate.
fn temperature_sample_with_scores(
    candidates: &[(QueryHit, Option<f64>)],
    temperature: f64,
) -> Result<usize> {
    if candidates.len() == 1 || temperature <= 0.0 {
        return Ok(0);
    }

    // Use rerank scores if available, otherwise fall back to distance-based similarity.
    let similarities: Vec<f64> = candidates
        .iter()
        .map(|(h, score)| score.unwrap_or_else(|| 1.0 - h.distance as f64))
        .collect();

    let max_sim = similarities
        .iter()
        .cloned()
        .fold(f64::NEG_INFINITY, f64::max);
    let weights: Vec<f64> = similarities
        .iter()
        .map(|s| ((s - max_sim) / temperature).exp())
        .collect();

    let dist = WeightedIndex::new(&weights).context("failed to build sampling distribution")?;
    let mut rng = rand::rng();
    Ok(dist.sample(&mut rng))
}
