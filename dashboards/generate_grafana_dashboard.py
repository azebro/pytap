#!/usr/bin/env python3
"""Generate Grafana dashboard JSON for PV Production.

Converts the Home Assistant PV Production dashboard (3 views, 26 panels)
into a Grafana dashboard querying InfluxDB v3 via SQL.
"""

import json

# ---------------------------------------------------------------------------
# Panel layout definitions (mirrors physical roof arrangement)
# ---------------------------------------------------------------------------
# Each entry: (name, entity_suffix, orientation, grid_row, grid_col_start)
# orientation: "L" = landscape (w=3, h=3), "P" = portrait (w=2, h=4)

TOP_ROOF_ROW1 = [
    ("C2", "c2", "L", 0, 10),
    ("C3", "c3", "L", 0, 13),
    ("D12", "d12", "L", 0, 20),
]

TOP_ROOF_ROW2 = [
    ("C4", "c4", "P", 1, 8),
    ("C10", "c10", "P", 1, 10),
    ("C9", "c9", "P", 1, 12),
    ("C8", "c8", "P", 1, 14),
    ("C7", "c7", "P", 1, 16),
    ("D13", "d13", "L", 1, 20),
]

BOTTOM_ROOF_ROW3 = [
    ("C1", "c1", "P", 3, 7),
    ("C6", "c6", "P", 3, 9),
    ("D6", "d6", "P", 3, 11),
    ("D7", "d7", "P", 3, 13),
    ("D8", "d8", "P", 3, 15),
    ("D9", "d9", "P", 3, 17),
    ("C11", "c11", "P", 3, 19),
]

BOTTOM_ROOF_ROW4 = [
    ("D11", "d11", "P", 4, 3),
    ("D5", "d5", "P", 4, 5),
    ("C12", "c12", "P", 4, 7),
    ("D4", "d4", "P", 4, 9),
    ("D3", "d3", "P", 4, 11),
    ("D1", "d1", "P", 4, 13),
    ("D2", "d2", "P", 4, 15),
    ("C5", "c5", "P", 4, 17),
    ("C13", "c13", "P", 4, 19),
    ("D10", "d10", "P", 4, 21),
]

ALL_ROOF = TOP_ROOF_ROW1 + TOP_ROOF_ROW2 + BOTTOM_ROOF_ROW3 + BOTTOM_ROOF_ROW4

STRING_C = [
    "c1",
    "c2",
    "c3",
    "c4",
    "c5",
    "c6",
    "c7",
    "c8",
    "c9",
    "c10",
    "c11",
    "c12",
    "c13",
]
STRING_D = [
    "d1",
    "d2",
    "d3",
    "d4",
    "d5",
    "d6",
    "d7",
    "d8",
    "d9",
    "d10",
    "d11",
    "d12",
    "d13",
]

# Grid row -> y-offset within a section (portrait h=4, landscape h=3)
ROW_Y = {0: 0, 1: 3, 3: 8, 4: 12}

DS = {"uid": "HA", "type": "influxdb"}

_next_id = 0


def next_id():
    global _next_id
    _next_id += 1
    return _next_id


def sql_latest(entity_id: str) -> str:
    return (
        f'SELECT value FROM "{entity_id}" '
        f"WHERE $__timeFilter(time) "
        f"ORDER BY time DESC LIMIT 1"
    )


def sql_series(entity_id: str) -> str:
    return (
        f'SELECT time, value FROM "{entity_id}" '
        f"WHERE $__timeFilter(time) "
        f"ORDER BY time"
    )


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def stat_panel(
    panel_id,
    title,
    entity_id,
    x,
    y,
    w,
    h,
    color_scheme="continuous-greens",
):
    """Single stat panel showing latest value with colored background."""
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [
            {
                "datasource": DS,
                "rawSql": sql_latest(entity_id),
                "refId": "A",
                "format": "table",
            }
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": color_scheme},
                "min": 0,
                "thresholds": {
                    "mode": "percentage",
                    "steps": [
                        {"color": "dark-green", "value": None},
                        {"color": "green", "value": 30},
                        {"color": "light-green", "value": 70},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value_and_name",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
            "justifyMode": "center",
            "orientation": "auto",
        },
    }


def connectivity_stat_panel(panel_id, title, entity_id, x, y, w, h):
    """Stat panel with red-to-green color for connectivity."""
    p = stat_panel(panel_id, title, entity_id, x, y, w, h, "continuous-RdYlGr")
    p["fieldConfig"]["defaults"]["thresholds"]["steps"] = [
        {"color": "red", "value": None},
        {"color": "yellow", "value": 40},
        {"color": "green", "value": 70},
    ]
    return p


def timeseries_panel(panel_id, title, entity_ids_labels, x, y, w, h, unit="kWh"):
    """Time series panel with one query per entity."""
    targets = []
    for i, (eid, label) in enumerate(entity_ids_labels):
        ref = chr(65 + i)  # A, B, C, ...
        targets.append(
            {
                "datasource": DS,
                "rawSql": sql_series(eid),
                "refId": ref,
                "format": "time_series",
            }
        )

    overrides = []
    for i, (eid, label) in enumerate(entity_ids_labels):
        ref = chr(65 + i)
        overrides.append(
            {
                "matcher": {"id": "byFrameRefID", "options": ref},
                "properties": [
                    {"id": "displayName", "value": label},
                ],
            }
        )

    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "axisBorderShow": False,
                    "axisCenteredZero": False,
                    "axisLabel": "",
                    "drawStyle": "line",
                    "fillOpacity": 15,
                    "gradientMode": "scheme",
                    "lineInterpolation": "smooth",
                    "lineWidth": 2,
                    "pointSize": 5,
                    "showPoints": "auto",
                    "spanNulls": False,
                    "stacking": {"group": "A", "mode": "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "unit": unit,
            },
            "overrides": overrides,
        },
        "options": {
            "legend": {
                "calcs": ["lastNotNull", "max", "mean"],
                "displayMode": "table",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def bar_gauge_panel(
    panel_id,
    title,
    entity_ids_labels,
    x,
    y,
    w,
    h,
    unit="watt",
    color_scheme="continuous-greens",
):
    """Horizontal bar gauge comparing all panels."""
    targets = []
    overrides = []
    for i, (eid, label) in enumerate(entity_ids_labels):
        ref = chr(65 + i)
        targets.append(
            {
                "datasource": DS,
                "rawSql": sql_latest(eid),
                "refId": ref,
                "format": "table",
            }
        )
        overrides.append(
            {
                "matcher": {"id": "byFrameRefID", "options": ref},
                "properties": [{"id": "displayName", "value": label}],
            }
        )
    return {
        "id": panel_id,
        "type": "bargauge",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": color_scheme},
                "min": 0,
                "unit": unit,
                "thresholds": {
                    "mode": "percentage",
                    "steps": [
                        {"color": "dark-green", "value": None},
                        {"color": "green", "value": 50},
                    ],
                },
            },
            "overrides": overrides,
        },
        "options": {
            "displayMode": "gradient",
            "minVizHeight": 16,
            "minVizWidth": 75,
            "namePlacement": "auto",
            "orientation": "horizontal",
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
        },
    }


def row_panel(panel_id, title, y, collapsed=False, panels=None):
    r = {
        "id": panel_id,
        "type": "row",
        "title": title,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "collapsed": collapsed,
    }
    if panels:
        r["panels"] = panels
    return r


# ---------------------------------------------------------------------------
# Build roof layout panels for a given metric
# ---------------------------------------------------------------------------


def build_roof_section(
    y_offset, metric_suffix, unit, color_scheme="continuous-greens", connectivity=False
):
    """Build 26 stat panels arranged as the physical roof layout."""
    panels = []
    for name, suffix, orientation, grid_row, grid_col in ALL_ROOF:
        entity_id = f"sensor.tigo_ts4_{suffix}_{metric_suffix}"
        w = 3 if orientation == "L" else 2
        h = 3 if orientation == "L" else 4
        x = grid_col
        y = y_offset + ROW_Y[grid_row]

        if connectivity:
            p = connectivity_stat_panel(next_id(), name, entity_id, x, y, w, h)
        else:
            p = stat_panel(next_id(), name, entity_id, x, y, w, h, color_scheme)

        if unit:
            p["fieldConfig"]["defaults"]["unit"] = unit
        panels.append(p)
    return panels


# ---------------------------------------------------------------------------
# Build full dashboard
# ---------------------------------------------------------------------------


def build_dashboard():
    panels = []
    y = 0

    # ── Section 0: System Overview ────────────────────────────────────────
    panels.append(row_panel(next_id(), "⚡ System Overview", y))
    y += 1

    # Total Power
    panels.append(
        {
            "id": next_id(),
            "type": "stat",
            "title": "Total System Power",
            "datasource": DS,
            "gridPos": {"h": 5, "w": 8, "x": 0, "y": y},
            "targets": [
                {
                    "datasource": DS,
                    "rawSql": sql_latest("sensor.tigo_max_power"),
                    "refId": "A",
                    "format": "table",
                }
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "unit": "watt",
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "dark-red", "value": None},
                            {"color": "orange", "value": 50},
                            {"color": "green", "value": 200},
                            {"color": "dark-green", "value": 400},
                        ],
                    },
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "background_solid",
                "graphMode": "area",
                "textMode": "value",
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "",
                    "values": False,
                },
            },
        }
    )

    # Total Daily Energy
    panels.append(
        {
            "id": next_id(),
            "type": "stat",
            "title": "Max Panel Daily Energy",
            "datasource": DS,
            "gridPos": {"h": 5, "w": 8, "x": 8, "y": y},
            "targets": [
                {
                    "datasource": DS,
                    "rawSql": sql_latest("sensor.tigo_max_daily_energy"),
                    "refId": "A",
                    "format": "table",
                }
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "unit": "kWh",
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "dark-red", "value": None},
                            {"color": "orange", "value": 0.2},
                            {"color": "green", "value": 0.5},
                            {"color": "dark-green", "value": 1.0},
                        ],
                    },
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "background_solid",
                "graphMode": "area",
                "textMode": "value",
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "",
                    "values": False,
                },
            },
        }
    )

    # Max Readings Today
    panels.append(
        {
            "id": next_id(),
            "type": "stat",
            "title": "Max Readings Today",
            "datasource": DS,
            "gridPos": {"h": 5, "w": 8, "x": 16, "y": y},
            "targets": [
                {
                    "datasource": DS,
                    "rawSql": sql_latest("sensor.tigo_max_readings_today"),
                    "refId": "A",
                    "format": "table",
                }
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "red", "value": None},
                            {"color": "yellow", "value": 10},
                            {"color": "green", "value": 50},
                        ],
                    },
                },
                "overrides": [],
            },
            "options": {
                "colorMode": "background_solid",
                "graphMode": "area",
                "textMode": "value",
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "",
                    "values": False,
                },
            },
        }
    )
    y += 5

    # ── Section 1: Daily Energy — Roof Layout ─────────────────────────────
    panels.append(row_panel(next_id(), "☀️ Daily Energy — Roof Layout", y))
    y += 1
    roof_y = y
    energy_panels = build_roof_section(roof_y, "daily_energy", "kWh")
    panels.extend(energy_panels)
    y = roof_y + 16  # 4 grid rows: 0+3=3, 3+4=7..12+4=16

    # ── Section 2: Daily Energy — Trends ──────────────────────────────────
    panels.append(row_panel(next_id(), "📈 Daily Energy — Trends", y))
    y += 1

    c_energy = [(f"sensor.tigo_ts4_{s}_daily_energy", s.upper()) for s in STRING_C]
    d_energy = [(f"sensor.tigo_ts4_{s}_daily_energy", s.upper()) for s in STRING_D]
    panels.append(
        timeseries_panel(
            next_id(), "String C — Daily Energy", c_energy, 0, y, 12, 10, "kWh"
        )
    )
    panels.append(
        timeseries_panel(
            next_id(), "String D — Daily Energy", d_energy, 12, y, 12, 10, "kWh"
        )
    )
    y += 10

    # ── Section 3: Live Power — Roof Layout ───────────────────────────────
    panels.append(row_panel(next_id(), "⚡ Live Power — Roof Layout", y))
    y += 1
    roof_y = y
    power_panels = build_roof_section(roof_y, "power", "watt")
    panels.extend(power_panels)
    y = roof_y + 16

    # ── Section 4: Live Power — Trends & Comparison ───────────────────────
    panels.append(row_panel(next_id(), "📈 Live Power — Trends", y))
    y += 1

    c_power = [(f"sensor.tigo_ts4_{s}_power", s.upper()) for s in STRING_C]
    d_power = [(f"sensor.tigo_ts4_{s}_power", s.upper()) for s in STRING_D]
    panels.append(
        timeseries_panel(next_id(), "String C — Power", c_power, 0, y, 12, 10, "watt")
    )
    panels.append(
        timeseries_panel(next_id(), "String D — Power", d_power, 12, y, 12, 10, "watt")
    )
    y += 10

    # Bar gauges for quick power comparison
    panels.append(
        bar_gauge_panel(
            next_id(), "String C — Current Power", c_power, 0, y, 12, 10, "watt"
        )
    )
    panels.append(
        bar_gauge_panel(
            next_id(), "String D — Current Power", d_power, 12, y, 12, 10, "watt"
        )
    )
    y += 10

    # ── Section 5: Connectivity — Roof Layout ─────────────────────────────
    panels.append(row_panel(next_id(), "📡 Panel Connectivity — Roof Layout", y))
    y += 1
    roof_y = y
    conn_panels = build_roof_section(roof_y, "readings_today", None, connectivity=True)
    panels.extend(conn_panels)
    y = roof_y + 16

    # ── Section 6: Connectivity — Trends ──────────────────────────────────
    panels.append(row_panel(next_id(), "📈 Connectivity — Trends", y))
    y += 1

    c_conn = [(f"sensor.tigo_ts4_{s}_readings_today", s.upper()) for s in STRING_C]
    d_conn = [(f"sensor.tigo_ts4_{s}_readings_today", s.upper()) for s in STRING_D]
    panels.append(
        timeseries_panel(
            next_id(), "String C — Readings Today", c_conn, 0, y, 12, 10, "short"
        )
    )
    panels.append(
        timeseries_panel(
            next_id(), "String D — Readings Today", d_conn, 12, y, 12, 10, "short"
        )
    )
    y += 10

    # ── Assemble dashboard ────────────────────────────────────────────────
    dashboard = {
        "__inputs": [
            {
                "name": "DS_INFLUXDB",
                "label": "HA",
                "description": "InfluxDB v3 datasource named HA (SQL query language)",
                "type": "datasource",
                "pluginId": "influxdb",
                "pluginName": "InfluxDB",
            }
        ],
        "__requires": [
            {
                "type": "grafana",
                "id": "grafana",
                "name": "Grafana",
                "version": "10.0.0",
            },
            {
                "type": "datasource",
                "id": "influxdb",
                "name": "InfluxDB",
                "version": "1.0.0",
            },
            {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
            {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
            {"type": "panel", "id": "bargauge", "name": "Bar gauge", "version": ""},
        ],
        "id": None,
        "uid": "pv-production",
        "title": "PV Production",
        "description": (
            "Solar panel monitoring dashboard — 26 Tigo TS4 panels across "
            "String C (13) and String D (13). Shows daily energy, live power, "
            "and connectivity status arranged in physical roof layout."
        ),
        "tags": ["solar", "pv", "tigo", "energy"],
        "style": "dark",
        "timezone": "browser",
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,  # shared crosshair
        "refresh": "30s",
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["5s", "10s", "30s", "1m", "5m"],
        },
        "templating": {"list": []},
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "panels": panels,
    }
    return dashboard


if __name__ == "__main__":
    dashboard = build_dashboard()
    output_path = "/workspaces/pytap/dashboards/pv_production_grafana.json"
    with open(output_path, "w") as f:
        json.dump(dashboard, f, indent=2)
    print(f"Dashboard written to {output_path}")
    print(f"Total panels: {len(dashboard['panels'])}")
