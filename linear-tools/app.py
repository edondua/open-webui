from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_TITLE = "Linear Tools"
APP_VERSION = "1.1.0"

LINEAR_API_URL = os.getenv("LINEAR_API_URL", "https://api.linear.app/graphql")
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY", "").strip()
LINEAR_TIMEOUT_SECONDS = int(os.getenv("LINEAR_TIMEOUT_SECONDS", "30"))
TOOL_API_KEY = os.getenv("TOOL_API_KEY", "").strip()

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=(
        "Tools for managing Linear project management: list teams, projects, "
        "workflow states, and issues. Create, update, and search issues. "
        "Use these tools instead of generating code snippets — they call the "
        "Linear API directly."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request Models ───────────────────────────────────────────────────

class IssueCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="Issue title")
    description: str | None = Field(default=None, description="Markdown description with context, acceptance criteria, etc.")
    team_id: str = Field(..., min_length=1, description="Team ID (get from /v1/linear/teams)")
    project_id: str | None = Field(default=None, description="Project ID (get from /v1/linear/projects)")
    state_id: str | None = Field(default=None, description="Workflow state ID (get from /v1/linear/states)")
    priority: int | None = Field(default=None, ge=0, le=4, description="0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low")
    assignee_id: str | None = Field(default=None, description="User ID to assign")
    label_ids: list[str] | None = Field(default=None, description="Label IDs to attach")
    due_date: date | None = Field(default=None, description="Due date (YYYY-MM-DD)")
    estimate: int | None = Field(default=None, ge=0, description="Story point estimate")


class IssueUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500, description="New title")
    description: str | None = Field(default=None, description="New description (markdown)")
    project_id: str | None = Field(default=None, description="Move to project ID")
    state_id: str | None = Field(default=None, description="Change workflow state ID")
    priority: int | None = Field(default=None, ge=0, le=4, description="0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low")
    assignee_id: str | None = Field(default=None, description="Reassign to user ID")
    label_ids: list[str] | None = Field(default=None, description="Replace label IDs")
    due_date: date | None = Field(default=None, description="Due date (YYYY-MM-DD)")
    estimate: int | None = Field(default=None, ge=0, description="Story point estimate")


class BulkIssueCreateRequest(BaseModel):
    team_id: str = Field(..., min_length=1, description="Default team ID for all issues")
    project_id: str | None = Field(default=None, description="Default project ID for all issues")
    issues: list[IssueCreateRequest] = Field(..., min_length=1, description="List of issues to create")


# ── Auth ─────────────────────────────────────────────────────────────

def _auth_guard(authorization: str | None = Header(default=None)) -> None:
    if not TOOL_API_KEY:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != TOOL_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


# ── Linear GraphQL helper ────────────────────────────────────────────

def _linear_request(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    if not LINEAR_API_KEY:
        raise HTTPException(status_code=500, detail="LINEAR_API_KEY is not configured")

    headers = {
        "Authorization": LINEAR_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: dict[str, Any] = {"query": query, "variables": variables or {}}

    try:
        with httpx.Client(timeout=LINEAR_TIMEOUT_SECONDS) as client:
            response = client.post(LINEAR_API_URL, headers=headers, json=payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Linear request failed: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Linear API HTTP {response.status_code}: {response.text}")

    try:
        body = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Linear API returned non-JSON response") from exc

    if body.get("errors"):
        raise HTTPException(status_code=502, detail={"linear_errors": body["errors"]})

    return body.get("data", {})


def _issue_create_payload(issue: IssueCreateRequest, fallback_team_id: str, fallback_project_id: str | None) -> dict[str, Any]:
    team_id = issue.team_id or fallback_team_id
    project_id = issue.project_id if issue.project_id is not None else fallback_project_id
    return {
        "title": issue.title,
        "description": issue.description,
        "teamId": team_id,
        "projectId": project_id,
        "stateId": issue.state_id,
        "priority": issue.priority,
        "assigneeId": issue.assignee_id,
        "labelIds": issue.label_ids,
        "dueDate": issue.due_date.isoformat() if issue.due_date else None,
        "estimate": issue.estimate,
    }


# ── Endpoints ────────────────────────────────────────────────────────

@app.get("/health", dependencies=[Depends(_auth_guard)])
def health() -> dict[str, Any]:
    """Health check for the Linear Tools service."""
    return {
        "ok": True,
        "service": APP_TITLE,
        "version": APP_VERSION,
        "linear_api_key_configured": bool(LINEAR_API_KEY),
    }


@app.get(
    "/v1/linear/teams",
    dependencies=[Depends(_auth_guard)],
    summary="List all Linear teams",
    description="Returns all teams in the Linear workspace. Use this first to get team IDs needed for creating issues.",
)
def list_teams() -> dict[str, Any]:
    query = """
    query Teams {
      teams {
        nodes {
          id
          key
          name
          description
        }
      }
    }
    """
    data = _linear_request(query)
    return {"teams": data.get("teams", {}).get("nodes", [])}


@app.get(
    "/v1/linear/projects",
    dependencies=[Depends(_auth_guard)],
    summary="List Linear projects",
    description="Returns all projects, optionally filtered by team. Use this to get project IDs for organizing issues under a project.",
)
def list_projects(
    team_id: str | None = Query(default=None, description="Filter by team ID"),
    include_archived: bool = Query(default=False, description="Include archived projects"),
) -> dict[str, Any]:
    query = """
    query Projects($includeArchived: Boolean!) {
      projects(includeArchived: $includeArchived) {
        nodes {
          id
          name
          description
          state
          teams {
            nodes { id key name }
          }
        }
      }
    }
    """
    data = _linear_request(query, {"includeArchived": include_archived})
    projects = data.get("projects", {}).get("nodes", [])

    if team_id:
        projects = [
            p for p in projects
            if any(t.get("id") == team_id for t in p.get("teams", {}).get("nodes", []))
        ]

    return {"projects": projects}


@app.get(
    "/v1/linear/states",
    dependencies=[Depends(_auth_guard)],
    summary="List workflow states for a team",
    description=(
        "Returns all workflow states (e.g. Backlog, Todo, In Progress, Done) for a team. "
        "Use this to get state IDs when creating or updating issues."
    ),
)
def list_states(
    team_id: str = Query(..., description="Team ID to get states for"),
) -> dict[str, Any]:
    query = """
    query TeamStates($teamId: String!) {
      team(id: $teamId) {
        states {
          nodes {
            id
            name
            type
            position
          }
        }
      }
    }
    """
    data = _linear_request(query, {"teamId": team_id})
    states = data.get("team", {}).get("states", {}).get("nodes", [])
    states.sort(key=lambda s: s.get("position", 0))
    return {"states": states}


@app.get(
    "/v1/linear/issues",
    dependencies=[Depends(_auth_guard)],
    summary="Search and list Linear issues",
    description=(
        "Search issues by text query, or list issues filtered by team, project, or state. "
        "Use this to find existing issues before creating duplicates, or to check current work."
    ),
)
def list_issues(
    query: str | None = Query(default=None, description="Text search query (searches title and description)"),
    team_id: str | None = Query(default=None, description="Filter by team ID"),
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    state_name: str | None = Query(default=None, description="Filter by state name (e.g. 'In Progress', 'Todo')"),
    limit: int = Query(default=25, ge=1, le=100, description="Max results to return"),
) -> dict[str, Any]:
    # Build filter object
    filters: dict[str, Any] = {}
    if team_id:
        filters["team"] = {"id": {"eq": team_id}}
    if project_id:
        filters["project"] = {"id": {"eq": project_id}}
    if state_name:
        filters["state"] = {"name": {"eqIgnoreCase": state_name}}

    if query:
        gql = """
        query SearchIssues($query: String!, $limit: Int!) {
          searchIssues(query: $query, first: $limit) {
            nodes {
              id
              identifier
              title
              description
              url
              priority
              state { id name type }
              team { id key name }
              project { id name }
              assignee { id name }
              labels { nodes { id name } }
              createdAt
              updatedAt
            }
          }
        }
        """
        data = _linear_request(gql, {"query": query, "limit": limit})
        issues = data.get("searchIssues", {}).get("nodes", [])
    else:
        gql = """
        query ListIssues($filter: IssueFilter, $limit: Int!) {
          issues(filter: $filter, first: $limit, orderBy: updatedAt) {
            nodes {
              id
              identifier
              title
              description
              url
              priority
              state { id name type }
              team { id key name }
              project { id name }
              assignee { id name }
              labels { nodes { id name } }
              createdAt
              updatedAt
            }
          }
        }
        """
        data = _linear_request(gql, {"filter": filters if filters else None, "limit": limit})
        issues = data.get("issues", {}).get("nodes", [])

    return {"count": len(issues), "issues": issues}


@app.get(
    "/v1/linear/issues/{issue_id}",
    dependencies=[Depends(_auth_guard)],
    summary="Get a single Linear issue by ID",
    description="Returns full details of one issue including comments. Use the issue UUID or identifier (e.g. 'ENG-123').",
)
def get_issue(issue_id: str) -> dict[str, Any]:
    query = """
    query Issue($id: String!) {
      issue(id: $id) {
        id
        identifier
        title
        description
        url
        priority
        state { id name type }
        team { id key name }
        project { id name }
        assignee { id name }
        labels { nodes { id name } }
        comments {
          nodes {
            id
            body
            user { id name }
            createdAt
          }
        }
        createdAt
        updatedAt
      }
    }
    """
    data = _linear_request(query, {"id": issue_id})
    issue = data.get("issue")
    if not issue:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found")
    return {"issue": issue}


@app.post(
    "/v1/linear/issues",
    dependencies=[Depends(_auth_guard)],
    summary="Create a new Linear issue",
    description=(
        "Creates a new issue in Linear. Requires team_id (get from /v1/linear/teams). "
        "Optionally set project_id, state_id, priority (1=Urgent,2=High,3=Medium,4=Low), "
        "assignee, labels, due date, and estimate."
    ),
)
def create_issue(payload: IssueCreateRequest) -> dict[str, Any]:
    mutation = """
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue {
          id
          identifier
          title
          url
          priority
          state { id name }
          team { id key name }
          project { id name }
        }
      }
    }
    """
    issue_input = _issue_create_payload(payload, payload.team_id, payload.project_id)
    data = _linear_request(mutation, {"input": issue_input})
    created = data.get("issueCreate", {})
    return {
        "success": created.get("success", False),
        "issue": created.get("issue"),
    }


@app.patch(
    "/v1/linear/issues/{issue_id}",
    dependencies=[Depends(_auth_guard)],
    summary="Update an existing Linear issue",
    description="Update any field on an existing issue: title, description, state, priority, assignee, etc.",
)
def update_issue(issue_id: str, payload: IssueUpdateRequest) -> dict[str, Any]:
    mutation = """
    mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success
        issue {
          id
          identifier
          title
          url
          priority
          state { id name }
          team { id key name }
          project { id name }
        }
      }
    }
    """
    update_input: dict[str, Any] = {
        "title": payload.title,
        "description": payload.description,
        "projectId": payload.project_id,
        "stateId": payload.state_id,
        "priority": payload.priority,
        "assigneeId": payload.assignee_id,
        "labelIds": payload.label_ids,
        "dueDate": payload.due_date.isoformat() if payload.due_date else None,
        "estimate": payload.estimate,
    }
    update_input = {k: v for k, v in update_input.items() if v is not None}

    if not update_input:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    data = _linear_request(mutation, {"id": issue_id, "input": update_input})
    updated = data.get("issueUpdate", {})
    return {
        "success": updated.get("success", False),
        "issue": updated.get("issue"),
    }


@app.post(
    "/v1/linear/issues/bulk",
    dependencies=[Depends(_auth_guard)],
    summary="Create multiple Linear issues at once",
    description=(
        "Bulk-create issues under one team and optionally one project. "
        "Useful for creating a full set of tasks from a research plan."
    ),
)
def create_issues_bulk(payload: BulkIssueCreateRequest) -> dict[str, Any]:
    mutation = """
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue {
          id
          identifier
          title
          url
        }
      }
    }
    """
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for issue in payload.issues:
        issue_input = _issue_create_payload(issue, payload.team_id, payload.project_id)
        try:
            data = _linear_request(mutation, {"input": issue_input})
            result = data.get("issueCreate", {})
            if result.get("success") and result.get("issue"):
                created.append(result["issue"])
            else:
                failed.append({"title": issue.title, "error": "Linear returned unsuccessful response"})
        except HTTPException as exc:
            failed.append({"title": issue.title, "error": str(exc.detail)})

    return {
        "success": len(failed) == 0,
        "created_count": len(created),
        "failed_count": len(failed),
        "created": created,
        "failed": failed,
    }
