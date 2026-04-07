use std::sync::Arc;
use std::time::Instant;

use axum::{
    Json, Router,
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
};
use serde::{Deserialize, Serialize};

use crate::verse::{CollectionConfig, Engine, QueryResult};

/// Embedded HTML chat interface
const CHAT_HTML: &str = include_str!("chat.html");

/// Shared application state for axum handlers.
type AppState = Arc<Engine>;

/// POST /query body
#[derive(Deserialize)]
struct QueryBody {
    question: String,
    /// Target collection name. Omit to auto-route (or use the only collection).
    collection: Option<String>,
    /// Override server-level reranking setting. Omit to use server default.
    rerank: Option<bool>,
    /// Reranking model to use. Omit to use the server default (first configured model).
    rerank_model: Option<String>,
    /// Affect name to apply. Omit for no affect transformation.
    affect: Option<String>,
    /// Affect model to use. Omit to use the server default (first configured model).
    affect_model: Option<String>,
    /// Router model to use. Omit to use the server default.
    router_model: Option<String>,
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
}

async fn handle_query(
    State(engine): State<AppState>,
    Json(body): Json<QueryBody>,
) -> impl IntoResponse {
    let start = Instant::now();

    match engine
        .query(
            &body.question,
            body.collection,
            body.rerank,
            body.rerank_model,
            body.affect,
            body.affect_model,
            body.router_model,
        )
        .await
    {
        Ok(result) => {
            let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
            let response = QueryResponse { result, elapsed_ms };
            (StatusCode::OK, Json(response)).into_response()
        }
        Err(e) => {
            tracing::error!(error = %format!("{:#}", e), "query failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody {
                    error: e.to_string(),
                }),
            )
                .into_response()
        }
    }
}

/// Health / readiness probe.
async fn handle_health() -> StatusCode {
    StatusCode::OK
}

#[derive(Serialize)]
struct ModelsResponse {
    rerank_models: Vec<String>,
    affect_models: Vec<String>,
    router_models: Vec<String>,
}

async fn handle_models(State(engine): State<AppState>) -> impl IntoResponse {
    Json(ModelsResponse {
        rerank_models: engine.rerank_models().to_vec(),
        affect_models: engine.affect_models().to_vec(),
        router_models: engine.rerank_models().to_vec(),
    })
}

#[derive(Serialize)]
struct AffectsResponse {
    affects: Vec<String>,
}

async fn handle_affects(State(engine): State<AppState>) -> impl IntoResponse {
    Json(AffectsResponse {
        affects: engine.affects().iter().map(|s| s.to_string()).collect(),
    })
}

#[derive(Serialize)]
struct CollectionsResponse {
    collections: Vec<CollectionConfig>,
}

async fn handle_collections(State(engine): State<AppState>) -> impl IntoResponse {
    Json(CollectionsResponse {
        collections: engine.collections().to_vec(),
    })
}

/// Serve the embedded chat interface.
async fn handle_chat() -> impl IntoResponse {
    (
        StatusCode::OK,
        [(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")],
        CHAT_HTML,
    )
}

/// Response wrapper that includes timing information.
#[derive(Serialize)]
struct QueryResponse {
    #[serde(flatten)]
    result: QueryResult,
    elapsed_ms: f64,
}

/// Start the HTTP server on the given port and block forever.
pub async fn run(engine: Engine, port: u16) -> anyhow::Result<()> {
    let state: AppState = Arc::new(engine);

    let app = Router::new()
        .route("/query", post(handle_query))
        .route("/models", get(handle_models))
        .route("/affects", get(handle_affects))
        .route("/collections", get(handle_collections))
        .route("/health", get(handle_health))
        .route("/chat", get(handle_chat))
        .with_state(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(address = %addr, "HTTP server listening");
    tracing::info!(
        "Chat interface available at: http://{}:{}/chat",
        "localhost",
        port
    );

    axum::serve(listener, app).await?;

    Ok(())
}
