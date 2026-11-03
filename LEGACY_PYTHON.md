# Decisions MCP

General-purpose local MCP tools backed by OpenAI's Decisions API. This standalone
package has no YouTube dependencies, transcript-fetching logic, or application rubrics.

## Tools

| Tool | Input | Result |
|---|---|---|
| `check` | Evidence + statement | Estimated probability a condition is true |
| `classify` | Evidence + instructions + fixed choices | Selected category and probabilities |
| `score` | Evidence + instructions + ordered levels | Fractional score over zero-based indices |
| `evaluate` | Evidence + up to 400 typed questions | Multiple answers over shared evidence |

All tools use `gpt-6-luna`, preserve API refusals, and return structured results.
No generated explanations. Probabilities are model estimates; validate them on your
own labelled examples before interpreting them as calibrated confidence.

## Install and run

Requires Python 3.11+ and uv.

```sh
uv sync --frozen
uv run --frozen decisions-mcp
```

The server speaks MCP over stdio. It starts and lists tools without an API key.
Actual API calls require `OPENAI_API_KEY` (or `API_KEY`) from your API organization.
A local `.env` is supported; set `DECISIONS_ENV_FILE` to use an explicit file.
Process environment values take precedence over file values. A Codex/ChatGPT
sign-in token cannot be substituted. Configure the key in the environment that
launches Codex; do not store it in this repository.

Merge [examples/codex-config.toml](examples/codex-config.toml) into
`~/.codex/config.toml`. Adjust the command/path for your computer.
Restart the MCP server or Codex after changing the configuration.

## Evidence

Each tool accepts an `evidence` array mixing these objects:

```json
[
  {"type": "text", "text": "Material to evaluate"},
  {"type": "json", "data": {"field": "value"}},
  {"type": "image_url", "url": "https://example.com/photo.png"},
  {"type": "image_path", "path": "/absolute/path/photo.png"},
  {"type": "text_path", "path": "/absolute/path/document.txt"}
]
```

Image evidence supports `detail`: `auto` (default), `low`, `high`, or `original`.
Local PNG/JPEG/GIF/WebP images are encoded by the server, avoiding base64 tool
arguments. `image_url` also accepts validated base64 image data URLs.
JSON evidence is an object or an array of objects, limited to 256 KiB, 32 container
levels, and 10,000 values. The wrapper limits local files to 20 MiB and total API
request JSON to 32 MiB. Text files must be UTF-8. Audio/video/PDF need preprocessing.

Local file access defaults to the server's working directory. Configure
`DECISIONS_ALLOWED_ROOTS` as an OS path-separated list of directories to allow other
locations. Resolved symlink targets must remain inside those directories.
This controls local evidence files; HTTP image URLs are fetched by the API.

## Questions

`check` accepts a `statement`. `classify` accepts `instructions` and 2–255
`choices`, each with a distinct string/boolean `value` and optional `description`.
`score` accepts `instructions` and 2–10 `levels`, each with `label` and optional
`description`. Levels are ordered, not custom numeric scores.

`evaluate` accepts a `questions` array:
```json
[
  {"type": "predicate", "name": "damaged", "instructions": "Is the item damaged?"},
  {"type": "choice", "name": "route", "instructions": "Which team?",
   "choices": [{"value": "support"}, {"value": "billing"}]},
  {"type": "score", "name": "severity", "instructions": "How severe?",
   "levels": [{"label": "minor"}, {"label": "major"}]}
]
```

Question names must be unique. Instructions are text in this first version;
all images belong in shared evidence. Batch only independent questions.
[Example tool arguments](examples/) use generic tasks and need no YouTube data.

## Result cache

A separate SQLite cache hashes the exact normalized API request, including model,
question wording, choices/rubric, and evidence content. Local files are read before
hashing, so changed bytes invalidate results. Inputs, paths, and API keys are not
persisted; API outputs and usage metadata are cached.

- Default: `~/.cache/decisions-mcp/results.sqlite3`.
- `DECISIONS_CACHE_PATH` overrides the location.
- `DECISIONS_CACHE_TTL_SECONDS` defaults to 86400; 0 disables caching.
- Every tool accepts `use_cache=false` to bypass both cache reads and writes.
- HTTP(S) image URLs bypass the cache because their content can change.
- API failures are never cached; valid refusal answers are preserved and cached.
- On a cache hit, `api_called=false`, `usage=null`, and original usage moves to
  `cached_usage`. Cache metadata includes the key and original storage time.

## Development

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
```

Tests use fake HTTP responses and a real stdio handshake, with no paid API calls.

References:
- [Decisions API guide](https://developers.openai.com/api/docs/guides/decisions)
- [Create decision schema](https://developers.openai.com/api/reference/resources/decisions/methods/create)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Supported MCP Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
