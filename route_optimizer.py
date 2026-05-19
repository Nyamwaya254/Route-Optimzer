"""
Field Sales Route Optimization System
======================================
Satisfies all research objectives:
  ✅ Dijkstra's algorithm for distance/duration matrices (via osmnx + networkx)
  ✅ Priority scoring: conversion_rate, avg_order_value, last_visit_days, shop_size
  ✅ Priority barrier: top-k shop selection per agent per day
  ✅ Per-agent routing (each agent gets their own independent route)
  ✅ Asymmetric depot: HQ start → shops → agent residential end
  ✅ Working hours constraint (configurable, default 8h)
  ✅ Service time per shop (minutes spent at each stop)
  ✅ Shop operating hours / time windows
  ✅ Uses existing sub_cluster column — no redundant K-Means
  ✅ Interactive Folium maps per agent
  ✅ Route summary export (Excel: Route Summary + Dropped Shops sheets)

Required files
--------------
  locations.xlsx  — shop data (see column spec below)
  hq.xlsx         — company HQ coordinates
  agents.xlsx     — agent residential coordinates

Run
---
  python route_optimizer.py
"""

import os
import warnings

os.environ["OMP_NUM_THREADS"] = "2"
warnings.filterwarnings("ignore", category=FutureWarning)

from pathlib import Path
from datetime import time as dt_time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import networkx as nx
import osmnx as ox
import folium
import structlog

from folium.plugins import PolyLineTextPath, FeatureGroupSubGroup

log = structlog.get_logger()

# ===========================================================================
# CONFIGURATION
# All operational parameters in one place — change these to tune the system.
# ===========================================================================


@dataclass
class Config:
    # ── Working day ──────────────────────────────────────────────────────────
    work_start_hour: int = 8  # Agents depart HQ at 08:00
    max_work_hours: float = 8.0  # 8-hour working day (480 minutes)

    # ── Daily shop target (priority barrier) ─────────────────────────────────
    daily_shop_target: int = 30  # Top-k shops selected per agent per day
    min_priority_score: float = (
        0.0  # Hard floor — raise to e.g. 0.2 to exclude weak shops
    )

    # ── Priority score weights (must sum to 1.0) ─────────────────────────────
    weight_conversion_rate: float = 0.35
    weight_avg_order_value: float = 0.30
    weight_recency: float = 0.20
    weight_shop_size: float = 0.15

    # Recency decay constant (days): higher → slower urgency growth with time
    recency_decay_days: float = 30.0

    # Shop size encoding
    shop_size_map: dict = field(
        default_factory=lambda: {"small": 0.33, "medium": 0.66, "large": 1.0}
    )
    default_shop_size_score: float = 0.5  # used when shop_size column is absent

    # ── Fallback defaults (used when optional columns are absent) ─────────────
    default_service_time_minutes: float = 15.0
    default_open_hour: int = 8
    default_close_hour: int = 20

    # ── Road graph ───────────────────────────────────────────────────────────
    graph_buffer_deg: float = 0.025  # Bounding-box padding (~2.5 km at equator)
    graph_cache_dir: str = "graph_cache"  # Graphs saved here between runs

    # ── Output ───────────────────────────────────────────────────────────────
    maps_dir: str = "route_maps"
    summary_report_path: str = "route_summary.xlsx"
    map_zoom: int = 14


CFG = Config()


# ===========================================================================
# DATA LOADING & VALIDATION
# ===========================================================================


def _time_to_minutes(val) -> int:
    """Convert HH:MM string, datetime.time, or integer to minutes from midnight."""
    if isinstance(val, dt_time):
        return val.hour * 60 + val.minute
    if isinstance(val, str):
        parts = val.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    if isinstance(val, (int, float)):
        return int(val)
    return CFG.default_open_hour * 60


def load_and_validate(
    locations_path: str = "locations.xlsx",
    hq_path: str = "hq.xlsx",
    agents_path: str = "agents.xlsx",
) -> tuple[pd.DataFrame, dict, dict]:
    """
    Load and validate all three input files.

    Returns:
        locations_df: Full shop dataset with all required + injected columns.
        hq:           {'latitude': float, 'longitude': float}
        agent_homes:  {agent_name: {'latitude': float, 'longitude': float}}
                      Empty dict if agents.xlsx is missing — routes then end at HQ.
    """
    # ── locations.xlsx ───────────────────────────────────────────────────────
    df = pd.read_excel(locations_path)

    required_cols = {
        "shop_id",
        "shop_name",
        "latitude",
        "longitude",
        "cluster_code",
        "conversion_rate",
        "avg_order_value",
        "last_visit_days",
        "agent_name",
        "sub_cluster",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"locations.xlsx is missing columns: {missing}")

    # Inject optional columns with documented defaults if absent
    if "shop_size" not in df.columns:
        log.warning(
            "shop_size_missing_using_default",
            default="medium",
            note="Add a 'shop_size' column (small/medium/large) for better scoring",
        )
        df["shop_size"] = "medium"

    if "service_time_minutes" not in df.columns:
        log.warning(
            "service_time_missing_using_default",
            minutes=CFG.default_service_time_minutes,
        )
        df["service_time_minutes"] = CFG.default_service_time_minutes

    if "open_time" not in df.columns or "close_time" not in df.columns:
        log.warning(
            "operating_hours_missing_using_default",
            open=f"{CFG.default_open_hour:02d}:00",
            close=f"{CFG.default_close_hour:02d}:00",
        )
        df["open_time_minutes"] = CFG.default_open_hour * 60
        df["close_time_minutes"] = CFG.default_close_hour * 60
    else:
        df["open_time_minutes"] = df["open_time"].apply(_time_to_minutes)
        df["close_time_minutes"] = df["close_time"].apply(_time_to_minutes)

    # Validate coordinates
    bad = ~(df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180))
    if bad.any():
        log.warning("invalid_coordinates_dropped", count=int(bad.sum()))
        df = df[~bad].copy()

    log.info(
        "locations_loaded",
        rows=len(df),
        agents=df["agent_name"].nunique(),
        sub_clusters=df["sub_cluster"].nunique(),
    )

    # ── hq.xlsx ──────────────────────────────────────────────────────────────
    hq_df = pd.read_excel(hq_path)
    if not {"latitude", "longitude"}.issubset(hq_df.columns):
        raise ValueError("hq.xlsx must contain 'latitude' and 'longitude' columns")
    if hq_df.empty:
        raise ValueError("hq.xlsx has no data rows")

    hq = {
        "latitude": float(hq_df.iloc[0]["latitude"]),
        "longitude": float(hq_df.iloc[0]["longitude"]),
    }
    hq_name = hq_df.iloc[0].get("name", "HQ")
    log.info("hq_loaded", name=hq_name, lat=hq["latitude"], lon=hq["longitude"])

    # ── agents.xlsx (optional) ───────────────────────────────────────────────
    agent_homes: dict = {}
    try:
        ag_df = pd.read_excel(agents_path)
        if {"agent_name", "home_latitude", "home_longitude"}.issubset(ag_df.columns):
            for _, row in ag_df.iterrows():
                agent_homes[row["agent_name"]] = {
                    "latitude": float(row["home_latitude"]),
                    "longitude": float(row["home_longitude"]),
                }
            log.info("agent_homes_loaded", count=len(agent_homes))
        else:
            log.warning(
                "agents_xlsx_wrong_columns",
                required="agent_name, home_latitude, home_longitude",
                found=list(ag_df.columns),
            )
    except FileNotFoundError:
        log.warning(
            "agents_xlsx_not_found",
            effect="Routes will end at HQ instead of agent homes",
        )

    return df, hq, agent_homes


# ===========================================================================
# PRIORITY SCORING
# ===========================================================================


def compute_priority_scores(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute a 0–1 priority score for every shop.

    Formula (weights configurable in CFG):
        score = 0.35 × norm_conversion_rate
              + 0.30 × norm_avg_order_value
              + 0.20 × recency_score
              + 0.15 × shop_size_score

    Normalisation is computed globally (across all shops in the dataset) so
    scores are comparable between agents and territories.

    Args:
        df: Full locations DataFrame.

    Returns:
        DataFrame with an added 'priority_score' column.
    """
    df = df.copy()

    # Conversion rate — normalise to 0–1
    cr_min, cr_max = df["conversion_rate"].min(), df["conversion_rate"].max()
    df["_norm_cr"] = (
        (df["conversion_rate"] - cr_min) / (cr_max - cr_min) if cr_max > cr_min else 0.5
    )

    # Average order value — normalise to 0–1
    ov_min, ov_max = df["avg_order_value"].min(), df["avg_order_value"].max()
    df["_norm_ov"] = (
        (df["avg_order_value"] - ov_min) / (ov_max - ov_min) if ov_max > ov_min else 0.5
    )

    # Recency score: exponential decay — shops unvisited longer score higher
    df["_recency"] = 1 - np.exp(-df["last_visit_days"] / CFG.recency_decay_days)

    # Shop size
    df["_size_score"] = (
        df["shop_size"]
        .str.lower()
        .map(CFG.shop_size_map)
        .fillna(CFG.default_shop_size_score)
    )

    df["priority_score"] = (
        CFG.weight_conversion_rate * df["_norm_cr"]
        + CFG.weight_avg_order_value * df["_norm_ov"]
        + CFG.weight_recency * df["_recency"]
        + CFG.weight_shop_size * df["_size_score"]
    )

    df.drop(columns=["_norm_cr", "_norm_ov", "_recency", "_size_score"], inplace=True)

    log.info(
        "priority_scores_computed",
        mean=round(float(df["priority_score"].mean()), 3),
        min=round(float(df["priority_score"].min()), 3),
        max=round(float(df["priority_score"].max()), 3),
    )
    return df


def apply_priority_barrier(agent_df: pd.DataFrame, agent_name: str) -> pd.DataFrame:
    """
    Filter shops for one agent:
    1. Drop shops below CFG.min_priority_score.
    2. Take the top CFG.daily_shop_target by score.

    Args:
        agent_df:   All shops assigned to this agent in this sub_cluster.
        agent_name: Used only for logging.

    Returns:
        Filtered and sorted DataFrame (highest priority first).
    """
    eligible = agent_df[agent_df["priority_score"] >= CFG.min_priority_score]
    selected = eligible.nlargest(CFG.daily_shop_target, "priority_score")

    log.info(
        "priority_barrier_applied",
        agent=agent_name,
        total=len(agent_df),
        eligible=len(eligible),
        selected=len(selected),
        dropped_by_barrier=len(agent_df) - len(selected),
    )
    return selected.reset_index(drop=True)


# ===========================================================================
# ROAD GRAPH (with persistent disk cache)
# ===========================================================================


def get_graph_for_points(
    lat_lon_pairs: list[tuple[float, float]],
    cache_dir: str = CFG.graph_cache_dir,
    buffer_deg: float = CFG.graph_buffer_deg,
) -> nx.MultiDiGraph:
    """
    Download (or restore from disk) the drivable road graph covering all points.

    Cache key is derived from the bounding box rounded to ~10 km resolution.
    On first run for a region: downloads from OSM, saves as GraphML.
    Subsequent runs: loads from disk in seconds.

    Args:
        lat_lon_pairs: All (lat, lon) coords that must fall inside the graph.
        cache_dir:     Directory where .graphml files are stored.
        buffer_deg:    Padding added around the bounding box.

    Returns:
        networkx.MultiDiGraph with 'length' (m) and 'travel_time' (s) edges.
    """
    Path(cache_dir).mkdir(exist_ok=True)

    lats = [p[0] for p in lat_lon_pairs]
    lons = [p[1] for p in lat_lon_pairs]
    north = max(lats) + buffer_deg
    south = min(lats) - buffer_deg
    east = max(lons) + buffer_deg
    west = min(lons) - buffer_deg

    key = f"{round(north, 2)}_{round(south, 2)}_{round(east, 2)}_{round(west, 2)}"
    cache_path = Path(cache_dir) / f"graph_{key}.graphml"

    if cache_path.exists():
        log.info("graph_loaded_from_cache", key=key, path=str(cache_path))
        return ox.load_graphml(str(cache_path))

    log.info(
        "graph_downloading_from_osm", north=north, south=south, east=east, west=west
    )

    # Retry up to 3 times with increasing wait on transient failures
    last_error = None
    for attempt in range(1, 4):
        try:
            G = ox.graph_from_bbox((west, south, east, north), network_type="drive")
            G = ox.add_edge_speeds(G)
            G = ox.add_edge_travel_times(G)
            ox.save_graphml(G, str(cache_path))
            log.info(
                "graph_cached_to_disk",
                path=str(cache_path),
                nodes=len(G.nodes),
                edges=len(G.edges),
            )
            return G
        except Exception as exc:
            last_error = exc
            wait = attempt * 30  # 30s, 60s, 90s
            log.warning(
                "graph_download_failed_retrying",
                attempt=attempt,
                wait_seconds=wait,
                error=str(exc)[:120],
            )
            import time

            time.sleep(wait)

    raise RuntimeError(
        f"Graph download failed after 3 attempts: {last_error}"
    ) from last_error


# ===========================================================================
# DISTANCE & DURATION MATRIX (O(n) Dijkstra sweeps)
# ===========================================================================


def build_matrices(
    G: nx.MultiDiGraph,
    all_points: list[tuple[float, float]],
) -> tuple[list[list[float]], list[list[float]]]:
    """
    Build NxN distance (metres) and duration (seconds) matrices.

    Points are ordered: [HQ, shop_0, shop_1, ..., shop_N-1, agent_home].
    Uses single-source Dijkstra from each point — O(N) graph traversals
    instead of the naive O(N²) approach.

    Args:
        G:          Road network graph.
        all_points: Ordered list of (lat, lon) including HQ and home endpoints.

    Returns:
        (distance_matrix, duration_matrix) — both N×N lists.
    """
    n = len(all_points)

    # Snap every point to its nearest road node once
    graph_nodes = [
        ox.distance.nearest_nodes(G, pt[1], pt[0])  # nearest_nodes(G, lon, lat)
        for pt in all_points
    ]

    dist_mat = [[0.0] * n for _ in range(n)]
    dur_mat = [[0.0] * n for _ in range(n)]

    for i, src in enumerate(graph_nodes):
        try:
            d_map = nx.single_source_dijkstra_path_length(G, src, weight="length")
            t_map = nx.single_source_dijkstra_path_length(G, src, weight="travel_time")
        except Exception as exc:
            log.error("dijkstra_sweep_failed", point_index=i, error=str(exc))
            for j in range(n):
                if j != i:
                    dist_mat[i][j] = float("inf")
                    dur_mat[i][j] = float("inf")
            continue

        for j, dst in enumerate(graph_nodes):
            if i == j:
                continue
            dist_mat[i][j] = d_map.get(dst, float("inf"))
            dur_mat[i][j] = t_map.get(dst, float("inf"))

    return dist_mat, dur_mat


# ===========================================================================
# CONSTRAINED NEAREST-NEIGHBOR ROUTING
# ===========================================================================


def constrained_nearest_neighbor(
    dist_matrix: list[list[float]],
    dur_matrix: list[list[float]],
    shop_indices: list[int],
    hq_idx: int,
    home_idx: int,
    service_times_sec: list[float],
    time_windows: list[tuple[float, float]],
    max_work_minutes: float,
    work_start_minutes: float,
) -> tuple[list[int], dict]:
    """
    Greedy nearest-neighbor heuristic with three hard constraints:

      1. Working hours — a stop is only taken if the agent can still reach
         home before the end of the working day after departing that stop.
      2. Time windows — a stop is skipped if the agent arrives after closing.
         If the agent arrives before opening, they wait (realistic behaviour).
      3. Service time — each stop adds its pitch duration to the clock.

    Args:
        dist_matrix:        N×N distance matrix (indices: hq, shops..., home).
        dur_matrix:         N×N duration matrix in seconds.
        shop_indices:       Matrix indices that correspond to shops (1..N).
        hq_idx:             Matrix index of HQ (always 0).
        home_idx:           Matrix index of agent home (always last).
        service_times_sec:  Service duration per shop in seconds; same order
                            as shop_indices.
        time_windows:       (open_min, close_min) per shop; same order as
                            shop_indices.
        max_work_minutes:   Maximum working day length in minutes.
        work_start_minutes: Departure time from HQ in minutes from midnight.

    Returns:
        (route_indices, summary_dict)
        route_indices: full list including hq_idx at start and home_idx at end.
    """
    work_end_min = work_start_minutes + max_work_minutes

    # Lookup table: matrix_index → constraints
    shop_meta = {
        sidx: {
            "service_sec": service_times_sec[i],
            "open_min": time_windows[i][0],
            "close_min": time_windows[i][1],
        }
        for i, sidx in enumerate(shop_indices)
    }

    visited = set()
    route = [hq_idx]
    current_idx = hq_idx
    current_time = work_start_minutes  # minutes from midnight

    while True:
        best_idx = None
        best_dist = float("inf")
        best_depart = 0.0

        for sidx in shop_indices:
            if sidx in visited:
                continue

            travel_sec = dur_matrix[current_idx][sidx]
            if travel_sec == float("inf"):
                continue

            meta = shop_meta[sidx]
            travel_min = travel_sec / 60.0
            arrival = current_time + travel_min

            # Skip if arriving after closing
            if arrival > meta["close_min"]:
                continue

            # Wait if arriving before opening
            depart = max(arrival, meta["open_min"]) + meta["service_sec"] / 60.0

            # Constraint: must reach home before end of working day
            home_travel_min = dur_matrix[sidx][home_idx] / 60.0
            if depart + home_travel_min > work_end_min:
                continue

            d = dist_matrix[current_idx][sidx]
            if d < best_dist:
                best_dist = d
                best_idx = sidx
                best_depart = depart

        if best_idx is None:
            break  # No more feasible stops

        route.append(best_idx)
        visited.add(best_idx)
        current_idx = best_idx
        current_time = best_depart

    route.append(home_idx)

    # Shops selected but dropped due to time running out
    dropped_indices = [s for s in shop_indices if s not in visited]

    total_dist_m = sum(
        dist_matrix[route[i]][route[i + 1]]
        for i in range(len(route) - 1)
        if dist_matrix[route[i]][route[i + 1]] != float("inf")
    )
    total_dur_min = sum(
        dur_matrix[route[i]][route[i + 1]] / 60.0
        for i in range(len(route) - 1)
        if dur_matrix[route[i]][route[i + 1]] != float("inf")
    )

    summary = {
        "shops_visited": len(route) - 2,  # exclude HQ and home
        "shops_dropped": len(dropped_indices),
        "total_distance_m": round(total_dist_m),
        "total_travel_minutes": round(total_dur_min, 1),
        "dropped_indices": dropped_indices,
    }

    log.info(
        "route_complete",
        visited=summary["shops_visited"],
        dropped=summary["shops_dropped"],
        dist_km=round(total_dist_m / 1000, 2),
        travel_min=round(total_dur_min, 1),
    )

    return route, summary


# ===========================================================================
# MAP VISUALISATION
# ===========================================================================


def plot_route(
    G: nx.MultiDiGraph,
    route_points: list[dict],
) -> folium.Map:
    """
    Render an interactive Folium map for one agent's route.

    Each road segment is drawn using Dijkstra's shortest path on the graph
    so lines follow actual streets. Falls back to a straight line on any
    segment where no path is found.

    Args:
        G:            Road network (same graph used for distance computation).
        route_points: Ordered list of {'lat', 'lon', 'label', 'popup_html'}.
                      First entry = HQ (green marker).
                      Last entry  = Agent home (red marker).
                      Middle entries = Shop stops (blue markers).

    Returns:
        folium.Map ready to be saved as HTML.
    """
    coords = [(p["lat"], p["lon"]) for p in route_points]
    median = tuple(np.median(np.array(coords), axis=0))
    center = int(
        np.argmin([np.linalg.norm(np.array(c) - np.array(median)) for c in coords])
    )

    mymap = folium.Map(location=coords[center], zoom_start=CFG.map_zoom)

    palette = [
        "#FF0000",
        "#FF3300",
        "#FF6600",
        "#FF9900",
        "#FFCC00",
        "#FFFF00",
        "#CCFF00",
        "#99FF00",
        "#66FF00",
        "#33FF00",
        "#00FF00",
    ]
    n_seg = max(len(coords) - 1, 1)
    cmap = folium.LinearColormap(colors=palette, vmin=0, vmax=n_seg)

    for i in range(len(coords) - 1):
        n1 = ox.distance.nearest_nodes(G, coords[i][1], coords[i][0])
        n2 = ox.distance.nearest_nodes(G, coords[i + 1][1], coords[i + 1][0])
        try:
            path = nx.shortest_path(G, n1, n2, weight="length")
            seg = [(G.nodes[nd]["y"], G.nodes[nd]["x"]) for nd in path]
        except nx.NetworkXNoPath:
            log.warning("map_no_path_segment", segment=i)
            seg = [coords[i], coords[i + 1]]

        line = folium.PolyLine(locations=seg, color=cmap(i), weight=4, opacity=0.8)
        mymap.add_child(line)
        PolyLineTextPath(
            line,
            "\u25ba",
            repeat=True,
            offset=10,
            attributes={"font-weight": "bold", "font-size": "14"},
        ).add_to(mymap)

    all_fg = folium.FeatureGroup(name="All Stops")
    mymap.add_child(all_fg)

    for stop_num, (pt, coord) in enumerate(zip(route_points, coords)):
        is_hq = stop_num == 0
        is_home = stop_num == len(route_points) - 1

        if is_hq:
            folium.Marker(
                location=coord,
                icon=folium.Icon(color="green", icon="home", prefix="fa"),
                popup=folium.Popup(pt.get("popup_html", "HQ"), max_width=300),
            ).add_to(mymap)
        elif is_home:
            folium.Marker(
                location=coord,
                icon=folium.Icon(color="red", icon="home", prefix="fa"),
                popup=folium.Popup(pt.get("popup_html", "Home"), max_width=300),
            ).add_to(mymap)
        else:
            marker = folium.Marker(
                location=coord,
                icon=folium.Icon(color="blue", icon="info-sign"),
                popup=folium.Popup(pt.get("popup_html", pt["label"]), max_width=320),
            )
            fg = FeatureGroupSubGroup(all_fg, name=f"Stop {stop_num}: {pt['label']}")
            marker.add_to(fg)
            mymap.add_child(fg)

    cmap.caption = "Route Progress"
    cmap.add_to(mymap)
    folium.LayerControl(collapsed=False).add_to(mymap)
    return mymap


# ===========================================================================
# REPORTING
# ===========================================================================


def export_summary_report(all_summaries: list[dict], output_path: str) -> None:
    """
    Write an Excel workbook with two sheets:
      Route Summary  — one row per agent with KPIs.
      Dropped Shops  — all shops not visited and why.

    Args:
        all_summaries: List of summary dicts produced by _route_agent.
        output_path:   Destination .xlsx path.
    """
    if not all_summaries:
        log.warning("no_summaries_nothing_to_export")
        return

    summary_rows = []
    dropped_rows = []

    for s in all_summaries:
        summary_rows.append(
            {
                "Agent": s["agent_name"],
                "Sub Cluster": s["sub_cluster"],
                "Shops Selected (top-k)": s["shops_selected"],
                "Shops Visited": s["shops_visited"],
                "Shops Dropped (time)": s["shops_dropped_time"],
                "Shops Dropped (barrier)": s["shops_dropped_barrier"],
                "Total Distance (km)": round(s["total_distance_m"] / 1000, 2),
                "Total Travel Time (min)": s["total_travel_minutes"],
                "Est. Revenue (KSh)": s["estimated_revenue_ksh"],
                "Map File": s["map_file"],
            }
        )
        for shop in s.get("dropped_shop_details", []):
            dropped_rows.append(
                {
                    "Agent": s["agent_name"],
                    "Sub Cluster": s["sub_cluster"],
                    "Shop ID": shop["shop_id"],
                    "Shop Name": shop["shop_name"],
                    "Priority Score": shop["priority_score"],
                    "Drop Reason": shop["drop_reason"],
                }
            )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="Route Summary", index=False
        )
        if dropped_rows:
            pd.DataFrame(dropped_rows).to_excel(
                writer, sheet_name="Dropped Shops", index=False
            )

    log.info("report_exported", path=output_path, routes=len(summary_rows))


# ===========================================================================
# OUTLIER REMOVAL
# ===========================================================================


def remove_outliers(
    df: pd.DataFrame,
    columns: list[str],
    factor: float = 1.5,
) -> pd.DataFrame:
    """IQR-based outlier removal on specified numeric columns."""
    Q1 = df[columns].quantile(0.25)
    Q3 = df[columns].quantile(0.75)
    IQR = Q3 - Q1
    mask = ~(
        (df[columns] < (Q1 - factor * IQR)) | (df[columns] > (Q3 + factor * IQR))
    ).any(axis=1)
    removed = (~mask).sum()
    if removed:
        log.info("outliers_removed", count=int(removed))
    return df[mask]


# ===========================================================================
# SINGLE-AGENT ROUTING PIPELINE
# ===========================================================================


def _route_agent(
    agent_name: str,
    agent_df: pd.DataFrame,
    sub_cluster: str,
    G: nx.MultiDiGraph,
    hq: dict,
    agent_homes: dict,
    all_summaries: list,
) -> None:
    """
    Run the full routing pipeline for one agent within one sub_cluster.
    Appends one entry to all_summaries.

    Steps:
      1. Apply priority barrier → select top-k shops.
      2. Resolve agent home (falls back to HQ if not in agents.xlsx).
      3. Build point list [HQ, shops..., home].
      4. Build distance/duration matrices via Dijkstra.
      5. Run constrained nearest-neighbor routing.
      6. Render and save Folium map.
      7. Compute KPIs and append to all_summaries.
    """
    # 1. Priority barrier
    selected = apply_priority_barrier(agent_df, agent_name)
    if len(selected) < 2:
        log.warning(
            "agent_skipped_too_few_shops", agent=agent_name, count=len(selected)
        )
        return

    # 2. Resolve home coordinates
    home = agent_homes.get(agent_name, hq)
    if home is hq or home == hq:
        log.warning("agent_home_fallback_to_hq", agent=agent_name)

    # 3. Build ordered point list
    #    Index layout: 0=HQ, 1..N=shops, N+1=home
    hq_pt = (hq["latitude"], hq["longitude"])
    home_pt = (home["latitude"], home["longitude"])
    shop_pts = list(zip(selected["latitude"], selected["longitude"]))

    all_points = [hq_pt] + shop_pts + [home_pt]
    hq_idx = 0
    home_idx = len(all_points) - 1
    shop_indices = list(range(1, len(shop_pts) + 1))
    n_shops = len(shop_pts)

    # 4. Distance & duration matrices
    dist_mat, dur_mat = build_matrices(G, all_points)

    # 5. Per-shop constraints (same order as shop_indices)
    service_times_sec = [
        float(str(selected.loc[i, "service_time_minutes"])) * 60 for i in range(n_shops)
    ]
    time_windows = [
        (
            float(str(selected.loc[i, "open_time_minutes"])),
            float(str(selected.loc[i, "close_time_minutes"])),
        )
        for i in range(n_shops)
    ]

    # 6. Constrained nearest-neighbor
    route_indices, r_summary = constrained_nearest_neighbor(
        dist_matrix=dist_mat,
        dur_matrix=dur_mat,
        shop_indices=shop_indices,
        hq_idx=hq_idx,
        home_idx=home_idx,
        service_times_sec=service_times_sec,
        time_windows=time_windows,
        max_work_minutes=CFG.max_work_hours * 60,
        work_start_minutes=CFG.work_start_hour * 60,
    )

    # 7. Build map route points
    route_points = []
    for stop_num, idx in enumerate(route_indices):
        if idx == hq_idx:
            route_points.append(
                {
                    "lat": hq_pt[0],
                    "lon": hq_pt[1],
                    "label": "Company HQ",
                    "popup_html": "<b>Company HQ</b><br>Start of route",
                }
            )
        elif idx == home_idx:
            route_points.append(
                {
                    "lat": home_pt[0],
                    "lon": home_pt[1],
                    "label": f"{agent_name} — Home",
                    "popup_html": f"<b>{agent_name}</b><br>End of route (residential)",
                }
            )
        else:
            row = selected.loc[idx - 1]
            route_points.append(
                {
                    "lat": float(row["latitude"]),
                    "lon": float(row["longitude"]),
                    "label": row["shop_name"],
                    "popup_html": (
                        f"<b>Stop {stop_num}: {row['shop_name']}</b><br>"
                        f"Priority Score: <b>{row['priority_score']:.3f}</b><br>"
                        f"Conversion Rate: {row['conversion_rate']:.0%}<br>"
                        f"Avg Order Value: KSh {row['avg_order_value']:,}<br>"
                        f"Days Since Last Visit: {int(row['last_visit_days'])}<br>"
                        f"Service Time: {row['service_time_minutes']:.0f} min<br>"
                        f"Shop Size: {row.get('shop_size', 'N/A')}"
                    ),
                }
            )

    # 8. Render and save map
    m = plot_route(G, route_points)
    safe = "".join(
        c if (c.isalnum() or c == "_") else "_" for c in f"{agent_name}_{sub_cluster}"
    )
    map_path = os.path.join(CFG.maps_dir, f"route_{safe}.html")
    m.save(map_path)
    log.info("map_saved", agent=agent_name, path=map_path)

    # 9. KPIs
    visited_shop_positions = [
        idx - 1 for idx in route_indices if idx not in (hq_idx, home_idx)
    ]
    estimated_revenue = sum(
        (
            float(str(selected.loc[i, "conversion_rate"]))
            * float(str(selected.loc[i, "avg_order_value"]))
            for i in visited_shop_positions
        ),
        0.0,
    )

    # Shops dropped by time constraint
    dropped_time = []
    for sidx in r_summary["dropped_indices"]:
        row = selected.loc[sidx - 1]
        dropped_time.append(
            {
                "shop_id": row["shop_id"],
                "shop_name": row["shop_name"],
                "priority_score": round(float(row["priority_score"]), 3),
                "drop_reason": "time_constraint",
            }
        )

    # Shops dropped by priority barrier
    dropped_barrier = []
    all_ids = set(agent_df["shop_id"])
    sel_ids = set(selected["shop_id"])
    for _, row in agent_df[agent_df["shop_id"].isin(all_ids - sel_ids)].iterrows():
        dropped_barrier.append(
            {
                "shop_id": row["shop_id"],
                "shop_name": row["shop_name"],
                "priority_score": round(float(row["priority_score"]), 3),
                "drop_reason": "below_priority_barrier",
            }
        )

    all_summaries.append(
        {
            "agent_name": agent_name,
            "sub_cluster": sub_cluster,
            "shops_selected": len(selected),
            "shops_visited": r_summary["shops_visited"],
            "shops_dropped_time": r_summary["shops_dropped"],
            "shops_dropped_barrier": len(dropped_barrier),
            "total_distance_m": r_summary["total_distance_m"],
            "total_travel_minutes": r_summary["total_travel_minutes"],
            "estimated_revenue_ksh": round(estimated_revenue, 2),
            "map_file": map_path,
            "dropped_shop_details": dropped_time + dropped_barrier,
        }
    )


# ===========================================================================
# MAIN PIPELINE
# ===========================================================================


def main() -> None:
    """
    End-to-end pipeline:
      1. Load + validate all input files.
      2. Compute global priority scores.
      3. For each sub_cluster:
           a. Remove coordinate outliers.
           b. Download road graph (or load from disk cache).
           c. For each agent in the sub_cluster:
                - Apply priority barrier.
                - Build distance/duration matrices.
                - Run constrained nearest-neighbor routing.
                - Save Folium map.
      4. Export Excel summary report.
    """
    Path(CFG.maps_dir).mkdir(exist_ok=True)
    Path(CFG.graph_cache_dir).mkdir(exist_ok=True)

    # ── 1. Load data ─────────────────────────────────────────────────────────
    df, hq, agent_homes = load_and_validate()

    # ── 2. Priority scores (computed globally for fair normalisation) ─────────
    df = compute_priority_scores(df)

    all_summaries: list[dict] = []

    # ── 3. Process each sub_cluster ──────────────────────────────────────────
    for sub_cluster, sc_group in df.groupby("sub_cluster"):
        log.info("sub_cluster_start", sub_cluster=sub_cluster, shops=len(sc_group))
        try:
            sc_clean = remove_outliers(sc_group, ["latitude", "longitude"])

            # Collect all lat/lon that must fit inside the graph:
            # HQ + every shop in sub_cluster + all agent homes present here
            all_latlons: list[tuple[float, float]] = list(
                zip(sc_clean["latitude"], sc_clean["longitude"])
            )
            for ag in sc_clean["agent_name"].unique():
                if ag in agent_homes:
                    h = agent_homes[ag]
                    all_latlons.append((float(h["latitude"]), float(h["longitude"])))
            # One graph download per sub_cluster — all agents inside reuse it
            G = get_graph_for_points(all_latlons)

            for agent_name_raw, agent_group in sc_clean.groupby("agent_name"):
                try:
                    _route_agent(
                        agent_name=str(agent_name_raw),
                        agent_df=agent_group,
                        sub_cluster=str(sub_cluster),
                        G=G,
                        hq=hq,
                        agent_homes=agent_homes,
                        all_summaries=all_summaries,
                    )
                except Exception:
                    log.exception(
                        "agent_routing_failed",
                        agent=str(agent_name_raw),
                        sub_cluster=sub_cluster,
                    )

        except Exception:
            log.exception("sub_cluster_failed", sub_cluster=sub_cluster)

    # ── 4. Export summary ────────────────────────────────────────────────────
    export_summary_report(all_summaries, CFG.summary_report_path)
    log.info(
        "pipeline_complete",
        total_routes=len(all_summaries),
        maps_dir=CFG.maps_dir,
        report=CFG.summary_report_path,
    )


if __name__ == "__main__":
    main()
