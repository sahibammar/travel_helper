#!/usr/bin/env python3
"""
Travel helper: cheap round-trip flights from Düsseldorf Weeze / Köln, then hotels.

1. Collects return trips from Weeze (NRN) and Köln (CGN). Only the departure (outbound) must
   match the schedule: Thursday after 5 pm or Friday after 11 pm. Return is 3–4 nights later
   (any time); no schedule restriction on the return flight.
2. Picks the 10 cheapest such trips by outbound price.
3. For each, fetches hotels for 3–4 nights from the Trivago MCP server.

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


# Trivago MCP (optional: only if mcp is installed)
try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from trivago.fetch_hotels_mcp import get_location_suggestion, search_accommodations

    TRIVAGO_AVAILABLE = True
except ImportError:
    TRIVAGO_AVAILABLE = False

# GeoTemp Travel MCP (optional: weather + attractions per destination)
try:
    from mcp.client.sse import sse_client
    from geotemp_fetch_mcp import GEOTEMP_MCP_URL, get_attractions, get_weather

    GEOTEMP_AVAILABLE = True
except ImportError:
    GEOTEMP_AVAILABLE = False
    sse_client = None
    get_weather = None
    get_attractions = None
    GEOTEMP_MCP_URL = None

# --------------- Config: Weeze + Köln, Thu eve / Fri late outbound, 3–4 nights ---------------
ORIGIN_AIRPORTS = [
    ("CGN", "Köln"),
    ("NRN", "Düsseldorf Weeze"),
]
DAYS_AHEAD = 120  # search ahead for Thu/Fri departures
RETURN_DAYS_MIN = 2  # 2 nights at destination
RETURN_DAYS_MAX = 4  # 4 nights at destination
HOTEL_NIGHTS = 4  # legacy; hotel stay now matches return flight (arrival = outbound date, departure = return date)
TRIVAGO_MCP_URL = "https://mcp.trivago.com/mcp"

# Only the departure (outbound) is restricted: Thursday >= 17:00, or Friday >= 23:00 (11 pm).
# Return flight is 3–4 nights later with no time-of-day restriction. Monday=0 in weekday().
THURSDAY = 3
FRIDAY = 4
OUTBOUND_THURSDAY_AFTER_HOUR = 17
OUTBOUND_FRIDAY_AFTER_HOUR = 23  # 11 pm

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


def _outbound_departure_allowed(dt: datetime) -> bool:
    """True if outbound departure is Thursday after 5 pm or Friday after 11 am (only departure is restricted)."""
    wd = dt.weekday()
    hour = dt.hour
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


async def _top_hotels_for_destination(
    session: ClientSession,
    destination_city: str,
    arrival_date: str,
    departure_date: str,
    n: int = 3,
    adults: int = 2,
    rooms: int = 1,
) -> list[dict]:
    """Return up to n cheapest hotels (by price per night) for one destination/dates."""
    suggestion = None
    for query in _trivago_query_for_destination(destination_city):
        suggestion = await get_location_suggestion(session, query)
        if suggestion:
            break
    if not suggestion:
        return []
    location_id, location_ns = suggestion
    hotels = await search_accommodations(
        session,
        location_id,
        location_ns,
        arrival_date,
        departure_date,
        adults=adults,
        rooms=rooms,
    )
    if not hotels:
        return []
    hotels_sorted = sorted(hotels, key=_parse_price_night)
    return hotels_sorted[:n]


async def fetch_hotels_for_cheapest_flights(
    cheapest_flights: list[tuple[object, object, float]],
    hotels_per_flight: int = 3,
    adults: int = 2,
    rooms: int = 1,
) -> list[dict]:
    """
    For each (outbound, return_flight, price) entry, fetch hotels_per_flight hotels
    for that destination. Hotel stay = arrival (outbound date) to departure (return flight date).
    Returns list of { "destination", "arrival", "departure", "flight", "return_flight", "price", "hotels": [...] }.
    """
    if not TRIVAGO_AVAILABLE or not cheapest_flights:
        return []
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
                hotels = await _top_hotels_for_destination(
                    session, dest_city, arrival, departure,
                    n=hotels_per_flight, adults=adults, rooms=rooms,
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


def _dest_city_from_flight(ob: object) -> str:
    """Destination city string for a trip (for GeoTemp / display)."""
    dest_full = getattr(ob, "destinationFull", None) or ""
    if "," in dest_full:
        return dest_full.split(",")[0].strip()
    return dest_full.strip() or getattr(ob, "destination", "")


async def _fetch_geotemp_for_trips(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
) -> dict | None:
    """Fetch weather (per trip date range) and attractions (per destination) from GeoTemp MCP.
    Returns {'weather': {(dest, start_date, end_date): list}, 'attractions': {dest: list}} or None on error.
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
                    if dest not in attractions_by_dest:
                        dest_api = _city_name_for_api(dest)
                        a = await get_attractions(session, dest_api, limit=10)
                        attractions_by_dest[dest] = a if isinstance(a, list) else ([] if not a else [a])
    except Exception as e:
        print(f"GeoTemp MCP unavailable: {e}", file=sys.stderr)
        return None
    return {"weather": weather_by_key, "attractions": attractions_by_dest}


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


def _print_weather_attractions_text(
    dest_city: str,
    out_date: object,
    ret_date: object,
    weather_by_key: dict,
    attractions_by_dest: dict,
) -> None:
    """Print weather and attractions for one trip (human-readable)."""
    start_iso = out_date.isoformat() if hasattr(out_date, "isoformat") else str(out_date)
    end_iso = ret_date.isoformat() if hasattr(ret_date, "isoformat") else str(ret_date)
    key = (dest_city, start_iso, end_iso)
    weather_list = weather_by_key.get(key) or []
    att_list = attractions_by_dest.get(dest_city) or []
    weather_lines = [s for w in weather_list[:7] if (s := _format_weather_item(w))]
    if weather_lines:
        print("   Weather:")
        for line in weather_lines:
            print(f"     {line}")
    if att_list:
        print("   Attractions:")
        for a in att_list[:10]:
            print(f"     • {_format_attraction_item(a)}")


def _add_weather_attractions_html(
    lines: list[str],
    dest_city: str,
    out_date: object,
    ret_date: object,
    weather_by_key: dict,
    attractions_by_dest: dict,
) -> None:
    """Append weather and attractions blocks to lines (HTML)."""
    start_iso = out_date.isoformat() if hasattr(out_date, "isoformat") else str(out_date)
    end_iso = ret_date.isoformat() if hasattr(ret_date, "isoformat") else str(ret_date)
    key = (dest_city, start_iso, end_iso)
    weather_list = weather_by_key.get(key) or []
    att_list = attractions_by_dest.get(dest_city) or []
    weather_lines = [s for w in weather_list[:7] if (s := _format_weather_item(w))]
    if weather_lines:
        lines.append("    <div class=\"weather\">")
        lines.append("      <div class=\"weather-title\">Weather</div>")
        for line in weather_lines:
            lines.append(f"      <div>{html.escape(line)}</div>")
        lines.append("    </div>")
    if att_list:
        lines.append("    <div class=\"attractions\">")
        lines.append("      <div class=\"attractions-title\">Attractions</div>")
        for a in att_list[:10]:
            lines.append(f"      <div>{html.escape(_format_attraction_item(a))}</div>")
        lines.append("    </div>")


def _build_html(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
    adults: int = 2,
    travel_data: dict | None = None,
    timings: dict | None = None,
) -> str:
    """Build results as HTML string (same content as --html file)."""
    title = "Fly cheap, stay cheap — your daily Ryanair + Trivago deals"
    tagline = "Best-priced flights from Weeze & Köln (Thu eve / Fri) and lowest hotel rates from Trivago. Weekend getaways in 2–4 nights."
    weather_by_key = (travel_data or {}).get("weather") or {}
    attractions_by_dest = (travel_data or {}).get("attractions") or {}
    lines = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "  <meta charset=\"utf-8\">",
        f"  <title>{html.escape(title)}</title>",
        "  <style>",
        "    body { font-family: system-ui, sans-serif; margin: 1rem 2rem; max-width: 900px; }",
        "    h1 { font-size: 1.35rem; color: #073590; margin-bottom: 0.25rem; }",
        "    .tagline { color: #555; font-size: 0.95rem; line-height: 1.4; margin-bottom: 1rem; }",
        "    .trip { margin: 1rem 0; padding: 0.75rem; border: 1px solid #ccc; border-radius: 6px; }",
        "    .trip-header { font-weight: bold; margin-bottom: 0.25rem; }",
        "    .trip-details { color: #444; font-size: 0.95rem; }",
        "    a.trip-link { color: #073590; text-decoration: none; }",
        "    a.trip-link:hover { text-decoration: underline; }",
        "    .hotel { margin: 0.2rem 0; }",
        "    .hotel a { color: #073590; }",
        "    .flight-title, .weather-title, .attractions-title, .hotels-title { font-weight: 600; margin-bottom: 0.2rem; }",
        "    .flight, .weather, .attractions, .hotels { margin-top: 0.5rem; font-size: 0.9rem; color: #444; }",
        "    .timings-note { margin-top: 2rem; font-size: 0.85rem; color: #666; }",
        "  </style>",
        "</head>",
        "<body>",
        f"  <h1>{html.escape(title)}</h1>",
        f"  <p class=\"tagline\">{html.escape(tagline)}</p>",
    ]
    if hotel_results:
        for i, r in enumerate(hotel_results, 1):
            outbound = r["flight"]
            ret = r["return_flight"]
            dest_city = r["destination"]
            total = r["price"] + ret.price
            out_weekday = outbound.departureTime.strftime("%Y-%m-%d %A %H:%M")
            ret_weekday = ret.departureTime.strftime("%Y-%m-%d %A %H:%M")
            out_dur = _flight_duration_str(outbound.origin, outbound.destination)
            ret_dur = _flight_duration_str(ret.origin, ret.destination)
            out_leg = f"{out_weekday}{out_dur}  {outbound.price}€  {outbound.origin}→{outbound.destination}"
            ret_leg = f"{ret_weekday}{ret_dur}  {ret.price}€  {ret.origin}→{ret.destination}"
            ryanair_url = _ryanair_booking_url(
                outbound.origin, outbound.destination,
                outbound.departureTime.date().isoformat(),
                ret.departureTime.date().isoformat(),
                adults=adults,
            )
            out_date = outbound.departureTime.date()
            ret_date = ret.departureTime.date()
            nights = (ret_date - out_date).days
            days = nights + 1
            lines.append("  <div class=\"trip\">")
            lines.append(f"    <div class=\"trip-header\">{html.escape(dest_city)} ({total:.2f}€) — {days} days, {nights} nights</div>")
            lines.append("    <div class=\"flight\">")
            lines.append("      <div class=\"flight-title\">Flight</div>")
            lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(ryanair_url)}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}  |  {html.escape(ret_leg)}</a>")
            lines.append("    </div>")
            _add_weather_attractions_html(lines, dest_city, out_date, ret_date, weather_by_key, attractions_by_dest)
            lines.append("    <div class=\"hotels\">")
            lines.append("      <div class=\"hotels-title\">Hotels</div>")
            for hotel in r["hotels"]:
                name = hotel.get("Accommodation Name") or hotel.get("accommodation_name") or "—"
                url = hotel.get("Accommodation URL") or hotel.get("accommodation_url") or ""
                price_stay = hotel.get("Price Per Stay") or hotel.get("price_per_stay") or ""
                if url:
                    lines.append(f"      <div class=\"hotel\"><a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener\">{html.escape(name)}</a> {html.escape(price_stay)}</div>")
                else:
                    lines.append(f"      <div class=\"hotel\">{html.escape(name)} {html.escape(price_stay)}</div>")
            lines.append("    </div>")
            lines.append("  </div>")
    else:
        for i, (ob, ib, price) in enumerate(cheapest_flights, 1):
            dest_city = ob.destinationFull.split(",")[0] if "," in ob.destinationFull else ob.destinationFull
            total = price + ib.price
            out_weekday = ob.departureTime.strftime("%Y-%m-%d %A %H:%M")
            ret_weekday = ib.departureTime.strftime("%Y-%m-%d %A %H:%M")
            out_dur = _flight_duration_str(ob.origin, ob.destination)
            ret_dur = _flight_duration_str(ib.origin, ib.destination)
            out_leg = f"{out_weekday}{out_dur}  {price}€  {ob.origin}→{ob.destination}"
            ret_leg = f"{ret_weekday}{ret_dur}  {ib.price}€  {ib.origin}→{ib.destination}"
            ryanair_url = _ryanair_booking_url(
                ob.origin, ob.destination,
                ob.departureTime.date().isoformat(),
                ib.departureTime.date().isoformat(),
                adults=adults,
            )
            out_date = ob.departureTime.date()
            ret_date = ib.departureTime.date()
            nights = (ret_date - out_date).days
            days = nights + 1
            lines.append("  <div class=\"trip\">")
            lines.append(f"    <div class=\"trip-header\">{html.escape(dest_city)} ({total:.2f}€) — {days} days, {nights} nights</div>")
            lines.append("    <div class=\"flight\">")
            lines.append("      <div class=\"flight-title\">Flight</div>")
            lines.append(f"      <a class=\"trip-details trip-link\" href=\"{html.escape(ryanair_url)}\" target=\"_blank\" rel=\"noopener\">{html.escape(out_leg)}  |  {html.escape(ret_leg)}</a>")
            lines.append("    </div>")
            _add_weather_attractions_html(lines, dest_city, out_date, ret_date, weather_by_key, attractions_by_dest)
            lines.append("  </div>")
    if not cheapest_flights:
        lines.append("  <p>(No round trips found.)</p>")
    if timings:
        total_s = timings.get("total") or 0
        flights_s = timings.get("flights") or 0
        weather_s = timings.get("weather_attractions") or 0
        hotels_s = timings.get("hotels") or 0
        lines.append("  <p class=\"timings-note\">")
        lines.append(f"    Total execution time: {total_s:.1f}s. Flights: {flights_s:.1f}s, Weather &amp; attractions: {weather_s:.1f}s, Hotels: {hotels_s:.1f}s.")
        lines.append("  </p>")
    lines.append("</body>")
    lines.append("</html>")
    return "\n".join(lines)


def _print_html(
    cheapest_flights: list[tuple[object, object, float]],
    hotel_results: list[dict],
    adults: int = 2,
    travel_data: dict | None = None,
    timings: dict | None = None,
) -> None:
    """Write results to travel_helper_YYYY-MM-DD.html and print path."""
    html_str = _build_html(cheapest_flights, hotel_results, adults, travel_data, timings)
    now = datetime.now()
    filename = f"travel_helper_{now.strftime('%Y-%m-%d')}.html"
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
    """Collect return trips from Weeze/Köln. Only the departure must match: Thu after 5pm or Fri after 11pm.
    Return is 3–4 nights later (any time of day). Uses API time windows so we get trips in those slots.
    Returns list of (outbound, return_flight, outbound_price).
    """
    api = Ryanair(currency="EUR")
    outbound = []
    n_days = days_ahead if days_ahead is not None else DAYS_AHEAD
    for airport_code, airport_name in ORIGIN_AIRPORTS:
        for day_offset in range(0, n_days):
            search_date = datetime.today().date() + timedelta(days=day_offset)
            wd = search_date.weekday()
            if wd == THURSDAY:
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
    outbound.sort(key=lambda x: (x[2] + x[1].price, x[0].departureTime.date(), x[0].destination))
    return outbound


def run(
    output_json: bool = False,
    output_html: bool = False,
    fetch_hotels: bool = True,
    adults: int = 2,
    rooms: int = 1,
    num_cheapest_flights: int = 10,
    hotels_per_flight: int = 3,
    days_ahead: int | None = None,
    email: str | None = None,
) -> None:
    t_start = time.perf_counter()

    # 1. Collect return trips (only departure restricted: Thu after 5pm / Fri after 11pm; return 3–4 nights later, any time)
    t0 = time.perf_counter()
    outbound_flights = collect_outbound_flights(days_ahead=days_ahead)
    t_flights = time.perf_counter() - t0
    # 2. Already sorted by price; take the N cheapest
    cheapest_flights = outbound_flights[:num_cheapest_flights]

    # 3. Fetch hotels for those flights (stay = outbound date to return date)
    hotel_results = []
    t_hotels = 0.0
    if fetch_hotels and TRIVAGO_AVAILABLE and cheapest_flights:
        if not output_json:
            print(f"Fetching {hotels_per_flight} hotels per trip (stay = outbound date → return date) for the {num_cheapest_flights} cheapest round trips...", file=sys.stderr)
        t0 = time.perf_counter()
        hotel_results = asyncio.run(
            fetch_hotels_for_cheapest_flights(
                cheapest_flights,
                hotels_per_flight=hotels_per_flight,
                adults=adults,
                rooms=rooms,
            )
        )
        t_hotels = time.perf_counter() - t0

    # 4. Optional: weather + attractions per destination (GeoTemp MCP)
    travel_data = None
    t_weather_attractions = 0.0
    if GEOTEMP_AVAILABLE and (cheapest_flights or hotel_results):
        if not output_json:
            print("Fetching weather and attractions (GeoTemp)...", file=sys.stderr)
        try:
            t0 = time.perf_counter()
            travel_data = asyncio.run(_fetch_geotemp_for_trips(cheapest_flights, hotel_results))
            t_weather_attractions = time.perf_counter() - t0
        except Exception as e:
            print(f"GeoTemp fetch failed: {e}", file=sys.stderr)

    t_total = time.perf_counter() - t_start
    timings = {
        "total": t_total,
        "flights": t_flights,
        "weather_attractions": t_weather_attractions,
        "hotels": t_hotels,
    }

    if output_json:
        # Prefer hotel_results when present; otherwise output flight-only from cheapest_flights
        if hotel_results:
            out = {
                "cheapest_flights_with_hotels": [
                    {
                        "outbound": {
                            "departure": r["flight"].departureTime.isoformat(),
                            "origin": r["flight"].origin,
                            "origin_full": r["flight"].originFull,
                            "destination": r["flight"].destination,
                            "destination_full": r["flight"].destinationFull,
                            "price_eur": r["price"],
                        },
                        "return": {
                            "departure": r["return_flight"].departureTime.isoformat(),
                            "origin": r["return_flight"].origin,
                            "origin_full": r["return_flight"].originFull,
                            "destination": r["return_flight"].destination,
                            "destination_full": r["return_flight"].destinationFull,
                            "price_eur": r["return_flight"].price,
                        },
                        "hotel_arrival": r["arrival"],
                        "hotel_departure": r["departure"],
                        "hotels": r["hotels"],
                    }
                    for r in hotel_results
                ],
            }
        else:
            out = {
                "cheapest_flights": [
                    {
                        "outbound": {
                            "departure": ob.departureTime.isoformat(),
                            "origin": ob.origin,
                            "origin_full": ob.originFull,
                            "destination": ob.destination,
                            "destination_full": ob.destinationFull,
                            "price_eur": ob.price,
                        },
                        "return": {
                            "departure": ib.departureTime.isoformat(),
                            "origin": ib.origin,
                            "origin_full": ib.originFull,
                            "destination": ib.destination,
                            "destination_full": ib.destinationFull,
                            "price_eur": ib.price,
                        },
                    }
                    for ob, ib, price in cheapest_flights
                ],
            }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return

    if output_html:
        _print_html(
            cheapest_flights=cheapest_flights,
            hotel_results=hotel_results,
            adults=adults,
            travel_data=travel_data,
            timings=timings,
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
        )
        _send_email_html(html_str, email)
        if output_html:
            return
        # If only --email (no --html), we're done
        return

    # Human-readable output
    weather_by_key = (travel_data or {}).get("weather") or {}
    attractions_by_dest = (travel_data or {}).get("attractions") or {}
    print("Fly cheap, stay cheap — Ryanair + Trivago deals from Weeze & Köln (Thu eve / Fri, 2–4 nights)")
    print("=" * 80)
    print("CHEAPEST ROUND TRIPS" + (" + HOTELS" if hotel_results else " (flights only)"))
    print("-" * 80)
    if hotel_results:
        for i, r in enumerate(hotel_results, 1):
            outbound = r["flight"]
            ret = r["return_flight"]
            price = r["price"]
            dest_city = r["destination"]
            arrival, departure = r["arrival"], r["departure"]
            out_weekday = outbound.departureTime.strftime("%Y-%m-%d %A %H:%M")
            ret_weekday = ret.departureTime.strftime("%Y-%m-%d %A %H:%M")
            out_dur = _flight_duration_str(outbound.origin, outbound.destination)
            ret_dur = _flight_duration_str(ret.origin, ret.destination)
            origin_city = outbound.originFull.split(",")[0] if "," in outbound.originFull else outbound.originFull
            ret_origin_city = ret.originFull.split(",")[0] if "," in ret.originFull else ret.originFull
            ret_dest_city = ret.destinationFull.split(",")[0].strip() if "," in ret.destinationFull else ret.destination
            total = price + ret.price
            nights = (ret.departureTime.date() - outbound.departureTime.date()).days
            days = nights + 1
            out_leg = f"{out_weekday}{out_dur}  {price}€  {origin_city} ({outbound._origin_code})→{dest_city} ({outbound.destination})"
            ret_leg = f"{ret_weekday}{ret_dur}  {ret.price}€  {ret_origin_city} ({ret.origin})→{ret_dest_city} ({ret.destination})"
            print(f"{i}. {dest_city} ({total:.2f}€) — {days} days, {nights} nights")
            print("Flight")
            print(f"   {out_leg}{LEG_SEP}{ret_leg}")
            ryanair_url = _ryanair_booking_url(
                outbound.origin, outbound.destination,
                outbound.departureTime.date().isoformat(),
                ret.departureTime.date().isoformat(),
                adults=adults,
            )
            print(f"   {ryanair_url}")
            _print_weather_attractions_text(dest_city, outbound.departureTime.date(), ret.departureTime.date(), weather_by_key, attractions_by_dest)
            nights = (datetime.fromisoformat(departure).date() - datetime.fromisoformat(arrival).date()).days
            print("Hotels")
            print(f"   {nights} nights, {arrival} → {departure}")
            for j, hotel in enumerate(r["hotels"], 1):
                name = hotel.get("Accommodation Name") or hotel.get("accommodation_name") or "—"
                price_night = hotel.get("Price Per Night") or hotel.get("price_per_night") or "—"
                price_stay = hotel.get("Price Per Stay") or hotel.get("price_per_stay") or "—"
                url = hotel.get("Accommodation URL") or hotel.get("accommodation_url") or ""
                print(f"   {j}. {name}  |  {price_night} (total {price_stay})")
                if url:
                    print(f"      {url}")
            if not r["hotels"]:
                print("   (none found)")
            print()
    else:
        for i, (ob, ib, price) in enumerate(cheapest_flights, 1):
            out_weekday = ob.departureTime.strftime("%Y-%m-%d %A %H:%M")
            ret_weekday = ib.departureTime.strftime("%Y-%m-%d %A %H:%M")
            out_dur = _flight_duration_str(ob.origin, ob.destination)
            ret_dur = _flight_duration_str(ib.origin, ib.destination)
            origin_city = ob.originFull.split(",")[0] if "," in ob.originFull else ob.originFull
            dest_city = ob.destinationFull.split(",")[0] if "," in ob.destinationFull else ob.destinationFull
            ret_origin_city = ib.originFull.split(",")[0] if "," in ib.originFull else ib.originFull
            ret_dest_city = ib.destinationFull.split(",")[0] if "," in ib.destinationFull else ib.destination
            total = price + ib.price
            nights = (ib.departureTime.date() - ob.departureTime.date()).days
            days = nights + 1
            out_leg = f"{out_weekday}{out_dur}  {price}€  {origin_city} ({ob._origin_code})→{dest_city} ({ob.destination})"
            ret_leg = f"{ret_weekday}{ret_dur}  {ib.price}€  {ret_origin_city} ({ib.origin})→{ret_dest_city} ({ib.destination})"
            print(f"{i}. {dest_city} ({total:.2f}€) — {days} days, {nights} nights")
            print("Flight")
            print(f"   {out_leg}{LEG_SEP}{ret_leg}")
            ryanair_url = _ryanair_booking_url(
                ob.origin, ob.destination,
                ob.departureTime.date().isoformat(),
                ib.departureTime.date().isoformat(),
                adults=adults,
            )
            print(f"   {ryanair_url}")
            _print_weather_attractions_text(dest_city, ob.departureTime.date(), ib.departureTime.date(), weather_by_key, attractions_by_dest)
        if not cheapest_flights:
            print("(No round trips found for Thu after 5pm / Fri after 11pm from Weeze or Köln.)")
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
        description="Round trips from Weeze/Köln (Thu after 5pm or Fri after 11pm outbound, 3–4 nights, return). N cheapest + M hotels each.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON for OpenClaw/machine use",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Write results to travel_helper_YYYY-MM-DD.html (full path printed to stderr)",
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
        "--num-cheapest-flights",
        type=int,
        default=10,
        metavar="N",
        help="Number of cheapest round trips to show and fetch hotels for (default: 10)",
    )
    parser.add_argument(
        "--cheapest-hotels-per-flight",
        type=int,
        default=3,
        metavar="M",
        help="Number of cheapest hotels to fetch per flight (default: 3)",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=120,
        metavar="N",
        help="Search for departures in the next N days (default: 120)",
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        metavar="ADDRESS",
        help="Send results as HTML email to ADDRESS (Gmail: set GMAIL_USER and GMAIL_APP_PASSWORD). Example: --email you@example.com",
    )
    args = parser.parse_args()
    run(
        output_json=args.json,
        output_html=args.html,
        fetch_hotels=not args.no_hotels,
        adults=args.adults,
        rooms=args.rooms,
        num_cheapest_flights=args.num_cheapest_flights,
        hotels_per_flight=args.cheapest_hotels_per_flight,
        days_ahead=args.days_ahead,
        email=args.email,
    )


if __name__ == "__main__":
    main()
