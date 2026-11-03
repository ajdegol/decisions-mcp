# Decisions

A Rust CLI and stdio MCP server sharing one Decisions client. Text, structured JSON,
image URLs and local image/text files work through either interface. This repository
has no YouTube dependencies or application-specific scoring prompts.

## Install

With Rust 1.90 or later, from this checkout:

```sh
cargo install --path . --locked
```

This installs the standalone `decisions` binary in Cargo's bin directory (usually
`~/.cargo/bin`). Python and a running MCP server are not required for CLI commands.
From another machine with access to this private repository:

```sh
cargo install --git ssh://git@github.com/ajdegol/decisions-mcp.git --locked
```

## Credentials and configuration

```sh
decisions auth login
decisions config show
```

Login reads a hidden prompt and saves the key in the local OS credential store:
macOS Keychain, Windows Credential Manager, or Linux Secret Service with the native
keyring. Linux builds require `pkg-config` and D-Bus development headers (Ubuntu:
`libdbus-1-dev`). A desktop Secret Service is needed for persistent Linux credentials;
headless machines can use an environment variable. `decisions auth login --stdin`
accepts a key from a secret manager or pipe. Never put a key in a command argument.
`decisions auth logout` removes the stored credential for the selected endpoint.
Each user/machine logs in separately. Copy only the secret-free config between machines.

Configuration lives at `$XDG_CONFIG_HOME/decisions/config.toml` or
`~/.config/decisions/config.toml`; `DECISIONS_CONFIG` overrides the full path.
A missing file defaults to OpenAI (`https://api.openai.com/v1`, `gpt-6-luna`).

For LiteLLM:

```sh
decisions config set base_url https://gateway.example.com/v1
decisions config set model your-luna-alias
decisions auth login
```

Use your LiteLLM key at that prompt. The proxy holds the upstream OpenAI credential.
LiteLLM needs native Decisions support (introduced in v1.104.2), or an
administrator-configured compatible route. The client appends `/decisions` to
`base_url`; include any API prefix such as `/v1` in the configured URL.
HTTPS is required except for loopback development servers. Redirects are disabled.
Credentials are associated with the entire normalized base URL. Switching endpoints
does not reuse the old stored key. A model alias change on the same gateway uses
the same endpoint credential.

Example config (all fields optional):

```toml
base_url = "https://gateway.example.com/v1"
model = "your-luna-alias"
roots = ["/absolute/path/to/evidence"]
cache_ttl = 86400
# cache_path = "/absolute/path/to/results.sqlite3"
```

Environment overrides: `DECISIONS_BASE_URL`, `DECISIONS_MODEL`,
`DECISIONS_ALLOWED_ROOTS` (OS path-separated list), `DECISIONS_CACHE_PATH`, and
`DECISIONS_CACHE_TTL_SECONDS`. Explicit `DECISIONS_API_KEY` or legacy `API_KEY`
override the selected endpoint's stored credential. `OPENAI_API_KEY` is accepted
only for the default OpenAI endpoint, so it cannot silently follow a gateway switch.
No `.env` files are automatically loaded by the Rust binary. Environment credentials
are useful for CI; use the OS store for interactive machines.

## CLI

```sh
decisions check --text 'The screen arrived cracked.' --statement 'Is there physical damage?'
decisions check --input examples/check.json
decisions classify --input examples/classify.json
decisions score --input my-score-request.json
cat request.json | decisions evaluate
```

JSON input uses the same arguments as the MCP tools. `--input -` also reads stdin.
`--text` supplies plain text evidence. `--statement`, `--instructions`, `--name`,
`--choices` (JSON array), and `--levels` (JSON array) can supply or override fields.
Use `--no-cache` to bypass cache reads and writes. Success prints only result JSON
to stdout; failures print a concise error to stderr and exit nonzero.

| Command / MCP tool | Arguments | Answer |
|---|---|---|
| `check` | evidence, statement | Estimated probability (0–1) |
| `classify` | evidence, instructions, 2–255 choices | Fixed category with probabilities |
| `score` | evidence, instructions, 2–10 ordered levels | Fractional score over zero-based indices |
| `evaluate` | evidence, 1–400 typed questions | Multiple answers over shared evidence |

Optional `name` defaults to `result` for single-question tools. `evaluate` requires
unique names. Choices have distinct string/boolean `value` and optional `description`.
Levels have `label` and optional `description`. Refusals are preserved; no generated
explanations. Probabilities are model estimates.

Evidence objects:

```json
[
  {"type":"text","text":"Material to evaluate"},
  {"type":"json","data":{"field":"value"}},
  {"type":"image_url","url":"https://example.com/photo.png"},
  {"type":"image_path","path":"/absolute/path/photo.png"},
  {"type":"text_path","path":"/absolute/path/document.txt"}
]
```

Image `detail` defaults to `auto`; also accepts `low`, `high`, `original`. Validated
base64 PNG/JPEG/GIF/WebP data URLs work too. Local files are limited to 20 MiB,
UTF-8 for text, PNG/JPEG/GIF/WebP for images. Resolved symlink targets must be inside
allowed roots (default: current working directory). JSON evidence is an object or
array of objects, at most 256 KiB, 32 container levels and 10,000 values. Requests
and responses are limited to 32 MiB. Audio/video/PDF need preprocessing.

## Codex / MCP

Merge [examples/codex-config.toml](examples/codex-config.toml) into your Codex config,
using the absolute installed binary path. The command is `decisions mcp`. Codex
starts and stops this process as needed. CLI commands do not start MCP.
Discovery works without credentials; tool calls use the same config, credential
store and cache as CLI commands. Restart the MCP process after config changes.

## Cache

Results are stored under the OS cache directory in `decisions/results.sqlite3`.
The hash includes the normalized request, model, endpoint and a credential digest.
Changed local bytes, prompts, aliases, endpoints or credentials create distinct
entries. Inputs, file paths and credentials are not stored; API answers/usage are.
Remote HTTP(S) images bypass caching because their bytes can change.

TTL defaults to 86400 seconds; 0 disables caching. `use_cache:false` works through
JSON input and MCP. Failures are not cached. Hits return `api_called:false`,
`usage:null`, original usage under `cached_usage`, and `cache.hit:true`.
The Rust cache intentionally does not reuse the old Python cache namespace.

## Development

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked
python3 tests/test_rust_cli.py
```

Tests use local fake HTTP endpoints; no paid API calls or real credentials.
The previous Python implementation remains for compatibility with existing consumers;
see [LEGACY_PYTHON.md](LEGACY_PYTHON.md). Its hardcoded endpoint and dotenv behavior
are separate from the Rust CLI. New development targets Rust.

References: [Decisions API](https://developers.openai.com/api/docs/guides/decisions),
[LiteLLM Decisions](https://docs.litellm.ai/docs/decisions),
[LiteLLM v1.104.2](https://docs.litellm.ai/release_notes/v1.104.2/v1-104-2).
