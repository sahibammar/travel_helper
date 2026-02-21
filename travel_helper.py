#!/usr/bin/env python3
"""
Travel helper: cheap round-trip flights from Düsseldorf Weeze / Köln / Dortmund, then hotels.

1. Collects return trips from Weeze (NRN), Köln (CGN), and Dortmund (DTM). Only the departure (outbound) must
   match the schedule: Wednesday after 6 pm, Thursday after 5 pm, or Friday after 11 am. Return is 2–4 nights later
   (any time); no schedule restriction on the return flight.
2. Picks the 10 cheapest such trips by outbound price.
3. For each, fetches hotels for 2–4 nights from the Trivago MCP server.

Callable by OpenClaw:
  - Run: python travel_helper.py [--json] [--no-hotels]
  - Use --json for machine-readable output (OpenClaw-friendly).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Project root on path for trivago package
if __name__ == "__main__" and __package__ is None:
    _root = Path(__file__).resolve().parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from ryanair import Ryanair

# Optional: airport coords for estimated flight duration
try:
    from ryanair.airport_utils import load_airports, get_distance_between_airports
    _DURATION_AVAILABLE = True
except ImportError:
    _DURATION_AVAILABLE = False


def _flight_duration_str(origin_iata: str, destination_iata: str) -> str:
    """Return estimated flight duration as (Xh:Ym) or empty string if unknown."""
    if not _DURATION_AVAILABLE:
        return ""
    try:
        load_airports()
        km = get_distance_between_airports(origin_iata, destination_iata)
        total_minutes = (km / 800.0) * 60 + 38  # ~800 km/h + 38 min taxi/takeoff/landing
        h = int(total_minutes // 60)
        m = int(round(total_minutes % 60))
        if m == 60:
            h += 1
            m = 0
        return f" ({h}h:{m:02d}m)" if h > 0 or m > 0 else ""
    except (KeyError, TypeError):
        return ""


def _flight_duration_for_json(origin_iata: str, destination_iata: str) -> dict:
    """Return estimated flight duration for JSON: duration_minutes (int or null) and duration (e.g. '2h 15m')."""
    if not _DURATION_AVAILABLE:
        return {"duration_minutes": None, "duration": ""}
    try:
        load_airports()
        km = get_distance_between_airports(origin_iata, destination_iata)
        total_minutes = (km / 800.0) * 60 + 38
        m = int(round(total_minutes % 60))
        h = int(total_minutes // 60)
        if m == 60:
            h += 1
            m = 0
        mins = h * 60 + m
        return {"duration_minutes": mins, "duration": f"{h}h {m:02d}m" if h > 0 or m > 0 else ""}
    except (KeyError, TypeError):
        return {"duration_minutes": None, "duration": ""}


# Trivago MCP (optional: only if mcp is installed)
try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from trivago.fetch_hotels_mcp import get_location_suggestion, search_accommodations, search_accommodations_radius

    TRIVAGO_AVAILABLE = True
except ImportError:
    TRIVAGO_AVAILABLE = False

# GeoTemp Travel MCP (optional: weather + attractions per destination)
try:
    from mcp.client.sse import sse_client
    from geotemp.geotemp_fetch_mcp import (
    GEOTEMP_MCP_URL,
    compare_cities,
    find_best_month,
    find_nearby_destinations,
    find_similar_cities,
    get_attractions,
    get_city_profile,
    get_dataset_stats,
    get_seasonal_calendar,
    get_weather,
    multi_activity_search,
    plan_trip,
    search_by_activity,
    search_destinations,
)

    GEOTEMP_AVAILABLE = True
except ImportError:
    GEOTEMP_AVAILABLE = False
    sse_client = None
    get_weather = None
    get_attractions = None
    get_city_profile = None
    find_best_month = None
    find_similar_cities = None
    find_nearby_destinations = None
    get_seasonal_calendar = None
    plan_trip = None
    compare_cities = None
    search_destinations = None
    search_by_activity = None
    multi_activity_search = None
    get_dataset_stats = None
    GEOTEMP_MCP_URL = None

# --------------- Config: Weeze + Köln + Dortmund, Wed eve / Thu eve / Fri late outbound, 2–4 nights ---------------
ORIGIN_AIRPORTS = [
    ("CGN", "Köln"),
    ("NRN", "Düsseldorf Weeze"),
    ("DTM", "Dortmund"),
]
DAYS_AHEAD = 90  # search ahead for Wed/Thu/Fri departures
RETURN_DAYS_MIN = 2  # 2 nights at destination
RETURN_DAYS_MAX = 4  # 4 nights at destination
HOTEL_NIGHTS = 4  # legacy; hotel stay now matches return flight (arrival = outbound date, departure = return date)
TRIVAGO_MCP_URL = "https://mcp.trivago.com/mcp"

# Only the departure (outbound) is restricted: Wednesday >= 18:00, Thursday >= 17:00, or Friday >= 11:00 (11 am).
# Return flight is 2–4 nights later with no time-of-day restriction. Monday=0 in weekday().
WEDNESDAY = 2
THURSDAY = 3
FRIDAY = 4
OUTBOUND_WEDNESDAY_AFTER_HOUR = 18  # 6 pm
OUTBOUND_THURSDAY_AFTER_HOUR = 17
OUTBOUND_FRIDAY_AFTER_HOUR = 11  # 11 am

# Display: separator between the two legs on one line
LEG_SEP = "  |  "    # between outbound and inbound on one line

RYANAIR_BOOKING_BASE = "https://www.ryanair.com/de/de/trip/flights/select"


def _ryanair_booking_url(
    origin_iata: str,
    destination_iata: str,
    date_out: str,
    date_in: str,
    adults: int = 2,
) -> str:
    """Build Ryanair round-trip flight select URL (German site)."""
    params = (
        f"adults={adults}&teens=0&children=0&infants=0"
        f"&dateOut={date_out}&dateIn={date_in}"
        "&isConnectedFlight=false&discount=0&promoCode=&isReturn=true"
        f"&originIata={origin_iata}&destinationIata={destination_iata}"
        "&tpAdults=1&tpTeens=0&tpChildren=0&tpInfants=0"
        f"&tpStartDate={date_out}&tpEndDate={date_in}"
        "&tpDiscount=0&tpPromoCode="
        f"&tpOriginIata={origin_iata}&tpDestinationIata={destination_iata}"
    )
    return f"{RYANAIR_BOOKING_BASE}?{params}"


def _ryanair_booking_url_one_way(
    origin_iata: str,
    destination_iata: str,
    date_iso: str,
    adults: int = 2,
) -> str:
    """Build Ryanair one-way flight select URL (German site)."""
    params = (
        f"adults={adults}&teens=0&children=0&infants=0"
        f"&dateOut={date_iso}&dateIn={date_iso}"
        "&isConnectedFlight=false&discount=0&promoCode=&isReturn=false"
        f"&originIata={origin_iata}&destinationIata={destination_iata}"
    )
    return f"{RYANAIR_BOOKING_BASE}?{params}"


def _booking_urls_for_trip(ob: object, ret: object, adults: int = 2) -> dict:
    """Return booking URL(s): one round-trip URL for same-airport, or two one-way URLs for open-jaw.
    Uses display airport codes (e.g. NRN not WEE) so Ryanair booking links work."""
    date_out = ob.departureTime.date().isoformat()
    date_in = ret.departureTime.date().isoformat()
    origin_out = _display_airport(ob.origin)
    dest_out = _display_airport(ob.destination)
    origin_ret = _display_airport(ret.origin)
    dest_ret = _display_airport(ret.destination)
    if ob.origin == ret.destination:
        return {"booking_url": _ryanair_booking_url(origin_out, dest_out, date_out, date_in, adults)}
    return {
        "booking_url_outbound": _ryanair_booking_url_one_way(origin_out, dest_out, date_out, adults),
        "booking_url_return": _ryanair_booking_url_one_way(origin_ret, dest_ret, date_in, adults),
    }


def _flight_route_label(ob: object, ret: object) -> str:
    """Return label for flight route: (NRN→FAO→NRN) same airport, (NRN→FAO→CGN) open-jaw."""
    return f" ({ob.origin}→{ob.destination}→{ret.destination})"


def _outbound_departure_allowed(dt: datetime) -> bool:
    """True if outbound departure is Wed after 6 pm, Thu after 5 pm, or Fri after 11 am (only departure is restricted)."""
    wd = dt.weekday()
    hour = dt.hour
    if wd == WEDNESDAY:
        return hour >= OUTBOUND_WEDNESDAY_AFTER_HOUR
    if wd == THURSDAY:
        return hour >= OUTBOUND_THURSDAY_AFTER_HOUR
    if wd == FRIDAY:
        return hour >= OUTBOUND_FRIDAY_AFTER_HOUR
    return False


def _parse_price_night(h: dict) -> float:
    """Parse 'Price Per Night' e.g. '€77' to float. Return inf if missing/invalid."""
    raw = h.get("Price Per Night") or h.get("price_per_night") or ""
    if not raw:
        return float("inf")
    m = re.search(r"[\d.,]+", raw.replace(",", "."))
    if not m:
        return float("inf")
    try:
        return float(m.group(0))
    except ValueError:
        return float("inf")


def _city_name_for_trivago(destination: str) -> str:
    """Extract city name only for Trivago: remove airport code in parentheses, e.g. 'Nador (NDR)' -> 'Nador'."""
    s = destination.strip()
    if " (" in s and s.endswith(")"):
        s = s[: s.rindex(" (")].strip()
    return s


def _trivago_query_for_destination(destination_city: str) -> list[str]:
    """Build search queries for Trivago: city only (no airport code), then try part before ' - ' if present."""
    city_only = _city_name_for_trivago(destination_city)
    queries = [city_only]
    if " - " in city_only:
        queries.append(city_only.split(" - ")[0].strip())
    return queries


def _trivago_search_url(destination_city: str, arrival_iso: str, departure_iso: str) -> str:
    """Trivago destination search URL for city and dates (arrival/departure YYYY-MM-DD)."""
    city = _city_name_for_trivago(destination_city)
    q = quote(city)
    return f"https://www.trivago.com/en/destination?search={q}&arrival={arrival_iso}&departure={departure_iso}"


# Radius (km) for "walking distance from attractions" when using Trivago radius search
HOTEL_RADIUS_ATTRACTIONS_KM = 1.5


# Max attractions to use when computing centroid (middle point) for hotel radius search
_ATTRACTION_CENTROID_LIMIT = 25


def _attraction_center(attractions: list) -> tuple[float, float] | None:
    """Middle point of all attractions with coordinates: centroid (average lat, average lon). Used as reference for hotel radius search."""
    if not attractions or not isinstance(attractions, list):
        return None
    coords = []
    for a in attractions[:_ATTRACTION_CENTROID_LIMIT]:
        if not isinstance(a, dict):
            continue
        lat = a.get("latitude") or a.get("lat")
        lon = a.get("longitude") or a.get("lon") or a.get("lng")
        if lat is not None and lon is not None:
            try:
                coords.append((float(lat), float(lon)))
            except (TypeError, ValueError):
                continue
    if not coords:
        return None
    n = len(coords)
    return (sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n)


def _hotel_key(h: dict) -> str:
    """Stable key for deduplication (same hotel from radius vs city search)."""
    return (h.get("Accommodation Name") or h.get("accommodation_name") or "").strip() or str(id(h))


def _cheapest_hotels(hotels: list[dict], n: int) -> list[dict]:
    """Return the n cheapest hotels by price per night (ascending). Missing prices sort last."""
    return sorted(hotels, key=_parse_price_night)[:n]


async def _top_hotels_for_destination(
    session: ClientSession,
    destination_city: str,
    arrival_date: str,
    departure_date: str,
    n: int = 1,
    adults: int = 2,
    rooms: int = 1,
    attraction_coords: tuple[float, float] | None = None,
) -> list[dict]:
    """Return up to n cheapest hotels (by price per night). Combines radius (near attractions) and city search when coords given, then always sorts by price."""
    hotels: list[dict] = []
    # City search: always run so we can merge with radius and pick cheapest overall
    suggestion = None
    for query in _trivago_query_for_destination(destination_city):
        suggestion = await get_location_suggestion(session, query)
        if suggestion:
            break
    if suggestion:
        location_id, location_ns = suggestion
        city_hotels = await search_accommodations(
            session,
            location_id,
            location_ns,
            arrival_date,
            departure_date,
            adults=adults,
            rooms=rooms,
        )
        hotels = list(city_hotels) if city_hotels else []
    # Optionally add radius results (near attractions) and merge
    if attraction_coords is not None and search_accommodations_radius:
        lat, lon = attraction_coords
        radius_hotels = await search_accommodations_radius(
            session, lat, lon, HOTEL_RADIUS_ATTRACTIONS_KM,
            arrival_date, departure_date, adults=adults, rooms=rooms,
        )
        if radius_hotels:
            seen = {_hotel_key(h) for h in hotels}
            for h in radius_hotels:
                if _hotel_key(h) not in seen:
                    seen.add(_hotel_key(h))
                    hotels.append(h)
    if not hotels:
        return []
    # Always sort by price ascending and return the n cheapest
    return _cheapest_hotels(hotels, n)


async def fetch_hotels_for_cheapest_flights(
    cheapest_flights: list[tuple[object, object, float]],
    adults: int = 2,
    rooms: int = 1,
    attractions_by_dest: dict | None = None,
) -> list[dict]:
    """
    For each (outbound, return_flight, price) entry, fetch the single cheapest hotel
    for that destination (near attractions when coordinates available, else city).
    Returns list of { "destination", "arrival", "departure", "flight", "return_flight", "price", "hotels": [one hotel] }.
    """
    if not TRIVAGO_AVAILABLE or not cheapest_flights:
        return []
    attractions_by_dest = attractions_by_dest or {}
    tasks = []
    for outbound, return_flight, price in cheapest_flights:
        dest_city = (
            outbound.destinationFull.split(",")[0].strip()
            if "," in outbound.destinationFull
            else outbound.destinationFull
        )
        arrival = outbound.departureTime.date().isoformat()
        departure = return_flight.departureTime.date().isoformat()
        tasks.append((dest_city, arrival, departure, outbound, return_flight, price))

    results = []
    async with streamable_http_client(TRIVAGO_MCP_URL) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for dest_city, arrival, departure, outbound, return_flight, price in tasks:
                attraction_coords = _attraction_center(attractions_by_dest.get(dest_city) or [])
                hotels = await _top_hotels_for_destination(
                    session, dest_city, arrival, departure,
                    n=1, adults=adults, rooms=rooms,
                    attraction_coords=attraction_coords,
                )
                results.append({
                    "destination": dest_city,
                    "arrival": arrival,
                    "departure": departure,
                    "flight": outbound,
                    "return_flight": return_flight,
                    "price": price,
                    "hotels": hotels,
                })
    return results


# Known airport names that often appear after the city (e.g. "Barcelona Girona", "London Stansted").
# Used to strip to city-only for weather/attractions API lookups.
_AIRPORT_SUFFIXES = frozenset({
    "Bergamo", "Beauvais", "Charleroi", "Ciampino", "Eindhoven", "Girona",
    "Hahn", "Knock", "Luton", "Malpensa", "Mazury", "Memmingen", "Modlin",
    "Prestwick", "Sandefjord", "Shannon", "Stansted", "Southend", "Torp",
    "Weeze",
})


def _city_name_for_api(dest: str) -> str:
    """Extract city name from destination string for weather/attractions API.
    E.g. 'Olsztyn - Mazury' -> 'Olsztyn', 'Barcelona Girona' -> 'Barcelona', 'London Stansted' -> 'London'.
    """
    if not dest or not dest.strip():
        return dest
    s = dest.strip()
    # "City - Airport/Region" (e.g. Olsztyn - Mazury)
    if " - " in s:
        return s.split(" - ", 1)[0].strip() or s
    # "City Airport" (e.g. Barcelona Girona, London Stansted, Milan Bergamo)
    parts = s.split()
    if len(parts) >= 2 and parts[-1] in _AIRPORT_SUFFIXES:
        return " ".join(parts[:-1]).strip() or s
    return s


def _anchor_slug(dest_city: str, days: int, nights: int) -> str:
    """URL-safe anchor id for a deal section (destination + days/nights)."""
    base = re.sub(
        r"[^a-z0-9-]",
        "",
        dest_city.lower().replace(" - ", "-").replace(" ", "-"),
    )
    return f"{base}-{days}-{nights}" if base else f"deal-{days}-{nights}"


def _dest_city_from_flight(ob: object) -> str:
    """Destination city string for a trip (for GeoTemp / display)."""
    dest_full = getattr(ob, "destinationFull", None) or ""
    if "," in dest_full:
        return dest_full.split(",")[0].strip()
    return dest_full.strip() or getattr(ob, "destination", "")


def _display_airport(code: str) -> str:
    """Airport code for display (e.g. NRN instead of WEE for Weeze)."""
    if code == "WEE":
        return "NRN"
    return code


def _flight_route_label_display(ob: object, ret: object) -> str:
    """Like _flight_route_label but uses display airport codes (e.g. NRN not WEE)."""
    return f" ({_display_airport(ob.origin)}→{_display_airport(ob.destination)}→{_display_airport(ret.destination)})"


def _aggregate_hotel_results(hotel_results: list[dict]) -> list[dict]:
    """Group hotel_results by (destination, days, nights). Each group has trips sorted by total price (cheapest first)."""
    groups: dict[tuple[str, int, int], list[dict]] = {}
    for r in hotel_results:
        outbound = r["flight"]
        ret = r["return_flight"]
        dest = r["destination"]
        ret_date = ret.departureTime.date()
        out_date = outbound.departureTime.date()
        nights = (ret_date - out_date).days
        days = nights + 1
        key = (dest, days, nights)
        if key not in groups:
            groups[key] = []
        groups[key].append(r)
    # Sort each group by total price
    result = []
    for (dest, days, nights), trips in groups.items():
        trips_sorted = sorted(trips, key=lambda t: t["price"] + t["return_flight"].price)
        result.append({
            "destination": dest,
            "days": days,
            "nights": nights,
            "trips": trips_sorted,
            "min_total": trips_sorted[0]["price"] + trips_sorted[0]["return_flight"].price,
        })
    result.sort(key=lambda g: g["min_total"])
    return result


def _aggregate_cheapest_flights(cheapest_flights: list[tuple[object, object, float]]) -> list[tuple[str, int, int, list[tuple[object, object, float]]]]:
    """Group cheapest_flights by (destination, days, nights). Each group has flights sorted by total price (cheapest first)."""
    groups: dict[tuple[str, int, int], list[tuple[object, object, float]]] = {}
    for ob, ib, price in cheapest_flights:
        dest = _dest_city_from_flight(ob)
        ret_date = ib.departureTime.date()
        out_date = ob.departureTime.date()
        nights = (ret_date - out_date).days
        days = nights + 1
        key = (dest, days, nights)
        if key not in groups:
            groups[key] = []
        groups[key].append((ob, ib, price))
    result = []
    for (dest, days, nights), flights in groups.items():
        flights_sorted = sorted(flights, key=lambda x: x[2] + x[1].price)
        result.append((dest, days, nights, flights_sorted))
    result.sort(key=lambda g: g[3][0][2] + g[3][0][1].price)
    return result


async def _fetch_geotemp_for_trips(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
) -> dict | None:
    """Fetch weather, attractions, city profile and best months per destination from GeoTemp MCP.
    Returns {'weather': ..., 'attractions': ..., 'city_profiles': {dest: dict}, 'best_months': {dest: dict}} or None on error.
    """
    if not GEOTEMP_AVAILABLE or not sse_client:
        return None
    # Collect (dest_city, start_iso, end_iso) and unique destinations
    weather_keys: list[tuple[str, str, str]] = []
    destinations: set[str] = set()
    if hotel_results:
        for r in hotel_results:
            dest = r["destination"]
            start_iso = r["arrival"]  # YYYY-MM-DD
            end_iso = r["departure"]
            weather_keys.append((dest, start_iso, end_iso))
            destinations.add(dest)
    else:
        for ob, ib, _ in cheapest_flights:
            dest = _dest_city_from_flight(ob)
            start_iso = ob.departureTime.date().isoformat()
            end_iso = ib.departureTime.date().isoformat()
            weather_keys.append((dest, start_iso, end_iso))
            destinations.add(dest)
    if not weather_keys and not destinations:
        return None
    weather_by_key: dict[tuple[str, str, str], list] = {}
    attractions_by_dest: dict[str, list] = {}
    city_profiles_by_dest: dict[str, dict] = {}
    best_months_by_dest: dict[str, dict] = {}
    similar_cities_by_dest: dict[str, dict] = {}
    seasonal_calendar_by_dest: dict[str, dict] = {}
    nearby_destinations_by_dest: dict[str, dict] = {}
    dataset_stats: dict | None = None
    plan_trip_result: dict | None = None
    compare_cities_result: dict | None = None
    search_destinations_result: dict | None = None
    search_by_activity_result: dict | None = None
    multi_activity_search_result: dict | None = None
    # Month for global tools (from first trip or current)
    first_month = None
    if weather_keys:
        try:
            first_month = int(weather_keys[0][1].split("-")[1])
        except (IndexError, ValueError):
            pass
    if first_month is None:
        first_month = datetime.now().month
    try:
        async with sse_client(GEOTEMP_MCP_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for (dest, start_iso, end_iso) in weather_keys:
                    key = (dest, start_iso, end_iso)
                    if key not in weather_by_key:
                        dest_api = _city_name_for_api(dest)
                        month = None
                        try:
                            month = int(start_iso.split("-")[1])
                        except (IndexError, ValueError):
                            pass
                        w = await get_weather(
                            session, dest_api, start_iso, end_iso, month=month
                        )
                        weather_by_key[key] = w if isinstance(w, list) else ([w] if w else [])
                for dest in destinations:
                    dest_api = _city_name_for_api(dest)
                    if dest not in attractions_by_dest:
                        a = await get_attractions(session, dest_api, limit=10)
                        attractions_by_dest[dest] = a if isinstance(a, list) else ([] if not a else [a])
                    if dest not in city_profiles_by_dest and get_city_profile:
                        profile = await get_city_profile(session, dest_api)
                        city_profiles_by_dest[dest] = profile or {}
                    if dest not in best_months_by_dest and find_best_month:
                        best = await find_best_month(session, dest_api, prefer_warm=True)
                        best_months_by_dest[dest] = best or {}
                    if dest not in similar_cities_by_dest and find_similar_cities:
                        sim = await find_similar_cities(session, dest_api, limit=5)
                        similar_cities_by_dest[dest] = sim or {}
                    if dest not in seasonal_calendar_by_dest and get_seasonal_calendar:
                        cal = await get_seasonal_calendar(session, dest_api)
                        seasonal_calendar_by_dest[dest] = cal or {}
                    if dest not in nearby_destinations_by_dest and find_nearby_destinations:
                        nearby = await find_nearby_destinations(session, city_name=dest_api, radius_km=500, limit=5)
                        nearby_destinations_by_dest[dest] = nearby or {}
                # Global tools (once per run)
                if get_dataset_stats:
                    dataset_stats = await get_dataset_stats(session)
                if plan_trip:
                    plan_trip_result = await plan_trip(session, first_month, continent="Europe", limit=5)
                city_names_for_compare = [_city_name_for_api(d) for d in list(destinations)[:5]]
                if compare_cities and len(city_names_for_compare) >= 2:
                    compare_cities_result = await compare_cities(session, city_names_for_compare, month=first_month)
                if search_destinations:
                    search_destinations_result = await search_destinations(session, continent="Europe", limit=10)
                if search_by_activity:
                    search_by_activity_result = await search_by_activity(session, "city_break", month=first_month, limit=5)
                if multi_activity_search:
                    multi_activity_search_result = await multi_activity_search(
                        session, ["beach_holiday", "swimming"], month=first_month, limit=5
                    )
    except Exception as e:
        print(f"GeoTemp MCP unavailable: {e}", file=sys.stderr)
        return None
    return {
        "weather": weather_by_key,
        "attractions": attractions_by_dest,
        "city_profiles": city_profiles_by_dest,
        "best_months": best_months_by_dest,
        "similar_cities": similar_cities_by_dest,
        "seasonal_calendar": seasonal_calendar_by_dest,
        "nearby_destinations": nearby_destinations_by_dest,
        "dataset_stats": dataset_stats,
        "plan_trip_result": plan_trip_result,
        "compare_cities_result": compare_cities_result,
        "search_destinations_result": search_destinations_result,
        "search_by_activity_result": search_by_activity_result,
        "multi_activity_search_result": multi_activity_search_result,
    }


def _format_weather_item(item: dict) -> str | None:
    """Format a single weather day dict for display. Returns None if item is an error message."""
    if not isinstance(item, dict):
        return str(item)
    if item.get("error"):
        return None
    # GeoTemp month summary: { city, month, weather_summary: { avg_temperature_mean, avg_rain_mm, ... } }
    summary = item.get("weather_summary")
    if isinstance(summary, dict):
        parts = []
        if item.get("city"):
            parts.append(str(item["city"]))
        if item.get("month"):
            parts.append(str(item["month"]))
        avg_temp = summary.get("avg_temperature_mean") or summary.get("avg_temp")
        if avg_temp is not None:
            parts.append(f"avg {avg_temp}°C")
        rain = summary.get("avg_rain_mm") or summary.get("rain_mm")
        if rain is not None:
            parts.append(f"rain {rain} mm")
        if summary.get("description"):
            parts.append(str(summary["description"]))
        if parts:
            return " — ".join(str(p) for p in parts)
    # Daily-style: date, temperature, condition
    parts = []
    if "date" in item:
        parts.append(str(item["date"]))
    if "temperature" in item:
        parts.append(f"{item['temperature']}°C")
    elif "temp" in item:
        parts.append(f"{item['temp']}°C")
    if "condition" in item:
        parts.append(str(item["condition"]))
    elif "description" in item:
        parts.append(str(item["description"]))
    if parts:
        return " — ".join(parts)
    # Fallback: full JSON, no truncation
    return json.dumps(item, ensure_ascii=False)


def _format_attraction_item(item: dict) -> str:
    """Format a single attraction dict for display."""
    if not isinstance(item, dict):
        return str(item)
    name = item.get("name") or item.get("title") or item.get("attraction") or "—"
    return str(name)


def _format_city_profile(profile: dict) -> list[str]:
    """Format city profile dict into readable lines. Returns list of strings (empty if no useful data)."""
    if not profile or not isinstance(profile, dict):
        return []
    city = profile.get("city") if isinstance(profile.get("city"), dict) else {}
    if not city:
        return []
    parts = []
    country = city.get("country")
    continent = city.get("continent")
    if country or continent:
        parts.append(", ".join(str(x) for x in (country, continent) if x))
    safety = city.get("safety_score")
    if safety is not None:
        parts.append(f"Safety {safety}/5")
    budget = city.get("daily_budget_usd")
    if budget is not None:
        parts.append(f"~${int(budget)}/day")
    if city.get("is_coastal"):
        parts.append("Coastal")
    climate = city.get("climate_zone")
    if climate:
        parts.append(f"Climate {climate}")
    if not parts:
        return []
    return [" · ".join(parts)]


def _format_activities(profile: dict) -> list[str]:
    """Format city profile features (activities) into readable lines. Returns list of activity names with optional score."""
    if not profile or not isinstance(profile, dict):
        return []
    # API may nest under "city" or put "features" at top level
    city = profile.get("city") if isinstance(profile.get("city"), dict) else {}
    features = (city.get("features") or profile.get("features")) if isinstance(city.get("features"), list) else (profile.get("features") if isinstance(profile.get("features"), list) else [])
    if not features:
        return []
    lines = []
    month_names = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    for f in features:
        if not isinstance(f, dict):
            continue
        name = f.get("feature") or f.get("activity") or f.get("name")
        if not name:
            continue
        # Optional: show best month and score from monthly_scores
        monthly = f.get("monthly_scores")
        if isinstance(monthly, dict) and monthly:
            try:
                best = max(
                    ((k, v) for k, v in monthly.items() if v is not None and isinstance(v, (int, float))),
                    key=lambda x: float(x[1]),
                    default=None,
                )
                if best:
                    month_num, score = best[0], best[1]
                    idx = int(month_num) if str(month_num).isdigit() else 0
                    mn = month_names[idx] if 1 <= idx <= 12 else str(month_num)
                    lines.append(f"{name} ({mn} {int(score)})")
                else:
                    lines.append(str(name))
            except (ValueError, TypeError):
                lines.append(str(name))
        else:
            lines.append(str(name))
    return lines


def _format_activities_from_calendar(cal_data: dict) -> list[str]:
    """Build activity list from seasonal calendar top_activities (fallback when profile has no features)."""
    if not cal_data or not isinstance(cal_data, dict):
        return []
    calendar = cal_data.get("calendar")
    if not isinstance(calendar, list):
        return []
    # Collect (activity, best_month_name, score) across months
    activity_best: dict[str, tuple[str, int]] = {}
    for entry in calendar:
        if not isinstance(entry, dict):
            continue
        month_name = entry.get("month_name") or ""
        top = entry.get("top_activities") or []
        for ta in top[:3]:
            if not isinstance(ta, dict):
                continue
            act = ta.get("activity") or ta.get("feature")
            score = ta.get("score")
            if not act:
                continue
            if act not in activity_best or (score is not None and score > activity_best[act][1]):
                activity_best[act] = (month_name or "", int(score) if score is not None else 0)
    return [f"{act} ({info[0]} {info[1]})" if info[0] or info[1] else act for act, info in list(activity_best.items())]


def _format_best_months(best_data: dict) -> list[str]:
    """Format find_best_month result into readable lines. Returns list of strings."""
    if not best_data or not isinstance(best_data, dict):
        return []
    rankings = best_data.get("rankings") or best_data.get("ranking")
    if not isinstance(rankings, list) or len(rankings) == 0:
        return []
    lines = []
    for r in rankings[:5]:
        if not isinstance(r, dict):
            continue
        month_name = r.get("month_name") or r.get("month") or "—"
        avg_temp = r.get("avg_temp") or r.get("avg_temp_c")
        precip = r.get("precipitation") or r.get("total_rain_mm")
        score = r.get("score")
        part = month_name
        if avg_temp is not None:
            part += f" {avg_temp}°C"
        if precip is not None:
            part += f", {precip} mm rain"
        if score is not None:
            part += f" (score {score})"
        lines.append(part)
    return lines if lines else []


def _format_best_time_tip(
    best_months_data: dict | None,
    seasonal_calendar_data: dict | None,
) -> str | None:
    """Build a one-line tip for best time to visit: the month(s) with highest temperature through the year."""
    def _tip_with_temp_rain(month_name: str, avg_temp=None, rain=None) -> str:
        parts = [f"Best time: {month_name}"]
        if avg_temp is not None or rain is not None:
            details = []
            if avg_temp is not None:
                details.append(f"avg {int(round(avg_temp))}°C")
            if rain is not None:
                details.append(f"rain {int(round(rain))} mm")
            if details:
                parts.append(" — " + ", ".join(details))
        else:
            parts.append(" — warmest month")
        return "".join(parts) + "."

    if best_months_data and isinstance(best_months_data, dict):
        rankings = best_months_data.get("rankings") or best_months_data.get("ranking")
        if isinstance(rankings, list) and len(rankings) > 0:
            # Pick the month with highest temperature (ignore API score order)
            with_temp = []
            for r in rankings:
                if not isinstance(r, dict):
                    continue
                name = r.get("month_name") or r.get("month")
                avg_temp = r.get("avg_temp_c") or r.get("avg_temp")
                if name is not None and avg_temp is not None:
                    with_temp.append((float(avg_temp), name, r.get("total_rain_mm") or r.get("precipitation")))
            if with_temp:
                with_temp.sort(key=lambda x: x[0], reverse=True)
                hottest = with_temp[0]
                return _tip_with_temp_rain(hottest[1], hottest[0], hottest[2])
        best_month = best_months_data.get("best_month")
        if best_month:
            if isinstance(rankings, list):
                for r in rankings:
                    if isinstance(r, dict) and (r.get("month_name") or r.get("month")) == best_month:
                        avg_temp = r.get("avg_temp_c") or r.get("avg_temp")
                        rain = r.get("total_rain_mm") or r.get("precipitation")
                        return _tip_with_temp_rain(best_month, avg_temp, rain)
            return _tip_with_temp_rain(best_month, None, None)
    if seasonal_calendar_data and isinstance(seasonal_calendar_data, dict):
        calendar = seasonal_calendar_data.get("calendar")
        if isinstance(calendar, list) and len(calendar) >= 1:
            candidates = []
            for entry in calendar[:12]:
                if not isinstance(entry, dict):
                    continue
                weather = entry.get("weather") if isinstance(entry.get("weather"), dict) else {}
                avg_temp = weather.get("avg_temp") or weather.get("temperature_mean")
                precip = weather.get("total_precipitation_mm")
                month_name = entry.get("month_name") or entry.get("month")
                if month_name is not None and avg_temp is not None:
                    candidates.append((float(avg_temp), float(precip) if precip is not None else 0, str(month_name)))
            if candidates:
                # Highest temperature through the year (sort by temp only, descending)
                candidates.sort(key=lambda x: x[0], reverse=True)
                c = candidates[0]
                return _tip_with_temp_rain(c[2], c[0], c[1] if c[1] else None)
    return None


def _format_nearby_destinations(nearby_data: dict) -> list[str]:
    """Format find_nearby_destinations result. Returns list of 'City (Country) Xkm' lines."""
    if not nearby_data or not isinstance(nearby_data, dict):
        return []
    destinations = nearby_data.get("nearby_destinations")
    if not isinstance(destinations, list):
        return []
    lines = []
    for d in destinations[:8]:
        if not isinstance(d, dict):
            continue
        city = d.get("city") or d.get("name") or "—"
        country = d.get("country") or ""
        dist = d.get("distance_km")
        part = f"{city}"
        if country:
            part += f" ({country})"
        if dist is not None:
            part += f" {int(dist)} km"
        lines.append(part)
    return lines


def _format_similar_cities(similar_data: dict) -> list[str]:
    """Format find_similar_cities result. Returns list of 'City (Country) score%' lines."""
    if not similar_data or not isinstance(similar_data, dict):
        return []
    destinations = similar_data.get("similar_destinations")
    if not isinstance(destinations, list):
        return []
    lines = []
    for d in destinations[:8]:
        if not isinstance(d, dict):
            continue
        city = d.get("city") or d.get("name") or "—"
        country = d.get("country") or ""
        score = d.get("similarity_score") or d.get("score")
        part = f"{city}"
        if country:
            part += f" ({country})"
        if score is not None:
            part += f" {int(score)}%"
        lines.append(part)
    return lines


def _format_seasonal_calendar(cal_data: dict) -> list[str]:
    """Format get_seasonal_calendar into compact lines: month, avg temp, top activity."""
    if not cal_data or not isinstance(cal_data, dict):
        return []
    calendar = cal_data.get("calendar")
    if not isinstance(calendar, list) or len(calendar) == 0:
        return []
    lines = []
    for entry in calendar[:12]:
        if not isinstance(entry, dict):
            continue
        month_name = entry.get("month_name") or entry.get("month") or "—"
        weather = entry.get("weather") if isinstance(entry.get("weather"), dict) else {}
        avg_temp = weather.get("avg_temp") or weather.get("temperature_mean")
        top_activities = entry.get("top_activities") or []
        first_activity = top_activities[0] if top_activities and isinstance(top_activities[0], dict) else None
        activity_name = first_activity.get("activity") if first_activity else None
        part = month_name
        if avg_temp is not None and isinstance(avg_temp, (int, float)):
            part += f" {avg_temp}°C"
        elif isinstance(weather.get("total_precipitation_mm"), (int, float)):
            part += f" {weather.get('total_precipitation_mm')} mm"
        if activity_name:
            part += f" — {activity_name}"
        lines.append(part)
    return lines


def _format_dataset_stats(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    lines = []
    if "cities" in data:
        lines.append(f"Cities: {data['cities']}")
    if "countries" in data:
        lines.append(f"Countries: {data['countries']}")
    if "attractions" in data:
        lines.append(f"Attractions: {data['attractions']}")
    return lines


def _format_plan_trip(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    destinations = data.get("destinations") or []
    if not isinstance(destinations, list):
        return []
    return [
        f"{d.get('city', '—')} ({d.get('country', '')}) ~${d.get('daily_budget_usd', '')}/day"
        for d in destinations[:10] if isinstance(d, dict)
    ]


def _format_compare_cities(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    comps = data.get("comparisons") or []
    if not isinstance(comps, list):
        return []
    lines = []
    for c in comps:
        if not isinstance(c, dict):
            continue
        city = c.get("city") or c.get("name") or "—"
        w = c.get("weather") if isinstance(c.get("weather"), dict) else {}
        temp = w.get("avg_temp")
        budget = c.get("daily_budget_usd")
        part = city
        if temp is not None:
            part += f" {temp}°C"
        if budget is not None:
            part += f" ~${int(budget)}/day"
        lines.append(part)
    return lines


def _format_search_destinations(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    destinations = data.get("destinations") or []
    if not isinstance(destinations, list):
        return []
    return [
        f"{d.get('name', d.get('city', '—'))} ({d.get('country', '')}) ~${d.get('daily_budget_usd', '')}/day"
        for d in destinations[:10] if isinstance(d, dict)
    ]


def _format_search_by_activity(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    destinations = data.get("destinations") or []
    if not isinstance(destinations, list):
        return []
    activity = data.get("activity") or "activity"
    lines = [f"Top {activity}:"]
    for d in destinations[:8]:
        if not isinstance(d, dict):
            continue
        city = d.get("city") or d.get("name") or "—"
        country = d.get("country") or ""
        score = d.get("score")
        part = f"  {city} ({country})" + (f" {score}" if score is not None else "")
        lines.append(part)
    return lines


def _format_multi_activity_search(data: dict | None) -> list[str]:
    if not data or not isinstance(data, dict):
        return []
    destinations = data.get("destinations") or []
    if not isinstance(destinations, list):
        return []
    activities = data.get("activities_required") or data.get("activities") or []
    act_str = " + ".join(activities) if isinstance(activities, list) else ""
    lines = [f"Destinations for {act_str}:"] if act_str else []
    for d in destinations[:8]:
        if not isinstance(d, dict):
            continue
        city = d.get("city") or d.get("name") or "—"
        country = d.get("country") or ""
        lines.append(f"  {city} ({country})")
    return lines


def _print_weather_attractions_text(
    dest_city: str,
    out_date: object,
    ret_date: object,
    weather_by_key: dict,
    attractions_by_dest: dict,
    city_profiles_by_dest: dict | None = None,
    best_months_by_dest: dict | None = None,
    similar_cities_by_dest: dict | None = None,
    seasonal_calendar_by_dest: dict | None = None,
    nearby_destinations_by_dest: dict | None = None,
) -> None:
    """Print destination info, weather, best months, similar cities, nearby, seasonal calendar and attractions for one trip."""
    start_iso = out_date.isoformat() if hasattr(out_date, "isoformat") else str(out_date)
    end_iso = ret_date.isoformat() if hasattr(ret_date, "isoformat") else str(ret_date)
    key = (dest_city, start_iso, end_iso)
    weather_list = weather_by_key.get(key) or []
    att_list = attractions_by_dest.get(dest_city) or []
    profiles = city_profiles_by_dest or {}
    best_months = best_months_by_dest or {}
    similar = similar_cities_by_dest or {}
    nearby = (nearby_destinations_by_dest or {}).get(dest_city) or {}
    calendar = seasonal_calendar_by_dest or {}
    profile_lines = _format_city_profile(profiles.get(dest_city) or {})
    best_lines = _format_best_months(best_months.get(dest_city) or {})
    similar_lines = _format_similar_cities(similar.get(dest_city) or {})
    nearby_lines = _format_nearby_destinations(nearby)
    activity_lines = _format_activities(profiles.get(dest_city) or {}) or _format_activities_from_calendar(calendar.get(dest_city) or {})
    calendar_lines = _format_seasonal_calendar(calendar.get(dest_city) or {})
    weather_lines = [s for w in weather_list[:7] if (s := _format_weather_item(w))]
    if profile_lines:
        print("   Destination:")
        for line in profile_lines:
            print(f"     {line}")
    if weather_lines:
        print("   Weather:")
        for line in weather_lines:
            print(f"     {line}")
    if best_lines:
        print("   Best months to visit:")
        for line in best_lines:
            print(f"     • {line}")
    if similar_lines:
        print("   Similar cities:")
        for line in similar_lines:
            print(f"     • {line}")
    if nearby_lines:
        print("   Nearby destinations:")
        for line in nearby_lines:
            print(f"     • {line}")
    if activity_lines:
        print("   Activities:")
        for line in activity_lines:
            print(f"     • {line}")
    if calendar_lines:
        print("   Seasonal calendar:")
        for line in calendar_lines:
            print(f"     {line}")
    if att_list:
        print("   Attractions:")
        for a in att_list:
            print(f"     • {_format_attraction_item(a)}")


def _add_weather_attractions_html(
    lines: list[str],
    dest_city: str,
    out_date: object,
    ret_date: object,
    weather_by_key: dict,
    attractions_by_dest: dict,
    city_profiles_by_dest: dict | None = None,
    best_months_by_dest: dict | None = None,
    similar_cities_by_dest: dict | None = None,
    nearby_destinations_by_dest: dict | None = None,
    seasonal_calendar_by_dest: dict | None = None,
) -> None:
    """Append destination, weather, best months, similar cities, nearby, seasonal calendar and attractions blocks to lines (HTML)."""
    start_iso = out_date.isoformat() if hasattr(out_date, "isoformat") else str(out_date)
    end_iso = ret_date.isoformat() if hasattr(ret_date, "isoformat") else str(ret_date)
    key = (dest_city, start_iso, end_iso)
    weather_list = weather_by_key.get(key) or []
    att_list = attractions_by_dest.get(dest_city) or []
    profiles = city_profiles_by_dest or {}
    best_months = best_months_by_dest or {}
    similar = similar_cities_by_dest or {}
    nearby = nearby_destinations_by_dest or {}
    calendar = seasonal_calendar_by_dest or {}
    profile_lines = _format_city_profile(profiles.get(dest_city) or {})
    best_lines = _format_best_months(best_months.get(dest_city) or {})
    similar_lines = _format_similar_cities(similar.get(dest_city) or {})
    nearby_lines = _format_nearby_destinations(nearby.get(dest_city) or {})
    activity_lines = _format_activities(profiles.get(dest_city) or {}) or _format_activities_from_calendar(calendar.get(dest_city) or {})
    calendar_lines = _format_seasonal_calendar(calendar.get(dest_city) or {})
    weather_lines = [s for w in weather_list[:7] if (s := _format_weather_item(w))]
    tip_line = _format_best_time_tip(best_months.get(dest_city), calendar.get(dest_city))
    if profile_lines:
        lines.append("    <div class=\"destination\">")
        lines.append("      <div class=\"destination-title\">Destination</div>")
        for line in profile_lines:
            lines.append(f"      <div>{html.escape(line)}</div>")
        lines.append("    </div>")
    if weather_lines or tip_line:
        lines.append("    <div class=\"weather\">")
        lines.append("      <div class=\"weather-title\">Weather</div>")
        if weather_lines:
            first_line = weather_lines[0]
            if tip_line:
                first_line = f"{first_line} (tip: {tip_line})"
            lines.append(f"      <div>{html.escape(first_line)}</div>")
            for line in weather_lines[1:]:
                lines.append(f"      <div>{html.escape(line)}</div>")
        elif tip_line:
            lines.append(f"      <div>{html.escape(tip_line)}</div>")
        lines.append("    </div>")
    if best_lines:
        lines.append("    <div class=\"best-months\">")
        lines.append("      <div class=\"best-months-title\">Best months to visit</div>")
        for line in best_lines:
            lines.append(f"      <div>{html.escape(line)}</div>")
        lines.append("    </div>")
    if activity_lines:
        lines.append("    <div class=\"activities\">")
        lines.append("      <div class=\"activities-title\">Activities</div>")
        line = " . ".join(html.escape(l) for l in activity_lines)
        lines.append(f"      <div>{line}</div>")
        lines.append("    </div>")
    if att_list:
        lines.append("    <div class=\"attractions\">")
        lines.append("      <div class=\"attractions-title\">Attractions</div>")
        names = [_format_attraction_item(a) for a in att_list]
        line = " . ".join(html.escape(n) for n in names)
        lines.append(f"      <div>{line}</div>")
        lines.append("    </div>")


def _build_html(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
    adults: int = 2,
    travel_data: dict | None = None,
    timings: dict | None = None,
    num_cheapest_trips: int = 100,
    days_ahead: int = 90,
    summary_only: bool = False,
) -> str:
    """Build results as HTML string (same content as --html file)."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "Fly cheap, stay cheap — your daily Ryanair + Trivago deals"
    tagline = f"Top {num_cheapest_trips} round trips from Weeze, Köln & Dortmund (Wed eve / Thu eve / Fri) over the next {days_ahead} days. Lowest hotel rates from Trivago. Weekend getaways in 2–4 nights."
    weather_by_key = (travel_data or {}).get("weather") or {}
    attractions_by_dest = (travel_data or {}).get("attractions") or {}
    city_profiles_by_dest = (travel_data or {}).get("city_profiles") or {}
    best_months_by_dest = (travel_data or {}).get("best_months") or {}
    similar_cities_by_dest = (travel_data or {}).get("similar_cities") or {}
    nearby_destinations_by_dest = (travel_data or {}).get("nearby_destinations") or {}
    seasonal_calendar_by_dest = (travel_data or {}).get("seasonal_calendar") or {}
    dataset_stats = (travel_data or {}).get("dataset_stats")
    plan_trip_result = (travel_data or {}).get("plan_trip_result")
    compare_cities_result = (travel_data or {}).get("compare_cities_result")
    search_destinations_result = (travel_data or {}).get("search_destinations_result")
    search_by_activity_result = (travel_data or {}).get("search_by_activity_result")
    multi_activity_search_result = (travel_data or {}).get("multi_activity_search_result")
    # Styles inspired by Trivago offer email: #cdcdcd background, white cards, #008513 accent, rounded corners
    lines = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "  <meta charset=\"utf-8\">",
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
        f"  <title>{html.escape(title)}</title>",
        "  <style>",
        "    body { margin: 0; padding: 1rem; background-color: #cdcdcd; font-family: Arial, Helvetica, sans-serif; -webkit-font-smoothing: antialiased; }",
        "    .main-wrap { max-width: 700px; margin: 0 auto; background: #ffffff; padding: 2rem; box-shadow: 0 4px 16px rgba(0,0,0,0.2); border-radius: 16px; border: 1px solid #cdcdcd; }",
        "    h1 { font-size: 1.75rem; font-weight: 700; color: #000; margin-bottom: 0.25rem; line-height: 1.3; }",
        "    .tagline { color: #555; font-size: 1rem; line-height: 1.5; margin-bottom: 1.25rem; }",
        "    .trip { margin: 1rem 0; padding: 1.25rem; background: #fff; border: 1px solid #cdcdcd; border-radius: 16px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); }",
        "    .trip-header { font-weight: 700; font-size: 1.125rem; margin-bottom: 0.5rem; color: #000; }",
        "    .trip-details { color: #444; font-size: 0.95rem; line-height: 1.5; }",
        "    a.trip-link { color: #008513; text-decoration: none; }",
        "    a.trip-link:hover { text-decoration: underline; }",
        "    .hotel { margin: 0.35rem 0; }",
        "    .hotel a { color: #008513; }",
        "    .flight-title, .weather-title, .attractions-title, .hotels-title, .destination-title, .best-months-title, .similar-cities-title, .nearby-destinations-title, .activities-title, .seasonal-calendar-title, .tip-title { font-weight: 700; font-size: 1rem; margin-bottom: 0.25rem; color: #000; }",
        "    .flight, .weather, .attractions, .hotels, .destination, .best-months, .similar-cities, .nearby-destinations, .activities, .seasonal-calendar, .tip { margin-top: 0.5rem; font-size: 0.9rem; color: #444; line-height: 1.5; }",
        "    .flight-option { margin-bottom: 0.5rem; padding: 0.5rem 0.75rem; border: 1px solid #cdcdcd; border-radius: 8px; background: #fafafa; }",
        "    .flight-option-cheapest { font-weight: bold; }",
        "    .global-section { margin-top: 1.5rem; padding: 1rem 1.25rem; border: 1px solid #cdcdcd; border-radius: 16px; background: #ffffff; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }",
        "    .global-section-title { font-weight: 700; font-size: 1rem; margin-bottom: 0.4rem; color: #008513; }",
        "    .timings-note { margin-top: 2rem; font-size: 0.85rem; color: #666; }",
        "    .preheader { font-size: 0.95rem; color: #555; margin-bottom: 1rem; line-height: 1.4; }",
        "    .intro { color: #333; font-size: 1rem; line-height: 1.5; margin-bottom: 1.25rem; }",
        "    .section-heading { font-size: 1.125rem; font-weight: 700; color: #000; margin: 1.5rem 0 0.75rem 0; }",
        "    .deals-summary-box { margin-bottom: 1rem; padding: 0.75rem 1rem; border: 1px solid #cdcdcd; border-radius: 8px; background: #fafafa; }",
        "    .deals-summary-heading { font-size: 1rem; font-weight: 700; color: #000; margin-bottom: 0.5rem; }",
        "    .deals-summary { font-size: 0.95rem; font-weight: bold; color: #444; line-height: 1.6; margin: 0; }",
        "    a.deals-summary-link { color: #008513; text-decoration: none; }",
        "    a.deals-summary-link:hover { text-decoration: underline; }",
        "    .deals-summary-table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }",
        "    .deals-summary-table th, .deals-summary-table td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #e0e0e0; }",
        "    .deals-summary-table th { font-weight: 700; color: #000; background: #f5f5f5; }",
        "    .deals-summary-table td:last-child { white-space: nowrap; }",
        "    .footer-note { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #e0e0e0; font-size: 0.85rem; color: #666; line-height: 1.5; }",
        "    @media screen and (max-width: 600px) {",
        "      body { padding: 0; -webkit-text-size-adjust: 100%; }",
        "      .main-wrap { margin: 0; padding: 16px 12px; border-radius: 0; border-left: none; border-right: none; max-width: 100%; box-sizing: border-box; }",
        "      .trip { margin: 12px 0; padding: 14px 12px; border-radius: 12px; }",
        "      .global-section { margin-top: 1.25rem; padding: 14px 12px; border-radius: 12px; }",
        "      .section-heading { margin: 1.25rem 0 0.5rem 0; }",
        "      .footer-note { margin-top: 1.5rem; padding: 12px 0 0 0; }",
        "    }",
        "  </style>",
        "</head>",
        "<body>",
        "  <div class=\"main-wrap\">",
        "  <!-- Duplicate style in body so email clients that ignore head still apply Trivago-style layout -->",
        "  <style>",
        "    body { margin: 0; padding: 1rem; background-color: #cdcdcd; font-family: Arial, Helvetica, sans-serif; -webkit-font-smoothing: antialiased; }",
        "    .main-wrap { max-width: 700px; margin: 0 auto; background: #ffffff; padding: 2rem; box-shadow: 0 4px 16px rgba(0,0,0,0.2); border-radius: 16px; border: 1px solid #cdcdcd; }",
        "    h1 { font-size: 1.75rem; font-weight: 700; color: #000; margin-bottom: 0.25rem; line-height: 1.3; }",
        "    .tagline { color: #555; font-size: 1rem; line-height: 1.5; margin-bottom: 0.75rem; }",
        "    .trip { margin: 1rem 0; padding: 1.25rem; background: #fff; border: 1px solid #cdcdcd; border-radius: 16px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); }",
        "    .trip-header { font-weight: 700; font-size: 1.125rem; margin-bottom: 0.5rem; color: #000; }",
        "    .trip-details { color: #444; font-size: 0.95rem; line-height: 1.5; }",
        "    a.trip-link { color: #008513; text-decoration: none; }",
        "    a.trip-link:hover { text-decoration: underline; }",
        "    .hotel { margin: 0.35rem 0; }",
        "    .hotel a { color: #008513; }",
        "    .flight-title, .weather-title, .attractions-title, .hotels-title, .destination-title, .best-months-title, .similar-cities-title, .nearby-destinations-title, .activities-title, .seasonal-calendar-title, .tip-title { font-weight: 700; font-size: 1rem; margin-bottom: 0.25rem; color: #000; }",
        "    .flight, .weather, .attractions, .hotels, .destination, .best-months, .similar-cities, .nearby-destinations, .activities, .seasonal-calendar, .tip { margin-top: 0.5rem; font-size: 0.9rem; color: #444; line-height: 1.5; }",
        "    .flight-option { margin-bottom: 0.5rem; padding: 0.5rem 0.75rem; border: 1px solid #cdcdcd; border-radius: 8px; background: #fafafa; }",
        "    .flight-option-cheapest { font-weight: bold; }",
        "    .global-section { margin-top: 1.5rem; padding: 1rem 1.25rem; border: 1px solid #cdcdcd; border-radius: 16px; background: #ffffff; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }",
        "    .global-section-title { font-weight: 700; font-size: 1rem; margin-bottom: 0.4rem; color: #008513; }",
        "    .timings-note { margin-top: 2rem; font-size: 0.85rem; color: #666; }",
        "    .preheader { font-size: 0.95rem; color: #555; margin-bottom: 1rem; line-height: 1.4; }",
        "    .intro { color: #333; font-size: 1rem; line-height: 1.5; margin-bottom: 1.25rem; }",
        "    .section-heading { font-size: 1.125rem; font-weight: 700; color: #000; margin: 1.5rem 0 0.75rem 0; }",
        "    .deals-summary-box { margin-bottom: 1rem; padding: 0.75rem 1rem; border: 1px solid #cdcdcd; border-radius: 8px; background: #fafafa; }",
        "    .deals-summary-heading { font-size: 1rem; font-weight: 700; color: #000; margin-bottom: 0.5rem; }",
        "    .deals-summary { font-size: 0.95rem; font-weight: bold; color: #444; line-height: 1.6; margin: 0; }",
        "    a.deals-summary-link { color: #008513; text-decoration: none; }",
        "    a.deals-summary-link:hover { text-decoration: underline; }",
        "    .deals-summary-table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }",
        "    .deals-summary-table th, .deals-summary-table td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #e0e0e0; }",
        "    .deals-summary-table th { font-weight: 700; color: #000; background: #f5f5f5; }",
        "    .deals-summary-table td:last-child { white-space: nowrap; }",
        "    .footer-note { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #e0e0e0; font-size: 0.85rem; color: #666; line-height: 1.5; }",
        "    @media screen and (max-width: 600px) {",
        "      body { padding: 0; -webkit-text-size-adjust: 100%; }",
        "      .main-wrap { margin: 0; padding: 16px 12px; border-radius: 0; border-left: none; border-right: none; max-width: 100%; box-sizing: border-box; }",
        "      .trip { margin: 12px 0; padding: 14px 12px; border-radius: 12px; }",
        "      .global-section { margin-top: 1.25rem; padding: 14px 12px; border-radius: 12px; }",
        "      .section-heading { margin: 1.25rem 0 0.5rem 0; }",
        "      .footer-note { margin-top: 1.5rem; padding: 12px 0 0 0; }",
        "    }",
        "  </style>",
    ]
    # Preheader (shows in email preview) and intro (count = aggregated groups)
    agg_hotel = _aggregate_hotel_results(hotel_results) if hotel_results else []
    agg_flights = _aggregate_cheapest_flights(cheapest_flights) if not hotel_results and cheapest_flights else []
    num_deals = len(agg_hotel) if agg_hotel else len(agg_flights)
    if num_deals:
        lines.append(f"  <p class=\"preheader\">Your daily flight + hotel deals from Weeze, Köln &amp; Dortmund — next {days_ahead} days, {num_deals} deal{'s' if num_deals != 1 else ''} inside.</p>")
        lines.append(f"  <p class=\"intro\">Here are today's top deals (up to {num_cheapest_trips} round trips, {days_ahead}-day window). Click any flight or hotel link to compare and book.</p>")
    else:
        lines.append(f"  <p class=\"preheader\">Your daily flight + hotel deals from Weeze, Köln &amp; Dortmund — next {days_ahead} days.</p>")
    lines.extend([
        f"  <h1>{html.escape(title)}</h1>",
        f"  <p class=\"tagline\">{html.escape(tagline)}</p>",
    ])
    if agg_hotel:
        summary_rows = []
        for g in agg_hotel:
            dest_city = g["destination"]
            days, nights = g["days"], g["nights"]
            min_total = g["min_total"]
            n = len(g["trips"])
            slug = _anchor_slug(dest_city, days, nights)
            link = f'<a href="#{html.escape(slug)}" class="deals-summary-link">{html.escape(dest_city)}</a>'
            deals_str = f"{n} deal{'s' if n != 1 else ''}"
            summary_rows.append(f"    <tr><td>{link}</td><td>{deals_str}</td><td>{min_total:.2f}€</td></tr>")
        lines.append("  <div class=\"deals-summary-box\">")
        lines.append("  <div class=\"deals-summary-heading\">Summary</div>")
        lines.append("  <table class=\"deals-summary-table\"><thead><tr><th>Destination</th><th>Deals</th><th>From</th></tr></thead><tbody>")
        lines.extend(summary_rows)
        lines.append("  </tbody></table>")
        lines.append("  </div>")
        lines.append("  <h2 class=\"section-heading\">Top deals (flight + hotel)</h2>")
        for g in agg_hotel:
            dest_city = g["destination"]
            days, nights = g["days"], g["nights"]
            min_total = g["min_total"]
            first = g["trips"][0]
            out_date = first["flight"].departureTime.date()
            ret_date = first["return_flight"].departureTime.date()
            route_label = _flight_route_label(first["flight"], first["return_flight"])
            slug = _anchor_slug(dest_city, days, nights)
            lines.append(f"  <div class=\"trip\" id=\"{html.escape(slug)}\">")
            lines.append(f"    <div class=\"trip-header\">{html.escape(dest_city)} (from {min_total:.2f}€) — {days} days, {nights} nights</div>")
            today = datetime.today().date()
            future_trips = [r for r in g["trips"] if r["flight"].departureTime.date() >= today]
            if not future_trips:
                continue
            lines.append("    <div class=\"flight\">")
            lines.append(f"      <div class=\"flight-title\">Flight{html.escape(route_label)}</div>")
            trips_to_show = future_trips[:1] if summary_only else sorted(future_trips, key=lambda r: r["flight"].departureTime)
            cheapest_total = min(r["flight"].price + r["return_flight"].price for r in trips_to_show) if trips_to_show else 0
            for r in trips_to_show:
                outbound = r["flight"]
                ret = r["return_flight"]
                out_weekday = outbound.departureTime.strftime("%Y-%m-%d %A %H:%M")
                ret_weekday = ret.departureTime.strftime("%Y-%m-%d %A %H:%M")
                out_dur = _flight_duration_str(outbound.origin, outbound.destination)
                ret_dur = _flight_duration_str(ret.origin, ret.destination)
                out_leg = f"{out_weekday}{out_dur}  {outbound.price}€  {_display_airport(outbound.origin)}→{_display_airport(outbound.destination)}"
                ret_leg = f"{ret_weekday}{ret_dur}  {ret.price}€  {_display_airport(ret.origin)}→{_display_airport(ret.destination)}"
                urls = _booking_urls_for_trip(outbound, ret, adults)
                first_hotel = r["hotels"][0] if r["hotels"] else {}
                hotel_name = first_hotel.get("Accommodation Name") or first_hotel.get("accommodation_name") or "—"
                hotel_url = first_hotel.get("Accommodation URL") or first_hotel.get("accommodation_url") or ""
                hotel_price = first_hotel.get("Price Per Stay") or first_hotel.get("price_per_stay") or ""
                hotel_part = f"  |  <a class=\"trip-details trip-link\" href=\"{html.escape(hotel_url)}\" target=\"_blank\" rel=\"noopener\">{html.escape(hotel_name)}</a> {html.escape(hotel_price)}" if hotel_url else (f"  |  {html.escape(hotel_name)} {html.escape(hotel_price)}" if hotel_name or hotel_price else "")
                trip_total = outbound.price + ret.price
                opt_class = "flight-option flight-option-cheapest" if trip_total == cheapest_total else "flight-option"
                lines.append(f"      <div class=\"{opt_class}\">")
                if "booking_url" in urls:
                    lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}  |  {html.escape(ret_leg)}</a> ({trip_total:.2f}€){hotel_part}")
                else:
                    lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url_outbound'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}</a>  |  <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url_return'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(ret_leg)}</a> ({trip_total:.2f}€){hotel_part}")
                lines.append("      </div>")
            lines.append("    </div>")
            _add_weather_attractions_html(lines, dest_city, out_date, ret_date, weather_by_key, attractions_by_dest, city_profiles_by_dest, best_months_by_dest, similar_cities_by_dest, nearby_destinations_by_dest, seasonal_calendar_by_dest)
            lines.append("  </div>")
    elif agg_flights:
        summary_rows = []
        for dest_city, days, nights, flights in agg_flights:
            ob, ib, price = flights[0]
            min_total = price + ib.price
            n = len(flights)
            slug = _anchor_slug(dest_city, days, nights)
            link = f'<a href="#{html.escape(slug)}" class="deals-summary-link">{html.escape(dest_city)}</a>'
            deals_str = f"{n} deal{'s' if n != 1 else ''}"
            summary_rows.append(f"    <tr><td>{link}</td><td>{deals_str}</td><td>{min_total:.2f}€</td></tr>")
        lines.append("  <div class=\"deals-summary-box\">")
        lines.append("  <div class=\"deals-summary-heading\">Summary</div>")
        lines.append("  <table class=\"deals-summary-table\"><thead><tr><th>Destination</th><th>Deals</th><th>From</th></tr></thead><tbody>")
        lines.extend(summary_rows)
        lines.append("  </tbody></table>")
        lines.append("  </div>")
        lines.append("  <h2 class=\"section-heading\">Top deals (flights only)</h2>")
        for dest_city, days, nights, flights in agg_flights:
            ob, ib, price = flights[0]
            min_total = price + ib.price
            out_date = ob.departureTime.date()
            ret_date = ib.departureTime.date()
            route_label = _flight_route_label(ob, ib)
            slug = _anchor_slug(dest_city, days, nights)
            lines.append(f"  <div class=\"trip\" id=\"{html.escape(slug)}\">")
            lines.append(f"    <div class=\"trip-header\">{html.escape(dest_city)} (from {min_total:.2f}€) — {days} days, {nights} nights</div>")
            today = datetime.today().date()
            future_flights = [(ob, ib, price) for ob, ib, price in flights if ob.departureTime.date() >= today]
            if not future_flights:
                continue
            lines.append("    <div class=\"flight\">")
            lines.append(f"      <div class=\"flight-title\">Flight{html.escape(route_label)}</div>")
            flights_to_show = future_flights[:1] if summary_only else sorted(future_flights, key=lambda x: x[0].departureTime)
            cheapest_total = min(price + ib.price for ob, ib, price in flights_to_show) if flights_to_show else 0
            for ob, ib, price in flights_to_show:
                out_weekday = ob.departureTime.strftime("%Y-%m-%d %A %H:%M")
                ret_weekday = ib.departureTime.strftime("%Y-%m-%d %A %H:%M")
                out_dur = _flight_duration_str(ob.origin, ob.destination)
                ret_dur = _flight_duration_str(ib.origin, ib.destination)
                out_leg = f"{out_weekday}{out_dur}  {price}€  {_display_airport(ob.origin)}→{_display_airport(ob.destination)}"
                ret_leg = f"{ret_weekday}{ret_dur}  {ib.price}€  {_display_airport(ib.origin)}→{_display_airport(ib.destination)}"
                urls = _booking_urls_for_trip(ob, ib, adults)
                trip_total = price + ib.price
                opt_class = "flight-option flight-option-cheapest" if trip_total == cheapest_total else "flight-option"
                lines.append(f"      <div class=\"{opt_class}\">")
                if "booking_url" in urls:
                    lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}  |  {html.escape(ret_leg)}</a> ({trip_total:.2f}€)")
                else:
                    lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url_outbound'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}</a>  |  <a class=\"trip-details trip-link\" href=\"{html.escape(urls['booking_url_return'])}\" target=\"_blank\" rel=\"noopener\">{html.escape(ret_leg)}</a> ({trip_total:.2f}€)")
                lines.append("      </div>")
            lines.append("    </div>")
            _add_weather_attractions_html(lines, dest_city, out_date, ret_date, weather_by_key, attractions_by_dest, city_profiles_by_dest, best_months_by_dest, similar_cities_by_dest, nearby_destinations_by_dest, seasonal_calendar_by_dest)
            lines.append("  </div>")
    if not agg_hotel and not agg_flights:
        lines.append("  <p>(No round trips found.)</p>")
    # Global GeoTemp sections
    for title, data, formatter in [
        ("Dataset", dataset_stats, _format_dataset_stats),
        ("Trip ideas", plan_trip_result, _format_plan_trip),
        ("Compare destinations", compare_cities_result, _format_compare_cities),
        ("More destinations", search_destinations_result, _format_search_destinations),
        ("Top city break", search_by_activity_result, _format_search_by_activity),
        ("Beach & swimming", multi_activity_search_result, _format_multi_activity_search),
    ]:
        section_lines = formatter(data) if formatter else []
        if section_lines:
            lines.append("  <div class=\"global-section\">")
            lines.append(f"    <div class=\"global-section-title\">{html.escape(title)}</div>")
            for line in section_lines:
                lines.append(f"    <div>{html.escape(line)}</div>")
            lines.append("  </div>")
    lines.append("  <p class=\"footer-note\">")
    lines.append(f"    Report generated on {html.escape(generated_at)}.")
    if timings:
        total_s = timings.get("total") or 0
        flights_s = timings.get("flights") or 0
        weather_s = timings.get("weather_attractions") or 0
        hotels_s = timings.get("hotels") or 0
        lines.append(f"    Total run: {total_s:.1f}s (flights {flights_s:.1f}s, weather &amp; attractions {weather_s:.1f}s, hotels {hotels_s:.1f}s).")
    lines.append("    Reply to this email if you have questions.")
    lines.append("  </p>")
    lines.append("  </div>")
    lines.append("</body>")
    lines.append("</html>")
    return "\n".join(lines)


def _print_html(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
    adults: int = 2,
    travel_data: dict | None = None,
    timings: dict | None = None,
    num_cheapest_trips: int = 100,
    days_ahead: int = 90,
    summary_only: bool = False,
) -> None:
    """Write results to travel_helper.html and print path."""
    html_str = _build_html(
        cheapest_flights, hotel_results, adults, travel_data, timings,
        num_cheapest_trips=num_cheapest_trips, days_ahead=days_ahead,
        summary_only=summary_only,
    )
    filename = "travel_helper.html"
    path = Path(filename).resolve()
    path.write_text(html_str, encoding="utf-8")
    print(path, file=sys.stderr)
    if timings:
        total_s = timings.get("total") or 0
        flights_s = timings.get("flights") or 0
        weather_s = timings.get("weather_attractions") or 0
        hotels_s = timings.get("hotels") or 0
        print(f"Total execution time: {total_s:.1f}s. Flights: {flights_s:.1f}s, Weather & attractions: {weather_s:.1f}s, Hotels: {hotels_s:.1f}s.", file=sys.stderr)


def _send_email_html(html_body: str, to_email: str, subject: str | None = None) -> None:
    """Send HTML email via Gmail. Requires env GMAIL_USER and GMAIL_APP_PASSWORD."""
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        print("Cannot send email: set GMAIL_USER and GMAIL_APP_PASSWORD environment variables.", file=sys.stderr)
        return
    if subject is None:
        subject = f"Fly cheap, stay cheap — your Ryanair + Trivago deals {datetime.now().strftime('%Y-%m-%d')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, to_email, msg.as_string())
        print(f"Email sent to {to_email}", file=sys.stderr)
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)


def collect_outbound_flights(days_ahead: int | None = None) -> list[tuple[object, object, float]]:
    """Collect return trips from Weeze/Köln/Dortmund. Only the departure must match: Wed after 6pm, Thu after 5pm, or Fri after 11am.
    Return is 2–4 nights later (any time of day). Includes:
    - Same-airport round trips: NRN↔NRN, CGN↔CGN, DTM↔DTM.
    - Open-jaw: outbound from A, return to B (A≠B), e.g. NRN→dest→CGN, CGN→dest→DTM, etc.
    Returns list of (outbound, return_flight, outbound_price).
    """
    api = Ryanair(currency="EUR")
    outbound = []
    n_days = days_ahead if days_ahead is not None else DAYS_AHEAD

    # 1) Same-airport round trips (existing logic)
    for airport_code, airport_name in ORIGIN_AIRPORTS:
        for day_offset in range(0, n_days):
            search_date = datetime.today().date() + timedelta(days=day_offset)
            wd = search_date.weekday()
            if wd == WEDNESDAY:
                outbound_time_from, outbound_time_to = "18:00", "23:59"
            elif wd == THURSDAY:
                outbound_time_from, outbound_time_to = "17:00", "23:59"
            elif wd == FRIDAY:
                outbound_time_from, outbound_time_to = "11:00", "23:59"
            else:
                continue
            return_date_from = search_date + timedelta(days=RETURN_DAYS_MIN)
            return_date_to = search_date + timedelta(days=RETURN_DAYS_MAX)
            trips = api.get_cheapest_return_flights(
                airport_code,
                search_date, search_date,
                return_date_from, return_date_to,
                outbound_departure_time_from=outbound_time_from,
                outbound_departure_time_to=outbound_time_to,
            )
            if trips:
                for t in trips:
                    t._origin_airport = airport_name
                    t._origin_code = airport_code
                    ob = t.outbound
                    ob._origin_airport = t._origin_airport
                    ob._origin_code = t._origin_code
                    outbound.append((ob, t.inbound, ob.price))

    # 2) Open-jaw: outbound from A, return to B (A != B)
    airport_list = list(ORIGIN_AIRPORTS)
    for i, (out_code, out_name) in enumerate(airport_list):
        for j, (return_code, return_name) in enumerate(airport_list):
            if i == j:
                continue
            for day_offset in range(0, n_days):
                search_date = datetime.today().date() + timedelta(days=day_offset)
                wd = search_date.weekday()
                if wd == WEDNESDAY:
                    outbound_time_from, outbound_time_to = "18:00", "23:59"
                elif wd == THURSDAY:
                    outbound_time_from, outbound_time_to = "17:00", "23:59"
                elif wd == FRIDAY:
                    outbound_time_from, outbound_time_to = "11:00", "23:59"
                else:
                    continue
                return_date_from = search_date + timedelta(days=RETURN_DAYS_MIN)
                return_date_to = search_date + timedelta(days=RETURN_DAYS_MAX)
                outbounds_oneway = api.get_cheapest_flights(
                    out_code,
                    search_date,
                    search_date,
                    departure_time_from=outbound_time_from,
                    departure_time_to=outbound_time_to,
                )
                for ob in outbounds_oneway:
                    dest = ob.destination
                    returns_oneway = api.get_cheapest_flights(
                        dest,
                        return_date_from,
                        return_date_to,
                        destination_airport=return_code,
                    )
                    for ret in returns_oneway:
                        ob._origin_airport = out_name
                        ob._origin_code = out_code
                        outbound.append((ob, ret, ob.price))

    outbound.sort(key=lambda x: (x[2] + x[1].price, x[0].departureTime.date(), x[0].destination))
    return outbound


def run(
    output_json: bool = False,
    output_html: bool = False,
    fetch_hotels: bool = True,
    adults: int = 2,
    rooms: int = 1,
    num_cheapest_trips: int = 100,
    days_ahead: int | None = None,
    email: str | None = None,
    json_file: str | None = None,
    summary_only: bool = False,
) -> None:
    t_start = time.perf_counter()

    # 1. Collect return trips (only departure restricted: Wed after 6pm / Thu after 5pm / Fri after 11am; return 2–4 nights later, any time)
    print("Fetching flights from Ryanair...", file=sys.stderr)
    t0 = time.perf_counter()
    outbound_flights = collect_outbound_flights(days_ahead=days_ahead)
    t_flights = time.perf_counter() - t0
    same_airport = sum(1 for ob, ret, _ in outbound_flights if ob.origin == ret.destination)
    different_airport = len(outbound_flights) - same_airport
    print(f"{len(outbound_flights)} flights fetched (same airport ({same_airport}) - different airport ({different_airport}))", file=sys.stderr)
    # 2. Already sorted by price; take the N cheapest
    cheapest_flights = outbound_flights[:num_cheapest_trips]

    # 3. GeoTemp first (weather + attractions) so hotels can be chosen near attractions (walking distance)
    travel_data = None
    t_weather_attractions = 0.0
    if GEOTEMP_AVAILABLE and cheapest_flights:
        print("Fetching weather and attractions (GeoTemp)...", file=sys.stderr)
        try:
            t0 = time.perf_counter()
            travel_data = asyncio.run(_fetch_geotemp_for_trips(cheapest_flights, []))
            t_weather_attractions = time.perf_counter() - t0
        except Exception as e:
            print(f"GeoTemp fetch failed: {e}", file=sys.stderr)

    # 4. Fetch hotels (near attractions when GeoTemp data available; else by city)
    hotel_results = []
    t_hotels = 0.0
    if fetch_hotels and TRIVAGO_AVAILABLE and cheapest_flights:
        if not output_json:
            print("Fetching cheapest hotel per trip (near attractions when available)...", file=sys.stderr)
        t0 = time.perf_counter()
        hotel_results = asyncio.run(
            fetch_hotels_for_cheapest_flights(
                cheapest_flights,
                adults=adults,
                rooms=rooms,
                attractions_by_dest=(travel_data or {}).get("attractions") or {},
            )
        )
        t_hotels = time.perf_counter() - t0

    t_total = time.perf_counter() - t_start
    timings = {
        "total": t_total,
        "flights": t_flights,
        "weather_attractions": t_weather_attractions,
        "hotels": t_hotels,
    }

    if output_json:
        def _flight_leg_json(flight, price_eur: float | None = None) -> dict:
            dur = _flight_duration_for_json(flight.origin, flight.destination)
            leg = {
                "departure": flight.departureTime.isoformat(),
                "departure_time": flight.departureTime.strftime("%H:%M"),
                "duration_minutes": dur["duration_minutes"],
                "duration": dur["duration"],
                "flight_number": flight.flightNumber,
                "origin": flight.origin,
                "origin_full": flight.originFull,
                "destination": flight.destination,
                "destination_full": flight.destinationFull,
                "price_eur": price_eur if price_eur is not None else flight.price,
            }
            return leg

        def _geotemp_destination_info(
            td: dict | None,
            dest_city: str,
            start_iso: str,
            end_iso: str,
        ) -> dict:
            """Build per-destination GeoTemp block for JSON (weather, attractions, city_profile, etc.)."""
            if not td:
                return {}
            weather_key = (dest_city, start_iso, end_iso)
            return {
                "weather": (td.get("weather") or {}).get(weather_key),
                "attractions": (td.get("attractions") or {}).get(dest_city),
                "city_profile": (td.get("city_profiles") or {}).get(dest_city),
                "best_months": (td.get("best_months") or {}).get(dest_city),
                "similar_cities": (td.get("similar_cities") or {}).get(dest_city),
                "seasonal_calendar": (td.get("seasonal_calendar") or {}).get(dest_city),
                "nearby_destinations": (td.get("nearby_destinations") or {}).get(dest_city),
            }

        # Prefer hotel_results when present; otherwise output flight-only from cheapest_flights (aggregated by dest, days, nights).
        # Only include flights with outbound date >= today so booking links work on Ryanair.
        json_today = datetime.today().date()
        if hotel_results:
            agg = _aggregate_hotel_results(hotel_results)
            cheapest_with_hotels = []
            for g in agg:
                future_trips = [r for r in g["trips"] if r["flight"].departureTime.date() >= json_today]
                if not future_trips:
                    continue
                trips_sorted = future_trips[:1] if summary_only else sorted(future_trips, key=lambda r: r["flight"].departureTime)
                cheapest_with_hotels.append({
                    "destination": g["destination"],
                    "days": g["days"],
                    "nights": g["nights"],
                    "min_total_eur": round(g["min_total"], 2),
                    "flights": [
                        {
                            "outbound": _flight_leg_json(r["flight"], r["price"]),
                            "return": _flight_leg_json(r["return_flight"]),
                            **_booking_urls_for_trip(r["flight"], r["return_flight"], adults),
                            "hotel_arrival": r["arrival"],
                            "hotel_departure": r["departure"],
                            "hotels": r["hotels"],
                            "total_eur": round(r["price"] + r["return_flight"].price, 2),
                        }
                        for r in trips_sorted
                    ],
                    "destination_info": _geotemp_destination_info(
                        travel_data, g["destination"], g["trips"][0]["arrival"], g["trips"][0]["departure"]
                    ),
                })
            out = {"cheapest_flights_with_hotels": cheapest_with_hotels}
        else:
            agg = _aggregate_cheapest_flights(cheapest_flights)
            cheapest_flights_list = []
            for dest, days, nights, flights in agg:
                future_flights = [(ob, ib, price) for ob, ib, price in flights if ob.departureTime.date() >= json_today]
                if not future_flights:
                    continue
                flights_sorted = future_flights[:1] if summary_only else sorted(future_flights, key=lambda x: x[0].departureTime)
                ob0, ib0, price0 = flights_sorted[0]
                cheapest_flights_list.append({
                    "destination": dest,
                    "days": days,
                    "nights": nights,
                    "min_total_eur": round(price0 + ib0.price, 2),
                    "flights": [
                        {
                            "outbound": _flight_leg_json(ob, None),
                            "return": _flight_leg_json(ib),
                            **_booking_urls_for_trip(ob, ib, adults),
                            "total_eur": round(price + ib.price, 2),
                        }
                        for ob, ib, price in flights_sorted
                    ],
                    "destination_info": _geotemp_destination_info(
                        travel_data, dest,
                        ob0.departureTime.date().isoformat(),
                        ib0.departureTime.date().isoformat(),
                    ),
                })
            out = {"cheapest_flights": cheapest_flights_list}
        # Global GeoTemp data (dataset, trip ideas, compare, etc.)
        if travel_data:
            out["geotemp_global"] = {
                "dataset_stats": travel_data.get("dataset_stats"),
                "plan_trip_result": travel_data.get("plan_trip_result"),
                "compare_cities_result": travel_data.get("compare_cities_result"),
                "search_destinations_result": travel_data.get("search_destinations_result"),
                "search_by_activity_result": travel_data.get("search_by_activity_result"),
                "multi_activity_search_result": travel_data.get("multi_activity_search_result"),
            }
        path = json_file if json_file is not None else "travel_helper.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
        if not output_html:
            return

    if output_html:
        _print_html(
            cheapest_flights=cheapest_flights,
            hotel_results=hotel_results,
            adults=adults,
            travel_data=travel_data,
            timings=timings,
            num_cheapest_trips=num_cheapest_trips,
            days_ahead=days_ahead or 90,
            summary_only=summary_only,
        )
        if not email:
            return
    if email:
        html_str = _build_html(
            cheapest_flights=cheapest_flights,
            hotel_results=hotel_results,
            adults=adults,
            travel_data=travel_data,
            timings=timings,
            num_cheapest_trips=num_cheapest_trips,
            days_ahead=days_ahead or 90,
            summary_only=summary_only,
        )
        _send_email_html(html_str, email)
        if output_html:
            return
        # If only --email (no --html), we're done
        return

    # Human-readable output
    weather_by_key = (travel_data or {}).get("weather") or {}
    attractions_by_dest = (travel_data or {}).get("attractions") or {}
    city_profiles_by_dest = (travel_data or {}).get("city_profiles") or {}
    best_months_by_dest = (travel_data or {}).get("best_months") or {}
    similar_cities_by_dest = (travel_data or {}).get("similar_cities") or {}
    nearby_destinations_by_dest = (travel_data or {}).get("nearby_destinations") or {}
    seasonal_calendar_by_dest = (travel_data or {}).get("seasonal_calendar") or {}
    dataset_stats = (travel_data or {}).get("dataset_stats")
    plan_trip_result = (travel_data or {}).get("plan_trip_result")
    compare_cities_result = (travel_data or {}).get("compare_cities_result")
    search_destinations_result = (travel_data or {}).get("search_destinations_result")
    search_by_activity_result = (travel_data or {}).get("search_by_activity_result")
    multi_activity_search_result = (travel_data or {}).get("multi_activity_search_result")
    print("Fly cheap, stay cheap — Ryanair + Trivago deals from Weeze, Köln & Dortmund (Wed eve / Thu eve / Fri, 2–4 nights)")
    print("=" * 80)
    print("CHEAPEST ROUND TRIPS" + (" + HOTELS" if hotel_results else " (flights only)"))
    print("-" * 80)
    agg_hotel_print = _aggregate_hotel_results(hotel_results) if hotel_results else []
    agg_flights_print = _aggregate_cheapest_flights(cheapest_flights) if not hotel_results and cheapest_flights else []
    if agg_hotel_print:
        for i, g in enumerate(agg_hotel_print, 1):
            dest_city = g["destination"]
            days, nights = g["days"], g["nights"]
            min_total = g["min_total"]
            first = g["trips"][0]
            arrival, departure = first["arrival"], first["departure"]
            today = datetime.today().date()
            future_trips = [r for r in g["trips"] if r["flight"].departureTime.date() >= today]
            if not future_trips:
                continue
            print(f"{i}. {dest_city} (from {min_total:.2f}€) — {days} days, {nights} nights")
            print("Flight" + _flight_route_label_display(first["flight"], first["return_flight"]))
            trips_to_show = future_trips[:1] if summary_only else sorted(future_trips, key=lambda r: r["flight"].departureTime)
            for r in trips_to_show:
                outbound = r["flight"]
                ret = r["return_flight"]
                out_weekday = outbound.departureTime.strftime("%Y-%m-%d %A %H:%M")
                ret_weekday = ret.departureTime.strftime("%Y-%m-%d %A %H:%M")
                out_dur = _flight_duration_str(outbound.origin, outbound.destination)
                ret_dur = _flight_duration_str(ret.origin, ret.destination)
                origin_city = outbound.originFull.split(",")[0] if "," in outbound.originFull else outbound.originFull
                ret_origin_city = ret.originFull.split(",")[0] if "," in ret.originFull else ret.originFull
                ret_dest_city = ret.destinationFull.split(",")[0].strip() if "," in ret.destinationFull else ret.destination
                out_leg = f"{out_weekday}{out_dur}  {outbound.price}€  {origin_city} ({_display_airport(getattr(outbound, '_origin_code', outbound.origin))})→{dest_city} ({_display_airport(outbound.destination)})"
                ret_leg = f"{ret_weekday}{ret_dur}  {ret.price}€  {ret_origin_city} ({_display_airport(ret.origin)})→{ret_dest_city} ({_display_airport(ret.destination)})"
                trip_total = outbound.price + ret.price
                print(f"   {out_leg}{LEG_SEP}{ret_leg} ({trip_total:.2f}€)")
                urls = _booking_urls_for_trip(outbound, ret, adults)
                if "booking_url" in urls:
                    print(f"   {urls['booking_url']}")
                else:
                    print(f"   Departure: {urls['booking_url_outbound']}")
                    print(f"   Return:   {urls['booking_url_return']}")
                first_hotel = r.get("hotels") and r["hotels"][0]
                if first_hotel:
                    h_name = first_hotel.get("Accommodation Name") or first_hotel.get("accommodation_name") or "Hotel"
                    h_url = first_hotel.get("Accommodation URL") or first_hotel.get("accommodation_url")
                    if h_url:
                        print(f"   Hotel (Trivago): {h_name} — {h_url}")
            _print_weather_attractions_text(dest_city, first["flight"].departureTime.date(), first["return_flight"].departureTime.date(), weather_by_key, attractions_by_dest, city_profiles_by_dest, best_months_by_dest, similar_cities_by_dest, seasonal_calendar_by_dest, nearby_destinations_by_dest)
            print()
    elif agg_flights_print:
        for i, (dest_city, days, nights, flights) in enumerate(agg_flights_print, 1):
            ob, ib, price = flights[0]
            min_total = price + ib.price
            today = datetime.today().date()
            future_flights = [(ob, ib, price) for ob, ib, price in flights if ob.departureTime.date() >= today]
            if not future_flights:
                continue
            print(f"{i}. {dest_city} (from {min_total:.2f}€) — {days} days, {nights} nights")
            print("Flight" + _flight_route_label_display(ob, ib))
            flights_to_show = future_flights[:1] if summary_only else sorted(future_flights, key=lambda x: x[0].departureTime)
            for ob, ib, price in flights_to_show:
                out_weekday = ob.departureTime.strftime("%Y-%m-%d %A %H:%M")
                ret_weekday = ib.departureTime.strftime("%Y-%m-%d %A %H:%M")
                out_dur = _flight_duration_str(ob.origin, ob.destination)
                ret_dur = _flight_duration_str(ib.origin, ib.destination)
                origin_city = ob.originFull.split(",")[0] if "," in ob.originFull else ob.originFull
                ret_origin_city = ib.originFull.split(",")[0] if "," in ib.originFull else ib.originFull
                ret_dest_city = ib.destinationFull.split(",")[0] if "," in ib.destinationFull else ib.destination
                out_leg = f"{out_weekday}{out_dur}  {price}€  {origin_city} ({_display_airport(getattr(ob, '_origin_code', ob.origin))})→{dest_city} ({_display_airport(ob.destination)})"
                ret_leg = f"{ret_weekday}{ret_dur}  {ib.price}€  {ret_origin_city} ({_display_airport(ib.origin)})→{ret_dest_city} ({_display_airport(ib.destination)})"
                trip_total = price + ib.price
                print(f"   {out_leg}{LEG_SEP}{ret_leg} ({trip_total:.2f}€)")
                urls = _booking_urls_for_trip(ob, ib, adults)
                if "booking_url" in urls:
                    print(f"   {urls['booking_url']}")
                else:
                    print(f"   Departure: {urls['booking_url_outbound']}")
                    print(f"   Return:   {urls['booking_url_return']}")
            _print_weather_attractions_text(dest_city, flights[0][0].departureTime.date(), flights[0][1].departureTime.date(), weather_by_key, attractions_by_dest, city_profiles_by_dest, best_months_by_dest, similar_cities_by_dest, seasonal_calendar_by_dest, nearby_destinations_by_dest)
        print()
    else:
        print("(No round trips found for Wed after 6pm / Thu after 5pm / Fri after 11am from Weeze, Köln or Dortmund.)")
    # Global GeoTemp sections
    for section_title, data, formatter in [
        ("Dataset", dataset_stats, _format_dataset_stats),
        ("Trip ideas", plan_trip_result, _format_plan_trip),
        ("Compare destinations", compare_cities_result, _format_compare_cities),
        ("More destinations", search_destinations_result, _format_search_destinations),
        ("Top city break", search_by_activity_result, _format_search_by_activity),
        ("Beach & swimming", multi_activity_search_result, _format_multi_activity_search),
    ]:
        section_lines = formatter(data) if formatter else []
        if section_lines:
            print("-" * 80)
            print(section_title)
            for line in section_lines:
                print(f"  {line}")
    if not TRIVAGO_AVAILABLE and fetch_hotels:
        print("(Trivago MCP not installed: pip install 'mcp[cli]' for hotels.)", file=sys.stderr)
    elif not hotel_results and fetch_hotels and cheapest_flights:
        print("(No hotel results from Trivago. Check network and that Python/SSL support HTTPS.)", file=sys.stderr)
    print("=" * 80)
    total_s = timings.get("total") or 0
    flights_s = timings.get("flights") or 0
    weather_s = timings.get("weather_attractions") or 0
    hotels_s = timings.get("hotels") or 0
    print(f"Total execution time: {total_s:.1f}s. Flights: {flights_s:.1f}s, Weather & attractions: {weather_s:.1f}s, Hotels: {hotels_s:.1f}s.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Round trips from Weeze/Köln/Dortmund (Wed after 6pm, Thu after 5pm, or Fri after 11am outbound, 2–4 nights, return). N cheapest; one cheapest hotel per trip (near attractions) when not --no-hotels.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON to travel_helper.json (or --json-file PATH). Can be combined with --html.",
    )
    parser.add_argument(
        "--json-file",
        type=str,
        default=None,
        metavar="PATH",
        help="With --json: write JSON to PATH instead of travel_helper.json",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Write HTML report to travel_helper.html. Can be combined with --json.",
    )
    parser.add_argument(
        "--no-hotels",
        action="store_true",
        dest="no_hotels",
        help="Skip Trivago hotel fetch (flights only)",
    )
    parser.add_argument("--adults", type=int, default=2, help="Adults for hotel search")
    parser.add_argument("--rooms", type=int, default=1, help="Rooms for hotel search")
    parser.add_argument(
        "--num-cheapest-trips",
        type=int,
        default=100,
        metavar="N",
        help="Number of cheapest round trips to show and fetch hotels for (default: 100)",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=90,
        metavar="N",
        help="Search for departures in the next N days (default: 90)",
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        metavar="ADDRESS",
        help="Send results as HTML email to ADDRESS (Gmail: set GMAIL_USER and GMAIL_APP_PASSWORD). Example: --email you@example.com",
    )
    parser.add_argument(
        "--cheapest",
        action="store_true",
        dest="cheapest_only",
        help="Show only the cheapest trip per destination (one flight option per deal)",
    )
    args = parser.parse_args()
    run(
        output_json=args.json,
        output_html=args.html,
        fetch_hotels=not args.no_hotels,
        adults=args.adults,
        rooms=args.rooms,
        num_cheapest_trips=args.num_cheapest_trips,
        days_ahead=args.days_ahead,
        email=args.email,
        json_file=args.json_file,
        summary_only=args.cheapest_only,
    )


if __name__ == "__main__":
    main()
