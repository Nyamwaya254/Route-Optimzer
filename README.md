# Field Sales Route Optimization System

An automated route planning system for field sales agents. The system computes
optimal daily routes using Dijkstra's shortest path algorithm over real road
networks, prioritizes shops using historical sales data, and respects operational
constraints including working hours, shop operating windows, and agent residential
endpoints.

---

## What the System Does

Field sales agents start each day at company headquarters and need to visit a
set of assigned shops before returning home. This system takes that problem and
solves it automatically — selecting which shops to visit, in what order, and
producing an interactive map each agent can follow.

The pipeline runs in five stages:

1. Data loading and validation across three input files.
2. Priority scoring — every shop is given a score based on its conversion rate,
   average order value, how long ago it was last visited, and its size.
3. Priority barrier — for each agent, only the top-ranked shops up to the daily
   target are selected for routing.
4. Constrained routing — a nearest-neighbor algorithm with three hard constraints
   builds the final route: working hours must not be exceeded, shops closed at
   arrival time are skipped, and the route ends at the agent's home.
5. Output — one interactive HTML map per agent and one Excel summary report
   covering all agents.

---

## Research Objectives Satisfied

This implementation was built against the following research objectives from the
project proposal:

Dijkstra's shortest path algorithm is used for all distance and travel time
computation via the osmnx and networkx libraries, operating entirely on locally
cached road network data with no dependency on external routing APIs.

Priority queue-based shop selection implements a multi-factor scoring model
combining conversion rate (35%), average order value (30%), visit recency (20%),
and shop size (15%). A configurable priority barrier then selects the top-k
shops per agent per day.

Multi-constraint vehicle routing handles asymmetric depots (HQ start, home end),
maximum working hours, shop operating hour time windows, and per-shop service
durations simultaneously.

Per-agent routing gives every agent an independent optimized route within their
assigned sub-cluster rather than a shared territory-wide route.

Route evaluation exports an Excel workbook recording shops visited, shops dropped
and the reason for each, total distance, total travel time, and estimated revenue
per agent.

---

## Prerequisites

Python 3.10 or higher is required.

Install all dependencies with one command:

```bash
pip install numpy pandas scikit-learn folium osmnx networkx openpyxl structlog
```

No Docker installation, no local routing server, and no external API keys are
required. Road network data is downloaded from OpenStreetMap on first run and
cached to disk automatically.

---

## Project Structure

```
project/
├── route_optimizer.py        Main program + RouteService API
├── app.py                    Web app (agent self-service routes)
├── templates/
│   └── index.html            Agent login + map UI
├── locations.xlsx            Shop data (required)
├── hq.xlsx                   Company HQ coordinates (required)
├── agents.xlsx               Agent residential coordinates (recommended)
├── graph_cache/              Road network files cached here after first run
│   └── graph_*.graphml
├── route_maps/               Generated per-agent HTML maps
│   └── route_<agent>_<sub_cluster>.html
└── route_summary.xlsx        Generated KPI report (two sheets)
```

---

## Input Files

### locations.xlsx

One row per shop. The following columns are required:

| Column | Type | Description |
|---|---|---|
| shop_id | String or Integer | Unique shop identifier |
| shop_name | String | Display name |
| latitude | Float | Decimal degrees, e.g. -1.2921 |
| longitude | Float | Decimal degrees, e.g. 36.8219 |
| cluster_code | String | Top-level territory, e.g. Parklands |
| sub_cluster | String | Routing group, e.g. Nairobi_A |
| agent_name | String | Assigned agent (must match agents.xlsx) |
| conversion_rate | Float | 0.0 to 1.0, e.g. 0.25 means 25% |
| avg_order_value | Float | Average purchase amount in KSh |
| last_visit_days | Integer | Days since the shop was last visited |

The following columns are optional. If absent, documented defaults are applied
and a warning is logged — the program does not fail.

| Column | Type | Default | Description |
|---|---|---|---|
| shop_size | String | medium | small, medium, or large |
| service_time_minutes | Float | 15 | Minutes spent at the shop |
| open_time | String HH:MM | 07:00 | When the shop opens |
| close_time | String HH:MM | 20:00 | When the shop closes |

### hq.xlsx

One row representing the company headquarters. Required columns:

| Column | Type | Description |
|---|---|---|
| latitude | Float | HQ latitude |
| longitude | Float | HQ longitude |

Optional column: `name` (String) — used only in log output.

### agents.xlsx

One row per agent. If this file is missing, routes will end at HQ rather than
at each agent's home. Required columns:

| Column | Type | Description |
|---|---|---|
| agent_name | String | Must exactly match the name in locations.xlsx |
| home_latitude | Float | Agent residential latitude |
| home_longitude | Float | Agent residential longitude |

Optional columns: `phone`, `territory` — ignored by the program.

---

## Configuration

All tunable parameters are defined in the `Config` dataclass at the top of
`route_optimizer.py`. Edit these values before running to adjust system behaviour.

```python
@dataclass
class Config:
    work_start_hour: int   = 8       # Agents leave HQ at 08:00
    max_work_hours: float  = 8.0     # Maximum working day in hours

    daily_shop_target: int      = 30  # Top-k shops selected per agent per day
    min_priority_score: float   = 0.0 # Hard floor; raise to exclude weak shops

    weight_conversion_rate:  float = 0.35
    weight_avg_order_value:  float = 0.30
    weight_recency:          float = 0.20
    weight_shop_size:        float = 0.15

    recency_decay_days: float = 30.0  # Higher = slower urgency growth over time

    default_service_time_minutes: float = 15.0
    default_open_hour:  int = 7
    default_close_hour: int = 20

    graph_buffer_deg: float = 0.025   # Road graph bounding box padding
    graph_cache_dir:  str   = "graph_cache"

    maps_dir:            str = "route_maps"
    summary_report_path: str = "route_summary.xlsx"
    map_zoom:            int = 14
```

The four priority weights must sum to 1.0. If you remove the shop_size column
from your data, redistribute its 0.15 weight across the remaining three fields.

---

## Running the Program

### Batch mode (all agents)

```bash
cd your_project_folder
python route_optimizer.py
```

### Web app (per-agent on demand)

Agents open the site, search for their name, and receive a live-generated route map
(typically 30–60 seconds using cached road graphs).

```bash
pip install flask
python app.py
```

Then open [http://localhost:8080](http://localhost:8080) in a browser.

To use a different port: `PORT=9000 python app.py`

**Do not** open `templates/index.html` directly or use Live Server — the page needs
the Flask backend to load agent names and generate routes.

The first route request for a sub-cluster loads the road graph from disk cache.
Subsequent requests for agents in the same territory are faster.

---

The first run downloads road network data from OpenStreetMap and saves it to
`graph_cache/`. Subsequent runs load from disk. See the Runbook for expected
runtimes and troubleshooting.

---

## Output

### route_maps/

One HTML file per agent named `route_<agent_name>_<sub_cluster>.html`. Open any
file directly in a web browser — no server is required.

Each map contains:
- A green home marker at HQ (start of route)
- Blue numbered markers at each shop stop, with a popup showing priority score,
  conversion rate, average order value, days since last visit, and service time
- A red home marker at the agent's residential endpoint
- Road-following coloured polylines between stops, graduated from red at the
  start to green at the end
- Directional arrows showing travel direction on each segment
- A layer toggle panel to show or hide individual stops

### route_summary.xlsx

Two sheets:

Route Summary — one row per agent with: sub-cluster, shops selected by the
priority barrier, shops actually visited, shops dropped due to time constraint,
shops dropped by the priority barrier, total distance in km, total travel time in
minutes, estimated revenue in KSh, and the map file path.

Dropped Shops — one row per shop not included in the final route, with the agent
name, shop name, priority score, and drop reason (time_constraint or
below_priority_barrier).

---

## Algorithm Summary

Distance computation uses single-source Dijkstra from each point (HQ, all shops,
agent home), which runs one graph traversal per point rather than one per pair.
For n shops this reduces graph traversals from n-squared to n.

Route ordering uses a constrained nearest-neighbor heuristic. Starting from HQ,
the algorithm repeatedly selects the nearest unvisited shop that can be reached,
serviced, and departed from while still allowing the agent to reach home before
the end of the working day. Shops that are closed at projected arrival time are
skipped. Shops where arrival is before opening time incur a wait.

---

## Limitations

The nearest-neighbor heuristic produces routes that are typically 15 to 25
percent longer than the mathematical optimum. For the shop counts targeted by
this system (30 stops per day) the gap is acceptable and the heuristic runs in
milliseconds compared to seconds or minutes for exact solvers.

Road network data reflects OpenStreetMap at the time of download. Speed limits
are imputed from OSM road type tags where explicit values are absent. Actual
travel times will vary with traffic conditions, which this system does not model.

The priority scoring weights are fixed at configuration time. The system does not
learn or update weights based on outcomes. Adjustment based on agent feedback or
measured results requires manual changes to the Config dataclass.

---

## License

For academic and internal operational use. Not licensed for redistribution.
