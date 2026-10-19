#!/usr/bin/env python3
"""serve/mcp/assist_mcp.py - MCP server for the Assist tab: converse with NVIDIA OpenAI-compatible models.

HyperCore starts it from mcp.yaml. Its tools are `status`, `models`, and `chat`. The endpoint and API key are
cached in the project .env file; the tool sends the model name in the OpenAI Chat Completions request body.

    python serve/mcp/assist_mcp.py
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from serve import assist  # noqa: E402

TOOLS = [
    {
        "name": "status",
        "description": "Check whether the cached NVIDIA Assist endpoint is configured and reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "models",
        "description": "List model IDs available from the cached NVIDIA OpenAI-compatible endpoint.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "chat",
        "description": "Converse with one external NVIDIA model by sending model name and OpenAI-style messages.",
        "inputSchema": {
            "type": "object",
            "required": ["model", "messages"],
            "properties": {
                "model": {"type": "string", "description": "Model ID, for example openai/gpt-oss-20b."},
                "messages": {
                    "type": "array",
                    "description": "OpenAI-style messages: [{role:'user', content:'...'}].",
                    "items": {
                        "type": "object",
                        "required": ["role", "content"],
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant", "system"]},
                            "content": {"type": "string"},
                        },
                    },
                },
                "max_tokens": {"type": "integer", "minimum": 1},
            },
        },
    },
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    try:
        if name in ("status", "assist_status", "nvidia_status"):
            return text(json.dumps(assist.status(), indent=1))
        if name in ("models", "assist_models", "list_models"):
            return text(json.dumps(assist.list_models(), indent=1))
        if name in ("chat", "converse", "assist_chat", "nvidia_chat"):
            return text(json.dumps(
                assist.converse(
                    str(args.get("model") or ""),
                    args.get("messages") or [],
                    int(args.get("max_tokens") or assist.ASSIST_DEFAULTS["max_tokens"]),
                ),
                indent=1,
            ))
        return text(f"unknown assist tool: {name}", True)
    except Exception as exc:
        return text(str(exc), True)


def answer(msg: dict):
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "assist", "version": "0.1"},
        }}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    elif method in ("notifications", "ping"):
        out = {"jsonrpc": "2.0", "id": rid, "result": {}}
    elif method == "tools/call":
        out = {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""), params.get("arguments") or {})}
    else:
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}
    print(json.dumps(out), flush=True)


def stdio():
    try:
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
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    stdio()