#!/usr/bin/env python3
"""
GeoTemp Travel MCP client — all 13 tools.
https://mcp-travel-data.onrender.com/sse

Tools: search_destinations, search_by_activity, multi_activity_search,
find_nearby_destinations, find_similar_cities, plan_trip, get_city_profile,
get_weather, get_attractions, get_seasonal_calendar, find_best_month,
compare_cities, get_dataset_stats.

Requires: pip install mcp (sse_client used for GeoTemp).
"""

import json
from typing import Any

# MCP client
from mcp import ClientSession
from mcp.client.sse import sse_client

GEOTEMP_MCP_URL = "https://mcp-travel-data.onrender.com/sse"

# 29 activity names (exact values for search_by_activity, multi_activity_search, plan_trip)
ACTIVITY_NAMES = frozenset({
    "adventure_sports", "beach_holiday", "city_break", "cultural_sightseeing", "cycling",
    "diving", "family_friendly", "fishing", "food_tourism", "golf", "hiking", "nightlife",
    "photography", "rock_climbing", "romantic_getaway", "running_jogging", "sailing",
    "shopping", "skiing", "snorkeling", "spa_wellness", "surfing", "swimming",
    "water_sports", "wildlife_viewing", "wine_tasting", "winter_sports", "yoga_retreat",
})


def _get_block_text(block: Any) -> str | None:
    if block is None:
        return None
    if isinstance(block, dict):
        return block.get("text")
    if hasattr(block, "text"):
        return getattr(block, "text")
    if hasattr(block, "model_dump"):
        return block.model_dump().get("text")
    return None


def _parse_tool_result(result: Any) -> dict | list | None:
    """Extract JSON-serializable data from MCP CallToolResult (content[0].text pattern)."""
    if result is None:
        return None
    # content: list of blocks with .text (GeoTemp / standard MCP)
    content = getattr(result, "content", None)
    if content and len(content) > 0:
        text = _get_block_text(content[0])
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text[:500]}
    # structuredContent fallback
    if getattr(result, "structuredContent", None) is not None:
        sc = result.structuredContent
        if isinstance(sc, (dict, list)):
            return sc
        if isinstance(sc, str):
            try:
                return json.loads(sc)
            except json.JSONDecodeError:
                return {"raw": sc}
    # content with multiple blocks
    if content:
        parts = [_get_block_text(b) for b in content if _get_block_text(b)]
        if parts:
            for p in parts:
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    continue
    return None


async def get_weather(
    session: ClientSession,
    city_name: str,
    start_date: str,
    end_date: str,
    *,
    month: int | None = None,
) -> list[dict] | dict | None:
    """Get weather for a city. Uses month (1–12) if given, else start_date/end_date (YYYY-MM-DD). Returns list of daily data or dict."""
    if month is not None:
        params = {"city_name": city_name, "month": month}
    else:
        params = {"city_name": city_name, "start_date": start_date, "end_date": end_date}
    result = await session.call_tool("get_weather", params)
    data = _parse_tool_result(result)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "days" in data:
        return data.get("days", [])
    if isinstance(data, dict) and "weather" in data:
        return data["weather"]
    return data if isinstance(data, (list, dict)) else None


async def get_attractions(
    session: ClientSession,
    city_name: str,
    *,
    category: str | None = None,
    limit: int = 10,
) -> list[dict] | None:
    """Get attractions for a city. category: museum, monument, castle, viewpoint, etc. Returns list of attraction dicts."""
    params = {"city_name": city_name, "limit": min(limit, 50)}
    if category is not None:
        params["category"] = category
    result = await session.call_tool("get_attractions", params)
    data = _parse_tool_result(result)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "attractions" in data:
        return data["attractions"]
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    return data if isinstance(data, list) else None


async def get_city_profile(
    session: ClientSession,
    city_name: str,
) -> dict | None:
    """Get full city dossier: country, continent, safety, budget, climate, features."""
    result = await session.call_tool("get_city_profile", {"city_name": city_name})
    data = _parse_tool_result(result)
    if isinstance(data, dict) and data.get("error"):
        return None
    if isinstance(data, dict) and "city" in data:
        return data
    return data if isinstance(data, dict) else None


async def find_best_month(
    session: ClientSession,
    city_name: str,
    prefer_warm: bool = True,
    max_rain_mm: float | None = None,
    min_sunshine_hours: float | None = None,
) -> dict | None:
    """Get ranked months by weather (best time to visit)."""
    params = {"city_name": city_name, "prefer_warm": prefer_warm}
    if max_rain_mm is not None:
        params["max_rain_mm"] = max_rain_mm
    if min_sunshine_hours is not None:
        params["min_sunshine_hours"] = min_sunshine_hours
    result = await session.call_tool("find_best_month", params)
    data = _parse_tool_result(result)
    if isinstance(data, dict) and data.get("error"):
        return None
    if isinstance(data, dict) and "rankings" in data:
        return data
    return data if isinstance(data, dict) else None


# --------------- Discovery & Search (tools 1–5) ---------------

async def search_destinations(
    session: ClientSession,
    *,
    continent: str | None = None,
    country: str | None = None,
    is_coastal: bool | None = None,
    min_safety_score: int | None = None,
    max_daily_budget_usd: int | None = None,
    min_population: int | None = None,
    climate_zone: str | None = None,
    limit: int = 20,
) -> dict | None:
    """Find cities matching travel criteria. Returns {destinations: [...], count}."""
    params = {"limit": min(limit, 50)}
    if continent is not None:
        params["continent"] = continent
    if country is not None:
        params["country"] = country
    if is_coastal is not None:
        params["is_coastal"] = is_coastal
    if min_safety_score is not None:
        params["min_safety_score"] = min_safety_score
    if max_daily_budget_usd is not None:
        params["max_daily_budget_usd"] = max_daily_budget_usd
    if min_population is not None:
        params["min_population"] = min_population
    if climate_zone is not None:
        params["climate_zone"] = climate_zone
    result = await session.call_tool("search_destinations", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


async def search_by_activity(
    session: ClientSession,
    activity: str,
    *,
    month: int | None = None,
    min_score: int = 60,
    continent: str | None = None,
    limit: int = 15,
) -> dict | None:
    """Find cities for one activity, ranked by score. Returns {activity, destinations: [...], count}."""
    params = {"activity": activity, "min_score": min_score, "limit": limit}
    if month is not None:
        params["month"] = month
    if continent is not None:
        params["continent"] = continent
    result = await session.call_tool("search_by_activity", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


async def multi_activity_search(
    session: ClientSession,
    activities: list[str],
    *,
    month: int | None = None,
    min_score: int = 40,
    continent: str | None = None,
    limit: int = 15,
) -> dict | None:
    """Find cities that support ALL of the given activities. Returns {activities_required, month, destinations: [...], count}."""
    if not 2 <= len(activities) <= 6:
        return None
    params = {"activities": activities, "min_score": min_score, "limit": limit}
    if month is not None:
        params["month"] = month
    if continent is not None:
        params["continent"] = continent
    result = await session.call_tool("multi_activity_search", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


async def find_nearby_destinations(
    session: ClientSession,
    *,
    city_name: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: int = 500,
    limit: int = 15,
) -> dict | None:
    """Find destinations within radius_km of a city or lat/lon. Returns {reference, nearby_destinations: [...]}."""
    if city_name is not None:
        params = {"city_name": city_name, "radius_km": radius_km, "limit": limit}
    elif latitude is not None and longitude is not None:
        params = {"latitude": latitude, "longitude": longitude, "radius_km": radius_km, "limit": limit}
    else:
        return None
    result = await session.call_tool("find_nearby_destinations", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


async def find_similar_cities(
    session: ClientSession,
    city_name: str,
    limit: int = 10,
) -> dict | None:
    """Find cities similar in climate, activities, geography. Returns {reference_city, similar_destinations: [...]}."""
    result = await session.call_tool("find_similar_cities", {"city_name": city_name, "limit": limit})
    data = _parse_tool_result(result)
    if isinstance(data, dict) and data.get("error"):
        return None
    return data if isinstance(data, dict) else None


# --------------- Trip Planning (tool 6) ---------------

async def plan_trip(
    session: ClientSession,
    month: int,
    *,
    activities: list[str] | None = None,
    max_budget_usd: int | None = None,
    continent: str | None = None,
    min_safety: int | None = None,
    is_coastal: bool | None = None,
    limit: int = 15,
) -> dict | None:
    """Where should I go? Multi-criteria planner. Returns {destinations: [...], count}."""
    params = {"month": month, "limit": limit}
    if activities is not None:
        params["activities"] = activities
    if max_budget_usd is not None:
        params["max_budget_usd"] = max_budget_usd
    if continent is not None:
        params["continent"] = continent
    if min_safety is not None:
        params["min_safety"] = min_safety
    if is_coastal is not None:
        params["is_coastal"] = is_coastal
    result = await session.call_tool("plan_trip", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


# --------------- City Intelligence (tools 10, 12) ---------------

async def get_seasonal_calendar(
    session: ClientSession,
    city_name: str,
) -> dict | None:
    """12-month calendar: weather + top activities per month. Returns {city, calendar: [...]}."""
    result = await session.call_tool("get_seasonal_calendar", {"city_name": city_name})
    data = _parse_tool_result(result)
    if isinstance(data, dict) and data.get("error"):
        return None
    return data if isinstance(data, dict) else None


async def compare_cities(
    session: ClientSession,
    city_names: list[str],
    month: int | None = None,
) -> dict | None:
    """Side-by-side comparison of 2–5 cities. Returns {month, comparisons: [...]}."""
    if not 2 <= len(city_names) <= 5:
        return None
    params = {"city_names": city_names}
    if month is not None:
        params["month"] = month
    result = await session.call_tool("compare_cities", params)
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None


# --------------- Meta (tool 13) ---------------

async def get_dataset_stats(session: ClientSession) -> dict | None:
    """Dataset overview: cities, countries, continents, attractions, weather_records, features."""
    result = await session.call_tool("get_dataset_stats", {})
    return _parse_tool_result(result) if isinstance(_parse_tool_result(result), dict) else None
