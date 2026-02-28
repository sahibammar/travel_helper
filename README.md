# Travel Helper — Developer Documentation

**Cheap round-trip flights (Ryanair API) + hotels (Trivago MCP) + weather & destinations (GeoTemp MCP).**

This document is for **developers**: technical details of the Ryanair API, Trivago MCP server, and GeoTemp MCP server, plus how to run and extend the stack.

---

## Table of Contents

1. [Overview](#overview)
2. [Requirements & Setup](#requirements--setup)
3. [Ryanair API](#ryanair-api)
4. [Trivago MCP Server](#trivago-mcp-server)
5. [GeoTemp MCP Server](#geotemp-mcp-server)
6. [How to Run](#how-to-run)
7. [Project Layout](#project-layout)
8. [Deploy on Render](#deploy-on-render)

---

## Overview

The app:

1. **Flights** — Queries Ryanair's public services API (no API key) for round trips from **Düsseldorf Weeze (NRN)**, **Köln (CGN)**, and **Dortmund (DTM)**. Outbound: **Wednesday after 18:00**, **Thursday after 17:00**, or **Friday after 11:00**; return 2–4 nights later.
2. **Hotels** — Uses the **Trivago MCP server** (Streamable HTTP) to resolve city → location and fetch accommodations for each trip's dates.
3. **Weather & destinations** — Uses the **GeoTemp Travel MCP server** (SSE) for weather, attractions, city profiles, similar/nearby cities, trip planning, and dataset stats.

Output: human-readable text, JSON (`--json`), or HTML (`--html`). Optional email of the HTML report via Gmail.

---

## Requirements & Setup

- **Python 3.10+** (required for `mcp` and Trivago/GeoTemp clients).
- **Virtual environment** (recommended):

```bash
cd /path/to/travel_helper
python3 -m venv .venv-travel
source .venv-travel/bin/activate   # Windows: .venv-travel\Scripts\activate
pip install ryanair-py requests backoff "mcp[cli]"
```

- **Ryanair**: no API key; uses `https://services-api.ryanair.com/`.
- **Trivago MCP**: public endpoint `https://mcp.trivago.com/mcp`; no auth.
- **GeoTemp MCP**: public endpoint `https://mcp-travel-data.onrender.com/sse`; no auth.

If `mcp` is not installed, the script still runs but skips hotels and GeoTemp (flights only).

---

## Ryanair API

### Source

The project uses the **ryanair-py** library (or a vendored `ryanair/`), which calls Ryanair's **Services API** directly. No API key.

- **Base URL**: `https://services-api.ryanair.com/farfnd/v4/`
- **Endpoints used**:
  - **Round-trip fares**: `roundTripFares`  
    Parameters: `departureAirportIataCode`, `outboundDepartureDateFrom`, `outboundDepartureDateTo`, `inboundDepartureDateFrom`, `inboundDepartureDateTo`, and optional time windows and `currency`.

### Technical details

- **Client**: `ryanair.Ryanair(currency="EUR")`.
- **Method**: `get_cheapest_return_flights(source_airport, date_from, date_to, return_date_from, return_date_to, ...)`.
- **Time windows** (used by travel_helper):
  - Outbound: Wednesday ≥ 18:00, Thursday ≥ 17:00, or Friday ≥ 11:00 (`outboundDepartureTimeFrom` / `outboundDepartureTimeTo`).
  - Inbound: unrestricted (full day).
- **Return structure**: list of `(outbound, inbound, outbound_price)` where each leg is a `Flight`-like object with `departureTime`, `origin`, `destination`, `originFull`, `destinationFull`, `price`, `currency`, etc.

### Code references

- **API wrapper**: `ryanair/ryanair.py` — `Ryanair.get_cheapest_return_flights()`, `get_cheapest_flights()`.
- **Usage in app**: `travel_helper.py` builds date ranges (e.g. next 120 days), filters outbound by weekday/time, then sorts by price and takes the N cheapest round trips.
- **Booking URL**: `_ryanair_booking_url()` builds the German Ryanair round-trip select URL (`https://www.ryanair.com/de/de/trip/flights/select?...`) for manual booking.

### Rate limiting & robustness

- The library uses **backoff** for retries on failures.
- No explicit rate limit is documented; use reasonable request spacing in scripts.

---

## Trivago MCP Server

### Endpoint & transport

- **URL**: `https://mcp.trivago.com/mcp`
- **Transport**: **Streamable HTTP** (MCP over HTTP). The Python client uses `mcp.client.streamable_http.streamable_http_client(TRIVAGO_MCP_URL)`.

### Tools used

| Tool | Purpose | Main parameters |
|------|---------|------------------|
| `trivago-search-suggestions` | Resolve a place name (e.g. city) to a location id and ns | `query` (string) |
| `trivago-accommodation-search` | Search accommodations for a location and dates | `id`, `ns`, `arrival`, `departure`, `adults`, `rooms` |

### Flow in the app

1. **Location resolution**  
   For each destination city (e.g. from Ryanair's `destinationFull`), the app strips airport codes and calls `trivago-search-suggestions` with the city name (and optionally a fallback query). It parses the first `(ID, NS)` from the response (JSON or regex fallback).

2. **Accommodation search**  
   With `(id, ns)` and trip dates (arrival = outbound date, departure = return date), it calls `trivago-accommodation-search` with `adults` and `rooms` (from CLI, default 2 and 1). Results are sorted by price per night and the top N are kept per trip.

### Client module

- **File**: `trivago/fetch_hotels_mcp.py`
- **Functions**:
  - `get_location_suggestion(session, query) -> (id, ns) | None` — calls `trivago-search-suggestions`.
  - `search_accommodations(session, location_id, location_ns, arrival, departure, adults=2, rooms=1) -> list[dict]` — calls `trivago-accommodation-search`.
- **Response parsing**: Handles both `structuredContent` and text content blocks; accommodates Go-style and JSON responses (e.g. `output:[...]`). Hotel entries expose fields such as `Accommodation Name`, `Price Per Stay`, `Price Per Night`, `Accommodation URL`, `Review Rating`.

### Standalone run

```bash
python -m trivago.fetch_hotels_mcp "Berlin" --arrival 2026-03-15 --departure 2026-03-18 --adults 2 --rooms 1
# Optional: --json for raw JSON, --max 10 for result count
```

### Dependencies

- `mcp` (with streamable HTTP support): `pip install "mcp[cli]"`.
- No Trivago API key; the public MCP endpoint is used as-is.

---

## GeoTemp MCP Server

### Endpoint & transport

- **URL**: `https://mcp-travel-data.onrender.com/sse`
- **Transport**: **SSE (Server-Sent Events)**. The client uses `mcp.client.sse.sse_client(GEOTEMP_MCP_URL)` and an MCP `ClientSession` over the SSE streams.

### Dataset (high level)

- **384 cities**, **115 countries**, **29 scored activities** (e.g. `beach_holiday`, `city_break`, `swimming`).
- Weather, attractions, city features, and activity scores power the 13 tools.

### Tools (13 total)

| # | Tool | Purpose |
|---|------|--------|
| 1 | `search_destinations` | Cities by continent, country, coastal, safety, budget, etc. |
| 2 | `search_by_activity` | Cities best for one activity (e.g. `swimming`) in a given month |
| 3 | `multi_activity_search` | Cities that satisfy **all** of 2–6 activities in a month |
| 4 | `find_nearby_destinations` | Destinations within `radius_km` of a city or lat/lon |
| 5 | `find_similar_cities` | "Cities like X" (climate, activities, geography) |
| 6 | `plan_trip` | "Where should I go?" — month, activities, budget, continent, etc. |
| 7 | `get_city_profile` | Full city dossier (metadata, climate, features) |
| 8 | `get_weather` | Daily or monthly weather by city and date/month |
| 9 | `get_attractions` | Tourist POIs (category, limit) |
| 10 | `get_seasonal_calendar` | 12-month weather + top activities per month |
| 11 | `find_best_month` | Best months by weather (warm/rain/sunshine) |
| 12 | `compare_cities` | Side-by-side comparison of 2–5 cities for a month |
| 13 | `get_dataset_stats` | Counts: cities, countries, attractions, etc. |

### Activity names (exact strings)

Used by `search_by_activity`, `multi_activity_search`, `plan_trip` (among others):

`adventure_sports`, `beach_holiday`, `city_break`, `cultural_sightseeing`, `cycling`, `diving`, `family_friendly`, `fishing`, `food_tourism`, `golf`, `hiking`, `nightlife`, `photography`, `rock_climbing`, `romantic_getaway`, `running_jogging`, `sailing`, `shopping`, `skiing`, `snorkeling`, `spa_wellness`, `surfing`, `swimming`, `water_sports`, `wildlife_viewing`, `wine_tasting`, `winter_sports`, `yoga_retreat`.

### Client module

- **File**: `geotemp/geotemp_fetch_mcp.py`
- **Pattern**: All functions are `async` and take a `ClientSession` as first argument; they call `session.call_tool(tool_name, params)` and parse JSON from the result (`content[0].text` or `structuredContent`).
- **Examples**:
  - `get_weather(session, city_name, start_date, end_date, month=None)` — monthly or date-range weather.
  - `get_attractions(session, city_name, category=None, limit=10)`.
  - `get_city_profile(session, city_name)`.
  - `find_best_month(session, city_name, prefer_warm=True, ...)`.
  - `find_similar_cities(session, city_name, limit=10)`.
  - `find_nearby_destinations(session, city_name=..., radius_km=500, limit=15)` or `latitude=..., longitude=...`.
  - `get_seasonal_calendar(session, city_name)`.
  - `plan_trip(session, month, activities=..., max_budget_usd=..., continent=..., limit=15)`.
  - `compare_cities(session, city_names, month=None)`.
  - `search_destinations(session, continent=..., country=..., limit=20)`.
  - `search_by_activity(session, activity, month=..., min_score=60, limit=15)`.
  - `multi_activity_search(session, activities, month=..., min_score=40, limit=15)`.
  - `get_dataset_stats(session)`.

### Use in travel_helper

- **Per destination (per trip)**: weather for trip dates, attractions, city profile, best months, similar cities, nearby destinations, seasonal calendar. These are aggregated in `travel_data` and rendered in text and HTML.
- **Once per run**: `get_dataset_stats`, `plan_trip` (e.g. Europe, first month), `compare_cities` (first 5 destinations), `search_destinations`, `search_by_activity` (e.g. `city_break`), `multi_activity_search` (e.g. `beach_holiday` + `swimming`). Results are shown in global "Dataset", "Trip ideas", "Compare destinations", "More destinations", "Top city break", "Beach & swimming" sections.

### Dependencies

- `mcp` (SSE client): `pip install "mcp[cli]"`. No API key for the public GeoTemp SSE endpoint.

---

## Travel Helper MCP Server (file-based)

A **local MCP server** that reads **data/travel_helper.json** (the JSON produced by `travel_helper.py`) and exposes tools to list and search deals — no Trivago/GeoTemp APIs, data comes from the file.

- **Package**: `mcp_travel_helper/` (run with `python -m mcp_travel_helper`).
- **Data**: `data/travel_helper.json` (default path; override with `TRAVEL_HELPER_JSON`).
- **Tools**: `travel_deals_list`, `travel_deals_search`, `travel_deals_destination`, `travel_deals_cheapest`, `travel_deals_flights_for_destination`.

See [mcp_travel_helper/README.md](mcp_travel_helper/README.md) for tool descriptions and Cursor/VS Code MCP config. **Docker**: [mcp_travel_helper/ci/README.md](mcp_travel_helper/ci/README.md) — build and run the MCP server in a container.

---

## How to Run

### One-off (recommended: use venv)

```bash
# From project root
source .venv-travel/bin/activate   # or .venv-travel\Scripts\activate on Windows

# Human-readable (default: 10 cheapest round trips, 3 hotels per trip)
python travel_helper.py

# JSON (e.g. for pipelines / OpenClaw)
python travel_helper.py --json

# HTML report (writes travel_helper.html)
python travel_helper.py --html

# Flights only (no Trivago / no GeoTemp)
python travel_helper.py --no-hotels

# Tuning
python travel_helper.py --num-cheapest-trips 5 --adults 2 --rooms 1 --days-ahead 120
```

### CLI options (summary)

| Option | Default | Description |
|--------|---------|-------------|
| `--json` | — | Machine-readable JSON |
| `--html` | — | Write `travel_helper.html` (path on stderr) |
| `--no-hotels` | — | Skip Trivago (flights only; GeoTemp still used if available) |
| `--adults` | 2 | Adults for hotel search |
| `--rooms` | 1 | Rooms for hotel search |
| `--num-cheapest-trips` | 100 | Number of cheapest round trips to fetch (one cheapest hotel per trip when not --no-hotels) |
| `--days-ahead` | 90 | Search window for outbound dates |
| `--email` | — | Send HTML report to this email (Gmail; requires `GMAIL_USER` and `GMAIL_APP_PASSWORD`) |

### Email (Gmail)

```bash
export GMAIL_USER=your@gmail.com
export GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
python travel_helper.py --html --email recipient@example.com
```

### Scheduled run (GitHub Actions)

A workflow runs daily at **7 AM Germany time** (6:00 UTC) (see [.github/workflows/daily-travel-helper.yml](.github/workflows/daily-travel-helper.yml)) with:

- `--num-cheapest-trips 100 --days-ahead 90 --html`

The generated HTML report is uploaded as an artifact (retention 14 days). To run manually: **Actions** → **Daily travel helper** → **Run workflow**. To get the report by email from the workflow, add repo secrets `GMAIL_USER` and `GMAIL_APP_PASSWORD` and uncomment the `env` block in the workflow.

### Troubleshooting

- **"Trivago MCP not installed"** — Install in the same env: `pip install "mcp[cli]"`. Python 3.10+ required.
- **No hotels / HTTPS errors** — Use a Python build with proper SSL (e.g. OpenSSL 1.1.1+). Run with the project venv that has `mcp` installed.
- **GeoTemp unavailable** — If the GeoTemp import fails (e.g. missing `mcp` or SSE), the app still runs with flights and Trivago only; GeoTemp sections are omitted.

---

## Project Layout

```
travel_helper/
├── travel_helper.py       # Main script: Ryanair + Trivago + GeoTemp orchestration
├── data/
│   └── travel_helper.json # Generated JSON (consumed by mcp_travel_helper)
├── mcp_travel_helper/     # MCP server: reads data/travel_helper.json, exposes deal tools (stdio + streamable-http)
│   ├── server.py
│   ├── __main__.py
│   ├── README.md
│   └── ci/                # Docker: Dockerfile and sample data for MCP server image
│       ├── Dockerfile
│       ├── .dockerignore
│       └── README.md
├── geotemp/               # GeoTemp MCP client (SSE, 13 tools)
│   └── geotemp_fetch_mcp.py
├── trivago/
│   └── fetch_hotels_mcp.py # Trivago MCP client (Streamable HTTP; suggestions + accommodation search)
├── ryanair/               # Ryanair API client (or use ryanair-py from PyPI)
│   ├── ryanair.py
│   ├── SessionManager.py
│   └── ...
├── README.md              # User/developer docs (this file in repo)
├── setup.py               # Optional package setup
└── travel_helper.html     # Generated report (optional)
```

---

## Summary

| Component | Protocol / API | Auth | Role |
|-----------|----------------|------|------|
| **Ryanair** | REST (`services-api.ryanair.com`) | None | Cheapest return flights NRN/CGN/DTM → Europe |
| **Trivago MCP** | MCP over Streamable HTTP (`mcp.trivago.com`) | None | Location suggestions + accommodation search |
| **GeoTemp MCP** | MCP over SSE (`mcp-travel-data.onrender.com/sse`) | None | Weather, attractions, city/destination intelligence, trip ideas |
| **Travel Helper MCP** | MCP over stdio (local) | None | Query `data/travel_helper.json`: list/search deals, destination details, cheapest |

All three external services are integrated in `travel_helper.py`; Trivago and GeoTemp are optional and degrade gracefully if the MCP stack is not installed. The Travel Helper MCP server is a separate process that reads the generated JSON.

---

## Deploy on Render

The **Travel Deals Assistant** (Flask app in `travel_assistant.py`) can be deployed as a web service on [Render](https://render.com). Render builds and deploys on every push to your linked branch ([automatic deploys](https://render.com/docs/deploys#automatic-deploys)).

### One-click or Blueprint

1. **Connect the repo** in the [Render Dashboard](https://dashboard.render.com): **New → Web Service**, then connect your GitHub/GitLab/Bitbucket repo.
2. **Use the Blueprint** (recommended): **New → Blueprint**, then point Render at this repo. It will read `render.yaml` and create the web service with the correct build and start commands.
3. **Or set manually**:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 travel_assistant:app`

### Environment variables

Set these in the service **Environment** (Render Dashboard → your service → Environment):

| Variable | Required | Description |
|----------|----------|-------------|
| `GROQ_API_KEY` | Yes (for LLM) | Groq API key for the assistant LLM. Users can also paste a key in the app UI. |
| `TRAVEL_HELPER_JSON` | Optional | Path to deal data JSON; default is `data/travel_helper.json`. On Render the filesystem is ephemeral, so deploy without this to use an empty deal list, or mount a [persistent disk](https://render.com/docs/disks) and set this path. |

### Python version

Render uses the version in `.python-version` (e.g. `3.11.2`) or the `PYTHON_VERSION` environment variable. See [Setting your Python version](https://render.com/docs/python-version).

### Notes

- The app binds to `0.0.0.0` and the port from the `PORT` environment variable ([Render port binding](https://render.com/docs/web-services#port-binding)).
- The assistant starts the local **mcp_travel_helper** server in a background thread; without `data/travel_helper.json` (or with an empty one), deal listings will be empty until you run `travel_helper.py` and upload the JSON or point `TRAVEL_HELPER_JSON` to a persistent file.
