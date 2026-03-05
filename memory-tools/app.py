from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_TITLE = "Memory Tools"
APP_VERSION = "1.0.0"

DATABASE_URL = os.getenv("MEMORY_DATABASE_URL") or os.getenv("DATABASE_URL", "")
EMBEDDING_PROVIDER = os.getenv("MEMORY_EMBEDDING_PROVIDER", "none").lower()
OPENAI_API_KEY = os.getenv("MEMORY_OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
OPENAI_EMBED_MODEL = os.getenv("MEMORY_OPENAI_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("MEMORY_EMBEDDING_DIM", "1536"))
OPENAI_BASE_URL = os.getenv("MEMORY_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TIMEOUT_SECONDS = int(os.getenv("MEMORY_TIMEOUT_SECONDS", "30"))
VECTOR_INDEX_LISTS = int(os.getenv("MEMORY_VECTOR_INDEX_LISTS", "100"))
VECTOR_PROBES = int(os.getenv("MEMORY_VECTOR_PROBES", "10"))
REQUIRE_VECTOR = os.getenv("MEMORY_REQUIRE_VECTOR", "false").lower() in ("1", "true", "yes")

VECTOR_DB_ENABLED = False


app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MemoryDocIn(BaseModel):
    id: str | None = None
    kind: str = Field(default="note", description="spec|definition|taxonomy|investigation|ownership|note")
    title: str | None = None
    body: str
    summary: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    updated_at: str | None = None
    embedding: list[float] | None = None


class EvidenceIn(BaseModel):
    id: str | None = None
    question: str
    summary: str
    tool: str | None = None
    endpoint: str | None = None
    as_of: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    snippet: str | None = None
    embedding: list[float] | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=200)
    kinds: list[str] | None = None
    tags_contains: dict[str, Any] | None = None
    min_score: float = 0.0
    weight_vector: float = 0.6
    weight_keyword: float = 0.4


class SearchResult(BaseModel):
    id: str
    source_table: str
    title: str | None = None
    body: str | None = None
    summary: str | None = None
    tags: dict[str, Any]
    score: float
    keyword_score: float
    vector_score: float
    tool: str | None = None
    endpoint: str | None = None
    as_of: str | None = None
    updated_at: str


class SearchResponse(BaseModel):
    count: int
    items: list[SearchResult]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_or_now(value: str | None) -> str:
    if not value:
        return utc_now_iso()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return utc_now_iso()


def vec_to_pg_literal(vec: list[float]) -> str:
    if len(vec) != EMBEDDING_DIM:
        raise HTTPException(status_code=400, detail=f"embedding length must be {EMBEDDING_DIM}")
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def ensure_db() -> None:
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="MEMORY_DATABASE_URL/DATABASE_URL is not configured")



def db_conn() -> psycopg.Connection:
    ensure_db()
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_db() -> None:
    global VECTOR_DB_ENABLED
    with db_conn() as conn:
        with conn.cursor() as cur:
            vector_error: str | None = None
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                VECTOR_DB_ENABLED = True
            except Exception as ex:
                VECTOR_DB_ENABLED = False
                vector_error = str(ex)
                if REQUIRE_VECTOR:
                    raise HTTPException(status_code=500, detail=f"pgvector unavailable: {vector_error}")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS memory_docs (
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  title TEXT,
                  body TEXT NOT NULL,
                  summary TEXT,
                  tags JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                  source TEXT,
                  created_at TIMESTAMPTZ NOT NULL,
                  updated_at TIMESTAMPTZ NOT NULL,
                  embedding_json JSONB
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS memory_evidence (
                  id TEXT PRIMARY KEY,
                  question TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  tool TEXT,
                  endpoint TEXT,
                  as_of TIMESTAMPTZ,
                  tags JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                  provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                  snippet TEXT,
                  created_at TIMESTAMPTZ NOT NULL,
                  updated_at TIMESTAMPTZ NOT NULL,
                  embedding_json JSONB
                )
                """
            )

            if VECTOR_DB_ENABLED:
                cur.execute(f"ALTER TABLE memory_docs ADD COLUMN IF NOT EXISTS embedding vector({EMBEDDING_DIM})")
                cur.execute(f"ALTER TABLE memory_evidence ADD COLUMN IF NOT EXISTS embedding vector({EMBEDDING_DIM})")

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_docs_fts
                ON memory_docs USING GIN (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(body,'') || ' ' || coalesce(summary,'')))
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_docs_tags ON memory_docs USING GIN (tags)")
            if VECTOR_DB_ENABLED:
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_memory_docs_embedding
                    ON memory_docs USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = {VECTOR_INDEX_LISTS})
                    """
                )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_evidence_fts
                ON memory_evidence USING GIN (to_tsvector('simple', coalesce(question,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(snippet,'')))
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_evidence_tags ON memory_evidence USING GIN (tags)")
            if VECTOR_DB_ENABLED:
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_memory_evidence_embedding
                    ON memory_evidence USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = {VECTOR_INDEX_LISTS})
                    """
                )


def embed_text(text: str) -> list[float] | None:
    if EMBEDDING_PROVIDER in ("none", "disabled"):
        return None
    if EMBEDDING_PROVIDER != "openai":
        raise HTTPException(status_code=500, detail=f"Unsupported MEMORY_EMBEDDING_PROVIDER: {EMBEDDING_PROVIDER}")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="MEMORY_OPENAI_API_KEY/OPENAI_API_KEY is not configured")

    payload = {"model": OPENAI_EMBED_MODEL, "input": text}
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        resp = client.post(f"{OPENAI_BASE_URL}/embeddings", headers=headers, json=payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Embedding API error {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    data = body.get("data") or []
    if not data or "embedding" not in data[0]:
        raise HTTPException(status_code=502, detail="Embedding API returned no embedding")
    vec = data[0]["embedding"]
    if len(vec) != EMBEDDING_DIM:
        raise HTTPException(
            status_code=502,
            detail=f"Embedding dimension mismatch: got {len(vec)}, expected {EMBEDDING_DIM}. Set MEMORY_EMBEDDING_DIM correctly.",
        )
    return [float(x) for x in vec]


def choose_embedding(explicit: list[float] | None, text_for_embedding: str) -> list[float] | None:
    if not VECTOR_DB_ENABLED:
        return None
    if explicit is not None:
        if len(explicit) != EMBEDDING_DIM:
            raise HTTPException(status_code=400, detail=f"embedding length must be {EMBEDDING_DIM}")
        return explicit
    return embed_text(text_for_embedding)


def upsert_doc(doc: MemoryDocIn) -> dict[str, Any]:
    record_id = doc.id or str(uuid.uuid4())
    updated_at = parse_iso_or_now(doc.updated_at)
    created_at = utc_now_iso()
    vec = choose_embedding(doc.embedding, f"{doc.title or ''}\n{doc.summary or ''}\n{doc.body}")
    vec_param = vec_to_pg_literal(vec) if vec is not None and VECTOR_DB_ENABLED else None

    with db_conn() as conn:
        with conn.cursor() as cur:
            if VECTOR_DB_ENABLED:
                cur.execute(
                    """
                    INSERT INTO memory_docs (id, kind, title, body, summary, tags, source, created_at, updated_at, embedding, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::vector, %s::jsonb)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      kind = EXCLUDED.kind,
                      title = EXCLUDED.title,
                      body = EXCLUDED.body,
                      summary = EXCLUDED.summary,
                      tags = EXCLUDED.tags,
                      source = EXCLUDED.source,
                      updated_at = EXCLUDED.updated_at,
                      embedding = EXCLUDED.embedding,
                      embedding_json = EXCLUDED.embedding_json
                    """,
                    (
                        record_id,
                        doc.kind,
                        doc.title,
                        doc.body,
                        doc.summary,
                        json.dumps(doc.tags or {}),
                        doc.source,
                        created_at,
                        updated_at,
                        vec_param,
                        json.dumps(vec or []),
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO memory_docs (id, kind, title, body, summary, tags, source, created_at, updated_at, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      kind = EXCLUDED.kind,
                      title = EXCLUDED.title,
                      body = EXCLUDED.body,
                      summary = EXCLUDED.summary,
                      tags = EXCLUDED.tags,
                      source = EXCLUDED.source,
                      updated_at = EXCLUDED.updated_at,
                      embedding_json = EXCLUDED.embedding_json
                    """,
                    (
                        record_id,
                        doc.kind,
                        doc.title,
                        doc.body,
                        doc.summary,
                        json.dumps(doc.tags or {}),
                        doc.source,
                        created_at,
                        updated_at,
                        json.dumps(vec or []),
                    ),
                )
    return {"id": record_id, "kind": doc.kind, "updated_at": updated_at, "embedded": vec is not None}


def upsert_evidence(ev: EvidenceIn) -> dict[str, Any]:
    record_id = ev.id or str(uuid.uuid4())
    as_of = parse_iso_or_now(ev.as_of) if ev.as_of else None
    now = utc_now_iso()
    vec = choose_embedding(ev.embedding, f"{ev.question}\n{ev.summary}\n{ev.snippet or ''}")
    vec_param = vec_to_pg_literal(vec) if vec is not None and VECTOR_DB_ENABLED else None

    with db_conn() as conn:
        with conn.cursor() as cur:
            if VECTOR_DB_ENABLED:
                cur.execute(
                    """
                    INSERT INTO memory_evidence (id, question, summary, tool, endpoint, as_of, tags, provenance, snippet, created_at, updated_at, embedding, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::vector, %s::jsonb)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      question = EXCLUDED.question,
                      summary = EXCLUDED.summary,
                      tool = EXCLUDED.tool,
                      endpoint = EXCLUDED.endpoint,
                      as_of = EXCLUDED.as_of,
                      tags = EXCLUDED.tags,
                      provenance = EXCLUDED.provenance,
                      snippet = EXCLUDED.snippet,
                      updated_at = EXCLUDED.updated_at,
                      embedding = EXCLUDED.embedding,
                      embedding_json = EXCLUDED.embedding_json
                    """,
                    (
                        record_id,
                        ev.question,
                        ev.summary,
                        ev.tool,
                        ev.endpoint,
                        as_of,
                        json.dumps(ev.tags or {}),
                        json.dumps(ev.provenance or {}),
                        ev.snippet,
                        now,
                        now,
                        vec_param,
                        json.dumps(vec or []),
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO memory_evidence (id, question, summary, tool, endpoint, as_of, tags, provenance, snippet, created_at, updated_at, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      question = EXCLUDED.question,
                      summary = EXCLUDED.summary,
                      tool = EXCLUDED.tool,
                      endpoint = EXCLUDED.endpoint,
                      as_of = EXCLUDED.as_of,
                      tags = EXCLUDED.tags,
                      provenance = EXCLUDED.provenance,
                      snippet = EXCLUDED.snippet,
                      updated_at = EXCLUDED.updated_at,
                      embedding_json = EXCLUDED.embedding_json
                    """,
                    (
                        record_id,
                        ev.question,
                        ev.summary,
                        ev.tool,
                        ev.endpoint,
                        as_of,
                        json.dumps(ev.tags or {}),
                        json.dumps(ev.provenance or {}),
                        ev.snippet,
                        now,
                        now,
                        json.dumps(vec or []),
                    ),
                )
    return {"id": record_id, "as_of": as_of, "embedded": vec is not None}


def _search_table(
    table: str,
    req: SearchRequest,
    q_vec: list[float] | None,
    select_cols: str,
    fts_expr: str,
    updated_at_col: str,
    tags_col: str,
    extra_filter_sql: str = "",
    extra_params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if req.tags_contains:
        conditions.append(f"{tags_col} @> %s::jsonb")
        params.append(json.dumps(req.tags_contains))

    if extra_filter_sql:
        conditions.append(extra_filter_sql)
        if extra_params:
            params.extend(extra_params)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    q_vec_literal = vec_to_pg_literal(q_vec) if (q_vec is not None and VECTOR_DB_ENABLED) else None

    if q_vec_literal is None:
        sql = f"""
        SELECT
          {select_cols},
          ts_rank_cd(to_tsvector('simple', {fts_expr}), plainto_tsquery('simple', %s)) AS keyword_score,
          0.0::double precision AS vector_score,
          (%s * ts_rank_cd(to_tsvector('simple', {fts_expr}), plainto_tsquery('simple', %s)))::double precision AS score
        FROM {table}
        {where_clause}
        ORDER BY score DESC, {updated_at_col} DESC
        LIMIT %s
        """
        run_params = [req.query, req.weight_keyword, req.query, *params, req.limit]
    else:
        sql = f"""
        SELECT
          {select_cols},
          ts_rank_cd(to_tsvector('simple', {fts_expr}), plainto_tsquery('simple', %s)) AS keyword_score,
          (CASE WHEN embedding IS NULL THEN 0.0 ELSE (1 - (embedding <=> %s::vector)) END)::double precision AS vector_score,
          (
            (%s * ts_rank_cd(to_tsvector('simple', {fts_expr}), plainto_tsquery('simple', %s)))
            + (%s * (CASE WHEN embedding IS NULL THEN 0.0 ELSE (1 - (embedding <=> %s::vector)) END))
          )::double precision AS score
        FROM {table}
        {where_clause}
        ORDER BY score DESC, {updated_at_col} DESC
        LIMIT %s
        """
        run_params = [req.query, q_vec_literal, req.weight_keyword, req.query, req.weight_vector, q_vec_literal, *params, req.limit]

    with db_conn() as conn:
        with conn.cursor() as cur:
            if q_vec_literal is not None and VECTOR_DB_ENABLED:
                cur.execute("SET LOCAL ivfflat.probes = %s", (VECTOR_PROBES,))
            cur.execute(sql, run_params)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]

    items = [dict(zip(cols, row)) for row in rows]
    return [x for x in items if float(x.get("score", 0.0)) >= req.min_score]


def search_docs(req: SearchRequest, q_vec: list[float] | None) -> list[SearchResult]:
    extra = "kind = ANY(%s)" if req.kinds else ""
    extra_params = [req.kinds] if req.kinds else []
    rows = _search_table(
        table="memory_docs",
        req=req,
        q_vec=q_vec,
        select_cols="id, kind, title, body, summary, tags, source, updated_at",
        fts_expr="coalesce(title,'') || ' ' || coalesce(body,'') || ' ' || coalesce(summary,'')",
        updated_at_col="updated_at",
        tags_col="tags",
        extra_filter_sql=extra,
        extra_params=extra_params,
    )

    out: list[SearchResult] = []
    for r in rows:
        out.append(
            SearchResult(
                id=r["id"],
                source_table="memory_docs",
                title=r.get("title"),
                body=r.get("body"),
                summary=r.get("summary"),
                tags=r.get("tags") or {},
                score=float(r.get("score", 0.0)),
                keyword_score=float(r.get("keyword_score", 0.0)),
                vector_score=float(r.get("vector_score", 0.0)),
                updated_at=str(r.get("updated_at")),
            )
        )
    return out


def search_evidence(req: SearchRequest, q_vec: list[float] | None) -> list[SearchResult]:
    rows = _search_table(
        table="memory_evidence",
        req=req,
        q_vec=q_vec,
        select_cols="id, question, summary, tags, tool, endpoint, as_of, updated_at",
        fts_expr="coalesce(question,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(snippet,'')",
        updated_at_col="updated_at",
        tags_col="tags",
    )

    out: list[SearchResult] = []
    for r in rows:
        out.append(
            SearchResult(
                id=r["id"],
                source_table="memory_evidence",
                title=r.get("question"),
                summary=r.get("summary"),
                tags=r.get("tags") or {},
                score=float(r.get("score", 0.0)),
                keyword_score=float(r.get("keyword_score", 0.0)),
                vector_score=float(r.get("vector_score", 0.0)),
                tool=r.get("tool"),
                endpoint=r.get("endpoint"),
                as_of=str(r.get("as_of")) if r.get("as_of") is not None else None,
                updated_at=str(r.get("updated_at")),
            )
        )
    return out


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    init_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memory_docs")
            docs_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM memory_evidence")
            evidence_count = cur.fetchone()[0]
    return {
        "ok": True,
        "version": APP_VERSION,
        "embedding_provider": EMBEDDING_PROVIDER,
        "embedding_dim": EMBEDDING_DIM,
        "vector_db_enabled": VECTOR_DB_ENABLED,
        "has_database_url": bool(DATABASE_URL),
        "docs_count": docs_count,
        "evidence_count": evidence_count,
    }


@app.post("/ingest/doc")
def ingest_doc(doc: MemoryDocIn) -> dict[str, Any]:
    init_db()
    return upsert_doc(doc)


@app.post("/ingest/batch")
def ingest_batch(docs: list[MemoryDocIn]) -> dict[str, Any]:
    init_db()
    items = [upsert_doc(d) for d in docs]
    return {"count": len(items), "items": items}


@app.post("/evidence/write")
def evidence_write(ev: EvidenceIn) -> dict[str, Any]:
    init_db()
    return upsert_evidence(ev)


@app.post("/evidence/write_batch")
def evidence_write_batch(items: list[EvidenceIn]) -> dict[str, Any]:
    init_db()
    out = [upsert_evidence(ev) for ev in items]
    return {"count": len(out), "items": out}


@app.post("/search/docs", response_model=SearchResponse)
def search_docs_endpoint(req: SearchRequest) -> SearchResponse:
    init_db()
    q_vec = embed_text(req.query)
    docs = search_docs(req, q_vec)
    docs.sort(key=lambda x: x.score, reverse=True)
    return SearchResponse(count=len(docs), items=docs[: req.limit])


@app.post("/search/evidence", response_model=SearchResponse)
def search_evidence_endpoint(req: SearchRequest) -> SearchResponse:
    init_db()
    q_vec = embed_text(req.query)
    items = search_evidence(req, q_vec)
    items.sort(key=lambda x: x.score, reverse=True)
    return SearchResponse(count=len(items), items=items[: req.limit])


@app.post("/search", response_model=SearchResponse)
def search_all(req: SearchRequest) -> SearchResponse:
    init_db()
    q_vec = embed_text(req.query)
    docs = search_docs(req, q_vec)
    evidence = search_evidence(req, q_vec)
    all_items = docs + evidence
    all_items.sort(key=lambda x: x.score, reverse=True)
    return SearchResponse(count=len(all_items), items=all_items[: req.limit])
