use anyhow::{Context, Result};

use crate::llm_retry::{LlmAttempt, LlmErrorKind, LlmMessage, RetryConfig, retry_ollama_chat};

/// A reranker that scores question-item pairs using an LLM via Ollama.
pub struct Reranker {
    client: reqwest::Client,
    base_url: String,
    retry_config: RetryConfig,
}

impl Reranker {
    pub fn new(base_url: String) -> Self {
        Self {
            client: reqwest::Client::new(),
            base_url,
            retry_config: RetryConfig::default(),
        }
    }

    /// Score multiple items against a question in a single LLM call.
    ///
    /// Returns `(scores, attempts)`.
    pub async fn score_batch(
        &self,
        question: &str,
        items: &[(String, String)], // (item_id, display_text)
        model: &str,
    ) -> (Result<Vec<(String, f64)>>, Vec<LlmAttempt>) {
        if items.is_empty() {
            return (Ok(vec![]), vec![]);
        }

        let item_list = items
            .iter()
            .enumerate()
            .map(|(i, (_, text))| format!("{}. {}", i + 1, text))
            .collect::<Vec<_>>()
            .join("\n");

        let expected_count = items.len();
        let prompt = format!(
            "Score how well each text snippet answers this question, on a scale of 1 to 3 \
             where 1 is not at all, 2 is vaguely related and may reference a single matching \
             concept, 3 is conceptually related.\n\
             Question: {question}\n\n\
             Snippets:\n{item_list}\n\n\
             Return JSON with a \"scores\" array containing one number per snippet, in order."
        );

        let schema = serde_json::json!({
            "type": "object",
            "properties": {
                "scores": {
                    "type": "array",
                    "items": { "type": "number" }
                }
            },
            "required": ["scores"]
        });

        let messages = vec![LlmMessage {
            role: "user".to_string(),
            content: prompt,
        }];

        let url = format!("{}/api/chat", self.base_url);

        tracing::debug!(model = %model, items = %items.len(), "reranker request");

        let (result, attempts) = retry_ollama_chat(
            &self.client,
            &url,
            model,
            &schema,
            messages,
            &self.retry_config,
            |content| {
                // Parse "scores" array from content.
                #[derive(serde::Deserialize)]
                struct BatchScoreResponse {
                    scores: Vec<f64>,
                }
                let parsed: BatchScoreResponse = serde_json::from_str(content).map_err(|e| {
                    (
                        LlmErrorKind::ParseFailure,
                        format!("failed to parse scores JSON: {e}"),
                    )
                })?;
                if parsed.scores.len() != expected_count {
                    return Err((
                        LlmErrorKind::ValidationFailure,
                        format!(
                            "expected {expected_count} scores, got {}",
                            parsed.scores.len()
                        ),
                    ));
                }
                Ok(parsed.scores)
            },
            |attempt| {
                Some(match &attempt.error_kind {
                    Some(LlmErrorKind::ValidationFailure) => format!(
                        "Your response had the wrong number of scores. \
                         Expected exactly {expected_count}. \
                         Return only valid JSON like: {{\"scores\": [1, 2, 3]}}"
                    ),
                    _ => format!(
                        "Your response was not valid JSON with a 'scores' array. \
                         Return only valid JSON like: {{\"scores\": [{scores}]}}",
                        scores = (0..expected_count)
                            .map(|_| "1")
                            .collect::<Vec<_>>()
                            .join(", ")
                    ),
                })
            },
        )
        .await;

        let result = result.context("reranking failed").map(|scores| {
            items
                .iter()
                .zip(scores)
                .map(|((id, _), score)| (id.clone(), score))
                .collect()
        });

        (result, attempts)
    }
}
