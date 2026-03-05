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
APP_VERSION = "1.1.0"
REPO_ROOT = Path(os.getenv("REPO_ROOT", "/workspace/dua-codebase")).resolve()
REPO_URL = os.getenv("REPO_URL", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
MAX_LIMIT = 300

SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "coverage", "__pycache__", "vendor"}
SOURCE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".rs",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".m",
    ".mm",
    ".sql",
    ".graphql",
    ".yaml",
    ".yml",
    ".json",
}
CALL_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "await",
    "new",
    "typeof",
    "print",
    "console",
}

app = FastAPI(title=APP_TITLE, version=APP_VERSION)


def _repo_relative(path: Path) -> str:
    return str(path.resolve().relative_to(REPO_ROOT))


def _safe_path(rel: str) -> Path:
    base = (REPO_ROOT / rel).resolve()
    if REPO_ROOT not in [base, *base.parents]:
        raise HTTPException(status_code=400, detail="Path is outside repository root")
    return base


def _normalize_extensions(extensions: list[str] | None) -> set[str] | None:
    if not extensions:
        return None
    normalized = set()
    for ext in extensions:
        val = ext.strip().lower()
        if not val:
            continue
        normalized.add(val if val.startswith(".") else f".{val}")
    return normalized or None


def _file_allowed(path: Path, source_only: bool, extensions: set[str] | None, exclude_docs: bool) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    rel = _repo_relative(path).lower()
    if exclude_docs and rel.startswith("docs/"):
        return False
    if source_only and path.suffix.lower() not in SOURCE_EXTENSIONS:
        return False
    if extensions and path.suffix.lower() not in extensions:
        return False
    return True


def _iter_repo_files(base: Path, source_only: bool, extensions: set[str] | None, exclude_docs: bool):
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if _file_allowed(path, source_only=source_only, extensions=extensions, exclude_docs=exclude_docs):
            yield path


def _run_rg(
    query: str,
    base: Path,
    limit: int,
    word_boundaries: bool = False,
    source_only: bool = False,
    extensions: set[str] | None = None,
    exclude_docs: bool = False,
) -> list[str]:
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
        ]
        if exclude_docs:
            cmd += ["--glob", "!docs/**"]
        if source_only:
            for ext in sorted(SOURCE_EXTENSIONS):
                cmd += ["--glob", f"**/*{ext}"]
        if extensions:
            for ext in sorted(extensions):
                cmd += ["--glob", f"**/*{ext}"]
        cmd += [pattern, str(base)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode not in (0, 1):
                raise HTTPException(status_code=500, detail=result.stderr.strip() or "rg search failed")
            return result.stdout.splitlines()[:capped_limit]
        except FileNotFoundError:
            pass

    # Fallback when ripgrep is unavailable in runtime image.
    results: list[str] = []
    matcher = re.compile(pattern)
    for path in _iter_repo_files(base, source_only=source_only, extensions=extensions, exclude_docs=exclude_docs):
        if len(results) >= capped_limit:
            break
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
        return f"https://{token}@{url[len('https://'):] }"
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
    query: str = Field(min_length=1, description="Text/pattern to search")
    path: str = Field(default=".", description="relative path under repository root")
    limit: int = Field(default=50, ge=1, le=MAX_LIMIT)
    source_only: bool = Field(default=True, description="Prefer source code files over docs/assets")
    exclude_docs: bool = Field(default=True, description="Exclude docs/ folder")
    extensions: list[str] | None = Field(default=None, description="Optional file extensions filter")


class ReadFileRequest(BaseModel):
    path: str = Field(description="relative file path under repository root")
    start: int = Field(default=1, ge=1, description="start line, 1-based")
    end: int = Field(default=200, ge=1, description="end line, 1-based")


class ListFilesRequest(BaseModel):
    path: str = Field(default=".", description="relative path under repository root")
    limit: int = Field(default=200, ge=1, le=2000)
    source_only: bool = Field(default=True)
    exclude_docs: bool = Field(default=True)
    extensions: list[str] | None = Field(default=None)
    name_pattern: str | None = Field(default=None, description="substring to match in file path")


class TraceCallPathRequest(BaseModel):
    symbol: str = Field(min_length=1)
    path: str = Field(default=".")
    max_depth: int = Field(default=2, ge=1, le=5)
    max_edges: int = Field(default=40, ge=1, le=200)


def _parse_search_line(line: str) -> tuple[str, int, str] | None:
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    file_path, line_no, content = parts
    try:
        num = int(line_no)
    except ValueError:
        return None
    return file_path, num, content


def _find_symbol_definitions(symbol: str, base: Path, limit: int = 20) -> list[dict[str, object]]:
    patterns = [
        rf"\bfunction\s+{re.escape(symbol)}\b",
        rf"\bdef\s+{re.escape(symbol)}\b",
        rf"\bclass\s+{re.escape(symbol)}\b",
        rf"\bconst\s+{re.escape(symbol)}\s*=",
        rf"\blet\s+{re.escape(symbol)}\s*=",
        rf"\bvar\s+{re.escape(symbol)}\s*=",
        rf"\b{re.escape(symbol)}\s*:\s*(?:async\s*)?\(",
    ]
    found: list[dict[str, object]] = []
    seen = set()
    for pat in patterns:
        matches = _run_rg(pat, base, limit=limit, source_only=True, exclude_docs=True)
        for line in matches:
            parsed = _parse_search_line(line)
            if not parsed:
                continue
            file_path, line_no, content = parsed
            key = (file_path, line_no)
            if key in seen:
                continue
            seen.add(key)
            found.append({"symbol": symbol, "file": file_path, "line": line_no, "snippet": content.strip()})
            if len(found) >= limit:
                return found
    return found


def _extract_called_symbols(source_chunk: str) -> list[str]:
    symbols = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", source_chunk)
    unique = []
    seen = set()
    for s in symbols:
        if s in CALL_KEYWORDS or s in seen or len(s) < 2:
            continue
        seen.add(s)
        unique.append(s)
    return unique


@app.get("/health")
def health() -> dict[str, object]:
    _ensure_repo()
    return {"ok": True, "repo_root": str(REPO_ROOT), "version": APP_VERSION}


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


@app.post("/list_files")
def list_files(req: ListFilesRequest) -> dict[str, object]:
    _ensure_repo()
    base = _safe_path(req.path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {req.path}")

    extensions = _normalize_extensions(req.extensions)
    files = []
    name_pattern = req.name_pattern.lower() if req.name_pattern else None
    for path in _iter_repo_files(base, source_only=req.source_only, extensions=extensions, exclude_docs=req.exclude_docs):
        rel = _repo_relative(path)
        if name_pattern and name_pattern not in rel.lower():
            continue
        files.append(rel)
        if len(files) >= req.limit:
            break

    return {
        "path": req.path,
        "count": len(files),
        "source_only": req.source_only,
        "exclude_docs": req.exclude_docs,
        "files": files,
    }


@app.post("/search_code")
def search_code(req: SearchCodeRequest) -> dict[str, object]:
    _ensure_repo()
    base = _safe_path(req.path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {req.path}")
    extensions = _normalize_extensions(req.extensions)
    results = _run_rg(
        req.query,
        base,
        req.limit,
        word_boundaries=False,
        source_only=req.source_only,
        extensions=extensions,
        exclude_docs=req.exclude_docs,
    )
    return {
        "query": req.query,
        "path": req.path,
        "count": len(results),
        "source_only": req.source_only,
        "exclude_docs": req.exclude_docs,
        "results": results,
    }


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
def find_references(
    symbol: str,
    path: str = ".",
    limit: int = 100,
    source_only: bool = True,
    exclude_docs: bool = True,
) -> dict[str, object]:
    _ensure_repo()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {MAX_LIMIT}")

    base = _safe_path(path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

    results = _run_rg(
        symbol,
        base,
        limit,
        word_boundaries=True,
        source_only=source_only,
        exclude_docs=exclude_docs,
    )
    return {
        "symbol": symbol,
        "path": path,
        "count": len(results),
        "source_only": source_only,
        "exclude_docs": exclude_docs,
        "results": results,
    }


@app.post("/trace_call_path")
def trace_call_path(req: TraceCallPathRequest) -> dict[str, object]:
    _ensure_repo()
    base = _safe_path(req.path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {req.path}")

    entry_defs = _find_symbol_definitions(req.symbol, base, limit=10)
    queue = [req.symbol]
    visited_symbols = {req.symbol}
    nodes: dict[str, list[dict[str, object]]] = {req.symbol: entry_defs}
    edges: list[dict[str, object]] = []

    depth = 0
    while queue and depth < req.max_depth and len(edges) < req.max_edges:
        current = queue.pop(0)
        defs = nodes.get(current, [])
        if not defs:
            continue

        for d in defs[:3]:
            file_rel = d["file"]
            line_no = int(d["line"])
            file_abs = _safe_path(file_rel)
            try:
                lines = file_abs.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            start = max(0, line_no - 1)
            end = min(len(lines), line_no + 60)
            chunk = "\n".join(lines[start:end])
            called = _extract_called_symbols(chunk)

            for callee in called:
                callee_defs = _find_symbol_definitions(callee, base, limit=2)
                to_ref = callee_defs[0] if callee_defs else None
                edges.append(
                    {
                        "from_symbol": current,
                        "from_file": file_rel,
                        "from_line": line_no,
                        "to_symbol": callee,
                        "to_file": to_ref["file"] if to_ref else None,
                        "to_line": to_ref["line"] if to_ref else None,
                    }
                )
                if len(edges) >= req.max_edges:
                    break
                if callee not in visited_symbols and callee_defs:
                    visited_symbols.add(callee)
                    queue.append(callee)
                    nodes[callee] = callee_defs
            if len(edges) >= req.max_edges:
                break
        depth += 1

    return {
        "entry_symbol": req.symbol,
        "path": req.path,
        "definitions": entry_defs,
        "depth_traversed": depth,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }
