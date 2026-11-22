# Gyan

**Gyan** (ज्ञान, "knowledge") by [Sarastra Labs](https://sarastralabs.com): AI answers with sources.

A private, API-first LLM service for a small server: a local model (Ollama) behind an
**OpenAI-compatible gateway** with API keys, rate limits, a request queue and usage logs, plus
web research that answers with numbered sources. Other platforms call it like they would call
OpenAI, and people use it through a Gyan-branded chat UI (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

```
your apps / n8n / websites ──► gateway (API key, limits, queue, usage) ──► Ollama (qwen2.5:3b)
                                         │
                                       Redis (counters, queue)        SQLite (keys, usage)
```

Names used in the code: the Python package is `gyan_gateway`, the command is `gyan-gateway`,
API keys start with `sk-gyan-`, and the research model is `gyan-research`.

Status: **phases 1 (gateway), 2 (web research), 3 (chat UI) and 4 (deploy) done.** Phase 5
(hardening) is planned. To run it on a server, follow [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Quick start (local or on the VM)

```bash
cp .env.example .env                  # set ADMIN_TOKEN and SEARXNG_SECRET (the file shows how)
docker compose up -d --build          # gateway + Redis + Ollama + SearXNG, bound to 127.0.0.1
docker compose exec ollama ollama pull qwen2.5:3b
docker compose exec gateway gyan-gateway create-key --name my-app --rpm 30 --daily 1000
```

The key is shown once. Use it right away:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-gyan-YOUR-KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:3b","messages":[{"role":"user","content":"Hello"}]}'
```

Any OpenAI SDK works by changing the base URL and key:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-gyan-YOUR-KEY")
reply = client.chat.completions.create(
    model="qwen2.5:3b", messages=[{"role": "user", "content": "Hello"}], stream=True
)
for part in reply:
    print(part.choices[0].delta.content or "", end="")
```

## Chat UI

A Gyan-branded web chat (LibreChat, MIT license) with two choices: **Gyan Chat** and **Gyan
Research** (answers with sources). It looks like sarastralabs.com (warm paper, orange, Inter,
pill buttons, with a dark mode) and is branded through configuration, with no forked source.

```bash
docker compose exec gateway gyan-gateway create-key --name gyan-ui --rpm 300 --daily 5000
# put the printed key in .env as GYAN_API_KEY=, then:
docker compose --profile ui up -d        # open http://localhost:3080
```

Setup, first admin account, and how to change the look: [ui/README.md](ui/README.md).

## API

| Endpoint | Auth | What it does |
|---|---|---|
| `GET /v1/models` | API key | Models clients may use |
| `POST /v1/chat/completions` | API key | Chat, streaming (`"stream": true`) or not. Text only. |
| `POST /v1/research` | API key | Web research: search, read pages, answer with numbered sources |
| `GET /healthz` | none | Process is up |
| `GET /readyz` | none | Redis and Ollama reachable (503 when degraded) |
| `POST /admin/keys` | admin token | Create a key (returned once) |
| `GET /admin/keys` | admin token | List keys (never shows secrets) |
| `POST /admin/keys/{id}/revoke` and `/restore` | admin token | Disable or re-enable a key |
| `GET /admin/usage?hours=24` | admin token | Requests and tokens per key |
| `GET /admin/stats` | admin token | Queue depth, Redis and Ollama status |

Errors use the OpenAI format (`{"error": {"message", "type", "code"}}`), so SDKs show
useful messages. Notable codes: `401 invalid_api_key`, `429 rate_limit_exceeded`
(with `Retry-After`), `503 server_busy` (queue full), `404 model_not_found`.

Parameters forwarded to the model: `temperature`, `top_p`, `max_tokens` (capped by
`MAX_TOKENS_CAP`), `stop`, `seed`, `presence_penalty`, `frequency_penalty`,
`response_format` (`text` or `json_object`). Everything else is ignored, so clients cannot
change server settings such as context size or keep-alive.

## Research (web access for the model)

Ask a question and the gateway searches the web, reads the top pages, and answers using only
those pages, with `[1]`-style citations.

```bash
curl http://127.0.0.1:8000/v1/research -H "Authorization: Bearer sk-gyan-YOUR-KEY" \
  -H "Content-Type: application/json" -d '{"query": "What is Redis used for?"}'
# -> {"answer": "...[1]...", "sources": [{"n": 1, "title": "...", "url": "...", "from_page": true}], ...}
```

Add `"stream": true` for server-sent events (`status`, `sources`, `token`, `done`).

**Inside any OpenAI-compatible tool or chat UI**, use the model name `gyan-research`
(listed in `/v1/models`). It answers from the last user message, streams the text, and appends a
Markdown "Sources" list, so the UI needs no changes.

How it works: SearXNG (self-hosted search, no API key) finds pages; a fetcher downloads them with
SSRF protection; Trafilatura extracts the text; the passages that best match the question are kept
within a character budget; the model writes a cited answer. Searches (1 h) and pages (24 h) are
cached in Redis.

Good to know:

- **Speed:** about 25 to 40 seconds per answer on the 2-core VM (measured). Most of it is the model
  reading the sources (about 800 to 1000 prompt tokens), so `RESEARCH_CHARS_TOTAL` trades speed for
  detail. Quiet periods send SSE keep-alive comments so proxies do not time out.
- **Cost:** a research call uses `RESEARCH_COST` (default 3) units of a key's per-minute and daily
  limits, because it is much heavier than a plain chat.
- **Safety:** the fetcher only reaches public web pages (no private, loopback or metadata
  addresses, odd ports, or credentials in URLs; every redirect hop and the connected address are
  re-checked). Page text is untrusted: the prompt tells the model to ignore instructions inside it,
  which reduces but does not eliminate prompt-injection risk.
- **Limits:** pages that need JavaScript to show their content fall back to the search snippet
  (a browser-based fetcher is planned, see the architecture doc). Follow-up questions are not
  rewritten: only the last user message is searched. A 3B model can still misread or overstate its
  sources, so treat answers as a starting point and check the citations.

## How capacity is protected

A 2-core machine runs one generation at a time (about 7 tokens per second with a 3B model).

- **Queue:** extra requests wait (up to `MAX_QUEUE`, for up to `QUEUE_TIMEOUT_S`); beyond that
  the client gets `503 server_busy` with `Retry-After`. A watchdog frees a slot if a client
  vanishes mid-stream.
- **Per-key limits:** requests per minute and per day, kept in Redis.
- **Brute-force guard:** after `AUTH_FAILURES_PER_MIN` bad keys from one address, that address is
  blocked for a minute.
- **Run one gateway worker.** The slot is held per process (the Docker image already does).

## Security notes

- API keys are random 256-bit values; only their SHA-256 hash is stored.
- Redis, Ollama and the gateway bind to `127.0.0.1`. Public access goes through nginx and HTTPS
  (phase 4). Never expose Ollama (it has no login) or Redis.
- `.env` holds the admin token; it is git-ignored.

## Development

```bash
cd gateway
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest -q       # 124 tests: auth, chat, streaming, limits, queue, research, SSRF
.venv/Scripts/python -m ruff check .
```

Tests use a fake Redis and a fake Ollama, so no network or model is needed.
`gateway/` is a normal Python package (`gyan_gateway`); CI runs lint, tests and a
multi-arch (amd64 + arm64) image build.

## Configuration

All settings are environment variables; see `.env.example`.
