#!/usr/bin/env python3
"""
Fetch hotels from the Trivago MCP server (https://mcp.trivago.com/mcp).

Uses the MCP Python SDK to connect via Streamable HTTP and call:
- trivago-search-suggestions: get location id/ns from a query (e.g. city name)
- trivago-accommodation-search: search accommodations by location and dates
- trivago-accommodation-radius-search: search accommodations by coordinates and radius (walking distance from attractions)

Requires: pip install "mcp[cli]"
"""

import argparse
import asyncio
import json
import re
import sys

# MCP client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TRIVAGO_MCP_URL = "https://mcp.trivago.com/mcp"


def _get_block_text(block: object) -> str | None:
    """Get text from a content block (object with .text or dict with 'text' key)."""
    if block is None:
        return None
    if isinstance(block, dict):
        return block.get("text") or None
    # Pydantic model or object with .text
    text = getattr(block, "text", None)
    if text is not None and isinstance(text, str):
        return text
    # Some SDKs expose model_dump()
    if hasattr(block, "model_dump"):
        return block.model_dump().get("text")
    return None


def _parse_suggestions_from_data(data: object) -> tuple[int, int] | None:
    """Extract first (id, ns) from parsed suggestion data."""
    if isinstance(data, dict) and "output" in data:
        suggestions = data["output"]
    elif isinstance(data, list):
        suggestions = data
    else:
        suggestions = data if isinstance(data, list) else []
    for s in suggestions:
        if isinstance(s, dict):
            sid = s.get("ID") or s.get("id")
            ns = s.get("NS") or s.get("ns")
            if sid is not None and ns is not None:
                return (int(sid), int(ns))
    return None


async def get_location_suggestion(session: ClientSession, query: str) -> tuple[int, int] | None:
    """Get the best location suggestion (id, ns) for a search query like a city name."""
    result = await session.call_tool(
        "trivago-search-suggestions",
        {"query": query},
    )
    # Try structuredContent first (some MCP servers return structured output)
    if getattr(result, "structuredContent", None) and isinstance(result.structuredContent, dict):
        out = _parse_suggestions_from_data(result.structuredContent)
        if out:
            return out
        if "output" in result.structuredContent:
            out = _parse_suggestions_from_data(result.structuredContent["output"])
            if out:
                return out
    if not result.content:
        return None
    # Build full text from all blocks (in case response is split or block shape differs)
    all_text_parts = []
    for block in result.content:
        text = _get_block_text(block)
        if text:
            all_text_parts.append(text)
        # Fallback: Pydantic model may serialize differently; try model_dump if present
        if not text and hasattr(block, "model_dump"):
            d = block.model_dump()
            text = d.get("text")
            if text:
                all_text_parts.append(text)
    full_text = "\n".join(all_text_parts) if all_text_parts else None
    if full_text:
        try:
            data = json.loads(full_text)
            out = _parse_suggestions_from_data(data)
            if out:
                return out
        except json.JSONDecodeError:
            pass
        # Regex fallback: server may return Go-style "ID:3848" or JSON "\"ID\": 3848"
        if "ID" in full_text and "NS" in full_text:
            ids = re.findall(r'["\']?ID["\']?\s*:\s*(\d+)', full_text, re.IGNORECASE)
            ns_list = re.findall(r'["\']?NS["\']?\s*:\s*(\d+)', full_text, re.IGNORECASE)
            if ids and ns_list:
                return (int(ids[0]), int(ns_list[0]))
        # Try parsing first JSON object or array in the text
        for part in all_text_parts:
            try:
                data = json.loads(part)
                out = _parse_suggestions_from_data(data)
                if out:
                    return out
            except json.JSONDecodeError:
                if "ID" in part and "NS" in part:
                    ids = re.findall(r'["\']?ID["\']?\s*:\s*(\d+)', part, re.IGNORECASE)
                    ns_list = re.findall(r'["\']?NS["\']?\s*:\s*(\d+)', part, re.IGNORECASE)
                    if ids and ns_list:
                        return (int(ids[0]), int(ns_list[0]))
    # Last resort: scan entire result (e.g. if content is in a different shape)
    try:
        raw = result.model_dump() if hasattr(result, "model_dump") else {}
        raw_str = json.dumps(raw)
        if "ID" in raw_str and "NS" in raw_str:
            ids = re.findall(r'["\']?ID["\']?\s*:\s*(\d+)', raw_str, re.IGNORECASE)
            ns_list = re.findall(r'["\']?NS["\']?\s*:\s*(\d+)', raw_str, re.IGNORECASE)
            if ids and ns_list:
                return (int(ids[0]), int(ns_list[0]))
    except Exception:
        pass
    return None


def _parse_accommodations_from_result(result: object) -> list[dict]:
    """Extract list of accommodation dicts from MCP call_tool result (shared by search and radius search)."""
    if getattr(result, "structuredContent", None) and isinstance(result.structuredContent, dict):
        out = result.structuredContent.get("output")
        if isinstance(out, list) and out:
            return out
    all_text_parts = []
    if getattr(result, "content", None):
        for block in result.content:
            text = _get_block_text(block)
            if text:
                all_text_parts.append(text)
            if not text and hasattr(block, "model_dump"):
                t = block.model_dump().get("text")
                if t:
                    all_text_parts.append(t)

    def extract_array_from_text(s: str) -> list | None:
        start_marker = "output:["
        idx = s.find(start_marker)
        if idx != -1:
            start = idx + len("output:")
            depth = 0
            for i in range(start, len(s)):
                if s[i] == "[":
                    depth += 1
                elif s[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start : i + 1])
                        except json.JSONDecodeError:
                            break
            return None
        start = s.find("[")
        if start != -1:
            depth = 0
            for i in range(start, len(s)):
                if s[i] == "[":
                    depth += 1
                elif s[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start : i + 1])
                        except json.JSONDecodeError:
                            break
        return None

    for part in all_text_parts if all_text_parts else []:
        try:
            data = json.loads(part)
            if isinstance(data, dict) and "output" in data and isinstance(data["output"], list):
                return data["output"]
            if isinstance(data, list) and data:
                return data
        except json.JSONDecodeError:
            pass
        arr = extract_array_from_text(part)
        if isinstance(arr, list) and arr:
            return arr
    return []


async def search_accommodations(
    session: ClientSession,
    location_id: int,
    location_ns: int,
    arrival: str,
    departure: str,
    adults: int = 2,
    rooms: int = 1,
) -> list[dict]:
    """Search accommodations for the given location and dates."""
    args = {
        "id": location_id,
        "ns": location_ns,
        "arrival": arrival,
        "departure": departure,
        "adults": adults,
        "rooms": rooms,
    }
    result = await session.call_tool("trivago-accommodation-search", args)
    return _parse_accommodations_from_result(result)


async def search_accommodations_radius(
    session: ClientSession,
    latitude: float,
    longitude: float,
    radius_km: float,
    arrival: str,
    departure: str,
    adults: int = 2,
    rooms: int = 1,
) -> list[dict]:
    """Search accommodations within radius_km of (latitude, longitude). Use for hotels near attractions (walking distance).
    See https://mcp.trivago.com/docs — trivago-accommodation-radius-search.
    """
    args = {
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius_km,
        "arrival": arrival,
        "departure": departure,
        "adults": adults,
        "rooms": rooms,
    }
    result = await session.call_tool("trivago-accommodation-radius-search", args)
    return _parse_accommodations_from_result(result)


def format_hotels(hotels: list[dict], max_results: int = 10) -> str:
    """Format hotel list for console output."""
    lines = []
    for i, h in enumerate(hotels[:max_results], 1):
        name = h.get("Accommodation Name") or h.get("accommodation_name") or "—"
        price_stay = h.get("Price Per Stay") or h.get("price_per_stay") or "—"
        rating = h.get("Review Rating") or h.get("review_rating") or "—"
        url = h.get("Accommodation URL") or h.get("accommodation_url") or ""
        lines.append(f"  {i}. {name}")
        lines.append(f"     Price: {price_stay}  |  Rating: {rating}")
        if url:
            lines.append(f"     {url}")
        lines.append("")
    return "\n".join(lines)


async def run(
    query: str,
    arrival: str,
    departure: str,
    adults: int = 2,
    rooms: int = 1,
    max_results: int = 10,
    output_json: bool = False,
) -> None:
    async with streamable_http_client(TRIVAGO_MCP_URL) as streams:
        read_stream = streams[0]
        write_stream = streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Resolve location from query
            suggestion = await get_location_suggestion(session, query)
            if not suggestion:
                print(f"Could not find a location for query: {query!r}", file=sys.stderr)
                sys.exit(1)
            location_id, location_ns = suggestion
            print(f"Using location: {query} (id={location_id}, ns={location_ns})", file=sys.stderr)

            hotels = await search_accommodations(
                session,
                location_id,
                location_ns,
                arrival,
                departure,
                adults=adults,
                rooms=rooms,
            )

            if output_json:
                print(json.dumps(hotels, indent=2, ensure_ascii=False))
            else:
                print(f"\nHotels in {query} ({arrival} → {departure})\n")
                if not hotels:
                    print("  No results found.")
                else:
                    print(format_hotels(hotels, max_results=max_results))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch hotels from Trivago via MCP (mcp.trivago.com)",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="Berlin",
        help="Location query (e.g. city or country name)",
    )
    parser.add_argument(
        "--arrival",
        default="2026-03-15",
        help="Arrival date YYYY-MM-DD",
    )
    parser.add_argument(
        "--departure",
        default="2026-03-18",
        help="Departure date YYYY-MM-DD",
    )
    parser.add_argument(
        "--adults",
        type=int,
        default=2,
        help="Number of adults",
    )
    parser.add_argument(
        "--rooms",
        type=int,
        default=1,
        help="Number of rooms",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=10,
        dest="max_results",
        help="Max number of results to show",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output raw JSON",
    )
    args = parser.parse_args()

    # Basic date check (today is 2026-02-12 per context)
    today = "2026-02-12"
    if args.arrival < today:
        print("Warning: arrival date should be in the future.", file=sys.stderr)
    if args.departure <= args.arrival:
        print("Error: departure must be after arrival.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(
        run(
            query=args.query,
            arrival=args.arrival,
            departure=args.departure,
            adults=args.adults,
            rooms=args.rooms,
            max_results=args.max_results,
            output_json=args.output_json,
        )
    )


if __name__ == "__main__":
    main()
