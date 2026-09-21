# DigitalMe

DigitalMe is a local-first AI persona module for DeathTG. It imports a Telegram Desktop JSON export, derives style and per-contact relationship profiles, stores data in SQLite, and produces short multi-message replies through one global FIFO worker.

## Default local setup

The module starts with these values:

```text
provider = "ollama"
model = "qwen3:8b"
base_url = "http://127.0.0.1:11434"
enable_thinking = False
```

No API key is needed for Ollama. Install and start Ollama, then download the model once:

```powershell
ollama pull qwen3:8b
ollama serve
```

After DTG starts and you log in, run `.aitest` in Saved Messages. A healthy result shows provider, model, `Thinking: OFF`, latency, optional TTFT, and `Status: OK`.

## Primary commands

| Command | Purpose |
| --- | --- |
| `.aistart` | Enables automatic replies in eligible private chats. |
| `.aistop` | Immediately disables automatic replies and cancels waiting work. |
| `.aitakeinfo` | Reply to Telegram Desktop's `result.json` document, then run this command to download, import, and analyze it. |
| `.aitoken <token>` | Stores an API key for a remote OpenAI-compatible or OpenRouter provider. The value is never echoed back. |

`.aitoken local qwen3:8b` is a convenience command for returning to the default local Ollama configuration. It does not need a key.

Useful owner-only diagnostics and controls:

| Command | Purpose |
| --- | --- |
| `.aitest` | Safely tests the provider without sending a message to a contact. |
| `.aistatus` | Shows import counts, profiles, RAG state, current provider, and queue state. |
| `.aiqueue` | Shows the single global FIFO queue. |
| `.aiallow` | In a direct chat, limits future automatic replies to that chat once an allowlist exists. |
| `.aideny` | Disables automatic replies for a direct chat. |
| `.clone [--chat <id>] <text>` | Previews a generated reply without sending it. |
| `.aitakeinfo status` | Shows import progress. |
| `.aitakeinfo cancel` | Requests cancellation of an active import. |

All module commands are owner-only.

## Importing Telegram history

In Telegram Desktop, export data as JSON and include personal chats. Send the resulting `result.json` to Saved Messages (or any chat where you can reply as owner), reply to the document with `.aitakeinfo`, and wait for the completion message.

The importer reads the export incrementally rather than loading it all into memory. It accepts only personal/private chats and normal text messages. The owner is identified from numeric Telegram IDs, never display names. After import DigitalMe builds dialogue turns, a personality profile, relationship profiles, rolling chat summaries, and SQLite FTS/RAG documents.

The raw downloaded export and database remain local under `modules/DigitalMe/data/` and are ignored by Git. Do not share this directory or back it up to an untrusted location.

## Automatic reply behavior

DigitalMe only considers direct private chats with normal users. It ignores groups, channels, bots, service messages, Saved Messages, outgoing messages, and empty/media-only input.

There is exactly one generation worker. Short bursts from one person are buffered for `debounce_seconds` (default 2.5 seconds) while preserving separate bubbles. Tasks then run in global FIFO order. A later message from a chat already being generated becomes a new task at the end of the queue.

Until an allowlist is configured, every eligible private chat is allowed. To restrict replies to one chat, open that direct chat, run `.aiallow`, then run `.aistart`. Once at least one chat is allowed, only listed chats can receive automatic replies.

## Providers and privacy

Ollama uses `/api/chat` with HTTP streaming internally. DigitalMe accumulates the final response locally, validates its JSON structure, then sends finished Telegram bubbles; it never edits Telegram once per token.

For a model that declares native thinking support, DigitalMe sends the native Ollama `think` parameter. Normal live replies, `.clone`, and `.aitest` request thinking off. Separate `thinking`, `reasoning_content`, and analysis fields are discarded. Complete `<think>...</think>` or `<thinking>...</thinking>` blocks are removed before structured validation. If a server or model cannot advertise the native parameter, generation continues normally and only the owner receives a rate-limited warning.

For remote providers, select `openrouter` or `openai_compatible` in the module config, set the appropriate `base_url` and `model`, then use `.aitoken <token>`. Prompt sanitization is enabled by default for non-Ollama providers. API keys are secret configuration values and are never shown in commands, status text, prompts, or owner diagnostics.

## Storage and dependencies

SQLite database: `modules/DigitalMe/data/digitalme.sqlite3`.

The module needs `aiohttp`, `aiosqlite`, and `ijson`; `ijson` is included in both the module and DTG runtime requirements so a normal DTG startup can install it when missing. Optional semantic embeddings use `sentence-transformers` only when `embedding_mode` is changed from `off`.
