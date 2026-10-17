#!/usr/bin/env python3
"""The document tools behind serve/mcp/docs_mcp.py: create, read, edit, write, delete, list.

The path guard and the text rules live here; the stdio transport and the tool schemas are in
serve/mcp/docs_mcp.py. Paths are resolved and must stay inside the workspace (/home, or
HYPERCORE_DOCS_ROOT in tests). Non-text files are refused. Credential paths are readable: the
shell can already cat them, and this phase does not add a second denial. decide() is not
called; an edit proceeds on the first try. Both gates stay in the tree for the later
/home/ai-agent/ pass.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from . import doc_text, tool_permissions

MAX_CHARS = 1_500_000
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".mozilla", ".thunderbird", ".git"}
SECRET_SUFFIXES = {".pem", ".key", ".kdbx", ".p12", ".pfx", ".env", ".netrc"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "cookies.sqlite", "shadow", "authorized_keys"}
TEXT_EXT = {
    "txt", "text", "md", "markdown", "json", "py", "toml", "yaml", "yml", "csv", "tsv", "rst",
    "html", "htm", "xml", "ini", "conf", "cfg", "cnf", "properties", "rules", "list",
    "service", "socket", "timer", "mount", "automount", "path", "slice", "target", "network",
    "netdev", "link", "desktop", "sh", "bash", "zsh", "spec", "log", "org", "adoc", "css",
    "js", "ts", "jsx", "tsx", "svg", "c", "h", "cpp", "cc", "hpp", "cu",
    "cmake", "sqlite3", "db", "jsonl", "ndjson",
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
}
TEXT_NAMES = {"readme", "changelog", "license", "copying", "authors", "todo", "makefile", "dockerfile"}

_locks: dict[str, threading.Lock] = {}
_locks_mu = threading.Lock()


class WorkspaceGuard:
    def __init__(self, workspace_root: str | Path):
        self.root = Path(workspace_root).resolve()

    def validate_path(self, relative_path: str) -> Path:
        """Resolve a path and refuse anything that lands outside the workspace, including via a symlink."""
        raw = "" if relative_path is None else str(relative_path).strip()
        if raw in ("", "."):
            return self.root
        candidate = Path(raw)
        target = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        if not target.is_relative_to(self.root):
            raise PermissionError("Access denied: Target path is outside the workspace root.")
        return target


def _is_secret(target: Path, root: Path) -> bool:
    try:
        parts = {part.lower() for part in target.relative_to(root).parts}
    except ValueError:
        return True
    if parts & SECRET_DIRS:
        return True
    return target.name.lower() in SECRET_NAMES or target.suffix.lower() in SECRET_SUFFIXES


def _looks_like_text(path: Path) -> bool:
    """Decide by reading the head of the file, not by guessing from its name.

    A name with no extension says nothing: `.gitignore`, `.dockerignore` and a Makefile are
    plain text, while the engine binary beside them is not. A NUL byte or a run that will not
    decode is the signal, and 4 KiB is enough to see it without reading a whole model pack.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if not head:
        return True
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _require_text(path: Path, writing: bool = False) -> None:
    # A key file or a file under .ssh has no text extension. The shell can cat it, so the
    # document tools can read it too. Writing one is a different question: a credential is not a
    # document, and creating it here would put a key under a name the model can edit freely.
    if _is_secret(path, guard.root):
        if writing:
            raise PermissionError(f"'{path.name}' is a credential, not a document")
        return
    if path.name.lower() in TEXT_NAMES or path.suffix.lower().lstrip(".") in TEXT_EXT:
        return
    # No extension: the name carries no information, so the content decides. A file that does not
    # exist yet is text by construction - the content being written is already a str.
    if not path.suffix and (not path.exists() or _looks_like_text(path)):
        return
    raise ValueError(f"only text documents can be read or written, not '{path.name}'")


def _lock(path: Path) -> threading.Lock:
    """One lock per path, so two calls in this process cannot write the same file at once."""
    key = str(path)
    with _locks_mu:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _read_head(path: Path, limit: int) -> bytes:
    """Read at most `limit` bytes, under the per-path lock.

    The tool handlers are async, so the blocking read is run in a thread rather than on the
    event loop: a 1.5 MB document should not hold up every other call in the server.
    """
    with _lock(path), open(path, "rb") as handle:
        return handle.read(limit)


def _create_text(path: Path, content: str) -> None:
    with _lock(path), open(path, "x", encoding="utf-8") as handle:
        handle.write(content)


def _write_text(path: Path, content: str) -> None:
    with _lock(path), open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _write_bytes(path: Path, data: bytes) -> None:
    with _lock(path), open(path, "wb") as handle:
        handle.write(data)


def _bounded(content: str, path: str) -> str | None:
    if not isinstance(content, str):
        return f"Error: content for '{path}' must be text."
    if len(content) > MAX_CHARS:
        return f"Error: '{path}' is larger than {MAX_CHARS} characters."
    return None


guard = WorkspaceGuard(os.environ.get("HYPERCORE_DOCS_ROOT", "/home"))


async def create_file(path: str, content: str) -> str:
    try:
        too_big = _bounded(content, path)
        if too_big:
            return too_big
        target = guard.validate_path(path)
        _require_text(target, writing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_create_text, target, content)
        tool_permissions.note_created(target)
        return f"Successfully created {target}"
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except FileExistsError:
        return f"Error: File '{path}' already exists."
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


async def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    try:
        target = guard.validate_path(path)
        if not target.is_file():
            return f"Error: File '{path}' not found."
        _require_text(target)
        size = target.stat().st_size
        raw = await asyncio.to_thread(_read_head, target, MAX_CHARS + 1)
        try:
            content = raw.decode("utf-8")
            unreadable = ""
        except UnicodeDecodeError:
            # The bytes are shown as U+FFFD so the file can still be read; edit_file works on
            # the bytes themselves, so an edit never rewrites what the model could not see.
            content = raw.decode("utf-8", "replace")
            unreadable = ("\n\n[Note: this file is not valid UTF-8, so some bytes are shown as "
                         "U+FFFD. An old_text copied from here with those characters will not "
                         "match; edit the readable parts, or use run with sed.]" if "\ufffd" in content
                         else "")
        if "\x00" in content[:8000]:
            return f"Error: '{path}' is not a text document."

        if start_line is not None or end_line is not None:
            first, last = doc_text.resolve_line_range(start_line, end_line)
            body, note = doc_text.read_with_ranges(content, first, last, str(target))
            if not body:
                return f"Error: {note}"
            return body + note + unreadable

        if size > doc_text.AUTO_OUTLINE_SIZE:
            entries = doc_text.extract_outline(str(target), content)
            if entries:
                page, more = doc_text.render_outline(entries)
                total_lines = doc_text.line_count(content)
                tail = (
                    f"\n\n[{len(entries)} symbols in {total_lines} lines"
                    + ("; more are not shown - read a range with start_line from the outline above"
                       if more else "; the whole structure is shown")
                    + ". This file is too large to read all at once. Do NOT retry this call "
                      "without line numbers - you will get the same outline. To read a symbol, "
                      "call read_file with the same path plus start_line and end_line.]"
                )
                return f"# File outline for {path}\n\n{page}{tail}" + unreadable
            body = content[: doc_text.MAX_RANGE_CHARS]
            return (body +
                   f"\n\n[showing {len(body)} of {size} characters; this file has no readable "
                   f"structure, so it is too large to read all at once. Do NOT repeat this call - "
                   f"you will get the same first {doc_text.MAX_RANGE_CHARS} characters. Read a "
                   "range with start_line/end_line, or use run with head, tail or sed on the path.]"
                   + unreadable)

        if len(content) > MAX_CHARS:
            return (content[:MAX_CHARS] +
                    f"\n\n[showing {MAX_CHARS} of {size} characters; the rest is not included. "
                    "Use run with head, tail or sed on the path for a specific range]" + unreadable)
        return content + unreadable
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


async def edit_file(path: str, edits: list[dict], force: bool = False) -> str:
    try:
        target = guard.validate_path(path)
        if not target.is_file():
            return f"Error: File '{path}' not found. Use create_file for a new document."
        _require_text(target, writing=True)
        # decide() would deny this and tell the model to ask the user. It stays uncalled
        # until the /home/ai-agent/ pass. force is accepted and does not change the result.
        del force
        parsed, error = doc_text.parse_edits(edits)
        if error:
            return f"Error: {error}"
        raw = await asyncio.to_thread(_read_head, target, MAX_CHARS + 1)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Not valid UTF-8: matching in text would rewrite the bytes the model never saw, so
            # the edits are applied to the original bytes instead.
            if len(raw) > MAX_CHARS:
                return f"Error: '{path}' is larger than {MAX_CHARS} characters; edit a smaller file or use run with sed."
            patched, byte_report = doc_text.apply_edits(raw, parsed)
            await asyncio.to_thread(_write_bytes, target, patched)
            tool_permissions.note_created(target)
            return (f"Edited {target}: {len(parsed)} edit(s) applied (the file is not valid UTF-8, "
                    f"so the edits were matched byte by byte).\n" + "\n".join(byte_report))
        if len(content) > MAX_CHARS:
            return f"Error: '{path}' is larger than {MAX_CHARS} characters; edit a smaller file or use run with sed."
        updated, report = doc_text.apply_edits(content, parsed)
        await asyncio.to_thread(_write_bytes, target, updated.encode("utf-8"))
        tool_permissions.note_created(target)
        return f"Edited {target}: {len(parsed)} edit(s) applied.\n" + "\n".join(report)
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


async def write_file(path: str, content: str, force: bool = False) -> str:
    try:
        too_big = _bounded(content, path)
        if too_big:
            return too_big
        target = guard.validate_path(path)
        _require_text(target, writing=True)
        if not target.is_file():
            return f"Error: File '{path}' not found. Use create_file for a new document."
        del force
        await asyncio.to_thread(_write_text, target, content)
        tool_permissions.note_created(target)
        return f"Successfully saved {target}"
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


async def delete_file(path: str, force: bool = False) -> str:
    try:
        target = guard.validate_path(path)
        if not target.is_file():
            return f"Error: File '{path}' not found."
        del force
        size = target.stat().st_size
        with _lock(target):
            target.unlink()
        tool_permissions.note_removed(target)
        return f"Deleted {target} ({size} bytes removed)"
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except OSError as exc:
        return f"Error: could not delete '{path}': {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


async def list_directory(path: str = ".", depth: int = 1) -> str:
    """List a directory, up to `depth` levels (1 to 4). Directories carry their children when depth > 1."""
    depth = max(1, min(int(depth or 1), 4))
    try:
        target = guard.validate_path(path)
        if not target.is_dir():
            return f"Error: Directory '{path}' not found."

        def walk(directory, levels, budget):
            rows: list[dict[str, object]] = []
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
                if len(rows) >= budget:
                    rows.append({"name": "…", "type": "truncated"})
                    break
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if not resolved.is_relative_to(guard.root):
                    continue
                if resolved.is_dir():
                    row: dict[str, object] = {"name": entry.name, "type": "dir"}
                    if levels > 1:
                        children = walk(resolved, levels - 1, budget - len(rows))
                        if children:
                            row["children"] = children
                    rows.append(row)
                elif resolved.is_file():
                    try:
                        size = resolved.stat().st_size
                    except OSError:
                        size = None
                    rows.append({"name": entry.name, "type": "file",
                                 "bytes": size if size is not None else 0})
            return rows

        return json.dumps({"path": str(target), "depth": depth, "entries": walk(target, depth, 200)}, indent=1)
    except PermissionError as exc:
        return f"Security Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - the model reads the failure as a tool result
        return f"Unexpected Error: {exc}"


if __name__ == "__main__":
    print("the document server starts as serve/mcp/docs_mcp.py")
