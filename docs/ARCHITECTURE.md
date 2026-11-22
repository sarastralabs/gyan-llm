# Architecture and plan

## Goals

- Run on one small server (Oracle free ARM VM: 2 CPU cores, 12 GB RAM, no GPU) with a local
  model (`qwen2.5:3b` through Ollama).
- **Gyan** (ज्ञान, "knowledge"), a product of Sarastra Labs: AI answers with sources.
- A **Gyan-branded web UI** (Sarastra Labs colors and fonts) built from an open-source chat UI.
- Other platforms (apps, automation tools, websites) call the model through a standard
  **OpenAI-compatible API** with per-client API keys.
- A **research** mode: search the web, read pages, answer with numbered sources.
- Redis for limits, the queue and caching.

## Components

```
                nginx (80/443, HTTPS)   <- the only public door
                    │
        ┌───────────┴────────────┐
   LibreChat UI              other platforms
   (Gyan branding)           (API key)
        └───────────┬────────────┘
                    ▼
          Gateway (FastAPI)  ── Redis: rate limits, daily quota, queue, caches
          • /v1/chat/completions   ── SQLite: API keys (hashed), usage
          • /v1/research  (phase 2)
                    │
        ┌───────────┼───────────────┐
      Ollama      SearXNG       page fetcher
    (qwen2.5:3b)  (search)      (+ optional browser)
```

The UI and external platforms use the same gateway, so limits, queueing and usage logs apply
everywhere. Research is also exposed as a model name (for example `gyan-research`), so any
OpenAI-compatible tool can use it.

## Phases

| # | Phase | Status |
|---|---|---|
| 1 | **Gateway**: API keys, OpenAI-compatible chat (streaming), Redis limits and queue, usage, CLI, tests, Docker, CI | **Done** |
| 2 | **Research**: SearXNG search, safe page fetch and extraction, Redis cache, `/v1/research` and a research model alias | **Done** (about 25 to 40 s per answer on the VM) |
| 3 | **UI**: LibreChat v0.8.9 (MIT), branded as Gyan with the Sarastra Labs theme through its config file (no fork), pointed at the gateway | **Done** (verified locally: login, chat, research, light and dark) |
| 4 | **Deploy**: images built on the VM, compose, nginx + HTTPS (certbot), nightly backups, runbook | Done (HTTPS is a manual step, see the runbook) |
| 5 | **Hardening**: logs, metrics, usage view, load test | Planned |

## Web access for the model (decision for phase 2)

Layered, lightest first, because the VM has only 2 cores:

1. **Search: SearXNG** (self-hosted metasearch, no API key, JSON output).
2. **Fetch + extract static pages: `httpx` + Trafilatura.** Fast, no browser. Fetcher blocks
   private/loopback addresses (SSRF), limits size and redirects. Handles most articles and docs.
3. **JavaScript-heavy pages: Crawl4AI (Apache-2.0)** as an optional container, used only when
   step 2 returns too little text, with a concurrency cap of 1 (headless Chromium is heavy).
   Measure its RAM and CPU on the VM before enabling it.
4. **Not chosen for now: Firecrawl.** AGPL-3.0 for the server (SDKs are MIT), a multi-service
   self-host stack (API, Redis, PostgreSQL, browser service), ARM64 support not documented, and
   its search, structured extraction and several other features are cloud-only. Revisit if we
   need large site-wide crawling, or use its hosted API as an optional external fallback.

Results are cached in Redis (about a day) so repeated questions are fast.

Status: steps 1 and 2 are built and verified against the real internet and the real model.
Measured on the 2-core VM: about 25 to 40 seconds per answer (cold page fetches add roughly 10
seconds; the model reading about 800 to 1000 tokens of sources dominates). Step 3 (Crawl4AI) is
deliberately not built yet: we will measure its RAM and CPU on the VM first, and only add it if
too many useful pages come back empty without JavaScript rendering.

## Capacity budget (12 GB RAM)

Model about 3 GB, UI 0.5 to 1 GB, MongoDB about 0.5 GB, Redis under 0.1 GB, SearXNG about
0.2 GB, gateway about 0.15 GB, nginx small: roughly 5 to 6 GB. CPU is the limit, not memory.
Realistic use: demos, internal tools, a few users. The gateway can later route heavy traffic to a
hosted model.

## Security principles

- Only nginx is public; the gateway, Redis, Ollama and SearXNG listen on localhost.
- API keys hashed at rest; per-key limits; failed-attempt blocking.
- Server-side model options cannot be set by clients.
- Secrets live in `.env`, never in git.
