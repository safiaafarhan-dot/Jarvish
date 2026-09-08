"""The knowledge engine: indexing, hybrid retrieval and project structure.

This is deliberately separate from `personal.py`. The two hold different kinds
of thing and must not be confused:

* **Memory** (`personal.py`) — facts the user told Jarvish about themselves.
* **Knowledge** (here) — what is written in their files, code and documents.

Retrieval is hybrid. Keyword search is SQLite's FTS5 with BM25 ranking, which
is real ranked retrieval with no model and no download. Semantic search adds a
vector pass when an embedding model is available. The two are fused with
reciprocal rank fusion, so neither has to be score-normalised against the other.

**Honest degradation.** If no embedding model is installed, search is keyword
only and every result says so in its `method` field. Nothing is labelled
"semantic" that was not actually embedded.
"""

import ast
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path

import httpx

from .config import OLLAMA_HOST
from .util import DATA_DIR, as_bool, as_int, boolean, err, integer, ok, string, tool

try:
    import pypdf
    _PDF = True
except Exception:
    _PDF = False

DB_PATH = DATA_DIR / "knowledge.db"

# Text-ish files worth reading, mapped to a coarse kind used for chunking.
KINDS = {
    ".py": "python", ".pyw": "python",
    ".js": "code", ".mjs": "code", ".cjs": "code", ".jsx": "code",
    ".ts": "code", ".tsx": "code", ".java": "code", ".go": "code",
    ".rs": "code", ".c": "code", ".h": "code", ".cpp": "code", ".cs": "code",
    ".rb": "code", ".php": "code", ".sh": "code", ".ps1": "code", ".sql": "code",
    ".md": "markdown", ".markdown": "markdown", ".rst": "markdown",
    ".txt": "text", ".log": "text", ".csv": "text",
    ".html": "markup", ".htm": "markup", ".xml": "markup", ".css": "markup",
    ".json": "data", ".yaml": "data", ".yml": "data", ".toml": "data", ".ini": "data",
    ".pdf": "pdf",
}

# Directories that are never worth indexing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", ".next", ".nuxt", "target", "out",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages", ".cache", "coverage", ".tox", "browser-profile",
    "captures", "downloads", ".claude",
}

SKIP_FILES = re.compile(r"(package-lock\.json|yarn\.lock|poetry\.lock|\.min\.(js|css)$)",
                        re.IGNORECASE)

MAX_FILE_BYTES = 2 * 1024 * 1024
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 180

# Model families that exist specifically to produce embeddings.
EMBED_FAMILIES = ("nomic-embed", "mxbai-embed", "all-minilm", "bge-",
                  "snowflake-arctic-embed", "granite-embedding", "embeddinggemma",
                  "paraphrase-multilingual")

_lock = threading.RLock()
_embed_cache = {"checked": 0.0, "model": None, "reason": None}
_progress = {"running": False, "path": None, "done": 0, "total": 0, "started": 0.0}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _schema(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, root TEXT, name TEXT, kind TEXT,
        mtime REAL, size INTEGER, chunks INTEGER, indexed_at REAL
    );
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY, path TEXT, ord INTEGER,
        section TEXT, start_line INTEGER, end_line INTEGER,
        text TEXT, embedding BLOB
    );
    CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text, section, path UNINDEXED, content='chunks', content_rowid='id'
    );
    CREATE TABLE IF NOT EXISTS symbols (
        path TEXT, kind TEXT, name TEXT, line INTEGER, parent TEXT
    );
    CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
    CREATE TABLE IF NOT EXISTS imports (path TEXT, module TEXT);
    CREATE INDEX IF NOT EXISTS imports_module ON imports(module);
    -- Which function calls which, so "what calls this?" is answerable from the
    -- same index rather than a second one.
    CREATE TABLE IF NOT EXISTS calls (
        path TEXT, caller TEXT, callee TEXT, line INTEGER
    );
    CREATE INDEX IF NOT EXISTS calls_callee ON calls(callee);
    CREATE INDEX IF NOT EXISTS calls_caller ON calls(caller);
    -- Project-level facts worth remembering between sessions.
    CREATE TABLE IF NOT EXISTS projects (
        root TEXT PRIMARY KEY, detected TEXT, updated REAL
    );
    CREATE TABLE IF NOT EXISTS project_notes (
        root TEXT, kind TEXT, note TEXT, at REAL
    );
    CREATE INDEX IF NOT EXISTS project_notes_root ON project_notes(root, kind);
    CREATE TABLE IF NOT EXISTS roots (path TEXT PRIMARY KEY, added_at REAL);
    """)
    connection.commit()


def _pack(vector):
    return struct.pack("%sf" % len(vector), *vector)


def _unpack(blob):
    return list(struct.unpack("%sf" % (len(blob) // 4), blob))


# --------------------------------------------------------------------------
# Embeddings, when they exist
# --------------------------------------------------------------------------

def embedder(force=False):
    """The embedding model to use, or None with the reason why not.

    Chat models are not used for this even when the server would allow it: a
    forward pass per chunk makes indexing unusably slow, and the vectors are
    much worse than a purpose-built embedding model's.
    """
    now = time.time()
    if not force and now - _embed_cache["checked"] < 120 and _embed_cache["checked"]:
        return _embed_cache["model"], _embed_cache["reason"]

    model, reason = None, None
    try:
        tags = httpx.get(OLLAMA_HOST + "/api/tags", timeout=6).json()
        names = [m.get("name", "") for m in tags.get("models", [])]
        candidates = [n for n in names
                      if any(f in n.lower() for f in EMBED_FAMILIES)]
        if not candidates:
            reason = ("No embedding model is installed, so search is keyword-only. "
                      "For semantic search: `ollama pull nomic-embed-text` (274 MB).")
        else:
            probe = httpx.post(OLLAMA_HOST + "/api/embed",
                               json={"model": candidates[0], "input": "probe"}, timeout=25)
            if probe.status_code == 200 and probe.json().get("embeddings"):
                model = candidates[0]
            else:
                reason = ("An embedding model is present but the server refused the "
                          "request (HTTP " + str(probe.status_code) + "). Search is "
                          "keyword-only.")
    except Exception as exc:
        reason = "Could not reach the model server, so search is keyword-only. " + str(exc)[:90]

    _embed_cache.update({"checked": now, "model": model, "reason": reason})
    return model, reason


def _embed(texts, model):
    """Embed a batch. Returns None on any failure, never a fabricated vector."""
    try:
        response = httpx.post(OLLAMA_HOST + "/api/embed",
                              json={"model": model, "input": texts}, timeout=120)
        response.raise_for_status()
        vectors = response.json().get("embeddings")
        if not vectors or len(vectors) != len(texts):
            return None
        return vectors
    except Exception:
        return None


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# --------------------------------------------------------------------------
# Reading and chunking
# --------------------------------------------------------------------------

def _read_file(path, kind):
    """Return (text, note). Text is empty when the file cannot be read."""
    if kind == "pdf":
        if not _PDF:
            return "", "PDF support needs pypdf; install requirements.txt."
        try:
            reader = pypdf.PdfReader(str(path))
            pages = []
            for number, page in enumerate(reader.pages[:200], start=1):
                extracted = page.extract_text() or ""
                if extracted.strip():
                    pages.append("[page " + str(number) + "]\n" + extracted)
            return "\n\n".join(pages), None
        except Exception as exc:
            return "", "Could not read the PDF: " + str(exc)[:80]
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except Exception as exc:
        return "", str(exc)[:80]


def _chunk_markdown(text):
    """Split on headings so each chunk keeps the section it belongs to."""
    lines = text.split("\n")
    chunks, current, section, start = [], [], "", 1
    for number, line in enumerate(lines, start=1):
        if re.match(r"^#{1,6}\s+\S", line):
            if any(l.strip() for l in current):
                chunks.append((section, start, number - 1, "\n".join(current)))
            section = line.lstrip("#").strip()[:120]
            current, start = [line], number
        else:
            current.append(line)
    if any(l.strip() for l in current):
        chunks.append((section, start, len(lines), "\n".join(current)))
    return _split_oversized(chunks)


def _chunk_python(text):
    """One chunk per top-level function or class, with its real line numbers."""
    lines = text.split("\n")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _chunk_plain(text)

    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            spans.append((node.lineno, end, node.name))

    if not spans:
        return _chunk_plain(text)

    chunks = []
    if spans[0][0] > 1:
        head = "\n".join(lines[:spans[0][0] - 1])
        if head.strip():
            chunks.append(("module header", 1, spans[0][0] - 1, head))
    for start, end, name in spans:
        body = "\n".join(lines[start - 1:end])
        if body.strip():
            chunks.append((name, start, end, body))
    return _split_oversized(chunks)


def _chunk_plain(text):
    lines = text.split("\n")
    chunks, current, start, length = [], [], 1, 0
    for number, line in enumerate(lines, start=1):
        current.append(line)
        length += len(line) + 1
        if length >= CHUNK_CHARS:
            chunks.append(("", start, number, "\n".join(current)))
            # Overlap a little so a sentence split across chunks is still findable.
            back = max(0, len(current) - 3)
            current = current[back:]
            start = number - len(current) + 1
            length = sum(len(l) + 1 for l in current)
    if any(l.strip() for l in current):
        chunks.append(("", start, len(lines), "\n".join(current)))
    return chunks


def _split_oversized(chunks):
    """Break any chunk that is too long for a model to use comfortably."""
    out = []
    for section, start, end, body in chunks:
        if len(body) <= CHUNK_CHARS * 2:
            out.append((section, start, end, body))
            continue
        lines = body.split("\n")
        piece, first, length = [], start, 0
        for offset, line in enumerate(lines):
            piece.append(line)
            length += len(line) + 1
            if length >= CHUNK_CHARS:
                out.append((section, first, start + offset, "\n".join(piece)))
                piece, first, length = [], start + offset + 1, 0
        if any(l.strip() for l in piece):
            out.append((section, first, end, "\n".join(piece)))
    return out


def _chunk(text, kind):
    if not text.strip():
        return []
    if kind == "markdown":
        return _chunk_markdown(text)
    if kind == "python":
        return _chunk_python(text)
    return _chunk_plain(text)


# --------------------------------------------------------------------------
# Project structure
# --------------------------------------------------------------------------

_JS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", re.MULTILINE)
_JS_IMPORT = re.compile(r"""(?:from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"])""")


def _call_name(node):
    """The dotted name being called, as written in the source."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _extract_structure(path, text, kind):
    """Functions, classes, imports and the call graph, for project reasoning.

    Calls are recorded by name only — resolving them to a definition would need
    real type inference. `find_callers` therefore reports candidates, and says
    so, rather than claiming certainty it does not have.
    """
    symbols, imports, calls = [], [], []
    if kind == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return symbols, imports, calls

        def walk_body(body, parent=None):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(("method" if parent else "function",
                                    node.name, node.lineno, parent))
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Call):
                            callee = _call_name(inner)
                            if callee:
                                calls.append((node.name, callee, inner.lineno))
                    walk_body([n for n in node.body
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                 ast.ClassDef))], node.name)
                elif isinstance(node, ast.ClassDef):
                    symbols.append(("class", node.name, node.lineno, parent))
                    walk_body(node.body, node.name)

        walk_body(tree.body)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
                elif node.level:
                    # `from . import risk, models` carries no module — the
                    # imported names *are* the sibling modules. Without this,
                    # package-internal imports are invisible to the index,
                    # which is most of the imports in a Python package.
                    imports.extend(alias.name for alias in node.names)

    elif kind == "code":
        for match in _JS_SYMBOL.finditer(text):
            name = match.group(1) or match.group(2) or match.group(3)
            if name:
                line = text[:match.start()].count("\n") + 1
                kindname = "class" if match.group(2) else "function"
                symbols.append((kindname, name, line, None))
        for match in _JS_IMPORT.finditer(text):
            imports.append(match.group(1) or match.group(2))

    return symbols, imports, calls


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

def _walk(root):
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in names:
            suffix = Path(name).suffix.lower()
            if suffix not in KINDS or SKIP_FILES.search(name):
                continue
            path = Path(base) / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _index_file(connection, path, root, model):
    """Index one file. Returns (chunk_count, skipped)."""
    try:
        stat = path.stat()
    except OSError:
        return 0, True

    key = str(path.resolve())
    kind = KINDS.get(path.suffix.lower(), "text")

    existing = connection.execute(
        "SELECT mtime, size FROM files WHERE path=?", (key,)).fetchone()
    if existing and abs(existing["mtime"] - stat.st_mtime) < 0.001 \
            and existing["size"] == stat.st_size:
        return 0, True          # unchanged since last time

    text, note = _read_file(path, kind)
    if not text.strip():
        return 0, True

    pieces = _chunk(text, kind)
    if not pieces:
        return 0, True

    _forget_path(connection, key)

    bodies = [body for _s, _a, _b, body in pieces]
    vectors = _embed(bodies, model) if model else None

    for order, (section, start, end, body) in enumerate(pieces):
        blob = _pack(vectors[order]) if vectors else None
        cursor = connection.execute(
            "INSERT INTO chunks(path, ord, section, start_line, end_line, text, embedding)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, order, section, start, end, body, blob))
        connection.execute(
            "INSERT INTO chunks_fts(rowid, text, section, path) VALUES (?,?,?,?)",
            (cursor.lastrowid, body, section, key))

    symbols, imports, calls = _extract_structure(path, text, kind)
    for skind, name, line, parent in symbols:
        connection.execute(
            "INSERT INTO symbols(path, kind, name, line, parent) VALUES (?,?,?,?,?)",
            (key, skind, name, line, parent))
    for module in set(imports):
        connection.execute("INSERT INTO imports(path, module) VALUES (?,?)", (key, module))
    for caller, callee, line in calls:
        connection.execute(
            "INSERT INTO calls(path, caller, callee, line) VALUES (?,?,?,?)",
            (key, caller, callee, line))

    connection.execute(
        "INSERT OR REPLACE INTO files(path, root, name, kind, mtime, size, chunks, indexed_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (key, str(root), path.name, kind, stat.st_mtime, stat.st_size,
         len(pieces), time.time()))
    return len(pieces), False


def _forget_path(connection, key):
    # chunks_fts is an external-content FTS5 table, so a delete has to be given
    # the row's ORIGINAL column values — that is how FTS5 reverses the postings
    # it wrote. Passing blanks leaves the old terms in the index forever, which
    # bloats it and can surface phantom matches once rowids are reused.
    rows = connection.execute(
        "SELECT id, text, section, path FROM chunks WHERE path=?", (key,)).fetchall()
    for row in rows:
        connection.execute(
            "INSERT INTO chunks_fts(chunks_fts, rowid, text, section, path)"
            " VALUES ('delete', ?, ?, ?, ?)",
            (row["id"], row["text"], row["section"], row["path"]))
    connection.execute("DELETE FROM chunks WHERE path=?", (key,))
    connection.execute("DELETE FROM symbols WHERE path=?", (key,))
    connection.execute("DELETE FROM imports WHERE path=?", (key,))
    connection.execute("DELETE FROM calls WHERE path=?", (key,))
    connection.execute("DELETE FROM files WHERE path=?", (key,))


def index_folder(path, background=False):
    """Index a folder, adding only what changed since last time."""
    root = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    if not root.exists():
        return err("No such folder: " + str(root))
    if not root.is_dir():
        return err(str(root) + " is a file, not a folder.")

    if as_bool(background):
        if _progress["running"]:
            return err("An indexing run is already in progress.")
        thread = threading.Thread(target=lambda: index_folder(root, background=False),
                                  daemon=True)
        thread.start()
        return ok(started=True, path=str(root),
                  note="Indexing in the background. Call knowledge_status for progress.")

    model, reason = embedder()
    started = time.time()
    files = list(_walk(root))

    with _lock:
        _progress.update({"running": True, "path": str(root), "done": 0,
                          "total": len(files), "started": started})
        connection = _connect()
        _schema(connection)
        try:
            connection.execute("INSERT OR REPLACE INTO roots(path, added_at) VALUES (?,?)",
                               (str(root), time.time()))
            indexed = skipped = chunks = 0
            for number, file_path in enumerate(files, start=1):
                count, was_skipped = _index_file(connection, file_path, root, model)
                chunks += count
                indexed += 0 if was_skipped else 1
                skipped += 1 if was_skipped else 0
                _progress["done"] = number
                if number % 40 == 0:
                    connection.commit()

            # Drop anything that has been deleted from disk since last time.
            removed = 0
            known = connection.execute(
                "SELECT path FROM files WHERE root=?", (str(root),)).fetchall()
            for row in known:
                if not Path(row["path"]).exists():
                    _forget_path(connection, row["path"])
                    removed += 1
            connection.commit()
        finally:
            connection.close()
            _progress["running"] = False

    return ok(
        root=str(root),
        files_seen=len(files), files_indexed=indexed, unchanged=skipped,
        removed=removed, chunks_added=chunks,
        semantic=bool(model), embedding_model=model,
        limitation=reason,
        seconds=round(time.time() - started, 1),
    )


def forget_folder(path):
    """Remove a folder from the index."""
    root = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    with _lock:
        connection = _connect()
        _schema(connection)
        try:
            rows = connection.execute("SELECT path FROM files WHERE root=? OR path LIKE ?",
                                      (str(root), str(root) + "%")).fetchall()
            for row in rows:
                _forget_path(connection, row["path"])
            connection.execute("DELETE FROM roots WHERE path=?", (str(root),))
            connection.commit()
        finally:
            connection.close()
    return ok(removed_files=len(rows), root=str(root))


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

_STOP = {"the", "and", "for", "what", "where", "which", "was", "are", "how",
         "did", "does", "with", "this", "that", "from", "have", "has", "you",
         "your", "about", "show", "find", "everything", "related", "can", "all"}


def _terms(text):
    words = re.findall(r"[A-Za-z0-9_]{2,}", str(text or ""))
    if not words:
        return []
    kept = [w for w in words if w.lower() not in _STOP] or words
    return [w.replace('"', "") for w in kept[:12]]


def _run_fts(connection, expression, want):
    try:
        rows = connection.execute(
            "SELECT c.id, c.path, c.section, c.start_line, c.end_line, c.text,"
            "       bm25(chunks_fts) AS score"
            "  FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (expression, want)).fetchall()
    except sqlite3.OperationalError:
        return []
    # bm25 is negative and lower is better; flip so bigger is better.
    return [dict(row, score=-row["score"]) for row in rows]


def _keyword_search(connection, query, limit):
    """BM25 retrieval, narrow first then broad.

    Requiring every term (AND) is far more precise, but returns nothing when one
    word is absent. So AND runs first and OR only tops up the remainder — a
    chunk containing all the terms always outranks one that merely repeats the
    most common of them.
    """
    terms = _terms(query)
    if not terms:
        return []
    want = limit * 4
    quoted = ['"' + t + '"' for t in terms]

    results, seen = [], set()
    if len(quoted) > 1:
        for row in _run_fts(connection, " AND ".join(quoted), want):
            row["all_terms"] = True
            results.append(row)
            seen.add(row["id"])

    if len(results) < want:
        for row in _run_fts(connection, " OR ".join(quoted), want):
            if row["id"] not in seen:
                row["all_terms"] = len(quoted) == 1
                results.append(row)
                seen.add(row["id"])
    return results[:want]


def _semantic_search(connection, query, limit, model):
    vectors = _embed([query], model)
    if not vectors:
        return []
    target = vectors[0]
    rows = connection.execute(
        "SELECT id, path, section, start_line, end_line, text, embedding"
        "  FROM chunks WHERE embedding IS NOT NULL").fetchall()
    scored = []
    for row in rows:
        similarity = _cosine(target, _unpack(row["embedding"]))
        scored.append(dict(row, score=similarity))
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit * 4]


def _fuse(keyword, semantic, query, limit):
    """Reciprocal rank fusion, plus small boosts for path and recency.

    RRF avoids having to make BM25 scores and cosine similarities comparable,
    which they are not.
    """
    K = 60.0
    pool, ranks = {}, {}

    complete = set()
    for position, row in enumerate(keyword):
        pool[row["id"]] = row
        ranks.setdefault(row["id"], {})["keyword"] = position + 1
        if row.get("all_terms"):
            complete.add(row["id"])
    for position, row in enumerate(semantic):
        pool[row["id"]] = pool.get(row["id"], row)
        ranks.setdefault(row["id"], {})["semantic"] = position + 1

    words = {w.lower() for w in re.findall(r"[A-Za-z0-9_]{3,}", query or "")}
    now = time.time()

    results = []
    for identifier, row in pool.items():
        rank = ranks[identifier]
        score = sum(1.0 / (K + position) for position in rank.values())

        # Containing *every* term is the strongest keyword evidence there is,
        # and must outrank a chunk that merely repeats the commonest one.
        if identifier in complete:
            score *= 1.6

        # The filename is a useful hint, but only a hint — kept small enough
        # that it cannot beat a genuine full-term content match.
        name = Path(row["path"]).name.lower()
        stem = name.rsplit(".", 1)[0]
        if words & {stem} or any(w in stem for w in words if len(w) > 3):
            score *= 1.15
        elif any(w in row["path"].lower() for w in words):
            score *= 1.06
        if row["section"] and words & {row["section"].lower()}:
            score *= 1.2

        # Gentle recency preference, capped so it cannot outrank relevance.
        try:
            age_days = (now - Path(row["path"]).stat().st_mtime) / 86400.0
            score *= 1.0 + max(0.0, 0.12 - 0.12 * min(age_days, 30) / 30)
        except OSError:
            pass

        methods = sorted(rank)
        results.append({
            "source": Path(row["path"]).name,
            "path": row["path"],
            "section": row["section"] or None,
            "lines": [row["start_line"], row["end_line"]],
            "chunk": row["text"][:1200],
            "method": "+".join(methods),
            "score": round(score, 5),
        })

    results.sort(key=lambda r: r["score"], reverse=True)

    # One hit per (file, section): the best one. Different sections of the same
    # file are still allowed through, because they are genuinely different.
    seen, deduped = set(), []
    for row in results:
        key = (row["path"], row["section"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def search(query, limit=6):
    """Hybrid retrieval over everything indexed."""
    text = str(query or "").strip()
    if not text:
        return err("No query given.")
    limit = as_int(limit, 6, 1, 25)

    model, reason = embedder()
    connection = _connect()
    _schema(connection)
    try:
        total = connection.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        if not total:
            return err("Nothing has been indexed yet. Use index_folder on a folder first.")
        keyword = _keyword_search(connection, text, limit)
        semantic = _semantic_search(connection, text, limit, model) if model else []
        results = _fuse(keyword, semantic, text, limit)
    finally:
        connection.close()

    method = "hybrid (keyword + semantic)" if model else "keyword only (BM25)"
    payload = ok(
        query=text,
        results=results,
        count=len(results),
        method=method,
        semantic=bool(model),
        indexed_chunks=total,
    )
    if not model and reason:
        payload["limitation"] = reason
    return payload


def status():
    """What is indexed and which retrieval methods are actually available."""
    model, reason = embedder()
    connection = _connect()
    _schema(connection)
    try:
        files = connection.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        chunks = connection.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        embedded = connection.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL").fetchone()["n"]
        symbols = connection.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        call_edges = connection.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
        roots = [r["path"] for r in connection.execute("SELECT path FROM roots").fetchall()]
        kinds = {r["kind"]: r["n"] for r in connection.execute(
            "SELECT kind, COUNT(*) AS n FROM files GROUP BY kind").fetchall()}
    finally:
        connection.close()

    return ok(
        roots=roots, files=files, chunks=chunks, embedded_chunks=embedded,
        symbols=symbols, call_edges=call_edges, kinds=kinds,
        keyword_search=True,
        semantic_search=bool(model),
        embedding_model=model,
        limitation=reason,
        indexing=dict(_progress) if _progress["running"] else None,
        pdf_support=_PDF,
    )


def find_symbol(name, limit=15):
    """Locate a function or class by name across everything indexed."""
    needle = str(name or "").strip()
    if not needle:
        return err("No symbol name given.")
    connection = _connect()
    _schema(connection)
    try:
        rows = connection.execute(
            "SELECT path, kind, name, line, parent FROM symbols"
            " WHERE name = ? COLLATE NOCASE"
            " UNION ALL"
            " SELECT path, kind, name, line, parent FROM symbols"
            " WHERE name LIKE ? COLLATE NOCASE AND name <> ? COLLATE NOCASE"
            " LIMIT ?",
            (needle, "%" + needle + "%", needle, as_int(limit, 15, 1, 60))).fetchall()
    finally:
        connection.close()

    if not rows:
        return err("No function or class called '" + needle + "' is in the index.")
    return ok(symbol=needle, count=len(rows), matches=[
        {"name": r["name"], "kind": r["kind"], "source": Path(r["path"]).name,
         "path": r["path"], "line": r["line"], "parent": r["parent"]}
        for r in rows
    ])


def related(path_or_name, limit=20):
    """What connects to a file: what it imports, and what imports it."""
    needle = str(path_or_name or "").strip()
    if not needle:
        return err("No file given.")
    connection = _connect()
    _schema(connection)
    try:
        row = connection.execute(
            "SELECT path FROM files WHERE path = ? OR name = ? COLLATE NOCASE"
            " OR path LIKE ? LIMIT 1",
            (needle, needle, "%" + needle)).fetchone()
        if row is None:
            return err("'" + needle + "' is not in the index.")
        path = row["path"]

        imports = [r["module"] for r in connection.execute(
            "SELECT module FROM imports WHERE path=? LIMIT ?", (path, limit)).fetchall()]
        stem = Path(path).stem
        importers = [Path(r["path"]).name for r in connection.execute(
            "SELECT DISTINCT path FROM imports WHERE module = ? OR module LIKE ? LIMIT ?",
            (stem, "%." + stem, limit)).fetchall()]
        symbols = [{"kind": r["kind"], "name": r["name"], "line": r["line"]}
                   for r in connection.execute(
                       "SELECT kind, name, line FROM symbols WHERE path=?"
                       " ORDER BY line LIMIT ?", (path, limit)).fetchall()]
    finally:
        connection.close()

    return ok(source=Path(path).name, path=path, imports=imports,
              imported_by=importers, defines=symbols)


def project_overview(path=None):
    """What kind of project this is: stack, entry points, tests, dependencies."""
    connection = _connect()
    _schema(connection)
    try:
        if path:
            root = str(Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve())
            rows = connection.execute(
                "SELECT path, name, kind FROM files WHERE path LIKE ?",
                (root + "%",)).fetchall()
        else:
            roots = [r["path"] for r in connection.execute(
                "SELECT path FROM roots").fetchall()]
            if not roots:
                return err("Nothing is indexed yet.")
            root = roots[0]
            rows = connection.execute("SELECT path, name, kind FROM files").fetchall()

        if not rows:
            return err("Nothing indexed under " + str(root) + ".")

        names = {r["name"].lower() for r in rows}
        kinds = {}
        for r in rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1

        markers = {
            "package.json": "Node / JavaScript",
            "requirements.txt": "Python",
            "pyproject.toml": "Python",
            "cargo.toml": "Rust",
            "go.mod": "Go",
            "pom.xml": "Java (Maven)",
            "gemfile": "Ruby",
            "composer.json": "PHP",
            "next.config.js": "Next.js",
            "vite.config.js": "Vite",
            "dockerfile": "Docker",
        }
        stack = sorted({label for marker, label in markers.items() if marker in names})

        entry_points = sorted(
            r["name"] for r in rows
            if r["name"].lower() in {"main.py", "app.py", "server.py", "new.py",
                                     "index.js", "main.js", "app.js", "index.ts",
                                     "manage.py", "cli.py", "__main__.py"})
        tests = [r["name"] for r in rows
                 if "test" in r["name"].lower() or "/tests/" in r["path"].replace("\\", "/")]
        configs = sorted(n for n in names if n in markers or n.endswith((".toml", ".ini", ".cfg"))
                         or n.startswith(".env"))

        top_modules = [dict(r) for r in connection.execute(
            "SELECT module, COUNT(*) AS n FROM imports GROUP BY module"
            " ORDER BY n DESC LIMIT 12").fetchall()]
        biggest = [dict(r) for r in connection.execute(
            "SELECT name, chunks FROM files ORDER BY chunks DESC LIMIT 8").fetchall()]
        symbol_count = connection.execute(
            "SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    finally:
        connection.close()

    return ok(root=root, files=len(rows), file_kinds=kinds, stack=stack or ["unknown"],
              entry_points=entry_points, tests=sorted(set(tests))[:12], configs=configs[:12],
              most_imported=top_modules, largest_files=biggest, symbols=symbol_count)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

SCHEMAS = [
    tool("search_knowledge",
         "Search the user's indexed files, code and documents. Use this for anything "
         "about what is written in their files: 'find everything about the agent loop', "
         "'where is the tool router', 'what did I write about X'. Returns the source "
         "file and line numbers for every result. This is separate from `recall`, which "
         "is for personal facts the user told you.",
         {"query": string("What to look for."),
          "limit": integer("How many results to return. Default 6.")},
         ["query"]),
    tool("index_folder",
         "Add a folder to the knowledge index so its files become searchable. Indexes "
         "code, markdown, text, PDFs, config and data files. Only re-reads files that "
         "changed since last time.",
         {"path": string("The folder to index."),
          "background": boolean("Index in the background and return immediately.")},
         ["path"]),
    tool("knowledge_status",
         "Report what is indexed and which search methods are available, including "
         "whether semantic search is possible on this machine."),
    tool("find_symbol",
         "Find where a function or class is defined, with its file and line number.",
         {"name": string("The function or class name.")},
         ["name"]),
    tool("related_files",
         "Show what a file imports, what imports it, and what it defines.",
         {"file": string("A file name or path that has been indexed.")},
         ["file"]),
    tool("project_overview",
         "Summarise an indexed project: its stack, entry points, tests, configuration "
         "and most-used modules.",
         {"path": string("Project folder. Defaults to the first indexed root.")}),
    tool("forget_folder",
         "Remove a folder and its files from the knowledge index.",
         {"path": string("The folder to remove.")},
         ["path"]),
]

REGISTRY = {
    "search_knowledge": search,
    "index_folder": index_folder,
    "knowledge_status": status,
    "find_symbol": find_symbol,
    "related_files": related,
    "project_overview": project_overview,
    "forget_folder": forget_folder,
}
