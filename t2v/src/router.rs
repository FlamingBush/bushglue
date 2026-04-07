use anyhow::Result;

use crate::llm_retry::{LlmAttempt, LlmErrorKind, LlmMessage, RetryConfig, retry_ollama_chat};
use crate::verse::CollectionConfig;

pub struct Router {
    client: reqwest::Client,
    base_url: String,
    default_model: String,
    retry_config: RetryConfig,
}

pub struct RouterDecision {
    pub collection: String,
    /// Raw JSON content returned by the LLM. None when routing was skipped.
    pub raw_response: Option<String>,
    /// Model that was used. None when routing was skipped.
    pub model_used: Option<String>,
    /// Per-attempt records. Empty when routing was skipped.
    pub attempts: Vec<LlmAttempt>,
}

impl Router {
    pub fn new(base_url: String, default_model: String) -> Self {
        Self {
            client: reqwest::Client::new(),
            base_url,
            default_model,
            retry_config: RetryConfig::default(),
        }
    }

    /// Route a question to the best matching collection.
    /// If only one collection is provided, returns it immediately without LLM call.
    /// On any error, logs a warning and returns `collections[0].name`.
    pub async fn route(
        &self,
        question: &str,
        collections: &[CollectionConfig],
        model_override: Option<&str>,
    ) -> Result<RouterDecision> {
        if collections.is_empty() {
            anyhow::bail!("no collections configured for routing");
        }

        // Single-collection optimization: skip LLM call.
        if collections.len() <= 1 {
            return Ok(RouterDecision {
                collection: collections[0].name.clone(),
                raw_response: None,
                model_used: None,
                attempts: vec![],
            });
        }

        let model = model_override.unwrap_or(&self.default_model).to_string();

        let list = collections
            .iter()
            .map(|c| {
                format!(
                    "- name: \"{}\"\n  description: \"{}\"",
                    c.name, c.description
                )
            })
            .collect::<Vec<_>>()
            .join("\n");

        let valid_names: Vec<String> = collections.iter().map(|c| c.name.clone()).collect();

        let prompt = format!(
            "You are a routing assistant. Given a user question and a list of collections, \
             pick the single best collection name.\n\n\
             Question: {question}\n\n\
             Collections:\n{list}\n\n\
             Return JSON with a \"collection\" field containing exactly one of the collection \
             names listed above."
        );

        let schema = serde_json::json!({
            "type": "object",
            "properties": {
                "collection": { "type": "string" }
            },
            "required": ["collection"]
        });

        let messages = vec![LlmMessage {
            role: "user".to_string(),
            content: prompt,
        }];

        let url = format!("{}/api/chat", self.base_url);

        tracing::debug!(model = %model, question = %question, "router request");

        let (result, attempts) = retry_ollama_chat(
            &self.client,
            &url,
            &model,
            &schema,
            messages,
            &self.retry_config,
            |content| {
                #[derive(serde::Deserialize)]
                struct RouteResponse {
                    collection: String,
                }
                let parsed: RouteResponse = serde_json::from_str(content).map_err(|e| {
                    (
                        LlmErrorKind::ParseFailure,
                        format!("failed to parse route JSON: {e}"),
                    )
                })?;
                if valid_names.iter().any(|n| n == &parsed.collection) {
                    Ok((parsed.collection, content.to_string()))
                } else {
                    Err((
                        LlmErrorKind::ValidationFailure,
                        format!(
                            "unknown collection '{}'; valid names: {}",
                            parsed.collection,
                            valid_names.join(", ")
                        ),
                    ))
                }
            },
            |attempt| {
                Some(match &attempt.error_kind {
                    Some(LlmErrorKind::ValidationFailure) => format!(
                        "The collection name you returned is not valid. \
                         You must return one of these exact names: {}. \
                         Return only valid JSON like: {{\"collection\": \"{}\"}}",
                        valid_names.join(", "),
                        valid_names[0]
                    ),
                    _ => format!(
                        "Your response was not valid JSON with a 'collection' field. \
                         Return only valid JSON like: {{\"collection\": \"{}\"}}",
                        valid_names[0]
                    ),
                })
            },
        )
        .await;

        match result {
            Ok((collection, raw)) => {
                tracing::debug!(collection = %collection, "router selected collection");
                Ok(RouterDecision {
                    collection,
                    raw_response: Some(raw),
                    model_used: Some(model),
                    attempts,
                })
            }
            Err(e) => {
                tracing::warn!(error = %e, "router failed, using first collection");
                Ok(RouterDecision {
                    collection: collections[0].name.clone(),
                    raw_response: None,
                    model_used: Some(model),
                    attempts,
                })
            }
        }
    }
}
