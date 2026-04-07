use anyhow::Result;
use reqwest::Client;

use crate::llm_retry::{RetryConfig, retry_embed_call};

/// Ollama-compatible embedding client.
///
/// Holds a single `reqwest::Client` so TCP connections (and TLS sessions,
/// if any) are reused across calls.
pub struct Embedder {
    client: Client,
    url: String,
    model: String,
    retry_config: RetryConfig,
}

impl Embedder {
    pub fn new(ollama_url: &str, model: &str) -> Self {
        Self {
            client: Client::new(),
            url: format!("{}/api/embed", ollama_url.trim_end_matches('/')),
            model: model.to_string(),
            retry_config: RetryConfig::default(),
        }
    }

    /// Generate an embedding vector for `text`.
    pub async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        tracing::debug!(model = %self.model, input = %text, "embedder request");
        let vec = retry_embed_call(
            &self.client,
            &self.url,
            &self.model,
            text,
            &self.retry_config,
        )
        .await?;
        tracing::debug!(dims = vec.len(), "embedder response");
        Ok(vec)
    }

    /// Warm up the model by sending a tiny request so the first real query
    /// doesn't pay the model-load penalty.
    pub async fn warmup(&self) -> Result<()> {
        self.embed(".").await.map(|_| ())
    }
}
