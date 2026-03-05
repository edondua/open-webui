# Linear Tools (OpenAPI)

Focused OpenAPI wrapper for Linear issue operations, designed to plug into Open WebUI Tool Servers.

## Endpoints

- `GET /health`
- `GET /v1/linear/teams`
- `GET /v1/linear/projects`
- `POST /v1/linear/issues`
- `PATCH /v1/linear/issues/{issue_id}`
- `POST /v1/linear/issues/bulk`

OpenAPI spec URL:

- `http://localhost:8790/openapi.json`

## Environment Variables

Required:

- `LINEAR_API_KEY=<your_linear_api_key>`

Optional:

- `LINEAR_API_URL=https://api.linear.app/graphql`
- `LINEAR_TIMEOUT_SECONDS=30`
- `TOOL_API_KEY=<token_required_by_this_service>`
- `PORT=8790`

Notes:

- Linear auth header uses the API key directly in `Authorization`.
- If `TOOL_API_KEY` is set, this service requires `Authorization: Bearer <TOOL_API_KEY>` from Open WebUI.

## Run Locally

```bash
cd /Users/doruntinaramadani/brain/linear-tools
export LINEAR_API_KEY="lin_api_..."
export TOOL_API_KEY="local-linear-tools-key"   # optional but recommended
./run.sh
```

## Quick Health Check

Without `TOOL_API_KEY`:

```bash
curl -sS http://localhost:8790/health
```

With `TOOL_API_KEY`:

```bash
curl -sS http://localhost:8790/health \
  -H "Authorization: Bearer local-linear-tools-key"
```

## Open WebUI Setup

1. Open `Settings -> Tools -> Add Connection`.
2. Use:
   - `Type`: `OpenAPI`
   - `Name`: `Linear`
   - `ID`: `linear`
   - `URL`: `http://localhost:8790`
   - `Path`: `/openapi.json`
   - `Auth`: `Bearer`
   - `API Key`: value of `TOOL_API_KEY` (or leave empty if not set)
3. Verify connection, then Save.
4. In advanced options, set Function Name Filter List:

```text
list_teams,list_projects,create_issue,update_issue,create_issues_bulk
```

If Open WebUI is in Docker, use `http://host.docker.internal:8790`.

## Recommended System Prompt Add-on

```text
When I ask for task creation, always use the Linear tools.
For each task: choose team/project, set title, clear description, priority, assignee (if provided), and due date (if provided).
After each creation return issue identifier and URL.
```

## Example Create Issue

```bash
curl -sS http://localhost:8790/v1/linear/issues \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local-linear-tools-key" \
  -d '{
    "title": "Implement OpenAPI Linear bridge",
    "description": "Build and connect Linear tool server to Open WebUI",
    "team_id": "<TEAM_ID>",
    "priority": 2
  }'
```
