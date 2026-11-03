use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand};
use decisions_mcp::{
    client,
    config::{self, Settings},
    request_for_tool,
};
use serde_json::{json, Value};
use std::{
    fs::File,
    io::{self, Read},
    path::PathBuf,
};

mod mcp;

#[derive(Parser)]
#[command(version, about = "Decisions classification CLI and stdio MCP server")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Estimate a statement's probability.
    Check(Input),
    /// Select a fixed category.
    Classify(Input),
    /// Apply ordered rubric levels.
    Score(Input),
    /// Ask multiple questions over shared evidence.
    Evaluate(Input),
    /// Serve MCP over stdin/stdout.
    Mcp,
    /// Manage endpoint-specific credentials in the OS credential store.
    Auth {
        #[command(subcommand)]
        command: Auth,
    },
    /// View or update secret-free configuration.
    Config {
        #[command(subcommand)]
        command: Config,
    },
}
#[derive(Subcommand)]
enum Auth {
    Login {
        /// Read the key from stdin instead of a hidden prompt.
        #[arg(long)]
        stdin: bool,
    },
    Logout,
}
#[derive(Subcommand)]
enum Config {
    Show,
    Set { key: String, value: String },
}
#[derive(Args)]
struct Input {
    /// JSON tool arguments file; '-' reads stdin. Defaults to stdin without --text.
    #[arg(short, long)]
    input: Option<PathBuf>,
    /// Plain text evidence (alternative to JSON input).
    #[arg(long, conflicts_with = "input")]
    text: Option<String>,
    #[arg(long)]
    statement: Option<String>,
    #[arg(long)]
    instructions: Option<String>,
    /// JSON array of choice objects.
    #[arg(long)]
    choices: Option<String>,
    /// JSON array of ordered level objects.
    #[arg(long)]
    levels: Option<String>,
    #[arg(long)]
    name: Option<String>,
    /// Opt in to local result cache reads and writes.
    #[arg(long, conflicts_with = "no_cache")]
    cache: bool,
    /// Bypass caching, including use_cache:true in JSON input.
    #[arg(long)]
    no_cache: bool,
}
fn read_bounded(mut reader: impl Read, limit: u64) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    reader
        .by_ref()
        .take(limit + 1)
        .read_to_end(&mut data)
        .context("Could not read input")?;
    if data.len() as u64 > limit {
        bail!("Input exceeds size limit");
    }
    Ok(data)
}
fn arguments(input: Input) -> Result<Value> {
    let mut args = if let Some(text) = input.text {
        json!({"evidence": [{"type":"text", "text":text}]})
    } else {
        let bytes = match input.input {
            Some(path) if path.as_os_str() != "-" => read_bounded(
                File::open(path).context("Could not open input file")?,
                32 * 1024 * 1024,
            )?,
            _ => read_bounded(io::stdin().lock(), 32 * 1024 * 1024)?,
        };
        serde_json::from_slice(&bytes)
            .map_err(|_| anyhow::anyhow!("Input must be a JSON object of tool arguments"))?
    };
    let object = args
        .as_object_mut()
        .context("Input must be a JSON object")?;
    for (name, value) in [
        ("statement", input.statement),
        ("instructions", input.instructions),
        ("name", input.name),
    ] {
        if let Some(value) = value {
            object.insert(name.into(), Value::String(value));
        }
    }
    for (name, value) in [("choices", input.choices), ("levels", input.levels)] {
        if let Some(value) = value {
            object.insert(
                name.into(),
                serde_json::from_str(&value)
                    .map_err(|_| anyhow::anyhow!("{name} must be valid JSON"))?,
            );
        }
    }
    if input.cache {
        object.insert("use_cache".into(), json!(true));
    }
    if input.no_cache {
        object.insert("use_cache".into(), json!(false));
    }
    Ok(args)
}
async fn run_tool(name: &str, input: Input) -> Result<()> {
    let (mut request, use_cache) = request_for_tool(name, arguments(input)?)?;
    request
        .as_object_mut()
        .context("Invalid normalized request")?
        .insert("use_cache".into(), json!(use_cache));
    let result = client::evaluate(&Settings::load()?, request).await?;
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}
#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("Error: {error}");
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    match Cli::parse().command {
        Command::Check(input) => run_tool("check", input).await,
        Command::Classify(input) => run_tool("classify", input).await,
        Command::Score(input) => run_tool("score", input).await,
        Command::Evaluate(input) => run_tool("evaluate", input).await,
        Command::Mcp => mcp::serve().await,
        Command::Auth { command } => {
            let settings = Settings::load()?;
            match command {
                Auth::Login { stdin } => {
                    let key = if stdin {
                        let data = read_bounded(io::stdin().lock(), 16384)?;
                        String::from_utf8(data)
                            .context("API key must be UTF-8")?
                            .trim_end_matches(['\r', '\n'])
                            .to_owned()
                    } else {
                        rpassword::prompt_password("API key (stored locally for this endpoint): ")
                            .context("Could not read API key")?
                    };
                    settings.login(&key)?;
                    eprintln!("Credential saved for {}", settings.base_url);
                }
                Auth::Logout => {
                    settings.logout()?;
                    eprintln!("Stored credential removed for {}", settings.base_url);
                }
            }
            Ok(())
        }
        Command::Config {
            command: Config::Show,
        } => {
            println!("{}", serde_json::to_string_pretty(&Settings::load()?)?);
            Ok(())
        }
        Command::Config {
            command: Config::Set { key, value },
        } => {
            config::set_config(&key, &value)?;
            eprintln!("Configuration updated");
            Ok(())
        }
    }
}
