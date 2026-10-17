# Web-scrape MCP server

Sandboxed binary download and page-media extraction for AnythingLLM / Qwen.

All writes stay under `MCP_ALLOWED_DIR`. Private and loopback IPs are blocked by default (`MCP_BLOCK_PRIVATE_IPS`).

The server runs on the GPU box (`192.168.74.2`) as streamable HTTP so the AnythingLLM container can reach it on the `ai-bridge` Docker network.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `download-file` | Stream one URL to disk |
| `extract-media` | List image/video/audio URLs on a page (no page text) |
| `download-page-media` | Extract then download the first N media files |
| `batch-download` | Parallel download of an explicit URL list (max 50) |
| `get-downloaded-file-info` | Size, MIME, image/video dimensions |
| `list-downloads` | Recursive listing of `ALLOWED_DIR` |

## Setup on 192.168.74.2

```bash
cd /home/user0/web-scrape-mcp
docker compose up -d --build
```

Files land in `/mnt/anythingllm_shared` (AnythingLLM's `./anythingllm-fs/`). Pass `subfolder=YYYY-MM-DD_HHMM` so each run stays in its own directory. The Interesting_find workspace prompt is in `contrib/anythingllm_agent_prompt.md`.

Cursor / stdio:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
export MCP_ALLOWED_DIR=./downloads
python -m webscrape --stdio
```

## AnythingLLM

In `/var/lib/anythingllm/storage/plugins/anythingllm_mcp_servers.json`:

```json
{
  "mcpServers": {
    "web-scrape": {
      "url": "http://web-scrape-mcp:8766/mcp",
      "type": "http",
      "disabled": false,
      "alwaysAllow": true
    }
  }
}
```

Then in AnythingLLM: Agent Skills → start `web-scrape`.

## Tests

```bash
cd web-scrape-mcp
pytest -q
```
