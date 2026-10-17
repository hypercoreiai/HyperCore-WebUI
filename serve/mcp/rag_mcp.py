"""MCP tools for the /home document index (serve/rag.py).

stdio JSON-RPC, same shape as the Firefox server. Paths that resolve outside /home are refused.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from serve.rag import (
    read_document,
    search,
    start_background,
    status,
    structure,
)

TOOLS = [
    {"name": "status",
     "description": "How far the /home document index has got: files, folders, and whether a crawl is running.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search",
     "description": "Search Linux configs, documents, and folder names under /home. Returns paths and short passages.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Words to look for."},
         "limit": {"type": "integer", "description": "How many paths to return, 1 to 12. Default 6."}},
         "required": ["query"]}},
    {"name": "structure",
     "description": "List directories and document files under a path. The path must stay inside /home.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A directory inside /home. Default /home."},
         "depth": {"type": "integer", "description": "How many levels to show, 1 to 4. Default 2."}}}},
    {"name": "read",
     "description": ("Read one document under /home (text, markdown, Linux config, html, pdf, docx, and the other "
                     "indexed types).\n\n- With no line numbers a large file returns its structure with line "
                     "numbers. That outline IS a successful response: do not repeat the call without line numbers.\n"
                     "- start_line and end_line are 1-indexed and inclusive; a range comes back numbered and says "
                     "which start_line continues it."),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Full path of a file inside /home."},
         "start_line": {"type": "integer", "description": "1-indexed first line of a range to read."},
         "end_line": {"type": "integer", "description": "1-indexed last line of a range, inclusive."}},
         "required": ["path"]}},
    {"name": "reindex",
     "description": "Walk /home again and refresh the document index. Returns immediately if a crawl is already running.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    try:
        if name == "status":
            return text(json.dumps(status(), indent=1))
        if name == "search":
            hits = search(str(args.get("query") or ""), int(args.get("limit") or 6))
            if not hits:
                return text("No matching documents under /home yet.")
            return text("\n\n".join(f"[{h['kind']}] {h['path']}\n{h['snippet']}" for h in hits))
        if name == "structure":
            return text(structure(str(args.get("path") or "/home"), int(args.get("depth") or 2)))
        if name == "read":
            return text(read_document(str(args.get("path") or ""),
                                     args.get("start_line"), args.get("end_line")))
        if name == "reindex":
            start_background()
            return text("Crawl of /home started. Call status to see progress.")
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        return text(str(e), error=True)
    return text(f"unknown tool {name}", error=True)


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "home", "title": "Documents in /home", "version": "0.1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""),
                                    params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    out = threading.Lock()

    def answer(msg):
        reply = handle(msg)
        if reply is None:
            return
        with out:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "tools/call":
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    stdio()
