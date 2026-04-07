use anyhow::Result;
use rand::RngExt;
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};
use tokio::time::sleep;

// ── Public types ──────────────────────────────────────────────────────────

/// Classifies why an LLM attempt failed.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LlmErrorKind {
    Network,
    Timeout,
    RateLimit,
    ServerError,
    /// Non-retryable HTTP error (4xx excluding 429).
    HttpError,
    /// The Ollama envelope or outer JSON failed to parse.
    ParseFailure,
    /// The content parsed but failed domain validation.
    ValidationFailure,
}

/// A single message in an LLM conversation (matches Ollama `/api/chat` format).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmMessage {
    pub role: String,
    pub content: String,
}

/// Record of a single LLM call attempt, included in API responses for observability.
#[derive(Debug, Clone, Serialize)]
pub struct LlmAttempt {
    pub attempt: u32,
    pub model: String,
    /// Messages sent in this attempt (the full conversation at the time of the call).
    pub messages: Vec<LlmMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_response: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_kind: Option<LlmErrorKind>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_detail: Option<String>,
    pub duration_ms: f64,
    pub succeeded: bool,
}

/// Retry and timeout configuration for LLM calls.
#[derive(Debug, Clone)]
pub struct RetryConfig {
    pub max_attempts: u32,
    pub request_timeout: Duration,
    pub base_backoff: Duration,
    pub max_backoff: Duration,
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self {
            max_attempts: 3,
            request_timeout: Duration::from_secs(120),
            base_backoff: Duration::from_millis(500),
            max_backoff: Duration::from_secs(30),
        }
    }
}

// ── Ollama wire types ─────────────────────────────────────────────────────

#[derive(Serialize)]
struct OllamaRequest<'a> {
    model: &'a str,
    messages: &'a [LlmMessage],
    stream: bool,
    format: &'a serde_json::Value,
    think: bool,
}

#[derive(Deserialize)]
struct OllamaResponse {
    message: OllamaMessageResponse,
}

#[derive(Deserialize)]
struct OllamaMessageResponse {
    content: String,
}

// ── Core retry function ───────────────────────────────────────────────────

/// Make an Ollama `/api/chat` call with retry logic.
///
/// - `validate`: parses the raw content string into `T`, or returns
///   `(LlmErrorKind, error_detail)` on failure.
/// - `correction_for`: given a failed attempt (content failures only), returns
///   a correction message to append as a user turn (or `None` to skip mutation).
///   The model's bad response is automatically appended as an assistant turn before
///   the correction message.
///
/// Returns `(result, attempts)` where `attempts` records every call made.
pub async fn retry_ollama_chat<T, V, C>(
    client: &reqwest::Client,
    url: &str,
    model: &str,
    format: &serde_json::Value,
    initial_messages: Vec<LlmMessage>,
    config: &RetryConfig,
    validate: V,
    correction_for: C,
) -> (Result<T>, Vec<LlmAttempt>)
where
    V: Fn(&str) -> std::result::Result<T, (LlmErrorKind, String)>,
    C: Fn(&LlmAttempt) -> Option<String>,
{
    let mut attempts: Vec<LlmAttempt> = Vec::new();
    let mut messages = initial_messages;

    for attempt_num in 1..=config.max_attempts {
        let t_start = Instant::now();
        // Capture the messages sent in this attempt before any potential mutation.
        let sent_messages = messages.clone();

        let request = OllamaRequest {
            model,
            messages: &messages,
            stream: false,
            format,
            think: false,
        };

        let send_result = tokio::time::timeout(
            config.request_timeout,
            client.post(url).json(&request).send(),
        )
        .await;

        let duration_ms = t_start.elapsed().as_secs_f64() * 1000.0;

        match send_result {
            Err(_timeout) => {
                let attempt = LlmAttempt {
                    attempt: attempt_num,
                    model: model.to_string(),
                    messages: sent_messages,
                    raw_response: None,
                    http_status: None,
                    error_kind: Some(LlmErrorKind::Timeout),
                    error_detail: Some("request timed out".to_string()),
                    duration_ms,
                    succeeded: false,
                };
                tracing::warn!(attempt = attempt_num, "ollama chat timed out");
                attempts.push(attempt);
                if attempt_num < config.max_attempts {
                    sleep(compute_backoff(attempt_num, config)).await;
                }
            }

            Ok(Err(e)) => {
                let attempt = LlmAttempt {
                    attempt: attempt_num,
                    model: model.to_string(),
                    messages: sent_messages,
                    raw_response: None,
                    http_status: None,
                    error_kind: Some(LlmErrorKind::Network),
                    error_detail: Some(e.to_string()),
                    duration_ms,
                    succeeded: false,
                };
                tracing::warn!(attempt = attempt_num, error = %e, "ollama chat network error");
                attempts.push(attempt);
                if attempt_num < config.max_attempts {
                    sleep(compute_backoff(attempt_num, config)).await;
                }
            }

            Ok(Ok(response)) => {
                let status = response.status();
                let status_u16 = status.as_u16();

                if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
                    let raw = response.text().await.unwrap_or_default();
                    let attempt = LlmAttempt {
                        attempt: attempt_num,
                        model: model.to_string(),
                        messages: sent_messages,
                        raw_response: Some(raw),
                        http_status: Some(status_u16),
                        error_kind: Some(LlmErrorKind::RateLimit),
                        error_detail: Some("rate limited (429)".to_string()),
                        duration_ms,
                        succeeded: false,
                    };
                    tracing::warn!(attempt = attempt_num, "ollama rate limited (429)");
                    attempts.push(attempt);
                    if attempt_num < config.max_attempts {
                        // Double the backoff for rate limiting.
                        sleep(compute_backoff(attempt_num, config) * 2).await;
                    }
                } else if status.is_server_error() {
                    let raw = response.text().await.unwrap_or_default();
                    let attempt = LlmAttempt {
                        attempt: attempt_num,
                        model: model.to_string(),
                        messages: sent_messages,
                        raw_response: Some(raw.clone()),
                        http_status: Some(status_u16),
                        error_kind: Some(LlmErrorKind::ServerError),
                        error_detail: Some(format!("server error {status_u16}: {raw}")),
                        duration_ms,
                        succeeded: false,
                    };
                    tracing::warn!(attempt = attempt_num, status = status_u16, "ollama server error");
                    attempts.push(attempt);
                    if attempt_num < config.max_attempts {
                        sleep(compute_backoff(attempt_num, config)).await;
                    }
                } else if !status.is_success() {
                    // Non-retryable HTTP error (4xx except 429).
                    let raw = response.text().await.unwrap_or_default();
                    let attempt = LlmAttempt {
                        attempt: attempt_num,
                        model: model.to_string(),
                        messages: sent_messages,
                        raw_response: Some(raw.clone()),
                        http_status: Some(status_u16),
                        error_kind: Some(LlmErrorKind::HttpError),
                        error_detail: Some(format!("HTTP {status_u16}: {raw}")),
                        duration_ms,
                        succeeded: false,
                    };
                    tracing::error!(attempt = attempt_num, status = status_u16, "ollama non-retryable HTTP error");
                    attempts.push(attempt);
                    return (Err(anyhow::anyhow!("HTTP {status_u16}: {raw}")), attempts);
                } else {
                    // Successful HTTP response — read body as text first.
                    let raw_body = match response.text().await {
                        Ok(t) => t,
                        Err(e) => {
                            let attempt = LlmAttempt {
                                attempt: attempt_num,
                                model: model.to_string(),
                                messages: sent_messages,
                                raw_response: None,
                                http_status: Some(status_u16),
                                error_kind: Some(LlmErrorKind::Network),
                                error_detail: Some(format!("failed to read response body: {e}")),
                                duration_ms,
                                succeeded: false,
                            };
                            tracing::warn!(attempt = attempt_num, error = %e, "failed to read ollama response body");
                            attempts.push(attempt);
                            if attempt_num < config.max_attempts {
                                sleep(compute_backoff(attempt_num, config)).await;
                            }
                            continue;
                        }
                    };

                    // Parse the Ollama chat envelope.
                    let content = match serde_json::from_str::<OllamaResponse>(&raw_body) {
                        Ok(resp) => resp.message.content,
                        Err(e) => {
                            let attempt = LlmAttempt {
                                attempt: attempt_num,
                                model: model.to_string(),
                                messages: sent_messages,
                                raw_response: Some(raw_body),
                                http_status: Some(status_u16),
                                error_kind: Some(LlmErrorKind::ParseFailure),
                                error_detail: Some(format!("failed to parse Ollama envelope: {e}")),
                                duration_ms,
                                succeeded: false,
                            };
                            tracing::warn!(attempt = attempt_num, error = %e, "failed to parse Ollama envelope");
                            attempts.push(attempt);
                            // Structural API error — retry from scratch (no message mutation).
                            if attempt_num < config.max_attempts {
                                sleep(compute_backoff(attempt_num, config)).await;
                            }
                            continue;
                        }
                    };

                    // Validate the content.
                    match validate(&content) {
                        Ok(value) => {
                            let attempt = LlmAttempt {
                                attempt: attempt_num,
                                model: model.to_string(),
                                messages: sent_messages,
                                raw_response: Some(content),
                                http_status: Some(status_u16),
                                error_kind: None,
                                error_detail: None,
                                duration_ms,
                                succeeded: true,
                            };
                            attempts.push(attempt);
                            return (Ok(value), attempts);
                        }
                        Err((error_kind, detail)) => {
                            let attempt = LlmAttempt {
                                attempt: attempt_num,
                                model: model.to_string(),
                                messages: sent_messages,
                                raw_response: Some(content.clone()),
                                http_status: Some(status_u16),
                                error_kind: Some(error_kind),
                                error_detail: Some(detail),
                                duration_ms,
                                succeeded: false,
                            };
                            tracing::warn!(
                                attempt = attempt_num,
                                error = attempt.error_detail.as_deref().unwrap_or(""),
                                "ollama content validation failed"
                            );
                            // Append correction messages for the next attempt.
                            if attempt_num < config.max_attempts {
                                if let Some(correction) = correction_for(&attempt) {
                                    messages.push(LlmMessage {
                                        role: "assistant".to_string(),
                                        content: content,
                                    });
                                    messages.push(LlmMessage {
                                        role: "user".to_string(),
                                        content: correction,
                                    });
                                }
                            }
                            attempts.push(attempt);
                        }
                    }
                }
            }
        }
    }

    let last_error = attempts
        .last()
        .and_then(|a| a.error_detail.clone())
        .unwrap_or_else(|| "max attempts exhausted".to_string());
    (
        Err(anyhow::anyhow!(
            "LLM call failed after {} attempt(s): {}",
            attempts.len(),
            last_error
        )),
        attempts,
    )
}

// ── Embed retry ───────────────────────────────────────────────────────────

#[derive(Serialize)]
struct EmbedRequest<'a> {
    model: &'a str,
    input: &'a str,
}

#[derive(Deserialize)]
struct EmbedResponse {
    embeddings: Vec<Vec<f32>>,
}

/// Make an Ollama `/api/embed` call with network/timeout retry.
/// Attempt logs are not returned — embed calls don't have meaningful prompt content.
pub async fn retry_embed_call(
    client: &reqwest::Client,
    url: &str,
    model: &str,
    input: &str,
    config: &RetryConfig,
) -> Result<Vec<f32>> {
    let mut last_err = anyhow::anyhow!("no attempts made");

    for attempt_num in 1..=config.max_attempts {
        let body = EmbedRequest { model, input };

        let send_result = tokio::time::timeout(
            config.request_timeout,
            client.post(url).json(&body).send(),
        )
        .await;

        match send_result {
            Err(_) => {
                last_err = anyhow::anyhow!("embed request timed out");
                tracing::warn!(attempt = attempt_num, "embed request timed out");
            }
            Ok(Err(e)) => {
                last_err = anyhow::anyhow!("embed network error: {e}");
                tracing::warn!(attempt = attempt_num, error = %e, "embed network error");
            }
            Ok(Ok(response)) => {
                let status = response.status();
                if !status.is_success() {
                    let body_text = response.text().await.unwrap_or_default();
                    last_err = anyhow::anyhow!("embed error {status}: {body_text}");
                    if status.is_client_error() && status != reqwest::StatusCode::TOO_MANY_REQUESTS {
                        return Err(last_err);
                    }
                    tracing::warn!(attempt = attempt_num, status = %status, "embed API error");
                } else {
                    let mut parsed: EmbedResponse = response
                        .json()
                        .await
                        .map_err(|e| anyhow::anyhow!("failed to parse embed response: {e}"))?;
                    return parsed
                        .embeddings
                        .pop()
                        .ok_or_else(|| anyhow::anyhow!("embed response contained no vectors"));
                }
            }
        }

        if attempt_num < config.max_attempts {
            sleep(compute_backoff(attempt_num, config)).await;
        }
    }

    Err(last_err)
}

// ── Helpers ───────────────────────────────────────────────────────────────

/// Exponential backoff with ±25% jitter.
fn compute_backoff(attempt: u32, config: &RetryConfig) -> Duration {
    let base_ms = config.base_backoff.as_millis() as f64;
    let exp = 2_f64.powi(attempt as i32 - 1);
    let max_ms = config.max_backoff.as_millis() as f64;
    let backoff_ms = (base_ms * exp).min(max_ms);
    let jitter = rand::rng().random_range(0.75_f64..1.25_f64);
    Duration::from_millis((backoff_ms * jitter) as u64)
}
