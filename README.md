# Ryanair Python
![Testing status](https://github.com/cohaolain/ryanair-py/actions/workflows/python-app.yml/badge.svg) [![Coverage Status](https://coveralls.io/repos/github/cohaolain/ryanair-py/badge.svg?branch=develop)](https://coveralls.io/github/cohaolain/ryanair-py?branch=develop)

This module allows you to retrieve the cheapest flights, with or without return flights, within a fixed set of dates.

This is done directly through Ryanair's API, and does not require an API key.

## Disclaimer
> __DISCLAIMER:__ This library is not affiliated, endorsed, or sponsored by Ryanair or any of its affiliates.  
> All trademarks related to Ryanair and its affiliates are owned by the relevant companies.  
> The author(s) of this library assume no responsibility for any consequences resulting from the use of this library.  
> The author(s) of this library also assume no liability for any damages, losses, or expenses that may arise from the use of this library.  
> Any use of this library is entirely at the user's own risk.  
> It is solely the user's responsibility to ensure compliance with Ryanair's terms of use and any applicable laws 
> and regulations.  
> The library is an independent project aimed at providing a convenient way to interact with the Ryanair API, allowing
> individuals to find flights for personal use, and then ultimately purchase them via Ryanair's website.
> While the author(s) will make efforts to ensure the library's functionality, they do not guarantee the accuracy,
> completeness, or timeliness of the information provided.  
> The author(s) do not guarantee the availability or continuity of the library, and updates may not be guaranteed.  
> Support for this library may be provided at the author(s)'s discretion, but it is not guaranteed.  
> Users are encouraged to report any issues or feedback to the author(s) via appropriate channels.  
> By using this library, users acknowledge that they have read, understood, and agreed to the terms of this disclaimer.

## Installation
Run the following command in the terminal:
```
pip install ryanair-py
```
## Usage
To create an instance:
```python
from ryanair import Ryanair

# You can set a currency at the API instance level, so could also be GBP etc. also.
# Note that this may not *always* be respected by the API, so always check the currency returned matches
# your expectation.
api = Ryanair("EUR")
```
### Get the cheapest one-way flights
Get the cheapest flights from a given origin airport (returns at most 1 flight to each destination).
```python
from datetime import datetime, timedelta
from ryanair import Ryanair
from ryanair.types import Flight

api = Ryanair(currency="EUR")  # Euro currency, so could also be GBP etc. also
tomorrow = datetime.today().date() + timedelta(days=1)

flights = api.get_cheapest_flights("DUB", tomorrow, tomorrow + timedelta(days=1))

# Returns a list of Flight namedtuples
flight: Flight = flights[0]
print(flight)  # Flight(departureTime=datetime.datetime(2023, 3, 12, 17, 0), flightNumber='FR9717', price=31.99, currency='EUR' origin='DUB', originFull='Dublin, Ireland', destination='GOA', destinationFull='Genoa, Italy')
print(flight.price)  # 9.78
```
### Get the cheapest return trips (outbound and inbound)
```python
from datetime import datetime, timedelta
from ryanair import Ryanair

api = Ryanair(currency="EUR")  # Euro currency, so could also be GBP etc. also
tomorrow = datetime.today().date() + timedelta(days=1)
tomorrow_1 = tomorrow + timedelta(days=1)

trips = api.get_cheapest_return_flights("DUB", tomorrow, tomorrow, tomorrow_1, tomorrow_1)
print(trips[0])  # Trip(totalPrice=85.31, outbound=Flight(departureTime=datetime.datetime(2023, 3, 12, 7, 30), flightNumber='FR5437', price=49.84, currency='EUR', origin='DUB', originFull='Dublin, Ireland', destination='EMA', destinationFull='East Midlands, United Kingdom'), inbound=Flight(departureTime=datetime.datetime(2023, 3, 13, 7, 45), flightNumber='FR5438', price=35.47, origin='EMA', originFull='East Midlands, United Kingdom', destination='DUB', destinationFull='Dublin, Ireland'))
```

## Travel helper (flights + hotels)

The `travel_helper.py` script finds **round trips** from Düsseldorf Weeze (NRN) and Köln (CGN): outbound on **Thursday after 5 pm** or **Friday after 11 pm**, **3–4 nights** at destination, return to Weeze/Köln. It picks the 10 cheapest such trips and fetches M hotel options per trip (4 nights) from the Trivago MCP server.

### Setup

Use a virtual environment with SSL and MCP support (e.g. project venv):

```bash
python3 -m venv .venv-travel
.venv-travel/bin/pip install ryanair-py requests backoff "mcp[cli]"
```

**Python 3.10+ required for hotels.** The `mcp` package (Trivago MCP client) needs Python 3.10 or newer. If `pip3 install "mcp[cli]"` fails with "Could not find a version that satisfies the requirement mcp[cli]", use a newer Python: e.g. on macOS run `brew install python@3.12`, then `python3.12 -m venv .venv-travel` and `.venv-travel/bin/pip install ryanair-py requests backoff "mcp[cli]"`.

**Running on another machine / “No hotels”?** Run the script with the **project venv** and ensure `mcp[cli]` is installed in that env (`pip install "mcp[cli]"`). If you use system `python3` without the venv, the script will show flights but report no hotels because the Trivago MCP client is missing there. You should then see: *“Trivago MCP not installed: pip install 'mcp[cli]' for hotels.”* If you see a urllib3/OpenSSL warning (e.g. LibreSSL 2.8.3), HTTPS to the Trivago MCP may fail; use a Python built with OpenSSL 1.1.1+ or the project venv where possible.

### Run

```bash
# Default: 10 cheapest round trips, 3 hotels per trip (human-readable)
.venv-travel/bin/python travel_helper.py

# JSON output (e.g. for OpenClaw)
.venv-travel/bin/python travel_helper.py --json

# HTML output (writes travel_helper_YYYY-MM-DD.html, full path on stderr)
.venv-travel/bin/python travel_helper.py --html

# Flights only, no hotel fetch
.venv-travel/bin/python travel_helper.py --no-hotels

# Custom number of flights and hotels per flight
.venv-travel/bin/python travel_helper.py --num-cheapest-flights 5 --cheapest-hotels-per-flight 3

# Hotel search parameters
.venv-travel/bin/python travel_helper.py --adults 2 --rooms 1
```

### Options (all `--` options)

| Option | Default | Description |
|--------|---------|-------------|
| `--json` | — | Machine-readable JSON output |
| `--html` | — | Write results to travel_helper_YYYY-MM-DD.html (full path printed to stderr) |
| `--no-hotels` | — | Skip Trivago hotel fetch (flights only) |
| `--adults` | 2 | Adults for hotel search |
| `--rooms` | 1 | Rooms for hotel search |
| `--num-cheapest-flights` | 10 | Number of cheapest round trips to show and fetch hotels for |
| `--cheapest-hotels-per-flight` | 3 | Number of cheapest hotels to fetch per flight |
| `--days-ahead` | 120 | Search for departures in the next N days |
| `--email` | — | Send results as HTML to this address (via Gmail; see below) |

**Email (Gmail):** To use `--email you@example.com`, set two environment variables (Gmail account that sends the message, and an [App Password](https://support.google.com/accounts/answer/185833)):

```bash
export GMAIL_USER=your.gmail@gmail.com
export GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
.venv-travel/bin/python travel_helper.py --num-cheapest-flights 20 --days-ahead 180 --cheapest-hotels-per-flight 1 --html --email ammar.sahib@yahoo.com 
```
