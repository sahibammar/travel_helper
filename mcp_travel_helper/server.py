"""
MCP server that reads travel_helper.json and exposes tools for querying flight deals.

Data structure (from travel_helper.py):
- "cheapest_flights" or "cheapest_flights_with_hotels": list of destination groups.
- Each group: destination, days, nights, min_total_eur, flights[], destination_info (weather, attractions, etc.).
- Each flight: outbound, return, booking_url (or booking_url_outbound/return), total_eur, optional hotels.

Run:
  STDIO (default):  python -m mcp_travel_helper
  Streamable HTTP:  python -m mcp_travel_helper --transport streamable-http [--host 0.0.0.0] [--port 8000]

Env: TRAVEL_HELPER_JSON, TRAVEL_HELPER_MCP_TRANSPORT, TRAVEL_HELPER_MCP_HOST, TRAVEL_HELPER_MCP_PORT

Requires: pip install "mcp[cli]"
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

# Paths to try when loading data (first existing wins when resolving at runtime)
def _json_path_candidates() -> list[Path]:
    """Return candidate paths for travel_helper.json in order of preference."""
    candidates = []
    env_path = os.environ.get("TRAVEL_HELPER_JSON")
    if env_path:
        candidates.append(Path(env_path).resolve())
    # Repo root data/ relative to this file (server.py is in mcp_travel_helper/)
    repo_data = Path(__file__).resolve().parent.parent / "data" / "travel_helper.json"
    candidates.append(repo_data)
    # CWD-relative (e.g. when started from repo root)
    candidates.append(Path.cwd() / "data" / "travel_helper.json")
    return candidates


def _resolve_json_path() -> str:
    """Return the path to use for loading JSON (first candidate that exists, or first candidate)."""
    for p in _json_path_candidates():
        if p.exists():
            return str(p)
    return str(_json_path_candidates()[0])


mcp = FastMCP(
    "Travel Helper",
    instructions="Query flight deals and destination info from data/travel_helper.json (Weeze/Köln/Dortmund → Europe).",
)

_DOCS_HTML_PATH = Path(__file__).resolve().parent / "docs.html"


@mcp.custom_route("/docs", methods=["GET"])
async def _docs_handler(request: Request) -> Response:
    """Serve documentation at /docs (Trivago-style, when using Streamable HTTP)."""
    if _DOCS_HTML_PATH.exists():
        return HTMLResponse(_DOCS_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Docs</h1><p>docs.html not found.</p>", status_code=404)


def _load_data() -> dict:
    """Load data/travel_helper.json from disk. No cache — every tool call reads the file fresh.
    After you run travel_helper.py --json, the next request sees the new data (no restart needed)."""
    path = _resolve_json_path()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {"_error": str(e), "cheapest_flights": [], "cheapest_flights_with_hotels": []}


def _deals_list(data: dict) -> list[dict]:
    """Return list of deal groups (from cheapest_flights or cheapest_flights_with_hotels)."""
    out = data.get("cheapest_flights_with_hotels") or data.get("cheapest_flights") or []
    if isinstance(out, list):
        return out
    return []


def _file_metadata(path: str) -> dict:
    """Return mtime_iso and size_bytes for the path, or empty dict if not found."""
    try:
        st = os.stat(path)
        mtime_iso = datetime.fromtimestamp(st.st_mtime).isoformat()
        return {"file_mtime_iso": mtime_iso, "file_size_bytes": st.st_size}
    except OSError:
        return {}


@mcp.tool()
def travel_deals_data_status() -> dict:
    """Return which data file the server is using and how many deal groups are loaded.
    No cache: every request reads the file from disk. Use file_mtime_iso to verify you see the latest run.
    If deals_count is 0, set TRAVEL_HELPER_JSON to data/travel_helper.json and restart the app."""
    path = _resolve_json_path()
    data = _load_data()
    deals = _deals_list(data)
    out = {
        "path": path,
        "deals_count": len(deals),
        "error": data.get("_error"),
    }
    out.update(_file_metadata(path))
    return out


@mcp.tool()
def travel_deals_list(limit: int = 50) -> list[dict]:
    """List all destination deal groups from data/travel_helper.json.
    Each group has destination, days, nights, min_total_eur, and multiple flight options.
    Use limit to cap the number of destinations returned (default 50)."""
    data = _load_data()
    if data.get("_error"):
        return [{"error": data["_error"]}]
    deals = _deals_list(data)
    result = []
    for g in deals[:limit]:
        result.append({
            "destination": g.get("destination"),
            "days": g.get("days"),
            "nights": g.get("nights"),
            "min_total_eur": g.get("min_total_eur"),
            "num_flights": len(g.get("flights") or []),
        })
    return result


@mcp.tool()
def travel_deals_search(query: str, limit: int = 20) -> list[dict]:
    """Search deal groups by destination name (case-insensitive substring match).
    Returns matching destinations with their cheapest price and number of flight options.
    query: e.g. 'Manchester', 'Olsztyn', 'Barcelona'."""
    data = _load_data()
    if data.get("_error"):
        return [{"error": data["_error"]}]
    deals = _deals_list(data)
    q = (query or "").strip().lower()
    result = []
    for g in deals:
        dest = (g.get("destination") or "").lower()
        if q in dest:
            result.append({
                "destination": g.get("destination"),
                "days": g.get("days"),
                "nights": g.get("nights"),
                "min_total_eur": g.get("min_total_eur"),
                "num_flights": len(g.get("flights") or []),
            })
            if len(result) >= limit:
                break
    return result


@mcp.tool()
def travel_deals_destination(destination: str, max_flights: int = 10) -> dict:
    """Get full deal details for one destination (exact or first substring match).
    Returns the destination group including flights (outbound/return, booking_url, total_eur)
    and destination_info (weather, attractions) if present.
    max_flights: maximum number of flight options to return (default 10)."""
    data = _load_data()
    if data.get("_error"):
        return {"error": data["_error"]}
    deals = _deals_list(data)
    dest_lower = (destination or "").strip().lower()
    for g in deals:
        d = (g.get("destination") or "").lower()
        if dest_lower in d or d in dest_lower:
            flights = (g.get("flights") or [])[:max_flights]
            return {
                "destination": g.get("destination"),
                "days": g.get("days"),
                "nights": g.get("nights"),
                "min_total_eur": g.get("min_total_eur"),
                "flights": flights,
                "destination_info": g.get("destination_info"),
            }
    return {"error": f"No deal group found for destination: {destination!r}"}


@mcp.tool()
def travel_deals_cheapest(top_n: int = 10) -> list[dict]:
    """Return the top N cheapest deal groups by min_total_eur (ascending).
    Data is already sorted by min_total in data/travel_helper.json; this just slices the first top_n."""
    data = _load_data()
    if data.get("_error"):
        return [{"error": data["_error"]}]
    deals = _deals_list(data)
    result = []
    for g in deals[:top_n]:
        result.append({
            "destination": g.get("destination"),
            "days": g.get("days"),
            "nights": g.get("nights"),
            "min_total_eur": g.get("min_total_eur"),
            "num_flights": len(g.get("flights") or []),
        })
    return result


@mcp.tool()
def travel_deals_flights_for_destination(destination: str, outbound_date_from: str | None = None, outbound_date_to: str | None = None, limit: int = 20) -> list[dict]:
    """List flight options for a destination, optionally filtered by outbound date range.
    outbound_date_from / outbound_date_to: YYYY-MM-DD (inclusive). Dates are taken from outbound.departure.
    Returns list of flights with outbound, return, booking_url, total_eur."""
    data = _load_data()
    if data.get("_error"):
        return [{"error": data["_error"]}]
    deals = _deals_list(data)
    dest_lower = (destination or "").strip().lower()
    for g in deals:
        d = (g.get("destination") or "").lower()
        if dest_lower in d or d in dest_lower:
            flights = g.get("flights") or []
            result = []
            for f in flights:
                ob = f.get("outbound") or {}
                dep = (ob.get("departure") or "")[:10]  # YYYY-MM-DD
                if outbound_date_from and dep < outbound_date_from:
                    continue
                if outbound_date_to and dep > outbound_date_to:
                    continue
                result.append({
                    "outbound": ob,
                    "return": f.get("return"),
                    "booking_url": f.get("booking_url"),
                    "booking_url_outbound": f.get("booking_url_outbound"),
                    "booking_url_return": f.get("booking_url_return"),
                    "total_eur": f.get("total_eur"),
                })
                if len(result) >= limit:
                    break
            return result
    return [{"error": f"No deal group found for destination: {destination!r}"}]


if __name__ == "__main__":
    mcp.run(transport="stdio")
