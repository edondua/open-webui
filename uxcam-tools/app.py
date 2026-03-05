from __future__ import annotations

import hashlib
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

APP_TITLE = "UXCam Tools"
APP_VERSION = "1.0.1"

DATA_DIR = Path(os.getenv("UXCAM_DATA_DIR", "/app/data"))
DB_PATH = Path(os.getenv("UXCAM_DB_PATH", str(DATA_DIR / "uxcam.db")))

UXCAM_API_BASE_URL = os.getenv("UXCAM_API_BASE_URL", "").rstrip("/")
UXCAM_API_KEY = os.getenv("UXCAM_API_KEY", "")
UXCAM_PROJECT_ID = os.getenv("UXCAM_PROJECT_ID", "")
UXCAM_APP_ID = os.getenv("UXCAM_APP_ID", "") or UXCAM_PROJECT_ID
UXCAM_TIMEOUT_SECONDS = int(os.getenv("UXCAM_TIMEOUT_SECONDS", "30"))
UXCAM_AUTH_MODE = os.getenv("UXCAM_AUTH_MODE", "query").lower()
UXCAM_APP_ID_PARAM = os.getenv("UXCAM_APP_ID_PARAM", "appid")
UXCAM_API_KEY_PARAM = os.getenv("UXCAM_API_KEY_PARAM", "apikey")
UXCAM_PROJECT_HEADER = os.getenv("UXCAM_PROJECT_HEADER", "X-Project-Id")
UXCAM_INCLUDE_PROJECT_HEADER = os.getenv("UXCAM_INCLUDE_PROJECT_HEADER", "false").lower() in (
    "1",
    "true",
    "yes",
)

# API shape is configurable to avoid hard-coding one UXCam response format.
UXCAM_ENDPOINT_SESSIONS = os.getenv("UXCAM_ENDPOINT_SESSIONS", "/sessions")
UXCAM_ENDPOINT_EVENTS = os.getenv("UXCAM_ENDPOINT_EVENTS", "/events")
UXCAM_ITEMS_FIELD = os.getenv("UXCAM_ITEMS_FIELD", "data")
UXCAM_NEXT_CURSOR_FIELD = os.getenv("UXCAM_NEXT_CURSOR_FIELD", "next_cursor")
UXCAM_CURSOR_PARAM = os.getenv("UXCAM_CURSOR_PARAM", "cursor")
UXCAM_SINCE_PARAM = os.getenv("UXCAM_SINCE_PARAM", "from")
UXCAM_UNTIL_PARAM = os.getenv("UXCAM_UNTIL_PARAM", "to")
UXCAM_PAGE_SIZE_PARAM = os.getenv("UXCAM_PAGE_SIZE_PARAM", "page_size")
UXCAM_PAGE_SIZE = int(os.getenv("UXCAM_PAGE_SIZE", "200"))

app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SyncRequest(BaseModel):
    resource: str = Field(default="all", description="sessions | events | all")
    since: str | None = Field(default=None, description="ISO8601 time filter")
    until: str | None = Field(default=None, description="ISO8601 time filter")
    force_full: bool = Field(default=False, description="ignore last sync cursor/time")
    max_pages: int = Field(default=20, ge=1, le=500)


class ResyncRequest(BaseModel):
    resource: str = Field(default="all", description="sessions | events | all")
    max_pages: int = Field(default=50, ge=1, le=500)


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
            CREATE TABLE IF NOT EXISTS sessions_raw (
                ux_id TEXT PRIMARY KEY,
                occurred_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events_raw (
                ux_id TEXT PRIMARY KEY,
                session_id TEXT,
                occurred_at TEXT,
                payload TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_occurred_at ON sessions_raw(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events_raw(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_session_id ON events_raw(session_id);
            """
        )


def get_state(key: str) -> str | None:
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


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


def stable_id(item: dict[str, Any], resource: str) -> str:
    for k in ("id", "_id", "sessionId", "eventId", "uuid"):
        v = item.get(k)
        if v is not None:
            return f"{resource}:{v}"
    raw = json.dumps(item, sort_keys=True, separators=(",", ":"))
    return f"{resource}:sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def occurred_at(item: dict[str, Any]) -> str | None:
    for k in ("occurredAt", "timestamp", "createdAt", "time", "startedAt"):
        v = item.get(k)
        if v:
            return str(v)
    return None


def extract_items(resp_json: Any) -> list[dict[str, Any]]:
    if isinstance(resp_json, list):
        return [x for x in resp_json if isinstance(x, dict)]
    if isinstance(resp_json, dict):
        payload = resp_json.get(UXCAM_ITEMS_FIELD)
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        # fallback for common shapes
        for key in ("items", "results", "sessions", "events"):
            if isinstance(resp_json.get(key), list):
                return [x for x in resp_json[key] if isinstance(x, dict)]
    return []


def extract_next_cursor(resp_json: Any) -> str | None:
    if isinstance(resp_json, dict):
        nxt = resp_json.get(UXCAM_NEXT_CURSOR_FIELD)
        if nxt:
            return str(nxt)
        for key in ("next", "nextCursor", "cursor"):
            if resp_json.get(key):
                return str(resp_json[key])
    return None


def client_headers() -> dict[str, str]:
    if not UXCAM_API_KEY:
        raise HTTPException(status_code=500, detail="UXCAM_API_KEY is not configured")
    headers = {"Accept": "application/json"}
    if UXCAM_AUTH_MODE in ("bearer", "authorization"):
        headers["Authorization"] = f"Bearer {UXCAM_API_KEY}"
    elif UXCAM_AUTH_MODE == "token":
        headers["Authorization"] = f"Token {UXCAM_API_KEY}"
    if UXCAM_PROJECT_ID and UXCAM_INCLUDE_PROJECT_HEADER:
        headers[UXCAM_PROJECT_HEADER] = UXCAM_PROJECT_ID
    return headers


def auth_query_params() -> dict[str, str]:
    if UXCAM_AUTH_MODE not in ("query", "query_params"):
        return {}
    if not UXCAM_APP_ID:
        raise HTTPException(status_code=500, detail="UXCAM_APP_ID or UXCAM_PROJECT_ID is not configured")
    if not UXCAM_API_KEY:
        raise HTTPException(status_code=500, detail="UXCAM_API_KEY is not configured")
    return {
        UXCAM_APP_ID_PARAM: UXCAM_APP_ID,
        UXCAM_API_KEY_PARAM: UXCAM_API_KEY,
    }


def fetch_paginated(
    endpoint: str,
    since: str | None,
    until: str | None,
    max_pages: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not UXCAM_API_BASE_URL:
        raise HTTPException(status_code=500, detail="UXCAM_API_BASE_URL is not configured")

    all_items: list[dict[str, Any]] = []
    next_cursor = cursor

    with httpx.Client(timeout=UXCAM_TIMEOUT_SECONDS) as client:
        for _ in range(max_pages):
            params: dict[str, Any] = {UXCAM_PAGE_SIZE_PARAM: UXCAM_PAGE_SIZE}
            params.update(auth_query_params())
            if since:
                params[UXCAM_SINCE_PARAM] = since
            if until:
                params[UXCAM_UNTIL_PARAM] = until
            if next_cursor:
                params[UXCAM_CURSOR_PARAM] = next_cursor

            url = f"{UXCAM_API_BASE_URL}{endpoint}"
            resp = client.get(url, headers=client_headers(), params=params)
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"UXCam API error {resp.status_code}: {resp.text[:300]}",
                )

            body = resp.json()
            items = extract_items(body)
            all_items.extend(items)

            new_cursor = extract_next_cursor(body)
            if not new_cursor or new_cursor == next_cursor:
                next_cursor = None
                break
            next_cursor = new_cursor

    return all_items, next_cursor


def upsert_sessions(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    count = 0
    with db_conn() as conn:
        for it in items:
            ux_id = stable_id(it, "session")
            conn.execute(
                """
                INSERT INTO sessions_raw(ux_id, occurred_at, payload, ingested_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(ux_id) DO UPDATE SET
                  occurred_at = excluded.occurred_at,
                  payload = excluded.payload,
                  ingested_at = excluded.ingested_at
                """,
                (ux_id, occurred_at(it), json.dumps(it), now),
            )
            count += 1
    return count


def upsert_events(items: list[dict[str, Any]]) -> int:
    now = utc_now_iso()
    count = 0
    with db_conn() as conn:
        for it in items:
            ux_id = stable_id(it, "event")
            session_id = str(it.get("sessionId") or it.get("session_id") or "")
            conn.execute(
                """
                INSERT INTO events_raw(ux_id, session_id, occurred_at, payload, ingested_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(ux_id) DO UPDATE SET
                  session_id = excluded.session_id,
                  occurred_at = excluded.occurred_at,
                  payload = excluded.payload,
                  ingested_at = excluded.ingested_at
                """,
                (ux_id, session_id, occurred_at(it), json.dumps(it), now),
            )
            count += 1
    return count


def sync_resource(resource: str, since: str | None, until: str | None, force_full: bool, max_pages: int) -> dict[str, Any]:
    state_key_cursor = f"{resource}:cursor"
    state_key_since = f"{resource}:last_since"

    cursor = None if force_full else get_state(state_key_cursor)
    effective_since = since if since is not None else (None if force_full else get_state(state_key_since))

    if resource == "sessions":
        items, next_cursor = fetch_paginated(UXCAM_ENDPOINT_SESSIONS, effective_since, until, max_pages, cursor)
        upserted = upsert_sessions(items)
    elif resource == "events":
        items, next_cursor = fetch_paginated(UXCAM_ENDPOINT_EVENTS, effective_since, until, max_pages, cursor)
        upserted = upsert_events(items)
    else:
        raise HTTPException(status_code=400, detail="resource must be sessions or events")

    set_state(state_key_cursor, next_cursor)
    set_state(state_key_since, utc_now_iso() if effective_since is None else effective_since)
    set_state(f"{resource}:last_sync_at", utc_now_iso())

    return {
        "resource": resource,
        "fetched": len(items),
        "upserted": upserted,
        "next_cursor": next_cursor,
        "since": effective_since,
        "until": until,
    }


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
        "has_api_base": bool(UXCAM_API_BASE_URL),
        "has_api_key": bool(UXCAM_API_KEY),
        "has_app_id": bool(UXCAM_APP_ID),
        "auth_mode": UXCAM_AUTH_MODE,
    }


@app.post("/sync/run")
def sync_run(req: SyncRequest) -> dict[str, Any]:
    init_db()
    resource = req.resource.lower()
    if resource == "all":
        s = sync_resource("sessions", req.since, req.until, req.force_full, req.max_pages)
        e = sync_resource("events", req.since, req.until, req.force_full, req.max_pages)
        return {"resource": "all", "sessions": s, "events": e}
    return sync_resource(resource, req.since, req.until, req.force_full, req.max_pages)


@app.post("/sync/resync")
def sync_resync(req: ResyncRequest) -> dict[str, Any]:
    init_db()
    resource = req.resource.lower()
    with db_conn() as conn:
        if resource in ("all", "sessions"):
            conn.execute("DELETE FROM sessions_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'sessions:%'")
        if resource in ("all", "events"):
            conn.execute("DELETE FROM events_raw")
            conn.execute("DELETE FROM sync_state WHERE key LIKE 'events:%'")

    return sync_run(
        SyncRequest(
            resource=resource,
            since=None,
            until=None,
            force_full=True,
            max_pages=req.max_pages,
        )
    )


@app.get("/sync/status")
def sync_status() -> dict[str, Any]:
    init_db()
    with db_conn() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM sync_state ORDER BY key").fetchall()
        sessions_count = conn.execute("SELECT COUNT(*) AS c FROM sessions_raw").fetchone()["c"]
        events_count = conn.execute("SELECT COUNT(*) AS c FROM events_raw").fetchone()["c"]

    return {
        "sessions_count": sessions_count,
        "events_count": events_count,
        "state": [dict(r) for r in rows],
    }


@app.get("/data/sessions")
def data_sessions(
    limit: int = Query(default=100, ge=1, le=2000),
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    init_db()
    sql = "SELECT ux_id, occurred_at, payload, ingested_at FROM sessions_raw"
    wh = []
    params: list[Any] = []
    if since:
        wh.append("occurred_at >= ?")
        params.append(since)
    if until:
        wh.append("occurred_at <= ?")
        params.append(until)
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"])
        items.append(item)
    return {"count": len(items), "items": items}


@app.get("/data/events")
def data_events(
    limit: int = Query(default=100, ge=1, le=5000),
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    init_db()
    sql = "SELECT ux_id, session_id, occurred_at, payload, ingested_at FROM events_raw"
    wh = []
    params: list[Any] = []
    if session_id:
        wh.append("session_id = ?")
        params.append(session_id)
    if since:
        wh.append("occurred_at >= ?")
        params.append(since)
    if until:
        wh.append("occurred_at <= ?")
        params.append(until)
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"])
        items.append(item)
    return {"count": len(items), "items": items}
