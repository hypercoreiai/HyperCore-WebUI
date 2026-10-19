"""serve/rag.py - retrieval over documents in /home.

The index is lexical (SQLite FTS5): Linux config and unit files, ordinary documents, and one row per
directory so a question can find a folder as well as a page. Nothing outside /home is read or returned.
The database lives in ~/.cache/hypercore, which the crawl skips.
"""
from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from serve import doc_text

# The defaults are what the chat uses. HYPERCORE_RAG_ROOT / HYPERCORE_RAG_DB point the index at a
# different tree, which is how the tests in serve/mcp/tests run against a temporary workspace.
ROOT = Path(os.environ.get("HYPERCORE_RAG_ROOT") or "/home")
DB_PATH = Path(os.environ.get("HYPERCORE_RAG_DB") or (Path.home() / ".cache" / "hypercore" / "rag.sqlite"))
MAX_TEXT = 1_500_000
MAX_BINARY_DOC = 12_000_000
MAX_FILES = 80_000
CHUNK = 1100
OVERLAP = 160
SNIPPET = 700

# Names of directories that are caches, environments, secrets, or weight stores.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__", ".cache", "Cache",
    ".npm", ".cargo", "target", "dist", "build", ".tox", "site-packages", ".ssh", ".gnupg",
    ".aws", ".mozilla", ".thunderbird", "Trash", ".Trash", "snap", "Steam", "steamapps",
    "packs", "models", "blobs", "weights", ".gguf",
}
SECRET_SUFFIXES = {".pem", ".key", ".kdbx", ".p12", ".pfx"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "cookies.sqlite", "login.keyring", "shadow"}

LINUX_EXT = {
    "conf", "cfg", "ini", "cnf", "properties", "rules", "list",
    "service", "socket", "timer", "mount", "automount", "path", "slice", "target", "network",
    "netdev", "link", "desktop", "sh", "bash", "zsh", "spec",
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
}
UNIVERSAL_EXT = {
    "md", "markdown", "rst", "txt", "text", "csv", "tsv", "json", "html", "htm", "xml",
    "yaml", "yml", "toml", "pdf", "docx", "odt", "rtf", "log", "org", "adoc",
}

_lock = threading.Lock()
_state: dict[str, int | float | str] = {"phase": "idle", "files": 0, "dirs": 0, "skipped": 0, "error": "", "started": 0.0}
_thread = None


def _bump(key: str, by: int = 1) -> None:
    """Add to one of the crawl counters. The cast keeps mypy from reading the value as `object`."""
    _state[key] = int(_state.get(key, 0)) + by


def classify(path: Path) -> str | None:
    name = path.name.lower()
    if name in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES:
        return None
    ext = path.suffix.lower().lstrip(".")
    if ext.endswith(".gz") and name.count(".") >= 2:
        ext = name.split(".")[-2]
    if ext in LINUX_EXT:
        return "linux"
    if ext in UNIVERSAL_EXT:
        return "universal"
    if name in {"readme", "changelog", "license", "copying", "authors", "todo", "makefile", "dockerfile"}:
        return "linux"
    return None


def inside(path: Path, root: Path = ROOT) -> Path | None:
    """Resolved path if it is `root` or a real child of it. Symlinks that leave `root` are rejected."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return resolved


def _chunks(text: str):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return
    if len(text) <= CHUNK:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK)
        if end < len(text):
            cut = text.rfind("\n\n", start + CHUNK // 2, end)
            if cut == -1:
                cut = text.rfind("\n", start + CHUNK // 2, end)
            if cut != -1:
                end = cut
        piece = text[start:end].strip()
        if piece:
            yield piece
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)


def _html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", raw))


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def _pdf_text(path: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
                             capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""


def read_text(path: Path, limit: int = MAX_TEXT) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf_text(path)[:limit]
    if ext == ".docx":
        try:
            return _docx_text(path)[:limit]
        except (OSError, KeyError, zipfile.BadZipFile):
            return ""
    if ext == ".odt":
        try:
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("content.xml").decode("utf-8", "replace")
            return _html_text(xml)[:limit]
        except (OSError, KeyError, zipfile.BadZipFile):
            return ""
    try:
        if path.name.lower().endswith(".gz"):
            import gzip
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                raw = f.read(limit + 1)
        else:
            with open(path, "rt", encoding="utf-8", errors="replace") as f:
                raw = f.read(limit + 1)
    except OSError:
        return ""
    if "\x00" in raw[:8000]:
        return ""
    if ext in {".html", ".htm", ".xml", ".rtf"}:
        raw = _html_text(raw)
    return raw[:limit]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS file_rows(
        path TEXT NOT NULL, rowid INTEGER NOT NULL)""")
    con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(path, kind, body)""")
    con.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
    return con


def _drop_path(con: sqlite3.Connection, path: str):
    rows = list(con.execute("SELECT rowid FROM file_rows WHERE path = ?", (path,)))
    for (rowid,) in rows:
        con.execute("DELETE FROM docs WHERE rowid = ?", (rowid,))
    con.execute("DELETE FROM file_rows WHERE path = ?", (path,))


def _add(con: sqlite3.Connection, path: str, kind: str, body: str):
    cur = con.execute("INSERT INTO docs(path, kind, body) VALUES(?, ?, ?)", (path, kind, body))
    con.execute("INSERT INTO file_rows(path, rowid) VALUES(?, ?)", (path, cur.lastrowid))


def _dir_body(path: Path, names: list[str]) -> str:
    shown = ", ".join(names[:40])
    more = f" (+{len(names) - 40} more)" if len(names) > 40 else ""
    return f"directory {path.name or path} at {path}. Contains: {shown}{more}"


def crawl(root: Path = ROOT, db_path: Path | None = None, fresh: bool = False) -> dict:
    """Walk `root` and refresh the index. The server always passes /home. Returns the status dict."""
    root = root.resolve()
    db = db_path or DB_PATH
    lock_path = db.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _state.update(phase="walking", files=0, dirs=0, skipped=0, error="", started=time.time())
        with open(lock_path, "a", encoding="utf-8") as lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _state["phase"] = "ready"
                return dict(_state)
            con = connect(db)
            try:
                if fresh:
                    con.execute("DELETE FROM file_rows")
                    con.execute("DELETE FROM docs")
                seen_paths: set[str] = set()
                seen = 0
                for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                    current = Path(dirpath)
                    held = inside(current, root)
                    if held is None:
                        dirnames[:] = []
                        continue
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
                    names = sorted(dirnames) + sorted(filenames)
                    _drop_path(con, str(held))
                    _add(con, str(held), "directory", _dir_body(held, names))
                    seen_paths.add(str(held))
                    _bump("dirs")
                    for name in filenames:
                        if seen >= MAX_FILES:
                            _state["phase"] = "capped"
                            break
                        path = current / name
                        kind = classify(path)
                        if kind is None:
                            continue
                        try:
                            size = path.stat().st_size
                        except OSError:
                            _bump("skipped")
                            continue
                        limit = MAX_BINARY_DOC if path.suffix.lower() in {".pdf", ".docx", ".odt"} else MAX_TEXT
                        if size > limit or size == 0:
                            _bump("skipped")
                            continue
                        resolved = inside(path, root)
                        if resolved is None:
                            continue
                        text = read_text(resolved)
                        if not text.strip():
                            _bump("skipped")
                            continue
                        _drop_path(con, str(resolved))
                        for i, piece in enumerate(_chunks(text)):
                            _add(con, str(resolved), kind, f"{resolved.name}\n{piece}" if i == 0 else piece)
                        seen_paths.add(str(resolved))
                        _bump("files")
                        seen += 1
                    if seen >= MAX_FILES:
                        break
                    if int(_state["dirs"]) % 200 == 0:
                        con.commit()
                if _state["phase"] != "capped":
                    prefix = str(root) + os.sep
                    for (old,) in list(con.execute("SELECT DISTINCT path FROM file_rows")):
                        if old not in seen_paths and (old == str(root) or old.startswith(prefix)):
                            _drop_path(con, old)
                    _state["phase"] = "ready"
                con.execute("INSERT INTO meta(k, v) VALUES('built', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                            (str(int(time.time())),))
                con.commit()
            except Exception as e:  # noqa: BLE001 - the crawl reports the failure and the chat still runs
                _state["phase"] = "error"
                _state["error"] = str(e)
            finally:
                con.close()
            return dict(_state)


def start_background(root: Path = ROOT, db_path: Path | None = None):
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=crawl, args=(root, db_path), name="hypercore-rag", daemon=True)
    _thread.start()


def status(db_path: Path | None = None, root: Path = ROOT) -> dict:
    out = dict(_state)
    db = db_path or DB_PATH
    out["db"] = str(db)
    out["root"] = str(root)
    if db.is_file():
        try:
            con = connect(db)
            row = con.execute("SELECT count(*) FROM file_rows").fetchone()
            built = con.execute("SELECT v FROM meta WHERE k = 'built'").fetchone()
            # One file can be indexed as several chunks, so count paths, not rows.
            files = con.execute(
                "SELECT count(DISTINCT path) FROM docs WHERE kind != 'directory'").fetchone()
            dirs = con.execute(
                "SELECT count(DISTINCT path) FROM docs WHERE kind = 'directory'").fetchone()
            con.close()
            out["rows"] = row[0] if row else 0
            out["built"] = int(built[0]) if built else 0
            # _state only fills during a crawl in this process. A server that starts with an
            # index already on disk would otherwise report 0 files and 0 folders.
            if out["phase"] == "idle" and out["rows"]:
                out["phase"] = "ready"
                out["files"] = files[0] if files else 0
                out["dirs"] = dirs[0] if dirs else 0
        except sqlite3.Error as e:
            out["error"] = out.get("error") or str(e)
    else:
        out["rows"] = 0
        out["built"] = 0
    return out


def _match(query: str) -> str | None:
    words = re.findall(r"[\w./-]{2,}", query or "")[:16]
    if not words:
        return None
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words)


def search(query: str, limit: int = 6, db_path: Path | None = None, root: Path = ROOT) -> list[dict]:
    match = _match(query)
    if not match:
        return []
    path = db_path or DB_PATH
    if not path.is_file():
        return []
    limit = max(1, min(12, int(limit)))
    con = connect(path)
    try:
        rows = con.execute(
            "SELECT path, kind, snippet(docs, 2, '', '', ' … ', 28) FROM docs WHERE docs MATCH ? "
            "ORDER BY rank LIMIT ?", (match, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        con.close()
        return []
    con.close()
    hits, seen = [], set()
    for path_s, kind, snippet in rows:
        if path_s in seen:
            continue
        resolved = inside(Path(path_s), root)
        if resolved is None:
            continue
        seen.add(path_s)
        hits.append({"path": str(resolved), "kind": kind, "snippet": (snippet or "")[:SNIPPET]})
        if len(hits) >= limit:
            break
    return hits


def structure(path: str = "/home", depth: int = 2, root: Path = ROOT) -> str:
    """A short tree of directories and document files under `root` (the server uses /home)."""
    start = inside(Path(path or str(root)), root)
    if start is None or not start.is_dir():
        raise ValueError(f"path must be a directory inside {root}")
    depth = max(1, min(4, int(depth)))
    lines = [str(start)]

    def walk(directory: Path, level: int):
        if level > depth:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            lines.append("  " * level + f"(unreadable: {e})")
            return
        shown = 0
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir():
                lines.append("  " * level + entry.name + "/")
                shown += 1
                walk(entry, level + 1)
            elif classify(entry):
                lines.append("  " * level + entry.name)
                shown += 1
            if shown >= 80:
                lines.append("  " * level + "…")
                break

    walk(start, 1)
    return "\n".join(lines[:400])


def read_document(path: str, start_line: int | None = None, end_line: int | None = None,
                  root: Path = ROOT) -> str:
    """Read a document, or a line range of it.

    With no range a large file comes back as its structure with line numbers rather
    than as a blind slice of its first characters; the text says so, because a model
    that does not know will repeat the identical call.

    The whole text is read, up to MAX_TEXT characters - the same bound the crawl uses -
    so the line numbers and the "of N lines" counts are the file's rather than a window's.
    Reading only a window made a range past the window look like a range past the end of
    the file. A file bigger than MAX_TEXT is capped, and the answer says so.
    """
    resolved = inside(Path(path), root)
    if resolved is None or not resolved.is_file():
        raise ValueError(f"path must be a file inside {root}")
    if classify(resolved) is None:
        raise ValueError("that file is not a document type this index reads")

    full = read_text(resolved)
    if not full.strip():
        raise ValueError("the file had no readable text")
    # read_text stops at MAX_TEXT characters. When it did, say so, because every line
    # count below is then of that part of the file only.
    capped = len(full) >= MAX_TEXT and resolved.stat().st_size > MAX_TEXT
    cap_note = (f"\n\n[this index reads at most {MAX_TEXT:,} characters of a file, so the line "
                "counts are of that part only; for the rest use the terminal run tool with sed "
                "or awk on the path]") if capped else ""

    if start_line is not None or end_line is not None:
        first, last = doc_text.resolve_line_range(start_line, end_line)
        body, note = doc_text.read_with_ranges(full, first, last, str(resolved))
        if not body:
            if capped:
                raise ValueError(f"the text this index reads stops at line {doc_text.line_count(full)}: "
                                 f"the file is bigger than {MAX_TEXT:,} characters. For lines past that "
                                 "use the terminal run tool with sed or awk on the path.")
            raise ValueError(note)
        return f"{resolved}\n\n{body}{note}{cap_note}"

    entries = doc_text.extract_outline(str(resolved), full)
    if len(full) > doc_text.AUTO_OUTLINE_SIZE and entries:
        page, more = doc_text.render_outline(entries)
        total_lines = doc_text.line_count(full)
        tail = (f"\n\n[{len(entries)} symbols in {total_lines} lines"
                + ("; more are not shown - read a range with start_line from the outline above"
                   if more else "; the whole structure is shown")
                + ". This file is too large to read all at once. Do NOT retry this call without "
                  "line numbers - you will get the same outline. To read a symbol, call read with "
                  "the same path plus start_line and end_line.]")
        return f"{resolved}\n\n# File outline for {resolved}\n{page}{tail}{cap_note}"

    text = full[: doc_text.MAX_RANGE_CHARS]
    header = f"{resolved}\n\n{text}"
    if len(full) > len(text):
        header += (f"\n\n[showing {len(text)} of {len(full)} characters read; this file has no "
                   "readable structure, so it is too large to read all at once. Do NOT repeat this "
                   "call - you will get the same first characters. Read a range with "
                   "start_line/end_line, or use the terminal run tool with head, tail or sed.]")
    return header + cap_note


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") in (None, "text"))
    return ""


def _with_leading_system(messages: list[dict], content: str) -> list[dict]:
    """The chat template merges system messages only while they stay at the front."""
    out = list(messages)
    at = 0
    while at < len(out) and out[at].get("role") in ("system", "developer"):
        at += 1
    out.insert(at, {"role": "system", "content": content})
    return out


def attach(messages: list[dict], db_path: Path | None = None, root: Path = ROOT) -> tuple[list[dict], list[dict]]:
    """Add a leading system note with the best passages. The user's own text is unchanged."""
    query = _last_user_text(messages)
    hits = search(query, db_path=db_path, root=root) if query else []
    if not hits:
        if query and _state.get("phase") == "walking":
            note = "The document index of /home is still being built, so no local passages were retrieved."
            return _with_leading_system(messages, note), []
        return messages, []
    lines = ["Retrieved from /home. Use these passages when they answer the question, and cite the path."]
    for hit in hits:
        lines.append(f"\n[{hit['kind']}] {hit['path']}\n{hit['snippet']}")
    return _with_leading_system(messages, "\n".join(lines)), hits
