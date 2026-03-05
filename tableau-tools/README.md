# Tableau Tools (OpenAPI)

Local-cache Tableau API ingestion for AI queries.

## What It Does

- Connects to Tableau REST API with PAT auth
- Syncs workbooks/views/datasources/jobs
- Stores local cache in SQLite (`/app/data/tableau.db`)
- Serves local query endpoints
- Supports full resync when needed

## Endpoints

- `GET /health`
- `POST /sync/run`
- `POST /sync/resync`
- `GET /sync/status`
- `GET /data/workbooks`
- `GET /data/views`
- `GET /data/datasources`
- `GET /data/jobs`

## Required Environment Variables

- `TABLEAU_SERVER`
- `TABLEAU_SITE_NAME`
- `TABLEAU_PAT_NAME`
- `TABLEAU_PAT_VALUE`

## Optional Environment Variables

- `TABLEAU_API_VERSION=3.24`
- `TABLEAU_TIMEOUT_SECONDS=40`
- `TABLEAU_PAGE_SIZE=100`
- `TABLEAU_DATA_DIR=/app/data`
- `TABLEAU_DB_PATH=/app/data/tableau.db`

## Railway Setup

1. Service name: `tableau-tools`
2. Root directory: `tableau-tools`
3. Start command: `uvicorn app:app --host 0.0.0.0 --port 8080`
4. Volume mount: `/app/data`
5. Generate domain on port `8080`

## Open WebUI

Add OpenAPI server URL:

`https://<tableau-tools-domain>.up.railway.app/openapi.json`

## Typical Workflow

1. `POST /sync/run` (incremental refresh)
2. Ask AI from local cache endpoints (`/data/*`)
3. `POST /sync/resync` only when you need full fresh pull
