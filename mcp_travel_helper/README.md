# Travel Helper MCP Server

MCP server that reads **travel_helper.json** (produced by `travel_helper.py`) and exposes tools to list and search flight deals and destination info. No external APIs — data comes from the local file.

Inspired by the [Trivago MCP server](https://mcp.trivago.com/docs); supports **STDIO** (for Cursor/Claude Desktop) and **Streamable HTTP** (URL-based, for remote clients).

---

## Data source

- **File**: `travel_helper.json` (by default next to the repo root, or set `TRAVEL_HELPER_JSON`).
- **Root keys**: `cheapest_flights` or `cheapest_flights_with_hotels` (list of destination groups).
- **Each group**: `destination`, `days`, `nights`, `min_total_eur`, `flights[]`, optional `destination_info` (weather, attractions, etc.).

---

## Tools

| Tool | Description |
|------|-------------|
| **travel_deals_list** | List all destination deal groups (optional `limit`). |
| **travel_deals_search** | Search by destination name (substring, case-insensitive). |
| **travel_deals_destination** | Full details for one destination (flights + destination_info). |
| **travel_deals_cheapest** | Top N cheapest deals by `min_total_eur`. |
| **travel_deals_flights_for_destination** | Flight options for a destination, optional outbound date range. |

---

## Transport modes (Trivago-style)

### STDIO mode (default)

For local MCP clients that spawn the server as a subprocess (e.g. Cursor, Claude Desktop). The client runs the server with `command` + `args`; communication is over stdin/stdout.

```bash
# From repo root with venv activated
python -m mcp_travel_helper
# or explicitly:
python -m mcp_travel_helper --transport stdio
```

### Streamable HTTP mode

For clients that connect via URL (e.g. `mcp.client.streamable_http.streamable_http_client(url)`). The server runs an HTTP server; the MCP endpoint is at `/mcp`.

```bash
# Bind to localhost (default host=127.0.0.1, port=8000)
python -m mcp_travel_helper --transport streamable-http

# Accept external connections and custom port
python -m mcp_travel_helper --transport streamable-http --host 0.0.0.0 --port 8080
```

**Endpoint URL**: `http://<host>:<port>/mcp` (e.g. `http://127.0.0.1:8000/mcp`).

**Documentation**: When running Streamable HTTP, open `http://<host>:<port>/docs` in a browser for Trivago-style docs (intro, sample prompts, tools table, configuration, installation). See [Trivago MCP docs](https://mcp.trivago.com/docs) for the style reference.

Environment variables (optional): `TRAVEL_HELPER_MCP_TRANSPORT`, `TRAVEL_HELPER_MCP_HOST`, `TRAVEL_HELPER_MCP_PORT`.

---

## Run (summary)

From repo root with venv activated:

```bash
# STDIO (default): for Cursor / Claude Desktop
python -m mcp_travel_helper

# Streamable HTTP: for URL-based clients
python -m mcp_travel_helper --transport streamable-http [--host 127.0.0.1] [--port 8000]

# Custom JSON path (any mode)
TRAVEL_HELPER_JSON=/path/to/travel_helper.json python -m mcp_travel_helper
```

---

## Cursor / VS Code MCP config

Use **STDIO mode** by configuring a command (same pattern as Trivago when used via command):

```json
{
  "mcpServers": {
    "travel-helper": {
      "command": "/path/to/travel_helper/.venv-travel/bin/python",
      "args": ["-m", "mcp_travel_helper"],
      "cwd": "/path/to/travel_helper",
      "env": {}
    }
  }
}
```

Optional: set `TRAVEL_HELPER_JSON` in `env` if the JSON file is elsewhere. To force STDIO explicitly: `"args": ["-m", "mcp_travel_helper", "--transport", "stdio"]`.

If your client supports **URL-based** MCP servers (e.g. Streamable HTTP), start the server with `--transport streamable-http` and set the server URL to `http://127.0.0.1:8000/mcp` (or your host/port).

---

## Dependencies

- **mcp** (with FastMCP): `pip install "mcp[cli]"` (or use the project’s `.venv-travel` which already has it).

No API keys required.

---

## Test client (Streamable HTTP)

Run from the **repository root** (so Python finds the `mcp_travel_helper` package). With the server running (e.g. `python -m mcp_travel_helper --transport streamable-http --port 8080`), send a test query:

```bash
cd /path/to/travel_helper
python -m mcp_travel_helper.test_client --url http://127.0.0.1:8080/mcp --tool travel_deals_cheapest --top 3
python -m mcp_travel_helper.test_client --tool travel_deals_search --query Barcelona
python -m mcp_travel_helper.test_client --tool travel_deals_list --limit 5
```

If you use the project venv: `source .venv-travel/bin/activate` then run the same commands (still from repo root).

---

## Troubleshooting: no results

If tools return empty lists (`[]` or `{"result":[]}`):

1. **Check what the server is loading** — call the `travel_deals_data_status` tool (or run the test client with that tool). It returns `path`, `deals_count`, and any `error`. If `deals_count` is 0, the server is not reading your data file.

2. **Running in Docker** — The image uses empty sample data unless you mount your file:
   ```bash
   docker run -p 8000:8000 -v "$(pwd)/travel_helper.json:/app/travel_helper.json" travel-helper-mcp
   ```
   Run from the repo root so `$(pwd)/travel_helper.json` exists.

3. **Running locally** — Start the server from the **repository root** so it finds `travel_helper.json`, or set the path explicitly:
   ```bash
   TRAVEL_HELPER_JSON=/full/path/to/travel_helper.json python -m mcp_travel_helper --transport streamable-http
   ```

4. **Generate data** — If you don’t have `travel_helper.json` yet, create it with:
   ```bash
   python travel_helper.py --json
   ```
