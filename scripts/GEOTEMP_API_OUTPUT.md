# GeoTemp API – Tool outputs

Base URL: `https://mcp-travel-data.onrender.com/api`  
Auth: `Authorization: Bearer <API_KEY>`. The **REST `/api/tools/...`** endpoint expects a **`gt_live_...`** API key (not `gsk_...`).

---

## 1. get_dataset_stats

**Params:** none  
**Output:**

| Key | Type | Description |
|-----|------|-------------|
| `dataset` | str | e.g. "GeoTemp Travel Dataset" |
| `total_cities` | int | e.g. 1278 |
| `total_countries` | int | e.g. 166 |
| `total_continents` | int | 8 |
| `continent_breakdown` | dict | Keys = continent names, values = counts |
| `coastal_cities`, `inland_cities` | int | Counts |
| `weather_records`, `attractions`, `city_features`, … | int | Dataset stats |
| `available_tools` | list | List of tool names (e.g. 25) |

---

## 2. get_city_profile

**Params:** `city_name` (or `city_id`)  
**Output:**

| Key | Type | Description |
|-----|------|-------------|
| `city` | dict | `name`, `country`, `country_code`, `continent`, `latitude`, `longitude`, `population`, `timezone`, `elevation_m`, `is_capital`, `is_coastal`, `climate_zone` |
| `sea_water_temp_monthly_celsius` | list or null | Monthly sea temps if coastal |
| `features` | list | 29 items: `feature`, `category`, `best_months`, `description` |

---

## 3. get_weather

**Params:** `city_name`, and one of: `month` (1–12) OR `start_date` + `end_date` (YYYY-MM-DD)

**With `month`:**

| Key | Type |
|-----|------|
| `city` | str |
| `month` | str (e.g. "March") |
| `weather_summary` | dict: `avg_temperature_mean`, `avg_temperature_max`, `avg_temperature_min`, `total_precipitation_mm`, `avg_humidity_mean`, `avg_daylight_hours`, `avg_sunshine_hours`, `days_with_data` |

**With date range:**

| Key | Type |
|-----|------|
| `city` | str |
| `date_range` | str (e.g. "2025-06-01 to 2025-06-05") |
| `days` | int |
| `daily_weather` | list of dicts: `date`, `temperature_mean`, `temperature_max`, `temperature_min`, `precipitation`, `humidity_mean`, `uv_index_max`, `daylight_hours` |

---

## 4. get_attractions

**Params:** `city_name`, optional `category`, `limit` (default 20)  
**Output:**

| Key | Type |
|-----|------|
| `city` | str |
| `count` | int |
| `attractions` | list of dicts: `name`, `category`, `popularity_score`, `latitude`, `longitude`, `description` |

---

## 5. get_seasonal_calendar

**Params:** `city_name`  
**Output:**

| Key | Type |
|-----|------|
| `city` | str |
| `calendar` | list of 12 items: `month`, `month_name`, `weather`, `top_activities` |

---

## 6. find_best_month

**Params:** `city_name`, optional `prefer_warm`, `max_rain_mm`, `min_sunshine_hours`  
**Output:**

| Key | Type |
|-----|------|
| `city` | str |
| `best_month` | str (e.g. "June") |
| `ranking` | list of 12: `month`, `month_num`, `score`, `avg_temp_c`, `total_rain_mm`, `avg_sunshine_hours`, `avg_humidity_pct` |

Note: response uses `ranking`; some clients also accept `rankings`.

---

## 7. compare_cities

**Params:** `city_names` (list of 2–5), optional `month`  
**Output:**

| Key | Type |
|-----|------|
| `comparison_month` | str |
| `cities` | list: `city`, `country`, `continent`, `daily_budget_usd`, `safety_score`, `is_coastal`, `population`, `weather_<MonthAbbr>` |

---

## 8. search_destinations

**Params:** `continent`, `country`, `is_coastal`, `min_safety_score`, `max_daily_budget_usd`, `limit`, …  
**Output:**

| Key | Type |
|-----|------|
| `count` | int |
| `cities` | list: `name`, `country`, `continent`, `latitude`, `longitude`, `population`, `daily_budget_usd`, `safety_score` |
| `geo_filter` | dict: `input`, `resolved_continent`, `geo_hint` |

---

## 9. search_by_activity

**Params:** `activity`, optional `month`, `min_score`, `continent`, `limit`  
**Output:**

| Key | Type |
|-----|------|
| `activity` | str |
| `month` | int |
| `count` | int |
| `destinations` | list: `city`, `country`, `continent`, `daily_budget_usd`, `safety_score`, `matching_features`, `month_score` |
| `geo_filter` | dict |

---

## 10. multi_activity_search

**Params:** `activities` (list of 2–6), optional `month`, `min_score`, `continent`, `limit`  
**Output:**

| Key | Type |
|-----|------|
| `activities` | list |
| `month`, `min_score` | int |
| `count` | int |
| `destinations` | list: `city`, `country`, `continent`, `daily_budget_usd`, `safety_score`, `is_coastal`, `activity_scores`, `total_score` |
| `geo_filter` | dict |

---

## 11. find_nearby_destinations

**Params:** `city_name` (or `latitude`+`longitude`), `radius_km`, `limit`  
**Output:**

| Key | Type |
|-----|------|
| `center` | dict: `latitude`, `longitude` |
| `radius_km` | int |
| `count` | int |
| `nearby_destinations` | list: `city`, `country`, `continent`, `distance_km`, `is_coastal`, `daily_budget_usd`, `safety_score`, `activity_count` |

---

## 12. find_similar_cities

**Params:** `city_name`, `limit`  
**Output:**

| Key | Type |
|-----|------|
| `reference_city` | str |
| `count` | int |
| `similar_destinations` | list: `city`, `country`, `continent`, `similarity_score`, `shared_activities`, `daily_budget_usd`, `safety_score`, `is_coastal` |

---

## 13. plan_trip

**Params:** `month`, optional `activities`, `max_budget_usd`, `continent`, `min_safety`, `is_coastal`, `limit`  
**Output:**

| Key | Type |
|-----|------|
| `month` | str (e.g. "June") |
| `filters` | dict: `activities`, `max_budget_usd`, `continent`, `resolved_continent`, `geo_hint`, `min_safety`, `is_coastal` |
| `count` | int |
| `destinations` | list: `city`, `country`, `continent`, `daily_budget_usd`, `safety_score`, `is_coastal`, `weather`, `activity_scores` |

---

## 14. get_travel_intelligence

**Params:** `city`, `month`  
**Output:**

| Key | Type |
|-----|------|
| `city`, `country`, `continent` | str |
| `month`, `month_name` | int, str |
| `climate_normal` | dict: `avg_temp`, `temp_range`, `p10_p90_temp`, `avg_rain_mm`, `sunshine_hrs`, `rain_days` |
| `air_quality` | dict: `aqi`, `category`, `pm25` |
| `terrain` | dict: `type`, `elevation`, `elevation_range`, `has_mountains` |
| `crowd` | dict: `index`, `season`, `price_index` |
| `solar` | dict: `sunrise`, `sunset`, `golden_hour_pm`, `daylight_hours` |
| `water_bodies` | list: `name`, `water_type`, `distance_from_city_km`, `swimmable` |
| `top_activities` | list: `activity`, `score` |

---

## Key naming differences

- **Weather:** Month response uses `weather_summary`; date range uses `daily_weather` (list).
- **Search:** Some tools return `cities`, others `destinations`; `compare_cities` returns `cities`.
- **Best month:** Response has `ranking` (list); client docs sometimes say `rankings`.

Run all tools and print summaries:

```bash
export GEOTEMP_API_KEY='gt_live_...'   # Use gt_live_ key for REST API
python scripts/call_geotemp_tools.py
```
