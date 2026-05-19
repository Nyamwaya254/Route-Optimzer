# Runbook — Field Sales Route Optimization System

This document covers installation, first-run setup, day-to-day operation,
expected runtimes, and procedures for diagnosing and resolving common failures.
It is intended for the person running the system, not a developer.

---

## Table of Contents

1. First-Time Setup
2. Day-to-Day Operation
3. Expected Runtimes
4. Understanding the Logs
5. Output Verification Checklist
6. Failure Scenarios and Fixes
7. Changing Configuration
8. Updating Input Data
9. Resetting the System

---

## 1. First-Time Setup

This section is run once. After completing it, move to section 2 for daily use.

### 1.1 Verify Python Version

Open a terminal and run:

```bash
python --version
```

The output must show Python 3.10 or higher. If it shows Python 2.x or an older
3.x version, install Python 3.12 from python.org before continuing.

On some systems the command is `python3` rather than `python`. If that is the
case on your machine, replace `python` with `python3` in all commands in this
document.

### 1.2 Install Dependencies

```bash
pip install numpy pandas scikit-learn folium osmnx networkx openpyxl structlog
```

If pip reports a permission error, run:

```bash
pip install --user numpy pandas scikit-learn folium osmnx networkx openpyxl structlog
```

Verify the install completed without errors. It is acceptable for pip to show
warnings about dependency versions. Errors (lines beginning with ERROR:) are not
acceptable and must be resolved before continuing.

### 1.3 Create Your Project Folder

Create a folder with any name. All commands below assume you are inside this
folder in your terminal.

```bash
mkdir route_optimizer
cd route_optimizer
```

Copy the following files into this folder:

```
route_optimizer/
├── route_optimizer.py
├── locations.xlsx
├── hq.xlsx
└── agents.xlsx
```

### 1.4 Prepare hq.xlsx

Open the file and confirm it has one data row with at minimum a `latitude` and
`longitude` column containing your company headquarters coordinates as decimal
degrees.

To find coordinates for any building: open Google Maps in a browser, right-click
the building, and the latitude and longitude appear at the top of the context
menu. The first number is latitude, the second is longitude.

Example of a correctly filled hq.xlsx:

```
name           latitude    longitude   address
Company HQ     -1.2921     36.8219     Nairobi CBD, Kenya
```

Save and close the file.

### 1.5 Prepare agents.xlsx

Open the file. It must contain one row per field sales agent with three required
columns: `agent_name`, `home_latitude`, `home_longitude`.

The value in `agent_name` must be spelled exactly as it appears in the
`agent_name` column of locations.xlsx, including capitalisation and spacing.

Collect each agent's home coordinates using Google Maps and fill in the file.
If an agent's home is not available, leave that row with the placeholder
coordinates from the template. The system will route that agent back to HQ
and log a warning. All other agents are unaffected.

### 1.6 Prepare locations.xlsx



If you are starting from your original file, add these four columns:

`shop_size` — enter small, medium, or large for each shop. Small is a duka or
kiosk. Medium is a mid-size shop or superette. Large is a wholesale distributor
or large supermarket.

`service_time_minutes` — the number of minutes an agent typically spends at that
shop. Suggested starting values: 10 for small, 15 for medium, 25 for large.

`open_time` — the time the shop opens, formatted as HH:MM, for example 07:00
or 08:30.

`close_time` — the time the shop closes, formatted as HH:MM, for example 18:00
or 20:00.

If you leave these columns out entirely the system will apply defaults and warn
you — it will not crash. Adding accurate values produces better routes.

### 1.7 First Run

```bash
python route_optimizer.py
```

The first run downloads road network data from OpenStreetMap for the Nairobi
and Machakos regions. This requires an internet connection and takes between
5 and 15 minutes depending on your connection speed. The data is saved to a
folder named `graph_cache/` in your project directory. All future runs load
from this cache and do not require internet access.

Do not interrupt the program during the graph download phase. If interrupted,
delete the partial `.graphml` files from `graph_cache/` before restarting.

---

## 2. Day-to-Day Operation

After first-time setup is complete, the daily procedure is:

### Step 1 — Update locations.xlsx

Before each run, update the `last_visit_days` column to reflect how many days
ago each shop was last visited. This column directly affects shop prioritization.
Shops that have not been visited in a long time score higher and are more likely
to be included in the day's route.

If your CRM exports a fresh locations file each day, replace the file and ensure
the four columns added in setup (shop_size, service_time_minutes, open_time,
close_time) are preserved.

### Step 2 — Run the Program

```bash
cd field_sales_routes
python route_optimizer.py
```

### Step 3 — Distribute Outputs

Once the program completes, the `route_maps/` folder contains one HTML file
per agent. Send or share each agent's file with them. The file opens in any
web browser on any device including phones — no app installation required.

Open `route_summary.xlsx` to review the day's KPIs across all agents.

---

## 3. Expected Runtimes

All times assume the road network graphs are already cached. Add 5 to 15 minutes
for the very first run.

| Dataset size | Agents | Expected runtime |
|---|---|---|
| 1,575 shops, 4 sub-clusters | 15 | 20 to 45 minutes |
| Under 500 shops | Under 8 | 5 to 10 minutes |
| 5,000 shops | 30 to 50 | 2 to 4 hours |

The dominant cost is building distance matrices via Dijkstra. Each agent
requires one Dijkstra sweep per selected shop (default 30 sweeps). On a
standard laptop this runs at roughly 2 to 5 seconds per sweep depending on
road network density.

If runtimes are too long, reduce `daily_shop_target` in the Config section
of route_optimizer.py. Halving the shop target halves the Dijkstra cost.

---

## 4. Understanding the Logs

The program prints structured logs to the terminal as it runs. Each line has a
level (info, warning, error) and a set of key-value fields. You do not need to
read every line — the important ones to watch are:

```
[info]    locations_loaded        — confirms file was read; check rows and agents count
[info]    hq_loaded               — confirms HQ coordinates were read
[warning] agent_home_fallback_to_hq  — agent missing from agents.xlsx; route ends at HQ
[warning] shop_size_missing       — optional column absent; defaults applied
[warning] operating_hours_missing — optional columns absent; defaults applied
[info]    graph_downloading       — road data download has started
[info]    graph_cached_to_disk    — download complete; will not repeat
[info]    graph_loaded_from_cache — using existing cached graph (normal after first run)
[info]    priority_barrier_applied — shows how many shops selected per agent
[info]    route_complete          — shows visited, dropped, distance, time for one agent
[info]    map_saved               — confirms HTML file was written
[info]    report_exported         — confirms route_summary.xlsx was written
[error]   dijkstra_sweep_failed   — a shop could not be reached; see section 6
[error]   agent_routing_failed    — one agent failed; others continue
[error]   sub_cluster_failed      — entire sub-cluster failed; others continue
```

A run that produces no `error` lines and ends with `pipeline_complete` is
successful. Warnings are informational and do not indicate a problem.

---

## 5. Output Verification Checklist

After every run, verify the following before distributing maps to agents:

Check that `route_maps/` contains one HTML file per agent. If an agent's file
is missing, look in the terminal logs for an `agent_routing_failed` or
`agent_skipped_too_few_shops` line for that agent's name.

Open one or two HTML maps in a browser and confirm: the green marker is at HQ,
the red marker is at an agent home location (not HQ), the coloured route lines
follow roads (not straight lines across open space), and clicking a blue marker
shows the correct shop name and priority score.

Open `route_summary.xlsx` and confirm the Route Summary sheet has one row per
agent. Check that the Shops Visited column is greater than zero for each agent.
If any agent shows zero shops visited, see section 6.

Confirm the Dropped Shops sheet exists if shops were excluded. The drop reason
column should contain either `time_constraint` or `below_priority_barrier`.

---

## 6. Failure Scenarios and Fixes

### The program crashes immediately with "Missing required columns"

The error message lists exactly which columns are absent from which file.
Open that file, add the missing columns with the correct header spelling
(case-sensitive), and re-run.

### "hq.xlsx has no data rows"

The hq.xlsx file was saved with headers but no data row beneath them.
Open the file, add your HQ coordinates in row 2, save, and re-run.

### An agent has zero shops visited

This means the constrained routing could not fit any shops within the working
hour budget. The most common causes are:

The daily_shop_target is set too high relative to the number of shops actually
assigned to the agent. If an agent only has 15 shops in their territory but the
target is 30, the selected 15 may genuinely not fit in 8 hours after accounting
for service time and travel.

Service times are too high. Open locations.xlsx and check the
`service_time_minutes` column for that agent's shops. If any values are
unrealistically large (over 60 minutes for a regular stop), correct them.

The working day is set too short. Check `max_work_hours` in the Config.

The agent's home is very far from their territory. If the system must reserve a
large time budget for the return journey, fewer shops fit. Verify the agent's
home coordinates in agents.xlsx are correct.

### An agent's route ends at HQ instead of their home

The agent is missing from agents.xlsx or the name does not match exactly.
Check for extra spaces, different capitalisation, or a typo. The name must
match the `agent_name` value in locations.xlsx character for character.

### "dijkstra_sweep_failed" appears in the logs

A shop's coordinates could not be matched to a reachable point on the road
network. This usually means the shop coordinates are in a location the road
graph does not cover — for example, a shop whose coordinates were entered as
the centre of a building in a private compound with no road access.

Check the coordinates of the flagged shop in locations.xlsx against Google Maps
and correct them if wrong. If the location is genuinely inaccessible by road,
remove the shop row or mark it inactive.

### "graph_downloading" appears on every run (not caching)

The graph_cache/ folder may have been deleted, or the bounding box for the
territory has changed (because shops were added that extend outside the previous
bounds). The download will complete and a new cache file will be written.
This is not an error; it is expected behaviour when the territory changes.

### The program hangs at "graph_downloading_from_osm" for over 30 minutes

Your internet connection may have dropped or the OpenStreetMap server is
temporarily slow. Press Ctrl+C to stop the program, delete any incomplete
`.graphml` files from `graph_cache/`, and re-run when your connection is stable.

### A map file opens but shows only a blank map or straight lines between stops

The road graph was downloaded but is missing coverage for some segments. This
can happen if the graph bounding box was computed from shop coordinates only
and the HQ or an agent's home falls outside it. The system falls back to
straight-line segments for those gaps — the route is still navigable but the
drawn line does not follow the road.

To fix: increase `graph_buffer_deg` in the Config from 0.025 to 0.05 and delete
the affected graph file from `graph_cache/` so it is re-downloaded with wider
bounds.

---

## 7. Changing Configuration

Open `route_optimizer.py` in any text editor. The Config block is near the top
of the file, clearly labelled. Edit the values and save the file before running.

Common adjustments:

To increase or decrease the number of shops per agent per day, change
`daily_shop_target`. This is the most impactful single parameter.

To extend the working day, change `max_work_hours` from 8.0 to 9.0 or 10.0.

To change when agents leave HQ, change `work_start_hour`. The value is in 24-hour
format, so 8 means 08:00 and 7 means 07:00.

To increase the minimum acceptable priority score (excluding weak shops entirely
rather than just ranking them last), raise `min_priority_score` from 0.0 to
a value like 0.2 or 0.3. A shop scoring below this threshold is excluded even
if it would be in the top-k by rank.

To adjust how urgently long-unvisited shops are prioritised, change
`recency_decay_days`. A lower value (e.g. 15) makes the recency score grow
faster — shops unvisited for two weeks score almost as high as shops unvisited
for a month. A higher value (e.g. 60) spreads the urgency over a longer window.

---

## 8. Updating Input Data

### Adding new shops

Add rows to locations.xlsx. Ensure all required columns are filled. Include
values for shop_size, service_time_minutes, open_time, and close_time.

If the new shops are in a geographic area not previously covered, the road graph
bounding box will expand on the next run and the graph will be re-downloaded.
Delete the relevant `.graphml` file from `graph_cache/` to force a fresh download.

### Removing shops

Delete the row from locations.xlsx. No other action is required.

### Adding a new agent

Add a row to agents.xlsx with the agent's name and home coordinates.
Add shops in locations.xlsx with the agent's name in the `agent_name` column.
The agent is included in the next run automatically.

### Changing an agent's territory

Update the `agent_name` column in locations.xlsx for the affected shops.
No changes to agents.xlsx are needed unless the agent is new.

### Changing agent home coordinates

Update the `home_latitude` and `home_longitude` values in agents.xlsx.
The change takes effect on the next run.

---

## 9. Resetting the System

### Reset only the outputs (keep cached graphs)

Delete the `route_maps/` folder and `route_summary.xlsx`. The next run
regenerates them from scratch. Graph cache is preserved so no re-download occurs.

### Full reset (re-download road data on next run)

Delete the `route_maps/` folder, `route_summary.xlsx`, and the `graph_cache/`
folder. The next run re-downloads all road network data. Requires internet access
and 5 to 15 minutes for the download phase.

### Reset a single territory's road graph

Delete only the `.graphml` file inside `graph_cache/` whose filename corresponds
to the bounding box of the territory you want to refresh. The filename encodes
the bounding box coordinates. On the next run, only that territory is
re-downloaded.

---

## Support

For issues not covered in this runbook, review the full terminal log output and
identify the first `error` level line. The key-value fields on that line (agent
name, sub-cluster, shop name, error message) identify exactly where the failure
occurred. All failures are isolated — one agent or sub-cluster failing does not
prevent the rest of the run from completing.
