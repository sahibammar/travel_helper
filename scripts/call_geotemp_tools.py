#!/usr/bin/env python3
"""
Call all GeoTemp API tools at https://mcp-travel-data.onrender.com/api and summarize output.

Usage:
  export GEOTEMP_API_KEY='gt_live_...'   # REST API uses gt_live_ key (not gsk_)
  python scripts/call_geotemp_tools.py

See scripts/GEOTEMP_API_OUTPUT.md for full output shapes of each tool.
"""
import json
import os
import sys

try:
    import requests
except ImportError:
    print("Install requests: pip install requests", file=sys.stderr)
    sys.exit(1)

BASE = "https://mcp-travel-data.onrender.com/api"
API_KEY = os.environ.get("GEOTEMP_API_KEY", "").strip()
if not API_KEY:
    print("Set GEOTEMP_API_KEY environment variable.", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}
TIMEOUT = 30


def call(tool: str, params: dict | None = None) -> dict:
    """POST to /tools/{tool} and return JSON. On error return {'error': str}."""
    params = params or {}
    url = f"{BASE}/tools/{tool}"
    try:
        r = requests.post(url, headers=HEADERS, json=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "tool": tool}


def summarize(name: str, data: dict) -> str:
    """Describe top-level keys and types/lengths."""
    if not isinstance(data, dict):
        return f"  → not a dict: {type(data).__name__}"
    if data.get("error"):
        return f"  → ERROR: {data.get('error', '')[:80]}"
    lines = []
    for k, v in data.items():
        if k == "error":
            continue
        if isinstance(v, list):
            lines.append(f"  {k}: list, len={len(v)}")
            if v and isinstance(v[0], dict):
                lines.append(f"      first item keys: {list(v[0].keys())[:8]}")
        elif isinstance(v, dict):
            lines.append(f"  {k}: dict, keys={list(v.keys())[:10]}")
        else:
            preview = str(v)[:50] if v is not None else "null"
            lines.append(f"  {k}: {type(v).__name__} = {preview}")
    return "\n".join(lines) if lines else "  (empty)"


# ─── Tool calls (name, tool_name, params) ─────────────────────────────────────
TOOLS = [
    ("get_dataset_stats", "get_dataset_stats", {}),
    ("get_city_profile", "get_city_profile", {"city_name": "Faro"}),
    ("get_weather (month)", "get_weather", {"city_name": "Faro", "month": 3}),
    ("get_weather (date range)", "get_weather", {"city_name": "Faro", "start_date": "2025-06-01", "end_date": "2025-06-05"}),
    ("get_attractions", "get_attractions", {"city_name": "Faro", "limit": 5}),
    ("get_seasonal_calendar", "get_seasonal_calendar", {"city_name": "Faro"}),
    ("find_best_month", "find_best_month", {"city_name": "Faro", "prefer_warm": True}),
    ("compare_cities", "compare_cities", {"city_names": ["Faro", "Lisbon"], "month": 3}),
    ("search_destinations", "search_destinations", {"continent": "Europe", "limit": 5}),
    ("search_by_activity", "search_by_activity", {"activity": "swimming", "month": 6, "limit": 5}),
    ("multi_activity_search", "multi_activity_search", {"activities": ["beach_holiday", "swimming"], "month": 6, "limit": 5}),
    ("find_nearby_destinations", "find_nearby_destinations", {"city_name": "Faro", "radius_km": 200, "limit": 5}),
    ("find_similar_cities", "find_similar_cities", {"city_name": "Faro", "limit": 5}),
    ("plan_trip", "plan_trip", {"month": 6, "continent": "Europe", "limit": 5}),
    ("get_travel_intelligence", "get_travel_intelligence", {"city": "Faro", "month": 6}),
]

def main():
    print("GeoTemp API tools @", BASE)
    print("=" * 60)
    for name, tool, params in TOOLS:
        data = call(tool, params)
        print(f"\n{name}")
        print(summarize(name, data))
        if data.get("error"):
            continue
        # Optionally print full JSON for first few tools (comment out to reduce noise)
        if name in ("get_dataset_stats", "get_city_profile") and "error" not in data:
            print("  [sample] city keys:" if "city" in data and isinstance(data["city"], dict) else "")
            if "city" in data and isinstance(data["city"], dict):
                print("    ", list(data["city"].keys())[:12])
    print("\n" + "=" * 60)
    print("Done.")

if __name__ == "__main__":
    main()
