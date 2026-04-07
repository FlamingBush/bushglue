use anyhow::{Context, Result};

use crate::llm_retry::{LlmAttempt, LlmErrorKind, LlmMessage, RetryConfig, retry_ollama_chat};

/// Applies an affect template to a text using an LLM via Ollama.
pub struct AffectTransformer {
    client: reqwest::Client,
    base_url: String,
    retry_config: RetryConfig,
}

#[derive(serde::Deserialize)]
struct AnswerResponse {
    answer: String,
}

fn answer_schema() -> serde_json::Value {
    serde_json::json!({
        "type": "object",
        "properties": {
            "answer": { "type": "string" }
        },
        "required": ["answer"]
    })
}

impl AffectTransformer {
    pub fn new(base_url: String) -> Self {
        Self {
            client: reqwest::Client::new(),
            base_url,
            retry_config: RetryConfig::default(),
        }
    }

    /// Apply an affect template to the original text. The template must contain
    /// `{original}` which will be replaced with `original`.
    ///
    /// Returns `(result, attempts)`.
    pub async fn apply(
        &self,
        template: &str,
        original: &str,
        model: &str,
    ) -> (Result<String>, Vec<LlmAttempt>) {
        let prompt = template.replace("{original}", original);

        tracing::debug!(model = %model, prompt = %prompt, "affect transformer request");

        let schema = answer_schema();
        let messages = vec![LlmMessage {
            role: "user".to_string(),
            content: prompt,
        }];

        let url = format!("{}/api/chat", self.base_url);

        let (result, attempts) = retry_ollama_chat(
            &self.client,
            &url,
            model,
            &schema,
            messages,
            &self.retry_config,
            |content| {
                let parsed: AnswerResponse = serde_json::from_str(content).map_err(|e| {
                    (
                        LlmErrorKind::ParseFailure,
                        format!("failed to parse affect JSON: {e}"),
                    )
                })?;
                if parsed.answer.is_empty() {
                    return Err((
                        LlmErrorKind::ValidationFailure,
                        "answer field was empty".to_string(),
                    ));
                }
                Ok(parsed.answer)
            },
            |_| {
                Some(
                    "Your response was not valid JSON with an 'answer' field, or the answer was \
                     empty. Return only valid JSON like: {\"answer\": \"your response here\"}"
                        .to_string(),
                )
            },
        )
        .await;

        let result = result.context("affect transformation failed");
        tracing::debug!(succeeded = result.is_ok(), "affect transformer response");
        (result, attempts)
    }
}
