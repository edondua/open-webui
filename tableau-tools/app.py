from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_TITLE = "Tableau Tools"
APP_VERSION = "1.0.0"

DATA_DIR = Path(os.getenv("TABLEAU_DATA_DIR", "/app/data"))
DB_PATH = Path(os.getenv("TABLEAU_DB_PATH", str(DATA_DIR / "tableau.db")))

TABLEAU_SERVER = (os.getenv("TABLEAU_SERVER") or os.getenv("TABLEAU_BASE_URL") or "").rstrip("/")
TABLEAU_SITE_NAME = os.getenv("TABLEAU_SITE_NAME") or os.getenv("TABLEAU_SITE_CONTENT_URL") or ""
TABLEAU_PAT_NAME = os.getenv("TABLEAU_PAT_NAME") or ""
TABLEAU_PAT_VALUE = os.getenv("TABLEAU_PAT_VALUE") or os.getenv("TABLEAU_PAT_SECRET") or ""
TABLEAU_API_VERSION = os.getenv("TABLEAU_API_VERSION", "3.24")
TABLEAU_TIMEOUT_SECONDS = int(os.getenv("TABLEAU_TIMEOUT_SECONDS", "40"))
TABLEAU_PAGE_SIZE = int(os.getenv("TABLEAU_PAGE_SIZE", "100"))

app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SyncRequest(BaseModel):
    resource: str = Field(default="all", description="workbooks|views|datasources|jobs|all")
    max_pages: int = Field(default=20, ge=1, le=200)


class ResyncRequest(BaseModel):
    resource: str = Field(default="all")
    max_pages: int = Field(default=50, ge=1, le=300)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db_conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workbooks_raw (
                tableau_id TEXT PRIMARY KEY,
                name TEXT,
                updated_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS views_raw (
                tableau_id TEXT PRIMARY KEY,
                workbook_id TEXT,
                name TEXT,
                updated_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS datasources_raw (
                tableau_id TEXT PRIMARY KEY,
                name TEXT,
                updated_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs_raw (
                tableau_id TEXT PRIMARY KEY,
                job_type TEXT,
                status TEXT,
                created_at TEXT,
                completed_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_workbooks_name ON workbooks_raw(name);
            CREATE INDEX IF NOT EXISTS idx_views_name ON views_raw(name);
            CREATE INDEX IF NOT EXISTS idx_datasources_name ON datasources_raw(name);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs_raw(status);
            """
        )


def set_state(key: str, value: str | None) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_state(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, utc_now_iso()),
        )


def get_state_rows() -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM sync_state ORDER BY key").fetchall()
    return [dict(r) for r in rows]


class TableauClient:
    def __init__(self):
        self.server = TABLEAU_SERVER
        self.api_version = TABLEAU_API_VERSION
        self.timeout = TABLEAU_TIMEOUT_SECONDS

        if not self.server:
            raise HTTPException(status_code=500, detail="TABLEAU_SERVER is not configured")
        if not TABLEAU_PAT_NAME or not TABLEAU_PAT_VALUE:
            raise HTTPException(status_code=500, detail="TABLEAU_PAT_NAME/TABLEAU_PAT_VALUE are not configured")

    def _api(self, path: str) -> str:
        return f"{self.server}/api/{self.api_version}{path}"

    def signin(self) -> tuple[str, str]:
        payload = {
            "credentials": {
                "personalAccessTokenName": TABLEAU_PAT_NAME,
                "personalAccessTokenSecret": TABLEAU_PAT_VALUE,
                "site": {"contentUrl": TABLEAU_SITE_NAME},
            }
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self._api("/auth/signin"), json=payload)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Tableau signin failed {resp.status_code}: {resp.text[:300]}")

        body = resp.json()
        creds = body.get("credentials", {})
        token = creds.get("token")
        site_id = (creds.get("site") or {}).get("id")
        if not token or not site_id:
            raise HTTPException(status_code=502, detail="Tableau signin response missing token/site id")
        return token, site_id

    def signout(self, token: str) -> None:
        with httpx.Client(timeout=self.timeout) as client:
            client.post(self._api("/auth/signout"), headers={"X-Tableau-Auth": token})

    def paged_get(self, token: str, site_id: str, resource: str, max_pages: int) -> list[dict[str, Any]]:
        # Tableau REST resources:
        # /sites/{site_id}/workbooks
        # /sites/{site_id}/views
        # /sites/{site_id}/datasources
        # /sites/{site_id}/jobs
        items: list[dict[str, Any]] = []
        page_number = 1
        page_size = TABLEAU_PAGE_SIZE

        key_map = {
            "workbooks": "workbook",
            "views": "view",
            "datasources": "datasource",
            "jobs": "job",
        }
        item_key = key_map[resource]

        with httpx.Client(timeout=self.timeout) as client:
            for _ in range(max_pages):
                url = self._api(f"/sites/{site_id}/{resource}")
                resp = client.get(
                    url,
                    headers={"X-Tableau-Auth": token},
                    params={"pageSize": page_size, "pageNumber": page_number},
                )
                if resp.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Tableau {resource} failed {resp.status_code}: {resp.text[:300]}",
                    )

                body = resp.json()
                container = body.get(resource, {})
                page_items = container.get(item_key, [])
                if isinstance(page_items, dict):
                    page_items = [page_items]
                if not page_items:
                    break

                items.extend(page_items)

                pagination = body.get("pagination", {})
                total_available = int(pagination.get("totalAvailable", 0) or 0)
                if len(items) >= total_available and total_available > 0:
                    break

                page_number += 1

        return items


def upsert_workbooks(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    with db_conn() as conn:
        for it in items:
            tid = str(it.get("id"))
            conn.execute(
                """
                INSERT INTO workbooks_raw(tableau_id, name, updated_at, payload, ingested_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(tableau_id)
                DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at, payload=excluded.payload, ingested_at=excluded.ingested_at
                """,
                (tid, it.get("name"), it.get("updatedAt"), json.dumps(it), now),
            )
    return len(items)


def upsert_views(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    with db_conn() as conn:
        for it in items:
            tid = str(it.get("id"))
            wb = it.get("workbook") or {}
            workbook_id = wb.get("id") if isinstance(wb, dict) else None
            conn.execute(
                """
                INSERT INTO views_raw(tableau_id, workbook_id, name, updated_at, payload, ingested_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(tableau_id)
                DO UPDATE SET workbook_id=excluded.workbook_id, name=excluded.name, updated_at=excluded.updated_at, payload=excluded.payload, ingested_at=excluded.ingested_at
                """,
                (tid, workbook_id, it.get("name"), it.get("updatedAt"), json.dumps(it), now),
            )
    return len(items)


def upsert_datasources(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    with db_conn() as conn:
        for it in items:
            tid = str(it.get("id"))
            conn.execute(
                """
                INSERT INTO datasources_raw(tableau_id, name, updated_at, payload, ingested_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(tableau_id)
                DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at, payload=excluded.payload, ingested_at=excluded.ingested_at
                """,
                (tid, it.get("name"), it.get("updatedAt"), json.dumps(it), now),
            )
    return len(items)


def upsert_jobs(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    with db_conn() as conn:
        for it in items:
            tid = str(it.get("id"))
            conn.execute(
                """
                INSERT INTO jobs_raw(tableau_id, job_type, status, created_at, completed_at, payload, ingested_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tableau_id)
                DO UPDATE SET job_type=excluded.job_type, status=excluded.status, created_at=excluded.created_at, completed_at=excluded.completed_at, payload=excluded.payload, ingested_at=excluded.ingested_at
                """,
                (tid, it.get("type"), it.get("finishCode") or it.get("status"), it.get("createdAt"), it.get("completedAt"), json.dumps(it), now),
            )
    return len(items)


def run_sync(resource: str, max_pages: int) -> dict[str, Any]:
    client = TableauClient()
    token, site_id = client.signin()
    try:
        if resource == "workbooks":
            items = client.paged_get(token, site_id, "workbooks", max_pages)
            upserted = upsert_workbooks(items)
        elif resource == "views":
            items = client.paged_get(token, site_id, "views", max_pages)
            upserted = upsert_views(items)
        elif resource == "datasources":
            items = client.paged_get(token, site_id, "datasources", max_pages)
            upserted = upsert_datasources(items)
        elif resource == "jobs":
            items = client.paged_get(token, site_id, "jobs", max_pages)
            upserted = upsert_jobs(items)
        else:
            raise HTTPException(status_code=400, detail="resource must be workbooks|views|datasources|jobs")

        set_state(f"{resource}:last_sync_at", utc_now_iso())
        return {"resource": resource, "fetched": len(items), "upserted": upserted}
    finally:
        client.signout(token)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    init_db()
    return {
        "ok": True,
        "version": APP_VERSION,
        "db_path": str(DB_PATH),
        "has_server": bool(TABLEAU_SERVER),
        "has_site": TABLEAU_SITE_NAME is not None,
        "has_pat_name": bool(TABLEAU_PAT_NAME),
        "has_pat_value": bool(TABLEAU_PAT_VALUE),
    }


@app.post("/sync/run")
def sync_run(req: SyncRequest) -> dict[str, Any]:
    init_db()
    resource = req.resource.lower()
    if resource == "all":
        return {
            "resource": "all",
            "workbooks": run_sync("workbooks", req.max_pages),
            "views": run_sync("views", req.max_pages),
            "datasources": run_sync("datasources", req.max_pages),
            "jobs": run_sync("jobs", req.max_pages),
        }
    return run_sync(resource, req.max_pages)


@app.post("/sync/resync")
def sync_resync(req: ResyncRequest) -> dict[str, Any]:
    init_db()
    resource = req.resource.lower()
    with db_conn() as conn:
        if resource in ("all", "workbooks"):
            conn.execute("DELETE FROM workbooks_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'workbooks:%'")
        if resource in ("all", "views"):
            conn.execute("DELETE FROM views_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'views:%'")
        if resource in ("all", "datasources"):
            conn.execute("DELETE FROM datasources_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'datasources:%'")
        if resource in ("all", "jobs"):
            conn.execute("DELETE FROM jobs_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'jobs:%'")

    return sync_run(SyncRequest(resource=resource, max_pages=req.max_pages))


@app.get("/sync/status")
def sync_status() -> dict[str, Any]:
    init_db()
    with db_conn() as conn:
        counts = {
            "workbooks": conn.execute("SELECT COUNT(*) c FROM workbooks_raw").fetchone()["c"],
            "views": conn.execute("SELECT COUNT(*) c FROM views_raw").fetchone()["c"],
            "datasources": conn.execute("SELECT COUNT(*) c FROM datasources_raw").fetchone()["c"],
            "jobs": conn.execute("SELECT COUNT(*) c FROM jobs_raw").fetchone()["c"],
        }
    return {"counts": counts, "state": get_state_rows()}


@app.get("/data/workbooks")
def data_workbooks(limit: int = Query(default=100, ge=1, le=2000), q: str | None = None) -> dict[str, Any]:
    sql = "SELECT tableau_id, name, updated_at, payload, ingested_at FROM workbooks_raw"
    params: list[Any] = []
    if q:
        sql += " WHERE lower(name) LIKE ?"
        params.append(f"%{q.lower()}%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["payload"] = json.loads(item["payload"])
    return {"count": len(items), "items": items}


@app.get("/data/views")
def data_views(
    limit: int = Query(default=100, ge=1, le=5000),
    workbook_id: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    sql = "SELECT tableau_id, workbook_id, name, updated_at, payload, ingested_at FROM views_raw"
    wh = []
    params: list[Any] = []
    if workbook_id:
        wh.append("workbook_id = ?")
        params.append(workbook_id)
    if q:
        wh.append("lower(name) LIKE ?")
        params.append(f"%{q.lower()}%")
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["payload"] = json.loads(item["payload"])
    return {"count": len(items), "items": items}


@app.get("/data/datasources")
def data_datasources(limit: int = Query(default=100, ge=1, le=2000), q: str | None = None) -> dict[str, Any]:
    sql = "SELECT tableau_id, name, updated_at, payload, ingested_at FROM datasources_raw"
    params: list[Any] = []
    if q:
        sql += " WHERE lower(name) LIKE ?"
        params.append(f"%{q.lower()}%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["payload"] = json.loads(item["payload"])
    return {"count": len(items), "items": items}


@app.get("/data/jobs")
def data_jobs(limit: int = Query(default=100, ge=1, le=2000), status: str | None = None) -> dict[str, Any]:
    sql = "SELECT tableau_id, job_type, status, created_at, completed_at, payload, ingested_at FROM jobs_raw"
    params: list[Any] = []
    if status:
        sql += " WHERE lower(status) = ?"
        params.append(status.lower())
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["payload"] = json.loads(item["payload"])
    return {"count": len(items), "items": items}
