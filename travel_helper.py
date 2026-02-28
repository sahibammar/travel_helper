#!/usr/bin/env python3
"""
Travel helper: cheap round-trip flights from Düsseldorf Weeze / Köln / Dortmund, then hotels.

1. Collects return trips from Weeze (NRN), Köln (CGN), and Dortmund (DTM). Only the departure (outbound) must
   match the schedule: Wednesday after 6 pm, Thursday after 5 pm, or Friday after 11 am. Return is 2–4 nights later
   (any time); no schedule restriction on the return flight.
2. Picks the 10 cheapest such trips by outbound price.
3. For each, fetches hotels for 2–4 nights from the Trivago MCP server.
4. If GEOTEMP_API_KEY (gt_live_...) is set, fetches destination info from GeoTemp REST API for each destination:
   city profile, weather (month + trip dates), attractions, seasonal calendar, best months, travel intelligence,
   similar cities, nearby destinations. Output JSON includes a "destination_info" object per deal.

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

# #region agent log
_DEBUG_LOG = Path(__file__).resolve().parent / ".cursor" / "debug-5cc9a6.log"
def _debug_log(location: str, message: str, data: dict, hypothesis_id: str) -> None:
    try:
        payload = {"sessionId": "5cc9a6", "location": location, "message": message, "data": data, "timestamp": int(time.time() * 1000), "hypothesisId": hypothesis_id}
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion

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


# GeoTemp REST API (optional: set GEOTEMP_API_KEY=gt_live_... for destination info)
GEOTEMP_API_BASE = "https://mcp-travel-data.onrender.com/api"
_CITY_API_SUFFIXES = frozenset({
    "Bergamo", "Beauvais", "Charleroi", "Ciampino", "Eindhoven", "Girona",
    "Hahn", "Knock", "Luton", "Malpensa", "Mazury", "Memmingen", "Modlin",
    "Prestwick", "Sandefjord", "Shannon", "Stansted", "Southend", "Torp", "Weeze",
})

# Trivago MCP (optional: only if mcp is installed)
try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from trivago.fetch_hotels_mcp import get_location_suggestion, search_accommodations, search_accommodations_radius

    TRIVAGO_AVAILABLE = True
except ImportError:
    TRIVAGO_AVAILABLE = False

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
    async with streamable_http_client(TRIVAGO_MCP_URL, terminate_on_close=False) as streams:
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
def _anchor_slug(dest_city: str, days: int, nights: int) -> str:
    """URL-safe anchor id for a deal section (destination + days/nights)."""
    base = re.sub(
        r"[^a-z0-9-]",
        "",
        dest_city.lower().replace(" - ", "-").replace(" ", "-"),
    )
    return f"{base}-{days}-{nights}" if base else f"deal-{days}-{nights}"


def _dest_city_from_flight(ob: object) -> str:
    """Destination city string for a trip (for display / grouping)."""
    dest_full = getattr(ob, "destinationFull", None) or ""
    if "," in dest_full:
        return dest_full.split(",")[0].strip()
    return dest_full.strip() or getattr(ob, "destination", "")


def _city_name_for_api(dest: str) -> str:
    """Normalize destination for GeoTemp API (e.g. 'Olsztyn - Mazury' -> 'Olsztyn', 'Barcelona Girona' -> 'Barcelona')."""
    if not dest or not dest.strip():
        return dest
    s = dest.strip()
    if " - " in s:
        return s.split(" - ", 1)[0].strip() or s
    parts = s.split()
    if len(parts) >= 2 and parts[-1] in _CITY_API_SUFFIXES:
        return " ".join(parts[:-1]).strip() or s
    return s


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


def _geotemp_call(api_key: str, tool: str, params: dict | None = None) -> dict | None:
    """POST to GeoTemp REST API tool. Returns JSON dict or None on error."""
    try:
        import requests
    except ImportError:
        return None
    url = f"{GEOTEMP_API_BASE}/tools/{tool}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=params or {}, timeout=30)
        r.raise_for_status()
        out = r.json()
        return None if isinstance(out, dict) and out.get("error") else out
    except Exception:
        return None


def _fetch_geotemp_for_destination(
    api_key: str,
    city_api: str,
    month: int,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> dict:
    """Fetch all available GeoTemp API data for one city. Returns dict with city_profile, weather, attractions, etc."""
    out = {
        "city_profile": None,
        "weather_month": None,
        "weather_dates": None,
        "attractions": None,
        "seasonal_calendar": None,
        "best_months": None,
        "travel_intelligence": None,
        "similar_cities": None,
        "nearby_destinations": None,
    }
    if not api_key or not city_api:
        return out
    # get_city_profile
    out["city_profile"] = _geotemp_call(api_key, "get_city_profile", {"city_name": city_api})
    time.sleep(0.2)
    # get_weather by month
    out["weather_month"] = _geotemp_call(api_key, "get_weather", {"city_name": city_api, "month": month})
    time.sleep(0.2)
    # get_weather by date range (if we have trip dates)
    if start_iso and end_iso:
        out["weather_dates"] = _geotemp_call(
            api_key, "get_weather",
            {"city_name": city_api, "start_date": start_iso, "end_date": end_iso},
        )
        time.sleep(0.2)
    # get_attractions
    out["attractions"] = _geotemp_call(api_key, "get_attractions", {"city_name": city_api, "limit": 15})
    time.sleep(0.2)
    # get_seasonal_calendar
    out["seasonal_calendar"] = _geotemp_call(api_key, "get_seasonal_calendar", {"city_name": city_api})
    time.sleep(0.2)
    # find_best_month
    out["best_months"] = _geotemp_call(api_key, "find_best_month", {"city_name": city_api, "prefer_warm": True})
    time.sleep(0.2)
    # get_travel_intelligence
    out["travel_intelligence"] = _geotemp_call(api_key, "get_travel_intelligence", {"city": city_api, "month": month})
    time.sleep(0.2)
    # find_similar_cities
    out["similar_cities"] = _geotemp_call(api_key, "find_similar_cities", {"city_name": city_api, "limit": 5})
    time.sleep(0.2)
    # find_nearby_destinations
    out["nearby_destinations"] = _geotemp_call(
        api_key, "find_nearby_destinations",
        {"city_name": city_api, "radius_km": 200, "limit": 5},
    )
    return out


def _fetch_geotemp_for_destinations(
    api_key: str,
    destinations: set[str],
    first_month: int,
    trip_dates_by_dest: dict[str, tuple[str, str]] | None = None,
) -> dict[str, dict]:
    """Fetch GeoTemp data for each destination. Returns dict[display_dest_name, destination_info]."""
    result = {}
    for dest in destinations:
        city_api = _city_name_for_api(dest)
        start_iso, end_iso = (trip_dates_by_dest or {}).get(dest, (None, None))
        result[dest] = _fetch_geotemp_for_destination(
            api_key, city_api, first_month,
            start_iso=start_iso, end_iso=end_iso,
        )
        time.sleep(0.4)
    return result


def _rating_pill_color(score: float) -> str:
    """Return background color for a hotel review rating pill."""
    if score >= 9:
        return "#0079c2"
    if score >= 8:
        return "#24a3ec"
    if score >= 7:
        return "#47a7ef"
    if score >= 6:
        return "#ff9128"
    return "#8d8d8b"


def _rating_label_text(score: float) -> str:
    """Return human-readable label for a hotel review score."""
    if score >= 9:
        return "Exceptional"
    if score >= 8.5:
        return "Superb"
    if score >= 8:
        return "Very good"
    if score >= 7:
        return "Good"
    if score >= 6:
        return "Pleasant"
    return "Reviewed"


def _append_flight_leg_table(
    lines: list[str],
    origin: str,
    origin_full: str,
    dep_time: str,
    destination: str,
    dest_full: str,
    dur_str: str,
    flight_num: str,
    price: float,
    is_return: bool,
) -> None:
    """Append a single flight leg (outbound or return) as an HTML table row."""
    track = "&#x2190;&nbsp;&#x2708;" if is_return else "&#x2708;&nbsp;&#x2192;"
    dur_clean = dur_str.strip().lstrip("(").rstrip(")")
    lines.append('            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">')
    lines.append('              <tr>')
    lines.append('                <td style="width:84px;vertical-align:middle;padding:0;">')
    lines.append(f'                  <div style="font-size:20px;font-weight:700;color:#171717;line-height:1;">{html.escape(origin)}</div>')
    lines.append(f'                  <div style="font-size:12px;color:#6c6c6b;margin-top:2px;">{html.escape(dep_time)}</div>')
    lines.append(f'                  <div style="font-size:10px;color:#8d8d8b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:84px;">{html.escape(origin_full)}</div>')
    lines.append('                </td>')
    lines.append('                <td style="padding:0 8px;text-align:center;vertical-align:middle;">')
    if dur_clean:
        lines.append(f'                  <div style="font-size:10px;color:#8d8d8b;margin-bottom:3px;">{html.escape(dur_clean)}</div>')
    lines.append(f'                  <div style="color:#24a3ec;font-size:13px;line-height:1;">{track}</div>')
    lines.append(f'                  <div style="font-size:10px;color:#bbbbb9;margin-top:3px;">{html.escape(flight_num)} &bull; &euro;{price}</div>')
    lines.append('                </td>')
    lines.append('                <td style="width:84px;text-align:right;vertical-align:middle;padding:0;">')
    lines.append(f'                  <div style="font-size:20px;font-weight:700;color:#171717;line-height:1;">{html.escape(destination)}</div>')
    lines.append(f'                  <div style="font-size:10px;color:#8d8d8b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:84px;text-align:right;">{html.escape(dest_full)}</div>')
    lines.append('                </td>')
    lines.append('              </tr>')
    lines.append('            </table>')


def _append_hotel_card_html(
    lines: list[str],
    hotel_name: str,
    hotel_url: str,
    hotel_price: str,
    hotel_rating: str,
    dest_city: str,
) -> None:
    """Append a Trivago-style hotel card to lines."""
    lines.append('      <div style="border-top:1px solid #f2f2f1;padding:12px 20px 16px;background:#fafafa;">')
    lines.append('        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#8d8d8b;margin-bottom:8px;">Suggested hotel at destination</div>')
    lines.append('        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.1);">')
    lines.append('          <tr>')
    lines.append('            <td style="padding:12px 16px;vertical-align:top;width:58%;">')
    lines.append(f'              <a href="{html.escape(hotel_url)}" target="_blank" rel="noopener" style="font-size:15px;font-weight:700;color:#171717;text-decoration:none;">{html.escape(hotel_name)}</a>')
    lines.append(f'              <div style="font-size:13px;color:#6c6c6b;margin-top:4px;">&#128205; {html.escape(dest_city)}</div>')
    if hotel_rating:
        try:
            score = float(hotel_rating)
            color = _rating_pill_color(score)
            label = _rating_label_text(score)
            lines.append(f'              <div style="margin-top:8px;"><span style="background:{color};color:#fff;font-size:11px;font-weight:700;padding:2px 7px;border-radius:8px;display:inline-block;">{html.escape(str(hotel_rating))}</span>&nbsp;<span style="font-size:13px;color:#6c6c6b;">{html.escape(label)}</span></div>')
        except (ValueError, TypeError):
            pass
    lines.append('            </td>')
    lines.append('            <td style="vertical-align:top;width:42%;padding:8px;">')
    lines.append('              <div style="background:#e5f5ff;border-radius:10px;padding:12px;">')
    if hotel_price:
        lines.append(f'                <div style="font-size:18px;font-weight:700;color:#171717;">{html.escape(hotel_price)}</div>')
        lines.append('                <div style="font-size:12px;color:#6c6c6b;margin-top:2px;">total stay</div>')
    lines.append(f'                <a href="{html.escape(hotel_url)}" target="_blank" rel="noopener" style="display:inline-block;background:#0079c2;color:#fff;font-size:13px;font-weight:700;padding:7px 14px;border-radius:8px;text-decoration:none;margin-top:10px;white-space:nowrap;">View deal &#x2192;</a>')
    lines.append('              </div>')
    lines.append('            </td>')
    lines.append('          </tr>')
    lines.append('        </table>')
    lines.append('      </div>')


_TRIVAGO_CSS = """    body { margin:0; padding:16px; background:#f2f2f1; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:#171717; font-size:16px; line-height:1.5; }
    .main-wrap { max-width:750px; margin:0 auto; }
    h1 { font-size:22px; font-weight:700; color:#171717; margin:0 0 4px 0; line-height:1.3; }
    .tagline { color:#6c6c6b; font-size:14px; line-height:1.5; margin:0 0 12px 0; }
    .preheader { font-size:14px; color:#6c6c6b; margin-bottom:12px; line-height:1.4; }
    .intro { color:#4d4d4c; font-size:14px; line-height:1.5; margin-bottom:16px; }
    .page-header { background:#fff; border-radius:12px; box-shadow:0 2px 10px rgba(0,0,0,0.09); overflow:hidden; margin-bottom:20px; padding:16px 24px; border:1px solid #d9d8d6; }
    .logo { font-size:20px; font-weight:700; color:#0079c2; letter-spacing:-0.02em; margin-bottom:8px; }
    .section-heading { font-size:18px; font-weight:700; color:#171717; margin:24px 0 12px 0; }
    .deals-summary-box { margin-bottom:16px; padding:16px; background:#fff; border-radius:12px; border:1px solid #d9d8d6; box-shadow:0 2px 8px rgba(0,0,0,0.06); }
    .deals-summary-heading { font-size:16px; font-weight:700; color:#171717; margin-bottom:12px; }
    .deals-summary-table { width:100%; border-collapse:collapse; font-size:14px; }
    .deals-summary-table th,.deals-summary-table td { padding:8px 12px; text-align:left; border-bottom:1px solid #f2f2f1; }
    .deals-summary-table th { font-weight:700; color:#171717; background:#fafafa; }
    a.deals-summary-link { color:#0079c2; text-decoration:none; font-weight:600; }
    a.deals-summary-link:hover { text-decoration:underline; }
    .trip { background:#fff; border:1px solid #d9d8d6; border-radius:12px; margin-bottom:16px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06); }
    .flight-option { border-top:1px solid #f2f2f1; }
    .flight-option-cheapest { background:#f5fbff; }
    .global-section { margin-top:16px; padding:16px; border:1px solid #d9d8d6; border-radius:12px; background:#fff; }
    .global-section-title { font-weight:700; font-size:14px; margin-bottom:8px; color:#0079c2; }
    .footer-note { margin-top:24px; padding-top:12px; border-top:1px solid #f2f2f1; font-size:13px; color:#6c6c6b; line-height:1.5; }
    .flight-title,.weather-title,.attractions-title,.hotels-title,.destination-title,.best-months-title,.similar-cities-title,.nearby-destinations-title,.activities-title,.seasonal-calendar-title,.tip-title { font-weight:700; font-size:13px; margin-bottom:4px; color:#171717; }
    .flight,.weather,.attractions,.hotels,.destination,.best-months,.similar-cities,.nearby-destinations,.activities,.seasonal-calendar,.tip { margin-top:8px; font-size:13px; color:#6c6c6b; line-height:1.5; padding:12px 20px; border-top:1px solid #f2f2f1; }
    @media screen and (max-width:600px) {
      body { padding:0; -webkit-text-size-adjust:100%; }
      .main-wrap { margin:0; }
      .trip { border-radius:0; border-left:none; border-right:none; margin-bottom:8px; }
      .deals-summary-box { border-radius:0; border-left:none; border-right:none; }
      h1 { font-size:18px; }
    }"""


def _load_geotemp_from_json(json_path: str = "data/travel_helper.json") -> dict[str, dict]:
    """Load destination -> destination_info from data/travel_helper.json for temperature display."""
    out: dict[str, dict] = {}
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return out
    deals = data.get("cheapest_flights_with_hotels") or data.get("cheapest_flights") or []
    if not isinstance(deals, list):
        return out
    for g in deals:
        if not isinstance(g, dict):
            continue
        dest = g.get("destination")
        info = g.get("destination_info")
        if dest and isinstance(info, dict):
            out[dest] = info
    return out


def _temp_str_for_destination(geotemp_by_dest: dict[str, dict] | None, dest: str) -> str:
    """Return HTML snippet for temperature next to destination name, or empty string."""
    if not geotemp_by_dest or not dest:
        return ""
    info = geotemp_by_dest.get(dest) or {}
    if not isinstance(info, dict):
        return ""
    # Prefer trip-date weather (weather_dates), then month summary (weather_month)
    temp_val = None
    w_dates = info.get("weather_dates")
    if isinstance(w_dates, dict) and w_dates.get("daily_weather"):
        daily = w_dates["daily_weather"]
        if isinstance(daily, list) and daily:
            temps = [d.get("temperature_mean") for d in daily if isinstance(d, dict)]
            temps = [t for t in temps if t is not None]
            if temps:
                temp_val = round(sum(temps) / len(temps))
    if temp_val is None:
        w_month = info.get("weather_month")
        if isinstance(w_month, dict):
            summary = w_month.get("weather_summary")
            if isinstance(summary, dict) and "avg_temperature_mean" in summary:
                temp_val = round(summary["avg_temperature_mean"])
    if temp_val is None:
        return ""
    return f" <span style=\"font-weight:400;opacity:0.9\">{temp_val}°C</span>"


def _build_html(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
    adults: int = 2,
    timings: dict | None = None,
    num_cheapest_trips: int = 100,
    days_ahead: int = 90,
    summary_only: bool = False,
    json_path: str = "data/travel_helper.json",
) -> str:
    """Build results as HTML string (same content as --html file). Temperature is read from json_path."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    geotemp_by_dest = _load_geotemp_from_json(json_path)
    title = "Fly cheap, stay cheap — your daily Ryanair + Trivago deals"
    tagline = f"Top {num_cheapest_trips} round trips from Weeze, Köln & Dortmund (Wed eve / Thu eve / Fri) over the next {days_ahead} days. Lowest hotel rates from Trivago. Weekend getaways in 2–4 nights."
    lines = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "  <meta charset=\"utf-8\">",
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
        f"  <title>{html.escape(title)}</title>",
        "  <style>",
        _TRIVAGO_CSS,
        "  </style>",
        "</head>",
        "<body>",
        "  <div class=\"main-wrap\">",
        "  <!-- Duplicate style block so email clients that strip <head> still apply layout -->",
        "  <style>",
        _TRIVAGO_CSS,
        "  </style>",
    ]
    agg_hotel = _aggregate_hotel_results(hotel_results) if hotel_results else []
    agg_flights = _aggregate_cheapest_flights(cheapest_flights) if not hotel_results and cheapest_flights else []
    num_deals = len(agg_hotel) if agg_hotel else len(agg_flights)
    # Page header card (logo + title)
    lines.append('  <div class="page-header">')
    lines.append('    <div class="logo">trivago flights</div>')
    lines.append(f'    <h1>{html.escape(title)}</h1>')
    lines.append(f'    <p class="tagline">{html.escape(tagline)}</p>')
    if num_deals:
        lines.append(f'    <p class="preheader">Your daily flight + hotel deals from Weeze, K&ouml;ln &amp; Dortmund &mdash; next {days_ahead} days, {num_deals} deal{"s" if num_deals != 1 else ""} inside.</p>')
    lines.append('  </div>')
    if agg_hotel:
        summary_rows = []
        for g in agg_hotel:
            dest_city = g["destination"]
            days, nights = g["days"], g["nights"]
            min_total = g["min_total"]
            n = len(g["trips"])
            slug = _anchor_slug(dest_city, days, nights)
            temp_str = _temp_str_for_destination(geotemp_by_dest, dest_city)
            link = f'<a href="#{html.escape(slug)}" class="deals-summary-link">{html.escape(dest_city)}{temp_str}</a>'
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
            slug = _anchor_slug(dest_city, days, nights)
            today = datetime.today().date()
            future_trips = [r for r in g["trips"] if r["flight"].departureTime.date() >= today]
            if not future_trips:
                continue
            lines.append(f'  <div class="trip" id="{html.escape(slug)}">')
            # Destination header — blue bar
            route_info = f"{_display_airport(first['flight'].origin)} &rarr; {_display_airport(first['flight'].destination)} &bull; {days}&nbsp;days, {nights}&nbsp;nights"
            temp_str = _temp_str_for_destination(geotemp_by_dest, dest_city)
            lines.append('    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#0079c2;color:#fff;">')
            lines.append('      <div>')
            lines.append(f'        <div style="font-size:18px;font-weight:700;line-height:1;">{html.escape(dest_city)}{temp_str}</div>')
            lines.append(f'        <div style="font-size:12px;opacity:0.75;margin-top:2px;">{route_info} &bull; from &euro;{min_total:.2f}</div>')
            lines.append('      </div>')
            lines.append('    </div>')
            # Flight option cards
            trips_to_show = future_trips[:1] if summary_only else sorted(future_trips, key=lambda r: r["flight"].departureTime)
            cheapest_total = min(r["flight"].price + r["return_flight"].price for r in trips_to_show) if trips_to_show else 0
            for r in trips_to_show:
                outbound = r["flight"]
                ret = r["return_flight"]
                out_dur = _flight_duration_str(outbound.origin, outbound.destination)
                ret_dur = _flight_duration_str(ret.origin, ret.destination)
                urls = _booking_urls_for_trip(outbound, ret, adults)
                first_hotel = r["hotels"][0] if r["hotels"] else {}
                hotel_name = first_hotel.get("Accommodation Name") or first_hotel.get("accommodation_name") or ""
                hotel_url = first_hotel.get("Accommodation URL") or first_hotel.get("accommodation_url") or ""
                hotel_price = first_hotel.get("Price Per Stay") or first_hotel.get("price_per_stay") or ""
                hotel_rating = first_hotel.get("Review Rating") or first_hotel.get("review_rating") or ""
                trip_total = outbound.price + ret.price
                opt_class = "flight-option flight-option-cheapest" if trip_total == cheapest_total else "flight-option"
                booking_url = urls.get("booking_url") or urls.get("booking_url_outbound") or ""
                out_dep_str = outbound.departureTime.strftime("%a %d %b %Y")
                ret_dep_str = ret.departureTime.strftime("%a %d %b %Y")
                lines.append(f'    <div class="{opt_class}">')
                lines.append('      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">')
                lines.append('        <tr>')
                lines.append('          <td style="padding:16px 20px;vertical-align:top;">')
                lines.append(f'            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#8d8d8b;margin-bottom:4px;">Outbound &bull; {html.escape(out_dep_str)}</div>')
                _append_flight_leg_table(lines, outbound.origin, outbound.originFull, outbound.departureTime.strftime("%H:%M"), outbound.destination, outbound.destinationFull, out_dur, outbound.flightNumber, outbound.price, False)
                lines.append('            <div style="height:1px;background:#f2f2f1;margin:8px 0;"></div>')
                lines.append(f'            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#8d8d8b;margin-bottom:4px;">Return &bull; {html.escape(ret_dep_str)}</div>')
                _append_flight_leg_table(lines, ret.origin, ret.originFull, ret.departureTime.strftime("%H:%M"), ret.destination, ret.destinationFull, ret_dur, ret.flightNumber, ret.price, True)
                lines.append('          </td>')
                lines.append('          <td style="width:150px;border-left:1px solid #f2f2f1;padding:16px;vertical-align:middle;text-align:right;">')
                lines.append(f'            <div style="font-size:24px;font-weight:700;color:#171717;line-height:1;">&euro;{trip_total:.2f}</div>')
                lines.append('            <div style="font-size:10px;color:#8d8d8b;margin-top:2px;">per person, return</div>')
                lines.append(f'            <a href="{html.escape(booking_url)}" target="_blank" rel="noopener" style="display:inline-block;background:#0079c2;color:#fff;font-size:13px;font-weight:700;padding:8px 14px;border-radius:8px;text-decoration:none;margin-top:12px;white-space:nowrap;">Book on Ryanair</a>')
                lines.append('          </td>')
                lines.append('        </tr>')
                lines.append('      </table>')
                if hotel_name and hotel_url:
                    _append_hotel_card_html(lines, hotel_name, hotel_url, hotel_price, hotel_rating, dest_city)
                lines.append('    </div>')
            lines.append('  </div>')
    elif agg_flights:
        summary_rows = []
        for dest_city, days, nights, flights in agg_flights:
            ob, ib, price = flights[0]
            min_total = price + ib.price
            n = len(flights)
            slug = _anchor_slug(dest_city, days, nights)
            temp_str = _temp_str_for_destination(geotemp_by_dest, dest_city)
            link = f'<a href="#{html.escape(slug)}" class="deals-summary-link">{html.escape(dest_city)}{temp_str}</a>'
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
            slug = _anchor_slug(dest_city, days, nights)
            today = datetime.today().date()
            future_flights = [(ob, ib, price) for ob, ib, price in flights if ob.departureTime.date() >= today]
            if not future_flights:
                continue
            lines.append(f'  <div class="trip" id="{html.escape(slug)}">')
            # Destination header
            route_info = f"{_display_airport(ob.origin)} &rarr; {_display_airport(ob.destination)} &bull; {days}&nbsp;days, {nights}&nbsp;nights"
            temp_str = _temp_str_for_destination(geotemp_by_dest, dest_city)
            lines.append('    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#0079c2;color:#fff;">')
            lines.append('      <div>')
            lines.append(f'        <div style="font-size:18px;font-weight:700;line-height:1;">{html.escape(dest_city)}{temp_str}</div>')
            lines.append(f'        <div style="font-size:12px;opacity:0.75;margin-top:2px;">{route_info} &bull; from &euro;{min_total:.2f}</div>')
            lines.append('      </div>')
            lines.append('    </div>')
            flights_to_show = future_flights[:1] if summary_only else sorted(future_flights, key=lambda x: x[0].departureTime)
            cheapest_total = min(p + i.price for _, i, p in flights_to_show) if flights_to_show else 0
            for ob, ib, price in flights_to_show:
                out_dur = _flight_duration_str(ob.origin, ob.destination)
                ret_dur = _flight_duration_str(ib.origin, ib.destination)
                urls = _booking_urls_for_trip(ob, ib, adults)
                trip_total = price + ib.price
                opt_class = "flight-option flight-option-cheapest" if trip_total == cheapest_total else "flight-option"
                booking_url = urls.get("booking_url") or urls.get("booking_url_outbound") or ""
                out_dep_str = ob.departureTime.strftime("%a %d %b %Y")
                ret_dep_str = ib.departureTime.strftime("%a %d %b %Y")
                lines.append(f'    <div class="{opt_class}">')
                lines.append('      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">')
                lines.append('        <tr>')
                lines.append('          <td style="padding:16px 20px;vertical-align:top;">')
                lines.append(f'            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#8d8d8b;margin-bottom:4px;">Outbound &bull; {html.escape(out_dep_str)}</div>')
                _append_flight_leg_table(lines, ob.origin, ob.originFull, ob.departureTime.strftime("%H:%M"), ob.destination, ob.destinationFull, out_dur, ob.flightNumber, price, False)
                lines.append('            <div style="height:1px;background:#f2f2f1;margin:8px 0;"></div>')
                lines.append(f'            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#8d8d8b;margin-bottom:4px;">Return &bull; {html.escape(ret_dep_str)}</div>')
                _append_flight_leg_table(lines, ib.origin, ib.originFull, ib.departureTime.strftime("%H:%M"), ib.destination, ib.destinationFull, ret_dur, ib.flightNumber, ib.price, True)
                lines.append('          </td>')
                lines.append('          <td style="width:150px;border-left:1px solid #f2f2f1;padding:16px;vertical-align:middle;text-align:right;">')
                lines.append(f'            <div style="font-size:24px;font-weight:700;color:#171717;line-height:1;">&euro;{trip_total:.2f}</div>')
                lines.append('            <div style="font-size:10px;color:#8d8d8b;margin-top:2px;">per person, return</div>')
                lines.append(f'            <a href="{html.escape(booking_url)}" target="_blank" rel="noopener" style="display:inline-block;background:#0079c2;color:#fff;font-size:13px;font-weight:700;padding:8px 14px;border-radius:8px;text-decoration:none;margin-top:12px;white-space:nowrap;">Book on Ryanair</a>')
                lines.append('          </td>')
                lines.append('        </tr>')
                lines.append('      </table>')
                lines.append('    </div>')
            lines.append('  </div>')
    if not agg_hotel and not agg_flights:
        lines.append("  <p>(No round trips found.)</p>")
    lines.append("  <p class=\"footer-note\">")
    lines.append(f"    Report generated on {html.escape(generated_at)}.")
    if timings:
        total_s = timings.get("total") or 0
        flights_s = timings.get("flights") or 0
        hotels_s = timings.get("hotels") or 0
        lines.append(f"    Total run: {total_s:.1f}s (flights {flights_s:.1f}s, hotels {hotels_s:.1f}s).")
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
    timings: dict | None = None,
    num_cheapest_trips: int = 100,
    days_ahead: int = 90,
    summary_only: bool = False,
    json_path: str = "data/travel_helper.json",
) -> None:
    """Write results to travel_helper.html and print path. Temperature read from json_path."""
    html_str = _build_html(
        cheapest_flights, hotel_results, adults, timings,
        num_cheapest_trips=num_cheapest_trips, days_ahead=days_ahead,
        summary_only=summary_only,
        json_path=json_path,
    )
    filename = "travel_helper.html"
    path = Path(filename).resolve()
    path.write_text(html_str, encoding="utf-8")
    print(path, file=sys.stderr)
    if timings:
        total_s = timings.get("total") or 0
        flights_s = timings.get("flights") or 0
        hotels_s = timings.get("hotels") or 0
        print(f"Total execution time: {total_s:.1f}s. Flights: {flights_s:.1f}s, Hotels: {hotels_s:.1f}s.", file=sys.stderr)


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

    _geotemp_key = (os.environ.get("GEOTEMP_API_KEY") or "").strip()
    if not _geotemp_key:
        print("Error: GEOTEMP_API_KEY is not set. Set it in the environment to fetch destination info (weather, attractions, etc.).", file=sys.stderr)
        sys.exit(1)

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

    # 3. Fetch hotels first so we know which (dest, dates) will appear in the output
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
                attractions_by_dest={},
            )
        )
        t_hotels = time.perf_counter() - t0

    # 4. GeoTemp API: fetch all available destination info for each destination in the output
    geotemp_by_dest = {}
    # #region agent log
    _debug_log("travel_helper.py:geotemp_guard", "GeoTemp block", {"has_geotemp_key": bool(_geotemp_key), "len_cheapest_flights": len(cheapest_flights), "has_hotel_results": bool(hotel_results)}, "A")
    # #endregion
    if cheapest_flights and _geotemp_key:
        destinations_geotemp = set()
        trip_dates_by_dest = {}
        if hotel_results:
            for r in hotel_results:
                d = r["destination"]
                destinations_geotemp.add(d)
                if d not in trip_dates_by_dest:
                    trip_dates_by_dest[d] = (r["arrival"], r["departure"])
        else:
            for ob, ib, _ in cheapest_flights:
                d = _dest_city_from_flight(ob)
                destinations_geotemp.add(d)
                if d not in trip_dates_by_dest:
                    trip_dates_by_dest[d] = (
                        ob.departureTime.date().isoformat(),
                        ib.departureTime.date().isoformat(),
                    )
        first_month = datetime.now().month
        if trip_dates_by_dest:
            first_iso = next(iter(trip_dates_by_dest.values()))[0]
            try:
                first_month = int(first_iso.split("-")[1])
            except (IndexError, ValueError):
                pass
        if destinations_geotemp:
            # #region agent log
            _debug_log("travel_helper.py:before_fetch", "Before GeoTemp fetch", {"len_destinations_geotemp": len(destinations_geotemp), "sample_dests": list(destinations_geotemp)[:3]}, "B")
            # #endregion
            print("Fetching destination info (GeoTemp API)...", file=sys.stderr)
            try:
                geotemp_by_dest = _fetch_geotemp_for_destinations(
                    _geotemp_key, destinations_geotemp, first_month, trip_dates_by_dest,
                )
                # #region agent log
                sample_key = next(iter(geotemp_by_dest), None)
                sample_val = geotemp_by_dest.get(sample_key, {}) if sample_key else {}
                has_any = any(v is not None for v in (sample_val or {}).values())
                _debug_log("travel_helper.py:after_fetch", "After GeoTemp fetch", {"len_geotemp_by_dest": len(geotemp_by_dest), "sample_key": sample_key, "sample_has_any_value": has_any}, "D")
                # #endregion
                print(f"GeoTemp: loaded info for {len(geotemp_by_dest)} destinations", file=sys.stderr)
            except Exception as e:
                # #region agent log
                _debug_log("travel_helper.py:geotemp_exception", "GeoTemp fetch exception", {"error": str(e)}, "D")
                # #endregion
                print(f"GeoTemp fetch failed: {e}", file=sys.stderr)
        # #region agent log
        else:
            _debug_log("travel_helper.py:no_destinations", "destinations_geotemp empty", {"destinations_geotemp_empty": True}, "C")
        # #endregion

    t_total = time.perf_counter() - t_start
    timings = {
        "total": t_total,
        "flights": t_flights,
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
                    "destination_info": geotemp_by_dest.get(g["destination"], {}),
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
                dest_info = geotemp_by_dest.get(dest, {})
                # #region agent log
                if len(cheapest_flights_list) == 0:
                    _debug_log("travel_helper.py:no_hotels_first_dest", "First output deal dest vs geotemp key", {"output_dest": dest, "dest_in_geotemp": dest in geotemp_by_dest, "dest_info_non_empty": bool(dest_info and any(v is not None for v in dest_info.values()))}, "E")
                # #endregion
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
                    "destination_info": dest_info,
                })
            out = {"cheapest_flights": cheapest_flights_list}
        path = json_file if json_file is not None else "data/travel_helper.json"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
        abs_path = Path(path).resolve()
        print(f"Wrote JSON to {abs_path} ({len(out.get('cheapest_flights_with_hotels') or out.get('cheapest_flights') or [])} deals)", file=sys.stderr)
        if not output_html:
            return

    json_path_used = json_file if json_file is not None else "data/travel_helper.json"
    if output_html:
        _print_html(
            cheapest_flights=cheapest_flights,
            hotel_results=hotel_results,
            adults=adults,
            timings=timings,
            num_cheapest_trips=num_cheapest_trips,
            days_ahead=days_ahead or 90,
            summary_only=summary_only,
            json_path=json_path_used,
        )
        if not email:
            return
    if email:
        html_str = _build_html(
            cheapest_flights=cheapest_flights,
            hotel_results=hotel_results,
            adults=adults,
            timings=timings,
            num_cheapest_trips=num_cheapest_trips,
            days_ahead=days_ahead or 90,
            summary_only=summary_only,
            json_path=json_path_used,
        )
        _send_email_html(html_str, email)
        if output_html:
            return
        # If only --email (no --html), we're done
        return

    # Human-readable output
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
            print()
    else:
        print("(No round trips found for Wed after 6pm / Thu after 5pm / Fri after 11am from Weeze, Köln or Dortmund.)")
    if not TRIVAGO_AVAILABLE and fetch_hotels:
        print("(Trivago MCP not installed: pip install 'mcp[cli]' for hotels.)", file=sys.stderr)
    elif not hotel_results and fetch_hotels and cheapest_flights:
        print("(No hotel results from Trivago. Check network and that Python/SSL support HTTPS.)", file=sys.stderr)
    print("=" * 80)
    total_s = timings.get("total") or 0
    flights_s = timings.get("flights") or 0
    hotels_s = timings.get("hotels") or 0
    print(f"Total execution time: {total_s:.1f}s. Flights: {flights_s:.1f}s, Hotels: {hotels_s:.1f}s.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Round trips from Weeze/Köln/Dortmund (Wed after 6pm, Thu after 5pm, or Fri after 11am outbound, 2–4 nights, return). N cheapest; one cheapest hotel per trip (near attractions) when not --no-hotels.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON to data/travel_helper.json (or --json-file PATH). Can be combined with --html.",
    )
    parser.add_argument(
        "--json-file",
        type=str,
        default=None,
        metavar="PATH",
        help="With --json: write JSON to PATH instead of data/travel_helper.json",
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
