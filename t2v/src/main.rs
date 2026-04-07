use anyhow::Result;
use clap::{Parser, Subcommand};
use std::collections::HashMap;
use std::time::Duration;

mod affect_transformer;
mod chromadb;
mod embedder;
mod llm_retry;
mod reranker;
mod router;
mod server;
mod verse;

#[derive(Parser)]
#[command(name = "text-to-verse", about = "Semantic bible verse lookup")]
struct Cli {
    /// ChromaDB server URL
    #[arg(long, default_value = "http://localhost:8000", global = true)]
    chromadb_url: String,

    /// ChromaDB collection names (comma-separated; default: verse_embeddings)
    #[arg(
        long,
        value_delimiter = ',',
        default_value = "verse_embeddings",
        global = true
    )]
    collections: Vec<String>,

    /// Disable reading collection list from t2v.registry (use --collections instead)
    #[arg(long, global = true)]
    disable_registry: bool,

    /// Ollama server URL for embeddings
    #[arg(long, default_value = "http://localhost:11434", global = true)]
    ollama_url: String,

    /// Embedding model name
    #[arg(long, default_value = "qwen3-embedding:0.6b", global = true)]
    embedding_model: String,

    /// Number of top results to request from ChromaDB per query
    #[arg(long, default_value_t = 20, global = true)]
    top_n: u32,

    /// How many candidates (after dedup and recency filtering) to pass to the reranker
    #[arg(long, default_value_t = 4, global = true)]
    rerank_n: usize,

    /// Sampling temperature (0 = always best match, higher = more random)
    #[arg(long, default_value_t = 1.0, global = true)]
    temperature: f64,

    /// Maximum number of recently returned item IDs to remember per collection
    #[arg(long, default_value_t = 100, global = true)]
    max_recent: usize,

    /// How long (in seconds) an item ID stays in the recent-returns list
    #[arg(long, default_value_t = 3600, global = true)]
    recent_ttl_secs: u64,

    /// Ollama server URL for reranking
    #[arg(long, global = true)]
    rerank_ollama_url: Option<String>,

    /// Reranking model names (comma-separated; first is the default)
    #[cfg_attr(feature = "expanded-llms", arg(long, value_delimiter = ',', default_value = "qwen2.5-coder:1.5b,qwen3:4b,qwen3:1.7b,lfm2.5-thinking", global = true))]
    #[cfg_attr(not(feature = "expanded-llms"), arg(long, value_delimiter = ',', default_value = "qwen2.5-coder:1.5b", global = true))]
    rerank_models: Vec<String>,

    /// Disable reranking (enabled by default)
    #[arg(long, global = true)]
    disable_rerank: bool,

    /// Router model for multi-collection routing (defaults to first rerank model)
    #[arg(long, global = true)]
    router_model: Option<String>,

    /// Directory containing affect template files (.txt)
    #[arg(long, global = true)]
    affects_dir: Option<std::path::PathBuf>,

    /// LLM models for affect transformation (comma-separated; first is default)
    #[cfg_attr(feature = "expanded-llms", arg(long, value_delimiter = ',', default_value = "qwen2.5-coder:1.5b,qwen3:4b,qwen3:1.7b,lfm2.5-thinking", global = true))]
    #[cfg_attr(not(feature = "expanded-llms"), arg(long, value_delimiter = ',', default_value = "qwen2.5-coder:1.5b", global = true))]
    affect_models: Vec<String>,

    /// API URL for affect transformation LLM (defaults to --ollama-url)
    #[arg(long, global = true)]
    affect_api_url: Option<String>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Ask a question, get a verse, exit
    Query {
        /// The question to look up
        question: String,
        /// Route to a specific collection instead of using the router
        #[arg(long, conflicts_with = "random_collection")]
        collection: Option<String>,
        /// Route to a randomly chosen collection instead of using the router
        #[arg(long, conflicts_with = "collection")]
        random_collection: bool,
        /// Apply a named affect transformation to the result
        #[arg(long)]
        affect: Option<String>,
        /// Apply a randomly chosen affect from --affects-dir
        #[arg(long, conflicts_with = "affect")]
        random_affect: bool,
    },
    /// Start an HTTP server
    Serve {
        /// Port to listen on
        #[arg(long, default_value_t = 3000)]
        port: u16,
    },
    /// List available affect names loaded from --affects-dir
    ListAffects,
    /// List collections registered in ChromaDB
    ListCollections {
        /// Include display name, schema, and description for each collection
        #[arg(long)]
        describe: bool,
    },
    /// List all configured model names
    ListModels {
        /// Include the URL and component role for each model
        #[arg(long)]
        describe: bool,
    },
}

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();

    // Extract rerank URL before moving ollama_url
    let rerank_ollama_url = cli
        .rerank_ollama_url
        .unwrap_or_else(|| cli.ollama_url.clone());

    let affect_api_url = cli.affect_api_url.unwrap_or_else(|| cli.ollama_url.clone());

    // Load affect templates from --affects-dir.
    let affects: HashMap<String, String> = if let Some(ref dir) = cli.affects_dir {
        let entries = std::fs::read_dir(dir)
            .map_err(|e| anyhow::anyhow!("failed to read affects dir {:?}: {}", dir, e))?;
        let mut map = HashMap::new();
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("txt") {
                if let Some(name) = path.file_stem().and_then(|s| s.to_str()) {
                    let content = std::fs::read_to_string(&path).map_err(|e| {
                        anyhow::anyhow!("failed to read affect file {:?}: {}", path, e)
                    })?;
                    map.insert(name.to_string(), content);
                }
            }
        }
        map
    } else {
        HashMap::new()
    };

    // Build collection configs from CLI --collections flag.
    // Registry will override these at runtime if present and not disabled.
    let collections: Vec<verse::CollectionConfig> = cli
        .collections
        .iter()
        .map(|name| verse::CollectionConfig {
            name: name.clone(),
            display_name: String::new(),
            description: String::new(),
            schema: verse::CollectionSchema::Biblical,
        })
        .collect();

    // Handle commands that don't need the engine.
    if let Command::ListModels { describe } = cli.command {
        if describe {
            println!("embedding:  {} ({})", cli.embedding_model, cli.ollama_url);
            for m in &cli.rerank_models {
                println!("rerank:     {} ({})", m, rerank_ollama_url);
            }
            for m in &cli.affect_models {
                println!("affect:     {} ({})", m, affect_api_url);
            }
        } else {
            let mut names = std::collections::BTreeSet::new();
            names.insert(cli.embedding_model.as_str());
            for m in &cli.rerank_models { names.insert(m.as_str()); }
            for m in &cli.affect_models { names.insert(m.as_str()); }
            for name in names { println!("{name}"); }
        }
        return Ok(());
    }

    if let Command::ListCollections { describe } = cli.command {
        let rt = tokio::runtime::Runtime::new()?;
        return rt.block_on(async {
            let entries = chromadb::ChromaManager::read_registry(&cli.chromadb_url).await?;
            if entries.is_empty() {
                println!("No collections found in t2v.registry (ChromaDB at {}).", cli.chromadb_url);
            } else {
                for e in &entries {
                    if describe {
                        println!("{} — {} [{}]", e.name, e.display_name, e.schema);
                        if !e.description.is_empty() {
                            println!("  {}", e.description);
                        }
                    } else {
                        println!("{}", e.name);
                    }
                }
            }
            Ok(())
        });
    }

    if let Command::ListAffects = cli.command {
        if affects.is_empty() {
            println!("No affects loaded. Pass --affects-dir to load affect templates.");
        } else {
            let mut names: Vec<&str> = affects.keys().map(String::as_str).collect();
            names.sort();
            for name in names {
                println!("{name}");
            }
        }
        return Ok(());
    }

    // Snapshot names before they are moved into config.
    let affect_names: Vec<String> = affects.keys().cloned().collect();
    let collection_names: Vec<String> = collections.iter().map(|c| c.name.clone()).collect();

    let config = verse::Config {
        chromadb_url: cli.chromadb_url,
        collections,
        ollama_url: cli.ollama_url,
        embedding_model: cli.embedding_model,
        top_n: cli.top_n,
        rerank_n: cli.rerank_n,
        temperature: cli.temperature,
        max_recent: cli.max_recent,
        recent_ttl: Duration::from_secs(cli.recent_ttl_secs),
        rerank_ollama_url,
        rerank_models: cli.rerank_models,
        enable_rerank: !cli.disable_rerank,
        affects,
        affect_models: cli.affect_models,
        affect_api_url,
        router_model: cli.router_model,
        disable_registry: cli.disable_registry,
    };

    let rt = tokio::runtime::Runtime::new()?;

    match cli.command {
        Command::ListAffects | Command::ListCollections { .. } | Command::ListModels { .. } => unreachable!(),
        Command::Query { question, collection, random_collection, affect, random_affect } => rt.block_on(async {
            use rand::RngExt;
            let chosen_collection = if random_collection {
                let mut names = collection_names.clone();
                names.sort();
                if names.is_empty() { None } else {
                    let idx = rand::rng().random_range(0..names.len());
                    Some(names.remove(idx))
                }
            } else {
                collection
            };
            let chosen_affect = if random_affect {
                let mut names = affect_names.clone();
                names.sort();
                if names.is_empty() { None } else {
                    let idx = rand::rng().random_range(0..names.len());
                    Some(names.remove(idx))
                }
            } else {
                affect
            };
            let engine = verse::Engine::new(config).await?;
            let result = engine
                .query(&question, chosen_collection, None, None, chosen_affect, None, None)
                .await?;
            let output = result.affected_text.as_deref().unwrap_or(&result.text);
            println!("{}", output.trim());
            Ok(())
        }),
        Command::Serve { port } => rt.block_on(async {
            let engine = verse::Engine::new(config).await?;
            server::run(engine, port).await
        }),
    }
}
