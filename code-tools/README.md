# Dua Code Tools (OpenAPI)

Local OpenAPI server for browsing and searching the `dua-codebase` repository from Open WebUI.

## Endpoints

- `GET /health`
- `GET /list_services`
- `POST /search_code`
- `POST /read_file`
- `GET /find_references`

OpenAPI spec URL: `http://localhost:8787/openapi.json`

## Run

```bash
cd /Users/doruntinaramadani/brain/code-tools
chmod +x run.sh
./run.sh
```

## Connect In Open WebUI

1. Go to `Admin/Workspace -> Tools -> OpenAPI Servers`.
2. Add: `http://localhost:8787/openapi.json`
3. If Open WebUI runs in Docker, use: `http://host.docker.internal:8787/openapi.json`
4. Enable the tool server for your model/chat.

## Railway Deploy

Set these environment variables in Railway service:

- `REPO_ROOT=/workspace/dua-codebase`
- `REPO_URL=https://github.com/ilir93/dua-codebase.git`
- `GITHUB_TOKEN=<token with repo read access>` (required for private repo)
- `PORT` is provided by Railway automatically

Then set the start command to:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Use your Railway URL in Open WebUI:

`https://<your-service>.up.railway.app/openapi.json`

## Notes

- Repo root comes from env var `REPO_ROOT` (default: `/workspace/dua-codebase`)
- If repo path does not exist, server will clone from `REPO_URL` on first request.
- Requests are path-sandboxed to that repository.
- `search_code` and `find_references` use `rg` (`ripgrep`).
