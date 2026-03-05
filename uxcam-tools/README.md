# UXCam Tools (OpenAPI)

OpenAPI service to ingest UXCam API data, store it locally in SQLite, serve cached analytics, and resync on demand.

## What It Does

- Pulls UXCam data from API (`sessions`, `events`)
- Stores data locally (`/app/data/uxcam.db` by default)
- Serves local queries for fast AI responses
- Supports manual resync to refresh data from source

## Endpoints

- `GET /health`
- `POST /sync/run` (incremental or full)
- `POST /sync/resync` (clear local + full resync)
- `GET /sync/status`
- `GET /data/sessions`
- `GET /data/events`

## Environment Variables

Required:

- `UXCAM_API_BASE_URL`
- `UXCAM_API_KEY`

Optional (recommended):

- `UXCAM_PROJECT_ID`
- `UXCAM_DATA_DIR=/app/data`
- `UXCAM_DB_PATH=/app/data/uxcam.db`
- `UXCAM_ENDPOINT_SESSIONS=/sessions`
- `UXCAM_ENDPOINT_EVENTS=/events`
- `UXCAM_ITEMS_FIELD=data`
- `UXCAM_NEXT_CURSOR_FIELD=next_cursor`
- `UXCAM_CURSOR_PARAM=cursor`
- `UXCAM_SINCE_PARAM=from`
- `UXCAM_UNTIL_PARAM=to`
- `UXCAM_PAGE_SIZE_PARAM=page_size`
- `UXCAM_PAGE_SIZE=200`

## Local Run

```bash
cd /Users/doruntinaramadani/brain/uxcam-tools
./run.sh
```

## Railway Deploy

1. Create an `Empty Service`
2. Connect repo: `edondua/open-webui`
3. Root directory: `uxcam-tools`
4. Start command:
   `uvicorn app:app --host 0.0.0.0 --port 8080`
5. Generate domain on target port `8080`
6. Add a volume mounted at `/app/data`
7. Set env vars above

## Open WebUI Integration

Add OpenAPI server URL:

`https://<uxcam-tools-domain>.up.railway.app/openapi.json`

Then in chat, use this flow:

1. `POST /sync/run` to refresh local data (incremental)
2. Query `GET /data/sessions` and `GET /data/events` for answers
3. `POST /sync/resync` when you need guaranteed fresh full pull

## Example Calls

Incremental sync:

```json
POST /sync/run
{
  "resource": "all",
  "force_full": false,
  "max_pages": 20
}
```

Force full resync:

```json
POST /sync/resync
{
  "resource": "all",
  "max_pages": 100
}
```
