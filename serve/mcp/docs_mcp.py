#!/usr/bin/env python3
"""serve/mcp/docs_mcp.py - MCP server: the documents under /home.

HyperCore starts it from mcp.yaml.  Paths are resolved and must stay inside the workspace
(`HYPERCORE_DOCS_ROOT`, /home by default); credential stores and non-text files are refused.
The tools themselves are in serve/docs_mcp.py - this file is the stdio transport and the
tool list the model is shown, in the same shape as the other servers here.

    python serve/mcp/docs_mcp.py         stdio, JSON-RPC one message per line
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from serve import docs_mcp

TOOLS = [
    {"name": "create_file",
     "description": "Create a new text document under /home. Fails if the file already exists.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A path inside the workspace, relative or absolute."},
         "content": {"type": "string", "description": "The text to write."}},
         "required": ["path", "content"]}},
    {"name": "read_file",
     "description": (
         "Read the text of one document under /home.\n\n"
         "- With no line numbers a file under 16 KB is returned in full. A larger one returns its "
         "structure with line numbers instead. That outline IS a successful response: do not retry "
         "the same call without line numbers, you will get the same outline. Read the specific "
         "range you need with start_line/end_line.\n"
         "- start_line and end_line are 1-indexed and inclusive; a range comes back with line "
         "numbers, which is what edit_file matches against.\n"
         "- A range longer than 20000 characters is cut off and tells you the start_line to "
         "continue from. Do not re-read a file you have just edited: the call fails if it did not work."
     ),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file to read."},
         "start_line": {"type": "integer", "description": "1-indexed first line of a range."},
         "end_line": {"type": "integer", "description": "1-indexed last line of a range, inclusive."}},
         "required": ["path"]}},
    {"name": "edit_file",
     "description": (
         "Apply one or more old_text/new_text replacements to an existing document under /home.\n\n"
         "- Edits are applied in order, and the file is written only if every edit matched.\n"
         "- old_text must match the file exactly, including indentation. Do not include the "
         "line-number prefix from read_file output.\n"
         "- If old_text matches more than once the call fails and lists the matching lines; add "
         "more context or pass line=<one of them>.\n"
         "- To create a new file, or replace the whole content, use create_file or write_file."
     ),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file to edit."},
         "edits": {"type": "array", "description": "A list of {old_text, new_text} objects.",
                   "items": {"type": "object", "properties": {
                       "old_text": {"type": "string"},
                       "new_text": {"type": "string"},
                       "line": {"type": "integer", "description": "Which match to edit when old_text "
                                "appears more than once."}}}},
         "force": {"type": "boolean", "description": "Accepted and ignored. An edit proceeds on the first call."}},
         "required": ["path", "edits"]}},
    {"name": "write_file",
     "description": "Replace the full text of an existing document under /home. Does not create a new file.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file to replace."},
         "content": {"type": "string", "description": "The new text of the file."},
         "force": {"type": "boolean", "description": "Accepted and ignored. An overwrite proceeds on the first call."}},
         "required": ["path", "content"]}},
    {"name": "delete_file",
     "description": "Delete one text document under /home. Reports the size removed.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The file to delete."},
         "force": {"type": "boolean", "description": "Accepted and ignored. A delete proceeds on the first call."}},
         "required": ["path"]}},
    {"name": "list_directory",
     "description": "List files and folders inside a directory under /home, up to depth levels.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A directory inside the workspace. Default the root."},
         "depth": {"type": "integer", "description": "How many levels to show, 1 to 4. Default 1."}}}},
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    """Run one tool. The handlers in serve/docs_mcp.py are coroutines and do their work
    synchronously, so a short-lived event loop is enough; nothing else here is async."""
    try:
        if name == "create_file":
            return text(run(docs_mcp.create_file, str(args.get("path") or ""),
                            args.get("content") if args.get("content") is not None else ""))
        if name == "read_file":
            return text(run(docs_mcp.read_file, str(args.get("path") or ""),
                            args.get("start_line"), args.get("end_line")))
        if name == "edit_file":
            return text(run(docs_mcp.edit_file, str(args.get("path") or ""), args.get("edits") or [],
                            bool(args.get("force", False))))
        if name == "write_file":
            return text(run(docs_mcp.write_file, str(args.get("path") or ""),
                            args.get("content") if args.get("content") is not None else "",
                            bool(args.get("force", False))))
        if name == "delete_file":
            return text(run(docs_mcp.delete_file, str(args.get("path") or ""), bool(args.get("force", False))))
        if name == "list_directory":
            return text(run(docs_mcp.list_directory, str(args.get("path") or "."), int(args.get("depth") or 1)))
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        return text(str(e), error=True)
    return text(f"unknown tool {name}", error=True)


def run(handler, *args):
    return asyncio.run(handler(*args))


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "docs", "title": "Documents in /home", "version": "1.0.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""),
                                    params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    docs_mcp.guard.root.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
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
    main()
