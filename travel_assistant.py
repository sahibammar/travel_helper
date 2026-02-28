"""Travel Deals AI Assistant — Flask web app.

Usage:
    export GROQ_API_KEY=gsk_...
    python travel_assistant.py
    # open http://localhost:5001  (or set PORT=... to override)

Environment variables:
    GROQ_API_KEY     — Groq API key for LLM
    PORT             — HTTP port (default 5001; 5000 often used by macOS AirPlay)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context
from openai import OpenAI

# ---------------------------------------------------------------------------
# MCP client (shared by local travel-helper MCP and external Trivago MCP)
# ---------------------------------------------------------------------------
_TRIVAGO_DIR = Path(__file__).parent / "trivago"
if str(_TRIVAGO_DIR) not in sys.path:
    sys.path.insert(0, str(_TRIVAGO_DIR))

try:
    from fetch_hotels_mcp import (  # type: ignore[import]
        get_location_suggestion,
        search_accommodations,
        TRIVAGO_MCP_URL,
    )
    from mcp import ClientSession  # type: ignore[import]
    from mcp.client.streamable_http import streamable_http_client as _http_client  # type: ignore[import]
    _MCP_CLIENT_OK = True
except Exception:
    _MCP_CLIENT_OK = False

# ---------------------------------------------------------------------------
# Local mcp_travel_helper server  (Streamable-HTTP on loopback)
# ---------------------------------------------------------------------------
# Port may be updated if default is in use so we start our own server with correct data path
_MCP_PORT = int(os.environ.get("TRAVEL_HELPER_MCP_PORT", "8001"))


def _local_mcp_url() -> str:
    return f"http://127.0.0.1:{_MCP_PORT}/mcp"


def _count_deals_from_json(json_path: Path) -> tuple[str, int, list[str]]:
    """Read data/travel_helper.json directly and return (abs_path, deal_count, destination_names)."""
    abs_path = str(json_path.resolve())
    deals_count = 0
    destination_names: list[str] = []
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    deals = data.get("cheapest_flights_with_hotels") or data.get("cheapest_flights") or []
    if isinstance(deals, list):
        deals_count = len(deals)
        for g in deals:
            if isinstance(g, dict) and g.get("destination"):
                destination_names.append(str(g["destination"]))
    return abs_path, deals_count, destination_names


def _get_destination_chips(json_path: Path) -> list[dict]:
    """Read data/travel_helper.json and return list of {destination, min_total_eur} for chips."""
    out: list[dict] = []
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return out
    deals = data.get("cheapest_flights_with_hotels") or data.get("cheapest_flights") or []
    if not isinstance(deals, list):
        return out
    for g in deals:
        if not isinstance(g, dict) or not g.get("destination"):
            continue
        min_eur = g.get("min_total_eur")
        if min_eur is None:
            continue
        try:
            min_eur = float(min_eur)
        except (TypeError, ValueError):
            continue
        out.append({"destination": str(g["destination"]), "min_total_eur": round(min_eur, 2)})
    return out


def _log_data_status() -> None:
    """At startup: log absolute path of travel_helper.json and total deal count (read directly from file)."""
    app_dir = Path(__file__).resolve().parent
    json_path = app_dir / "data" / "travel_helper.json"
    try:
        abs_path, deals_count, destination_names = _count_deals_from_json(json_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Travel Assistant: could not load data: {e}", file=sys.stderr)
        sys.stderr.flush()
        return
    print(
        f"Travel Assistant: data/travel_helper.json = {abs_path}  deals = {deals_count}",
        file=sys.stderr,
    )
    if destination_names:
        print(f"Travel Assistant: destinations: {', '.join(destination_names)}", file=sys.stderr)
    sys.stderr.flush()


def _start_local_mcp_server() -> None:
    """Launch mcp_travel_helper as Streamable-HTTP in a daemon thread.
    If the default port is in use (e.g. old server with wrong data path), tries the next
    free port so we always run a server that has TRAVEL_HELPER_JSON set to data/travel_helper.json.
    """
    global _MCP_PORT
    _app_dir = Path(__file__).resolve().parent
    _json_path = _app_dir / "data" / "travel_helper.json"
    env = os.environ.copy()
    env["TRAVEL_HELPER_JSON"] = str(_json_path)

    for attempt in range(5):
        port = _MCP_PORT + attempt
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
            # Port in use — try next (likely old server with wrong path)
            continue
        except OSError:
            pass

        def _run(p: int) -> None:
            subprocess.run(
                [
                    sys.executable, "-m", "mcp_travel_helper",
                    "--transport", "streamable-http",
                    "--host", "127.0.0.1",
                    "--port", str(p),
                ],
                cwd=str(_app_dir),
                env=env,
            )

        threading.Thread(target=_run, args=(port,), daemon=True).start()

        for _ in range(60):
            time.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    _MCP_PORT = port
                    if attempt > 0:
                        print(
                            f"Travel Assistant: Started MCP server on port {port} (data: {_json_path})",
                            file=sys.stderr,
                        )
                    return
            except OSError:
                pass
    print(
        "Travel Assistant: Could not start MCP server on ports 8001–8005. "
        f"Ensure data/travel_helper.json exists at {_json_path}",
        file=sys.stderr,
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Start the local mcp_travel_helper server (once, at import time)
_log_data_status()
_start_local_mcp_server()

# Groq (LLM key from GROQ_API_KEY env only)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

SUGGESTED_QUESTIONS = [
    "What's the cheapest deal available?",
    "Which destinations have beach or sun?",
    "Show me weekend trips under €60",
    "What flights go to the UK?",
    "Best deals for a 4-day trip?",
    "Any flights to Spain or Portugal?",
]

# ---------------------------------------------------------------------------
# MCP helpers — all data comes from the local mcp_travel_helper server
# ---------------------------------------------------------------------------

def _parse_mcp_result(result: object) -> object:
    """Extract the Python value from an MCP tool call result.

    FastMCP (used by mcp_travel_helper) returns:
      - structuredContent = {"result": <value>}
      - One TextContent block per list item when the tool returns a list
    External MCPs (e.g. Trivago) may use structuredContent = {"output": <value>}.
    """
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP uses "result"; older/external MCPs may use "output"
        for key in ("result", "output"):
            if key in sc:
                return sc[key]
        return sc

    # Fall back: parse each TextContent block individually, then collect
    items: list = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if not text:
            continue
        try:
            items.append(json.loads(text))
        except json.JSONDecodeError:
            items.append(text)

    if not items:
        return None
    # If every item is a dict/primitive, return as list; if only one item return it directly
    if len(items) == 1:
        return items[0]
    return items


async def _get_all_mcp_data() -> tuple[list[dict], dict[str, dict]]:
    """Single MCP session: fetch summary list + full details for every destination.

    Returns (summary, details) where:
      summary  — list of {destination, days, nights, min_total_eur, num_flights}
      details  — dict keyed by destination name, value is the full deal group
                 (flights with hotels, destination_info, etc.)

    Using ONE session for everything avoids the asyncio event-loop reuse bug
    that occurs when asyncio.run() is called more than once per request.
    """
    if not _MCP_CLIENT_OK:
        return [], {}

    async with _http_client(_local_mcp_url()) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()

            # Step 1: get summary
            res = await session.call_tool("travel_deals_list", {"limit": 50})
            summary = _parse_mcp_result(res)
            if not isinstance(summary, list):
                summary = []

            # Step 2: fetch full details for every destination concurrently
            detail_tasks = [
                session.call_tool(
                    "travel_deals_destination",
                    {"destination": d["destination"], "max_flights": 3},
                )
                for d in summary
                if isinstance(d, dict) and d.get("destination")
            ]
            detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

    details: dict[str, dict] = {}
    for res in detail_results:
        if isinstance(res, Exception):
            continue
        parsed = _parse_mcp_result(res)
        if isinstance(parsed, dict) and "destination" in parsed:
            details[parsed["destination"]] = parsed

    return summary, details


def _destination_info_digest(detail: dict) -> str:
    """Build a short text summary of destination_info so the LLM can answer destination questions."""
    info = (detail or {}).get("destination_info") or {}
    if not info or not isinstance(info, dict):
        return ""
    parts = []
    # City profile: coastal, climate, country
    city = (info.get("city_profile") or {}) if isinstance(info.get("city_profile"), dict) else {}
    city_data = city.get("city") if isinstance(city.get("city"), dict) else {}
    if city_data:
        if city_data.get("is_coastal"):
            parts.append("coastal")
        if city_data.get("climate_description"):
            parts.append(city_data.get("climate_description", ""))
        if city_data.get("country"):
            parts.append(f"in {city_data.get('country')}")
    # Weather (month summary)
    w = (info.get("weather_month") or {}) if isinstance(info.get("weather_month"), dict) else {}
    summary = w.get("weather_summary") if isinstance(w.get("weather_summary"), dict) else {}
    if summary:
        t = summary.get("avg_temperature_mean")
        rain = summary.get("total_precipitation_mm")
        if t is not None:
            parts.append(f"avg temp ~{t}°C")
        if rain is not None:
            parts.append(f"~{rain}mm rain")
    # Best month
    best = (info.get("best_months") or {}) if isinstance(info.get("best_months"), dict) else {}
    if best.get("best_month"):
        parts.append(f"best month: {best.get('best_month')}")
    # Top activities (travel_intelligence)
    ti = (info.get("travel_intelligence") or {}) if isinstance(info.get("travel_intelligence"), dict) else {}
    top_act = ti.get("top_activities")
    if isinstance(top_act, list) and top_act:
        acts = [a.get("activity") for a in top_act[:5] if isinstance(a, dict) and a.get("activity")]
        if acts:
            parts.append("activities: " + ", ".join(acts))
    # Attraction names
    attr = (info.get("attractions") or {}) if isinstance(info.get("attractions"), dict) else {}
    attr_list = attr.get("attractions") if isinstance(attr.get("attractions"), list) else []
    if attr_list:
        names = [a.get("name") for a in attr_list[:5] if isinstance(a, dict) and a.get("name")]
        if names:
            parts.append("attractions: " + ", ".join(names))
    # Features (e.g. beach_holiday, swimming) from city_profile
    feats = city.get("features") if isinstance(city.get("features"), list) else []
    if feats:
        feature_names = [f.get("feature") for f in feats[:8] if isinstance(f, dict) and f.get("feature")]
        if feature_names:
            parts.append("features: " + ", ".join(feature_names))
    if not parts:
        return ""
    return " | ".join(parts)


def _build_llm_context(summary: list[dict], all_details: dict[str, dict] | None = None) -> str:
    """Build context from the MCP deals summary and destination info so the LLM can answer destination questions."""
    if not summary:
        return "No flight deals available at the moment."
    all_details = all_details or {}
    lines = [
        "Available flight deals departing from Weeze (NRN), Cologne (CGN), and Dortmund (DTM).",
        "Prices are EUR per person, return trip.",
        "Use the destination info below to answer questions about weather, beaches, activities, attractions.",
        "",
    ]
    for d in summary:
        if not isinstance(d, dict):
            continue
        dest_name = d.get("destination")
        line = (
            f"- {dest_name} | {d.get('days')} days / {d.get('nights')} nights"
            f" | cheapest from €{d.get('min_total_eur')} | {d.get('num_flights')} flight options"
        )
        detail = all_details.get(dest_name) if dest_name else None
        digest = _destination_info_digest(detail) if detail else ""
        if digest:
            line += f"\n  Destination info: {digest}"
        lines.append(line)
    return "\n".join(lines)


def _find_relevant_dests(answer: str, all_destinations: list[str]) -> list[str]:
    """Return destination names from the MCP list that appear in the LLM answer."""
    answer_lower = answer.lower()
    found: list[str] = []
    seen: set[str] = set()
    for dest in all_destinations:
        if dest in seen:
            continue
        words = re.split(r"[\s\-–,/]+", dest.lower())
        if any(len(w) > 3 and w in answer_lower for w in words):
            found.append(dest)
            seen.add(dest)
    return found[:6]



_TRIVAGO_CSS = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { font-size: 62.5%; height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #f2f2f1; color: #171717; font-size: 1.6rem; line-height: 1.5; height: 100%;
    }

    /* ── App shell ── */
    .app { max-width: 1280px; margin: 0 auto; padding: 1.6rem; display: flex; flex-direction: column; height: 100vh; }

    /* ── Page header ── */
    .page-header {
      background: #fff; border-radius: 1.2rem; border: 1px solid #d9d8d6;
      box-shadow: 0 2px 10px rgba(0,0,0,0.09); flex-shrink: 0; margin-bottom: 1.6rem;
    }
    .header-bar { display: flex; align-items: center; padding: 1.2rem 2rem 0.6rem; gap: 1.2rem; }
    .page-header .chips { padding: 0 2rem 1.2rem; }
    .logo { font-size: 2rem; font-weight: 700; color: #0079c2; letter-spacing: -0.02em; flex-shrink: 0; }
    .logo-sub { font-size: 1.2rem; font-weight: 400; color: #8d8d8b; margin-left: 0.6rem; }
    .settings-row { display: flex; align-items: center; gap: 0.8rem; margin-left: auto; flex-wrap: wrap; }
    .provider-select {
      height: 3.2rem; background: #f2f2f1; border: 1px solid #d9d8d6; border-radius: 0.6rem;
      padding: 0 0.8rem; font-size: 1.2rem; color: #171717; outline: none; cursor: pointer;
    }
    .provider-select:focus { border-color: #0079c2; }
    .key-input {
      width: 18rem; height: 3.2rem; background: #f2f2f1; border: 1px solid #d9d8d6;
      border-radius: 0.6rem; padding: 0 1rem; font-size: 1.2rem; color: #171717;
      font-family: monospace; outline: none; transition: border-color 0.15s;
    }
    .key-input:focus { border-color: #0079c2; }
    .key-input::placeholder { color: #bbbbb9; font-family: inherit; }
    .key-link { font-size: 1.1rem; color: #0079c2; text-decoration: none; white-space: nowrap; }
    .key-link:hover { text-decoration: underline; }
    .key-saved { font-size: 1.1rem; color: #47a7ef; white-space: nowrap; opacity: 0; transition: opacity 0.3s; }
    .key-saved.show { opacity: 1; }

    /* ── Two-pane layout ── */
    .panes { display: flex; gap: 1.6rem; flex: 1; min-height: 0; }

    /* ── LEFT: Chat pane ── */
    .chat-pane {
      width: 32rem; flex-shrink: 0; display: flex; flex-direction: column;
      background: #fff; border: 1px solid #d9d8d6; border-radius: 1.2rem;
      overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .chat-pane-header {
      padding: 1.2rem 1.6rem; border-bottom: 1px solid #f2f2f1;
      font-size: 1.1rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #8d8d8b; flex-shrink: 0;
    }
    .chat-history { flex: 1; overflow-y: auto; padding: 1.2rem; display: flex; flex-direction: column; gap: 0.8rem; }
    .chat-entry {
      padding: 1rem 1.2rem; border-radius: 0.8rem; border: 1px solid #f2f2f1;
      font-size: 1.3rem; color: #4d4d4c; cursor: pointer;
      transition: background 0.12s, border-color 0.12s;
    }
    .chat-entry:hover { background: #f5fbff; border-color: #bde7ff; }
    .chat-entry.active { background: #e5f5ff; border-color: #0079c2; color: #171717; font-weight: 600; }
    .chat-entry.loading { color: #8d8d8b; font-style: italic; }
    .chat-entry-time { font-size: 1rem; color: #bbbbb9; margin-top: 0.3rem; }
    .chat-empty { text-align: center; padding: 3.2rem 1.6rem; color: #bbbbb9; font-size: 1.3rem; }
    .chat-empty .icon { font-size: 2.8rem; margin-bottom: 0.8rem; }

    /* ── Chat input area ── */
    .chat-input-area { border-top: 1px solid #f2f2f1; padding: 1.2rem; flex-shrink: 0; }
    .ask-row { display: flex; gap: 0.8rem; margin-bottom: 0.8rem; }
    .ask-input {
      flex: 1; height: 4rem; background: #f2f2f1; border: 1px solid #d9d8d6;
      border-radius: 0.8rem; padding: 0 1.2rem; font-size: 1.3rem; color: #171717;
      outline: none; transition: border-color 0.15s;
    }
    .ask-input:focus { border-color: #0079c2; }
    .ask-input::placeholder { color: #8d8d8b; }
    .btn-ask {
      height: 4rem; padding: 0 1.6rem; background: #0079c2; color: #fff;
      font-size: 1.3rem; font-weight: 700; border: none; border-radius: 0.8rem;
      cursor: pointer; white-space: nowrap; transition: background 0.15s; flex-shrink: 0;
    }
    .btn-ask:hover { background: #00578b; }
    .btn-ask:disabled { background: #bbbbb9; cursor: not-allowed; }
    .chips { display: flex; flex-wrap: wrap; gap: 0.6rem; }
    .chip {
      font-size: 1.1rem; color: #0079c2; background: #e5f5ff; border: 1px solid #bde7ff;
      border-radius: 10rem; padding: 0.3rem 1rem; cursor: pointer; transition: background 0.15s; white-space: nowrap;
    }
    .chip:hover { background: #bde7ff; }

    /* ── RIGHT: Results pane ── */
    .results-pane { flex: 1; overflow-y: auto; min-width: 0; }
    .results-inner { display: flex; flex-direction: column; gap: 1.6rem; padding-bottom: 2rem; }

    /* ── Answer card ── */
    .answer-card {
      background: #fff; border: 1px solid #d9d8d6; border-radius: 1.2rem;
      overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .answer-header {
      display: flex; align-items: center; gap: 1rem; padding: 1.2rem 2rem;
      background: #0079c2; color: #fff; font-size: 1.4rem; font-weight: 700;
    }
    .answer-body { padding: 1.6rem 2rem; font-size: 1.4rem; color: #4d4d4c; line-height: 1.7; white-space: pre-wrap; }

    /* ── Spinners ── */
    .spinner {
      display: inline-block; width: 1.6rem; height: 1.6rem;
      border: 2px solid rgba(255,255,255,0.4); border-top-color: #fff;
      border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle;
    }
    .spinner-dark {
      display: inline-block; width: 1.2rem; height: 1.2rem;
      border: 2px solid #d9d8d6; border-top-color: #0079c2;
      border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ── Section label ── */
    .section-label {
      font-size: 1rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #8d8d8b; margin-bottom: 1.2rem;
    }

    /* ── Deal / Flight cards ── */
    .deal-card {
      background: #fff; border: 1px solid #d9d8d6; border-radius: 1.2rem;
      margin-bottom: 1.6rem; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .dest-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 1.2rem 2rem; background: #0079c2; color: #fff;
    }
    .dest-city { font-size: 1.8rem; font-weight: 700; line-height: 1; }
    .dest-meta { font-size: 1.2rem; opacity: 0.75; margin-top: 0.2rem; }
    .dest-badge {
      font-size: 1.3rem; font-weight: 700; background: rgba(255,255,255,0.2);
      border-radius: 0.8rem; padding: 0.4rem 1rem; white-space: nowrap;
    }
    .flight-option { border-top: 1px solid #f2f2f1; display: flex; align-items: stretch; }
    .flight-option.cheapest { background: #f5fbff; }
    .flight-legs { flex: 1; padding: 1.6rem 2rem; }
    .leg-label {
      font-size: 1rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.06em; color: #8d8d8b; margin-bottom: 0.4rem;
    }
    .leg-row { display: grid; grid-template-columns: 8rem 1fr 8rem; align-items: center; gap: 0.8rem; }
    .airport-code { font-size: 2rem; font-weight: 700; line-height: 1; }
    .airport-time { font-size: 1.2rem; color: #6c6c6b; margin-top: 0.2rem; }
    .airport-city { font-size: 1rem; color: #8d8d8b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .leg-center { text-align: center; }
    .leg-dur { font-size: 1rem; color: #8d8d8b; margin-bottom: 0.3rem; }
    .leg-track { color: #24a3ec; font-size: 1.3rem; line-height: 1; }
    .leg-fn { font-size: 1rem; color: #bbbbb9; margin-top: 0.3rem; }
    .legs-divider { height: 1px; background: #f2f2f1; margin: 0.8rem 0; }
    .leg-right { text-align: right; }
    .flight-cta {
      width: 15rem; flex-shrink: 0; border-left: 1px solid #f2f2f1; padding: 1.6rem;
      display: flex; flex-direction: column; align-items: flex-end; justify-content: center; gap: 1.2rem;
    }
    .price-total { font-size: 2.4rem; font-weight: 700; line-height: 1; }
    .price-label { font-size: 1rem; color: #8d8d8b; margin-top: 0.2rem; }
    .btn-book {
      display: inline-block; background: #0079c2; color: #fff;
      font-size: 1.3rem; font-weight: 700; padding: 0.8rem 1.4rem;
      border-radius: 0.8rem; text-decoration: none; white-space: nowrap; transition: background 0.15s;
    }
    .btn-book:hover { background: #00578b; }
    .hotel-section { border-top: 1px solid #f2f2f1; padding: 1.2rem 2rem 1.6rem; background: #fafafa; }
    .hotel-label {
      font-size: 1rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #8d8d8b; margin-bottom: 0.8rem;
    }
    .hotel-card { display: flex; background: #fff; border-radius: 1rem; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }
    .hotel-info { flex: 1; padding: 1.2rem 1.6rem; }
    .hotel-name { font-size: 1.5rem; font-weight: 700; color: #171717; text-decoration: none; display: block; }
    .hotel-name:hover { color: #0079c2; text-decoration: underline; }
    .hotel-city { font-size: 1.3rem; color: #6c6c6b; margin-top: 0.4rem; }
    .hotel-rating-row { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.8rem; }
    .review-pill { font-size: 1.1rem; font-weight: 700; color: #fff; padding: 0.2rem 0.7rem; border-radius: 0.8rem; display: inline-block; }
    .review-label { font-size: 1.3rem; color: #6c6c6b; }
    .hotel-price-box {
      width: 16rem; flex-shrink: 0; background: #e5f5ff; border-radius: 0 1rem 1rem 0;
      padding: 1.2rem; display: flex; flex-direction: column; justify-content: space-between;
    }
    .hotel-price-value { font-size: 1.8rem; font-weight: 700; }
    .hotel-price-stay { font-size: 1.2rem; color: #6c6c6b; margin-top: 0.2rem; }
    .btn-view {
      display: inline-block; background: #0079c2; color: #fff;
      font-size: 1.3rem; font-weight: 700; padding: 0.7rem 1.4rem; border-radius: 0.8rem;
      text-decoration: none; white-space: nowrap; margin-top: 1rem; transition: background 0.15s;
    }
    .btn-view:hover { background: #00578b; }

    /* ── Deals divider bar (inside unified card) ── */
    .deals-divider-bar {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.8rem 2rem;
      background: #f0f8ff; border-top: 1px solid #d4eeff;
      font-size: 1rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #0079c2;
    }
    /* Section dest-header within a merged card gets a top border */
    .dest-header--section { border-top: 1px solid rgba(255,255,255,0.15); }

    /* ── Right-pane empty state ── */
    .empty-state {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      height: 100%; min-height: 40rem; text-align: center; color: #8d8d8b; font-size: 1.4rem;
    }
    .empty-state .icon { font-size: 4rem; margin-bottom: 1.2rem; }
    .error-banner {
      background: #fff5f5; border: 1px solid #fca5a5; color: #dc2626;
      border-radius: 0.8rem; padding: 1.2rem 1.6rem; font-size: 1.4rem;
    }

    /* ── Responsive ── */
    @media (max-width: 800px) {
      .app { height: auto; }
      .panes { flex-direction: column; height: auto; }
      .chat-pane { width: 100%; max-height: 36rem; }
      .results-pane { min-height: 50vh; }
      .flight-option { flex-direction: column; }
      .flight-cta { width: 100%; border-left: none; border-top: 1px solid #f2f2f1; flex-direction: row; justify-content: space-between; align-items: center; }
      .hotel-card { flex-direction: column; }
      .hotel-price-box { width: 100%; border-radius: 0 0 1rem 1rem; }
    }
"""

HTML_TEMPLATE = (
    r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Travel Deals AI — Weekend Travel Helper</title>
  <style>"""
    + _TRIVAGO_CSS
    + r"""
  </style>
</head>
<body>
<div class="app">

  <!-- ── Page header ── -->
  <div class="page-header">
    <div class="header-bar">
      <div class="logo">Weekend Travel Helper<span class="logo-sub">AI deals assistant</span></div>
    </div>
    <div class="chips" id="chips-bar"></div>
  </div>

  <!-- ── Two-pane layout ── -->
  <div class="panes">

    <!-- LEFT: Chat pane -->
    <div class="chat-pane">
      <div class="chat-pane-header">Conversation</div>
      <div class="chat-history" id="chat-history">
        <div class="chat-empty" id="chat-empty">
          <div class="icon">💬</div>
          <div>Your questions will appear here</div>
        </div>
      </div>
      <div class="chat-input-area">
        <div class="ask-row">
          <input id="ask-input" class="ask-input" type="text"
            placeholder="Ask about deals…" autocomplete="off"/>
          <button class="btn-ask" id="ask-btn" onclick="submitQuestion()">Ask</button>
        </div>
        <div class="chips" id="suggested-chips-bar"></div>
      </div>
    </div>

    <!-- RIGHT: Results pane -->
    <div class="results-pane" id="results-pane">
      <div class="empty-state" id="empty-state">
        <div class="icon">✈️</div>
        <div>Ask a question to see deals here.</div>
        <div style="margin-top:0.8rem;font-size:1.2rem;color:#bbbbb9;">
          Powered by Llama&nbsp;4&nbsp;Scout &bull; answers based strictly on data/travel_helper.json
        </div>
        <div style="margin-top:0.4rem;font-size:1rem;color:#999;">
          Each new question uses the latest data — no refresh needed.
        </div>
      </div>
    </div>

  </div><!-- /panes -->

</div><!-- /app -->

<script>
const DESTINATION_CHIPS = {{ destination_chips | tojson }};
const SUGGESTED = {{ suggested_questions | tojson }};

/* ── Chips: destination chips in header, suggested questions under input ── */
(function renderChips() {
  const input = document.getElementById('ask-input');
  const destBar = document.getElementById('chips-bar');
  DESTINATION_CHIPS.forEach(function(d) {
    const label = d.destination + ' from €' + (Number(d.min_total_eur) === Math.floor(d.min_total_eur) ? Math.floor(d.min_total_eur) : d.min_total_eur);
    const c = document.createElement('span');
    c.className = 'chip'; c.textContent = label;
    c.onclick = function() { input.value = 'Tell me about ' + d.destination; submitQuestion(); };
    destBar.appendChild(c);
  });
  const suggestedBar = document.getElementById('suggested-chips-bar');
  SUGGESTED.forEach(function(q) {
    const c = document.createElement('span');
    c.className = 'chip'; c.textContent = q;
    c.onclick = function() { input.value = q; submitQuestion(); };
    suggestedBar.appendChild(c);
  });
})();
document.getElementById('ask-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') submitQuestion();
});

/* ── In-flight request: abort when user submits a new question so the next request is sent and processed ── */
let currentAskAbortController = null;
let currentAskReader = null;

/* ── Chat history state ── */
let chatHistory = [];
let activeId    = null;

function nowLabel() {
  return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

function selectEntry(id) {
  activeId = id;
  document.querySelectorAll('.chat-entry').forEach(el =>
    el.classList.toggle('active', el.dataset.id === String(id))
  );
  const entry = chatHistory.find(e => e.id === id);
  if (!entry || entry.loading) return;
  const pane = document.getElementById('results-pane');
  if (entry.error) {
    pane.innerHTML = `<div class="results-inner"><div class="deal-card">
      <div class="dest-header">
        <div><div class="dest-city" style="font-size:1.5rem">Error</div></div>
      </div>
      <div class="answer-body"><div class="error-banner">${escHtml(entry.error)}</div></div>
    </div></div>`;
    return;
  }
  // Build deal sections (inner content only, no wrapping .deal-card)
  const dealSections = (entry.deals && entry.deals.length)
    ? entry.deals.map(renderDealSection).join('')
    : '';
  const dealsDivider = dealSections
    ? `<div class="deals-divider-bar">
         <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="opacity:0.6"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>
         Relevant flights
       </div>`
    : '';
  // Single unified card: AI answer header + answer text + deal sections
  pane.innerHTML = `<div class="results-inner"><div class="deal-card">
    <div class="dest-header">
      <div>
        <div class="dest-city" style="font-size:1.5rem;display:flex;align-items:center;gap:0.8rem">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="opacity:0.85;flex-shrink:0">
            <path d="M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
          </svg>AI Answer
        </div>
        <div class="dest-meta">${escHtml(entry.question)}</div>
      </div>
    </div>
    <div class="answer-body">${escHtml(entry.answer)}</div>
    ${dealsDivider}${dealSections}
  </div></div>`;
}

function addChatEntry(id, question) {
  const chatEmpty = document.getElementById('chat-empty');
  if (chatEmpty) chatEmpty.style.display = 'none';
  const hist = document.getElementById('chat-history');
  const el   = document.createElement('div');
  el.className  = 'chat-entry loading active';
  el.dataset.id = id;
  el.innerHTML  = `<span class="spinner-dark"></span>&nbsp;${escHtml(question)}<div class="chat-entry-time">${nowLabel()}</div>`;
  el.onclick = () => selectEntry(id);
  if (hist) { hist.appendChild(el); el.scrollIntoView({ behavior: 'smooth', block: 'end' }); }
}

function updateChatEntry(id) {
  const entry = chatHistory.find(e => e.id === id);
  if (!entry) return;
  const el = document.querySelector(`.chat-entry[data-id="${id}"]`);
  if (!el) return;
  el.classList.remove('loading');
  el.classList.toggle('active', id === activeId);
  el.innerHTML = `${entry.error ? '⚠️ ' : ''}${escHtml(entry.question)}<div class="chat-entry-time">${nowLabel()}</div>`;
}

/* ── Render helpers ── */
function pillColor(s) {
  s = parseFloat(s);
  return s>=9?'#0079c2':s>=8?'#24a3ec':s>=7?'#47a7ef':s>=6?'#ff9128':'#8d8d8b';
}
function ratingLabel(s) {
  s = parseFloat(s);
  return s>=9?'Exceptional':s>=8.5?'Superb':s>=8?'Very good':s>=7?'Good':s>=6?'Pleasant':'Reviewed';
}
function renderLeg(ob, isReturn) {
  const t = isReturn ? '&#x2190;&nbsp;&#x2708;' : '&#x2708;&nbsp;&#x2192;';
  return `<div class="leg-row">
    <div><div class="airport-code">${ob.origin||''}</div><div class="airport-time">${ob.departure_time||''}</div><div class="airport-city">${ob.origin_full||''}</div></div>
    <div class="leg-center">${ob.duration?`<div class="leg-dur">${ob.duration}</div>`:''}<div class="leg-track">${t}</div><div class="leg-fn">${ob.flight_number||''} &bull; &euro;${ob.price_eur||''}</div></div>
    <div class="leg-right"><div class="airport-code">${ob.destination||''}</div><div class="airport-city" style="text-align:right">${ob.destination_full||''}</div></div>
  </div>`;
}
function fmtDate(iso) {
  try { return new Date(iso).toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short',year:'numeric'}); }
  catch(e) { return (iso||'').slice(0,10); }
}
/* ── renderDealSection: inner content only (no .deal-card wrapper) ── */
function renderDealSection(g) {
  const flights  = g.flights || [];
  const cheapest = Math.min(...flights.map(f => f.total_eur ?? Infinity));
  let opts = '';
  flights.slice(0,3).forEach(f => {
    const ob = f.outbound||{}, ret = f['return']||{};
    const total = f.total_eur ?? '?';
    const url   = f.booking_url || f.booking_url_outbound || '';
    opts += `<div class="flight-option${total===cheapest?' cheapest':''}">
      <div class="flight-legs">
        <div class="leg-label">Outbound &bull; ${fmtDate(ob.departure)}</div>${renderLeg(ob,false)}
        <div class="legs-divider"></div>
        <div class="leg-label" style="margin-top:0.4rem">Return &bull; ${fmtDate(ret.departure)}</div>${renderLeg(ret,true)}
      </div>
      <div class="flight-cta">
        <div><div class="price-total">&euro;${total}</div><div class="price-label">per person, return</div></div>
        ${url?`<a class="btn-book" href="${url}" target="_blank" rel="noreferrer">Book on Ryanair</a>`:''}
      </div>
    </div>`;
  });
  // Hotel data comes from mcp_travel_helper (travel_deals_destination) — already in f.hotels
  const hotelSrc = (flights.find(f => Array.isArray(f.hotels) && f.hotels.length) || {}).hotels?.[0] || null;
  const htlHtml  = hotelSrc
    ? `<div class="hotel-section">${renderLiveHotel(hotelSrc, g.destination)}</div>`
    : '';
  return `<div class="dest-header dest-header--section">
    <div><div class="dest-city">${g.destination}</div><div class="dest-meta">${g.days} days &bull; ${g.nights} nights</div></div>
    <div class="dest-badge">from &euro;${g.min_total_eur??g.min_total}</div>
  </div>${opts}${htlHtml}`;
}

/* ── renderDealCard: standalone card (used standalone if ever needed) ── */
function renderDealCard(g) {
  return `<div class="deal-card">${renderDealSection(g)}</div>`;
}

/* ── Live hotel rendering (Trivago MCP data) ── */
function renderLiveHotel(hotel, dest) {
  const name    = hotel['Accommodation Name'] || hotel.accommodation_name || '';
  const url     = hotel['Accommodation URL']  || hotel.accommodation_url  || '';
  const img     = hotel['Main Image']         || hotel.main_image         || '';
  const price   = hotel['Price Per Stay']     || hotel.price_per_stay     || '';
  const rating  = hotel['Review Rating']      || hotel.review_rating      || '';
  const amenities = hotel['Top Amenities']    || hotel.top_amenities      || '';
  if (!name || !url) return '';
  const r    = parseFloat(rating) || 0;
  const pill = rating
    ? `<div class="hotel-rating-row"><span class="review-pill" style="background:${pillColor(r)}">${rating}</span><span class="review-label">${ratingLabel(r)}</span></div>` : '';
  const imgEl = img
    ? `<div style="width:10rem;flex-shrink:0;overflow:hidden"><img src="${img}" style="width:100%;height:100%;min-height:9rem;object-fit:cover;display:block" alt="" loading="lazy"></div>`
    : '';
  return `<div class="hotel-label" style="display:flex;align-items:center;gap:0.5rem">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="#0079c2"><path d="M7 13h10v2H7zm0 4h7v2H7zm0-8h10v2H7z"/><path d="M19 2H5a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm0 18H5V4h14v16z"/></svg>
      Live hotel from trivago
    </div>
    <div class="hotel-card">
      ${imgEl}
      <div class="hotel-info">
        <a class="hotel-name" href="${url}" target="_blank" rel="noreferrer">${escHtml(name)}</a>
        <div class="hotel-city">&#128205; ${escHtml(dest)}</div>
        ${pill}
        ${amenities ? `<div style="font-size:1.1rem;color:#8d8d8b;margin-top:0.5rem">${escHtml(amenities)}</div>` : ''}
      </div>
      <div class="hotel-price-box">
        ${price ? `<div><div class="hotel-price-value">${escHtml(price)}</div><div class="hotel-price-stay">total stay</div></div>` : '<div></div>'}
        <a class="btn-view" href="${url}" target="_blank" rel="noreferrer">View deal &rarr;</a>
      </div>
    </div>`;
}

/* ── Stream helpers ── */
function showStreamCard(question) {
  document.getElementById('results-pane').innerHTML = `<div class="results-inner"><div class="deal-card" id="stream-card">
    <div class="dest-header">
      <div>
        <div class="dest-city" style="font-size:1.5rem;display:flex;align-items:center;gap:0.8rem">
          <span class="spinner"></span>Thinking…
        </div>
        <div class="dest-meta">${escHtml(question)}</div>
      </div>
    </div>
    <div class="answer-body" id="stream-body" style="min-height:2rem;white-space:pre-wrap;color:#8d8d8b;font-style:italic">Fetching AI answer…</div>
    <div id="stream-deals"></div>
  </div></div>`;
}
function updateStreamBody(text) {
  const el = document.getElementById('stream-body');
  if (el) { el.style.color = ''; el.style.fontStyle = ''; el.textContent = text; }
}

/* ── Submit question ── */
async function submitQuestion() {
  const input    = document.getElementById('ask-input');
  const btn      = document.getElementById('ask-btn');
  const question = input.value.trim();
  if (!question) return;

  // Cancel any in-flight request so this new question is sent and processed (fixes "next question not processed")
  if (currentAskAbortController) currentAskAbortController.abort();
  if (currentAskReader) try { currentAskReader.cancel(); } catch(e) {}
  const ourAbort = new AbortController();
  currentAskAbortController = ourAbort;
  currentAskReader = null;

  const id = Date.now();
  chatHistory.push({ id, question, loading: true });
  activeId = id;
  addChatEntry(id, question);
  const emptyState = document.getElementById('empty-state');
  if (emptyState) emptyState.style.display = 'none';
  showStreamCard(question);

  input.value = '';
  btn.innerHTML = '<span class="spinner"></span>';

  const ASK_TIMEOUT_MS = 60000;  // stop spinner and abort after 60 s if stream never completes
  const timeoutId = setTimeout(() => {
    if (currentAskAbortController === ourAbort) {
      ourAbort.abort();
      btn.innerHTML = 'Ask';
    }
  }, ASK_TIMEOUT_MS);

  try {
    const res = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ question }),
      signal: ourAbort.signal,
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `HTTP ${res.status}`);
    }

    const reader  = res.body.getReader();
    currentAskReader = reader;
    const decoder = new TextDecoder();
    let buf = '';
    let answerText = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let msg;
        try { msg = JSON.parse(line.slice(6)); } catch { continue; }

        if (msg.error) throw new Error(msg.error);

        if (msg.delta) {
          answerText += msg.delta;
          updateStreamBody(answerText);
          const entry = chatHistory.find(e => e.id === id);
          if (entry) entry.answer = answerText;
        }

        if (msg.done) {
          const entry = chatHistory.find(e => e.id === id);
          if (entry) {
            entry.loading = false;
            entry.deals = msg.deals || [];
          }
          updateChatEntry(id);
          if (activeId === id) selectEntry(id);
          currentAskReader = null;
          clearTimeout(timeoutId);
          btn.disabled = false;
          btn.innerHTML = 'Ask';
          reader.cancel().catch(() => {});
          return;
        }
      }
    }
  } catch(err) {
    if (err.name === 'AbortError') return;  // user submitted a new question; that request will handle UI
    const entry = chatHistory.find(e => e.id === id);
    if (entry) { entry.loading = false; entry.error = String(err); }
    updateChatEntry(id);
    if (activeId === id) selectEntry(id);
  } finally {
    clearTimeout(timeoutId);
    if (currentAskReader === reader) currentAskReader = null;
    if (currentAskAbortController === ourAbort) currentAskAbortController = null;
    btn.disabled = false; btn.innerHTML = 'Ask';
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>"""
)

# ---------------------------------------------------------------------------
# Async helper — always run async code in a brand-new thread so that
# anyio / asyncio thread-local state from a previous request never leaks
# into the next one.  Flask reuses threads (thread pool), so calling
# asyncio.run() directly in the request thread is unsafe after the first
# request when anyio leaves behind stale backend state.
# ---------------------------------------------------------------------------

def _run_mcp(coro):
    """Execute *coro* in a fresh OS thread, returning the result synchronously."""
    result: list = [None, None]   # [value, exception]

    def _target():
        try:
            result[0] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            result[1] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=45)          # hard cap — MCP + LLM context fetch should be <5 s
    if t.is_alive():
        raise TimeoutError("MCP data fetch timed out after 45 s")
    if result[1] is not None:
        raise result[1]
    return result[0]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    app_dir = Path(__file__).resolve().parent
    json_path = app_dir / "data" / "travel_helper.json"
    destination_chips = _get_destination_chips(json_path)
    html = render_template_string(
        HTML_TEMPLATE,
        destination_chips=destination_chips,
        suggested_questions=SUGGESTED_QUESTIONS,
    )
    # Cache-bust: unique comment per request so browser never uses cached HTML/JS
    html = html.replace("</head>", "<!-- v=%s -->\n  </head>" % time.time())
    resp = app.make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/ask", methods=["POST"])
def ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    base_url, model = GROQ_BASE_URL, GROQ_MODEL
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return jsonify(
            {"error": "GROQ_API_KEY is not set on the server."}
        ), 400

    tid = threading.get_ident()
    app.logger.info("[ask] Q=%r  thread=%d", question, tid)

    # ── Fetch all MCP data in a fresh thread (no asyncio state leakage) ──
    t0 = time.time()
    try:
        summary, all_details = _run_mcp(_get_all_mcp_data())
        app.logger.info("[ask] MCP done in %.2fs: %d dests",
                        time.time() - t0, len(summary))
    except Exception as exc:
        app.logger.warning("[ask] MCP FAILED after %.2fs: %s",
                           time.time() - t0, exc)
        summary, all_details = [], {}

    context   = _build_llm_context(summary, all_details)
    all_dests = list(all_details.keys())
    use_mock  = (api_key == "MOCK")
    client    = None if use_mock else OpenAI(base_url=base_url, api_key=api_key)

    @stream_with_context
    def generate():
        full_answer = ""
        app.logger.info("[gen] LLM stream START  mock=%s  thread=%d",
                        use_mock, threading.get_ident())
        try:
            if use_mock:
                # Fake streamed answer for automated testing — no real LLM call
                mock_text = f"MOCK answer for: {question}. Top destination: {all_dests[0] if all_dests else 'N/A'}."
                for word in mock_text.split():
                    full_answer += word + " "
                    yield f"data: {json.dumps({'delta': word + ' '})}\n\n"
            else:
                stream = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a travel deals assistant. "
                                "Answer questions strictly based on the flight deals listed below. "
                                "Do not invent destinations, prices, or dates not in the data. "
                                "If something is not covered, say so clearly. "
                                "Be concise and helpful.\n\n"
                                + context
                            ),
                        },
                        {"role": "user", "content": question},
                    ],
                    max_tokens=800,
                    temperature=0.2,
                    stream=True,
                )
                for chunk in stream:
                    delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                    if delta:
                        full_answer += delta
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as exc:
            app.logger.error("[gen] LLM error: %s  thread=%d", exc, threading.get_ident())
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        app.logger.info("[gen] LLM stream END  len=%d  thread=%d",
                        len(full_answer), threading.get_ident())

        # ── Step 2: look up pre-fetched deal details (no async, instant) ──
        mentioned = _find_relevant_dests(full_answer, all_dests)
        deals = [all_details[dest] for dest in mentioned if dest in all_details]
        app.logger.info("[gen] DONE sent  deals=%d  thread=%d",
                        len(deals), threading.get_ident())
        yield f"data: {json.dumps({'done': True, 'deals': deals})}\n\n"

    return Response(generate(), content_type="text/event-stream",
                    headers={
                        "X-Accel-Buffering": "no",
                        "Cache-Control": "no-cache",
                        "Connection": "close",   # close TCP after stream ends → browser reader.done fires immediately
                    })


# ---------------------------------------------------------------------------
# Live hotel prices via Trivago MCP
# ---------------------------------------------------------------------------


@app.route("/api/hotels")
def hotels_api():
    dest      = request.args.get("destination", "").strip()
    arrival   = request.args.get("arrival",     "").strip()
    departure = request.args.get("departure",   "").strip()
    if not (dest and arrival and departure):
        return jsonify({"error": "destination, arrival and departure required"}), 400
    if not _MCP_CLIENT_OK:
        return jsonify({"hotels": [], "error": "trivago MCP not available"})

    async def _fetch() -> list:
        async with _http_client(TRIVAGO_MCP_URL) as streams:  # type: ignore[attr-defined]
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                loc = await get_location_suggestion(session, dest)
                if not loc:
                    return []
                return await search_accommodations(
                    session, loc[0], loc[1], arrival, departure
                )

    try:
        hotels = asyncio.run(_fetch())
        return jsonify({"hotels": hotels[:3]})
    except Exception as exc:
        app.logger.warning("Hotel MCP error for %r: %s", dest, exc)
        return jsonify({"hotels": [], "error": str(exc)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Starting Travel Deals Assistant on http://localhost:{port}")
    print("Set GROQ_API_KEY (or paste your key in the app) before asking questions.")
    app.run(debug=True, port=port, use_reloader=False, threaded=True)
