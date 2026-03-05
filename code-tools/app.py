from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_TITLE = "Dua Code Tools"
APP_VERSION = "1.0.0"
REPO_ROOT = Path(os.getenv("REPO_ROOT", "/workspace/dua-codebase")).resolve()
REPO_URL = os.getenv("REPO_URL", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
MAX_LIMIT = 200

app = FastAPI(title=APP_TITLE, version=APP_VERSION)


def _repo_relative(path: Path) -> str:
    return str(path.resolve().relative_to(REPO_ROOT))


def _safe_path(rel: str) -> Path:
    base = (REPO_ROOT / rel).resolve()
    if REPO_ROOT not in [base, *base.parents]:
        raise HTTPException(status_code=400, detail="Path is outside repository root")
    return base


def _run_rg(query: str, base: Path, limit: int, word_boundaries: bool = False) -> list[str]:
    if limit < 1:
        return []
    capped_limit = min(limit, MAX_LIMIT)
    rg_path = shutil.which("rg")
    pattern = rf"\b{re.escape(query)}\b" if word_boundaries else query

    if rg_path:
        cmd = [
            rg_path,
            "-n",
            "--hidden",
            "--glob",
            "!.git",
            "--glob",
            "!**/node_modules/**",
            "--glob",
            "!**/.next/**",
            "--glob",
            "!**/dist/**",
            "--glob",
            "!**/build/**",
            "--glob",
            "!**/coverage/**",
            pattern,
            str(base),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1):
            raise HTTPException(status_code=500, detail=result.stderr.strip() or "rg search failed")
        return result.stdout.splitlines()[:capped_limit]

    # Fallback when ripgrep is unavailable in runtime image.
    results: list[str] = []
    matcher = re.compile(pattern)
    skip_dirs = {".git", "node_modules", ".next", "dist", "build", "coverage", "__pycache__"}
    for path in base.rglob("*"):
        if len(results) >= capped_limit:
            break
        if path.is_dir():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        rel = _repo_relative(path)
        for i, line in enumerate(lines, start=1):
            if matcher.search(line):
                results.append(f"{rel}:{i}:{line}")
                if len(results) >= capped_limit:
                    break
    return results


def _build_clone_url(url: str, token: str) -> str:
    if not token:
        return url
    if url.startswith("https://"):
        return f"https://{token}@{url[len('https://'):]}"
    return url


def _github_tarball_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.netloc != "github.com":
        return ""
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2:
        return ""
    owner, repo = parts
    return f"https://api.github.com/repos/{owner}/{repo}/tarball"


def _download_and_extract_github_repo(repo_url: str, token: str, target: Path) -> None:
    tarball_url = _github_tarball_url(repo_url)
    if not tarball_url:
        raise HTTPException(
            status_code=500,
            detail="git is unavailable and REPO_URL is not a supported GitHub repo URL.",
        )
    headers = {"User-Agent": "dua-code-tools"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(tarball_url, headers=headers)
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urlopen(req, timeout=60) as resp:
                tmp.write(resp.read())
        finally:
            tmp.flush()

    extract_dir = target_parent / f".extract-{target.name}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall(path=extract_dir)
        extracted_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if not extracted_dirs:
            raise HTTPException(status_code=500, detail="Failed to extract repository archive.")
        extracted_root = extracted_dirs[0]
        if target.exists():
            shutil.rmtree(target)
        extracted_root.rename(target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def _ensure_repo() -> None:
    if REPO_ROOT.exists():
        return
    if not REPO_URL:
        raise HTTPException(status_code=500, detail=f"Repository not found and REPO_URL is unset: {REPO_ROOT}")
    REPO_ROOT.parent.mkdir(parents=True, exist_ok=True)
    git_path = shutil.which("git")
    if git_path:
        clone_url = _build_clone_url(REPO_URL, GITHUB_TOKEN)
        clone = subprocess.run(
            [git_path, "clone", "--depth", "1", clone_url, str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=clone.stderr.strip() or "Failed to clone repository. Verify REPO_URL/GITHUB_TOKEN.",
            )
    else:
        _download_and_extract_github_repo(REPO_URL, GITHUB_TOKEN, REPO_ROOT)


class SearchCodeRequest(BaseModel):
    query: str = Field(min_length=1, description="ripgrep pattern or text to search for")
    path: str = Field(default=".", description="relative path under repository root")
    limit: int = Field(default=50, ge=1, le=MAX_LIMIT)


class ReadFileRequest(BaseModel):
    path: str = Field(description="relative file path under repository root")
    start: int = Field(default=1, ge=1, description="start line, 1-based")
    end: int = Field(default=200, ge=1, description="end line, 1-based")


@app.get("/health")
def health() -> dict[str, object]:
    _ensure_repo()
    return {"ok": True, "repo_root": str(REPO_ROOT)}


@app.get("/list_services")
def list_services() -> dict[str, object]:
    _ensure_repo()
    if not REPO_ROOT.exists():
        raise HTTPException(status_code=500, detail=f"Repository not found: {REPO_ROOT}")
    services = []
    for entry in sorted(REPO_ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        services.append(entry.name)
    return {"repo_root": str(REPO_ROOT), "services": services}


@app.post("/search_code")
def search_code(req: SearchCodeRequest) -> dict[str, object]:
    _ensure_repo()
    base = _safe_path(req.path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {req.path}")
    results = _run_rg(req.query, base, req.limit, word_boundaries=False)
    return {"query": req.query, "path": req.path, "count": len(results), "results": results}


@app.post("/read_file")
def read_file(req: ReadFileRequest) -> dict[str, object]:
    _ensure_repo()
    file_path = _safe_path(req.path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")
    if req.end < req.start:
        raise HTTPException(status_code=400, detail="end must be >= start")
    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = req.start
    end = min(req.end, len(lines))
    content = "\n".join(lines[start - 1 : end]) if lines else ""
    return {
        "path": _repo_relative(file_path),
        "start": start,
        "end": end,
        "total_lines": len(lines),
        "content": content,
    }


@app.get("/find_references")
def find_references(symbol: str, path: str = ".", limit: int = 100) -> dict[str, object]:
    _ensure_repo()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {MAX_LIMIT}")
    base = _safe_path(path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")
    results = _run_rg(symbol, base, limit, word_boundaries=True)
    return {"symbol": symbol, "path": path, "count": len(results), "results": results}
