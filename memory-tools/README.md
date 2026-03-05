# memory-tools

OpenAPI tool server for semantic company memory + hybrid search + evidence memory.

## What it does

- Store reusable company knowledge (`/ingest/doc`, `/ingest/batch`)
- Store investigation/tool evidence (`/evidence/write`, `/evidence/write_batch`)
- Query with hybrid search (keyword + vector):
  - `/search/docs`
  - `/search/evidence`
  - `/search` (combined)

## Environment variables

Required:

- `MEMORY_DATABASE_URL` (or `DATABASE_URL`) -> Postgres connection string

Optional:

- `MEMORY_EMBEDDING_PROVIDER` = `none` (default) or `openai`
- `MEMORY_OPENAI_API_KEY` (or `OPENAI_API_KEY`)
- `MEMORY_OPENAI_MODEL` (default `text-embedding-3-small`)
- `MEMORY_EMBEDDING_DIM` (default `1536`)
- `MEMORY_OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
- `MEMORY_TIMEOUT_SECONDS` (default `30`)

## Railway setup

1. Create service `memory-tools`
2. Connect repo `edondua/open-webui`
3. Root directory: `memory-tools`
4. Start command: `uvicorn app:app --host 0.0.0.0 --port 8080`
5. Add env vars listed above
6. Generate domain on port `8080`

Then in Open WebUI:

- `Admin -> Settings -> Integrations -> Manage Tool Servers`
- Add `https://<memory-tools-domain>/openapi.json`

## First smoke tests

Health:

```bash
curl -sS https://<memory-tools-domain>/health
```

Ingest one doc:

```bash
curl -sS -X POST https://<memory-tools-domain>/ingest/doc \
  -H 'content-type: application/json' \
  -d '{
    "kind": "definition",
    "title": "create_profile",
    "body": "Event fired when a new user completes profile creation.",
    "tags": {"domain":"analytics","event":"create_profile"},
    "source": "event-specs"
  }'
```

Write one evidence item:

```bash
curl -sS -X POST https://<memory-tools-domain>/evidence/write \
  -H 'content-type: application/json' \
  -d '{
    "question": "Why did Android onboarding drop?",
    "summary": "Drop correlates with registration->profile step on Android build 5.0.5.",
    "tool": "uxcam-tools",
    "endpoint": "/data/events",
    "tags": {"kpi":"onboarding","platform":"android"},
    "provenance": {"as_of":"2026-03-05"}
  }'
```

Hybrid search:

```bash
curl -sS -X POST https://<memory-tools-domain>/search \
  -H 'content-type: application/json' \
  -d '{
    "query": "create profile drop android onboarding",
    "limit": 10,
    "weight_vector": 0.6,
    "weight_keyword": 0.4
  }'
```

## Suggested system prompt routing

1. Search memory first (`/search`)
2. If memory is stale/missing confidence, call live tools (`code-tools`, `uxcam-tools`, `tableau-tools`)
3. Save summary/evidence back to memory (`/evidence/write`)
