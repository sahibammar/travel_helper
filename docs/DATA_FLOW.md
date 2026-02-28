# Data flow in Travel Helper

How data moves from external APIs and scripts into the JSON file, then to the MCP server, the assistant, and the UI.

---

## 1. Data generation (offline)

**Script:** `travel_helper.py`  
**Output:** `data/travel_helper.json`

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  travel_helper.py (CLI, run manually or via cron)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  1. Ryanair API        → cheapest return flights (NRN/CGN/DTM → Europe)      │
│  2. Trivago MCP        → hotel search per trip (optional, --no-hotels)      │
│  3. GeoTemp REST API   → destination_info per city (if GEOTEMP_API_KEY set) │
│                         (city_profile, weather, attractions, etc.)          │
│  4. Aggregate         → group by (destination, days, nights), sort by price  │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                    data/travel_helper.json  (single file on disk)
                    • cheapest_flights_with_hotels  OR  cheapest_flights
                    • each deal: destination, days, nights, min_total_eur,
                      flights[], destination_info (if GeoTemp used)
```

- **When:** You run `python travel_helper.py [--json] [--html] [--email ...]`.
- **Where it writes:** Default path is `data/travel_helper.json` (or `--json-file PATH`). Path is printed to stderr after write.
- **No cache:** Each run overwrites the file. The assistant does not run this script; it only reads the result.

---

## 2. Stored data (single source of truth)

**File:** `data/travel_helper.json`

- One JSON file under the repo root.
- Structure: root has either `cheapest_flights_with_hotels` or `cheapest_flights` (list of deal groups). Each group has `destination`, `days`, `nights`, `min_total_eur`, `flights[]`, optional `destination_info` (GeoTemp).
- The **Travel Assistant** and **MCP server** never write this file; they only read it.
- **MCP server** reads it from disk on every tool call (no in-memory cache).

---

## 3. MCP server (local, file-based)

**Process:** `mcp_travel_helper` (started by the Travel Assistant or run standalone)  
**Transport:** Streamable HTTP on `http://127.0.0.1:8001/mcp` (or next free port)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  mcp_travel_helper (server.py)                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  Path resolution (each request):                                             │
│    TRAVEL_HELPER_JSON env → repo/data/travel_helper.json → cwd/data/...     │
│  Tools (all read from disk every time, no cache):                            │
│    • travel_deals_list(limit)        → summary list of deal groups          │
│    • travel_deals_destination(name)  → full deal + flights + destination_info│
│    • travel_deals_data_status()      → path, deals_count, file_mtime_iso    │
│    • travel_deals_search, travel_deals_cheapest, ...                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                    reads                │                returns JSON
                    data/travel_helper.json  ◄────────────────────────────
```

- **Started by:** `travel_assistant.py` at import time (daemon thread), with `TRAVEL_HELPER_JSON` set to the absolute path of `data/travel_helper.json`.
- **Data source:** Only `data/travel_helper.json`. No other APIs; no cache.

---

## 4. Travel Assistant (Flask app)

**App:** `travel_assistant.py`  
**Role:** Web UI + LLM; gets deal data via MCP, then asks Groq for an answer and streams response + deal cards to the browser.

```
  Browser                    travel_assistant.py                    MCP server
     │                              │                                    │
     │  POST /api/ask { question }  │                                    │
     │ ──────────────────────────►  │                                    │
     │                              │  call_tool("travel_deals_list")     │
     │                              │ ──────────────────────────────────►│
     │                              │  ◄─────────────────────────────────│
     │                              │  summary: [{ dest, days, nights, … }]│
     │                              │                                    │
     │                              │  call_tool("travel_deals_destination", dest)
     │                              │  (once per destination)              │
     │                              │ ──────────────────────────────────►│
     │                              │  ◄─────────────────────────────────│
     │                              │  all_details: { dest → full deal }  │
     │                              │                                    │
     │                              │  _build_llm_context(summary, all_details)
     │                              │  → text block with deals + destination_info digest
     │                              │                                    │
     │                              │  Groq API (system + context, user question)
     │                              │ ──────────────────────────────────► (internet)
     │                              │  ◄─────────────────────────────────  stream
     │                              │                                    │
     │  SSE stream: { delta } … { done, deals }                           │
     │  ◄──────────────────────────│                                    │
     │                              │                                    │
```

**Steps in code:**

1. **`/api/ask`** receives `question`.
2. **`_run_mcp(_get_all_mcp_data())`**  
   - Calls MCP **`travel_deals_list`** → `summary` (list of `{ destination, days, nights, min_total_eur, num_flights }`).  
   - For each destination in `summary`, calls **`travel_deals_destination`** → builds **`all_details`** (full deal with flights, hotels, `destination_info`).
3. **`_build_llm_context(summary, all_details)`**  
   - Turns summary + destination digests (weather, activities, etc. from `destination_info`) into one text block for the LLM.
4. **Groq** gets system message (that context) + user question; returns streamed answer.
5. **`_find_relevant_dests(answer, all_dests)`**  
   - Finds which destinations were mentioned in the answer.
6. Response stream: text deltas, then a final **`{ done: true, deals: [...] }`** with the full deal objects for the mentioned destinations so the UI can render cards.

So: **data in the assistant always comes from the MCP server**, which in turn reads **`data/travel_helper.json`** on every request.

---

## 5. HTML report (optional, from travel_helper.py)

**When:** You run `travel_helper.py --html` or `--email`.

- **`_build_html(...)`** is called with in-memory results from the same run.
- **Temperature in HTML:** `_load_geotemp_from_json(json_path)` reads **`data/travel_helper.json`** from disk (default `data/travel_helper.json`) to get `destination_info` and show temperature next to destination names.
- So the **HTML report** uses the same JSON file for temperatures; if you run `--json` and then `--html` in the same run, the file was just written, so HTML reflects the same data.

---

## 6. End-to-end summary

| Step | Component | Input | Output |
|------|-----------|--------|--------|
| 1 | `travel_helper.py` | Ryanair, Trivago, GeoTemp APIs | `data/travel_helper.json` |
| 2 | `data/travel_helper.json` | — | Single source of truth on disk |
| 3 | `mcp_travel_helper` | Reads JSON on every tool call | Tools: list, destination, status, search, … |
| 4 | `travel_assistant.py` | User question + MCP (list + per-destination) | LLM context → Groq → answer + deal cards |
| 5 | Browser | SSE stream (deltas + done + deals) | Renders answer and deal cards |

**Important:**

- There is **no cache** of the JSON in the MCP server or the assistant; each question triggers fresh MCP calls, and each MCP call reads **`data/travel_helper.json`** from disk.
- To see new data in the assistant, regenerate the file with **`travel_helper.py --json`** (from repo root so it writes to `data/travel_helper.json`). Restarting the assistant is only needed if the MCP server was pointing at a different path or an old process was still running.

---

## How the AI response “refreshes” when the JSON is overwritten

1. **No restart required.** The MCP server does not cache the file; every tool call (`travel_deals_list`, `travel_deals_destination`, etc.) reads **`data/travel_helper.json`** from disk.
2. **Refresh = ask a new question.** Once you overwrite `data/travel_helper.json` (e.g. by running `travel_helper.py --json`), the **next** question the user submits will cause the assistant to call the MCP again, which will load the updated file. The new answer will be based on the new data.
3. **Existing answers do not change.** A response already shown in the UI was built from the data that was current at request time. To see the effect of new data, the user must ask another question (or reload the page and ask).
4. **Optional check.** You can call the MCP tool `travel_deals_data_status()` to see `path`, `file_mtime_iso`, and `deals_count` and confirm the server is reading the file you expect.
