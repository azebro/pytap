# PV Production Dashboard

Home Assistant Lovelace dashboard displaying the physical solar panel layout with dynamic status coloring.

## Files

| File | Purpose |
|------|---------|
| [`pv_production_dashboard.yaml`](pv_production_dashboard.yaml) | Lovelace dashboard (2 views, 571 lines) |
| [`tigo.yaml`](tigo.yaml) | HA template sensors for max-value aggregation |

## Prerequisites

**HACS frontend components:**

- **[custom:button-card](https://github.com/custom-cards/button-card)** — tile rendering with JS templates for dynamic styling
- **[custom:layout-card](https://github.com/thomasloven/lovelace-layout-card)** — CSS Grid layout for precise column positioning

**Template sensors** — include `tigo.yaml` in your HA configuration (e.g. via `template: !include tigo.yaml` or a packages directory). It provides:

| Sensor | Entity ID | Purpose |
|--------|-----------|---------|
| Tigo Max Daily Energy | `sensor.tigo_max_daily_energy` | Highest `_daily_energy` across all 24 panels (kWh) |
| Tigo Max Readings Today | `sensor.tigo_max_readings_today` | Highest `_readings_today` across all 24 panels |

These are computed server-side with Jinja2 templates so each button-card only needs a single `states[]` lookup instead of scanning all 24 sensors in JavaScript.

## Views

### 1. PV Panel Layout (`/pv-layout`)

Shows daily energy production per panel. Each tile displays the panel name and `sensor.tigo_ts4_<panel>_daily_energy` value.

**Color scale:** Transparent → Green. Green overlay alpha scales from 0 to 0.7 relative to `sensor.tigo_max_daily_energy`. An inner green glow intensifies with production.

### 2. Panel Connectivity (`/panel-connectivity`)

Shows the number of readings received today per panel using `sensor.tigo_ts4_<panel>_readings_today`.

**Color scale:** Red → Green. HSL hue scales 0° (red) → 120° (green) relative to `sensor.tigo_max_readings_today`. Low-connectivity panels are immediately visible in red.

## Physical Layout

Both views mirror the actual roof panel arrangement using a 26-column CSS grid. Panels have two orientations matching real hardware:

- **Landscape (3:2)** — C2, C3 (top row)
- **Portrait (2:3)** — all other panels

```
               col 11        col 14
                 ┌──────────┐ ┌──────────┐
     Row 1       │  C2 (L)  │ │  C3 (L)  │
                 └──────────┘ └──────────┘
        col 9   col 11  col 13  col 15  col 17
         ┌──┐    ┌──┐    ┌──┐    ┌──┐    ┌──┐
  Row 2  │C4│    │C10│   │C9 │   │C8 │   │C7 │
         └──┘    └──┘    └──┘    └──┘    └──┘

    col 4  col 6  col 8  col 10 col 12 col 14 col 16 col 18 col 20 col 22
                   ┌──┐    ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐
     Row 3         │C1│    │C6│   │D6│   │D7│   │D8│   │D9│   │C11│
                   └──┘    └──┘   └──┘   └──┘   └──┘   └──┘   └──┘
     ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐   ┌──┐
     │D11│  │D5│   │C12│  │D4│   │D3│   │D1│   │D2│   │C5│   │C13│  │D10│
     └──┘   └──┘   └──┘   └──┘   └──┘   └──┘   └──┘   └──┘   └──┘   └──┘
     Row 4
```

- **Top section** (`grid-layout`): Row 1 has C2/C3 centered (landscape); Row 2 has C4, C10, C9, C8, C7 (portrait)
- **Bottom section** (`grid-layout`): Row 3 has C1–C11 main array; Row 4 extends left with D11/D5 and continues through D10

## Panels (24 total)

| String C       | String D       |
|----------------|----------------|
| C1, C2, C3     | D1, D2, D3     |
| C4, C5, C6     | D4, D5, D6     |
| C7, C8, C9     | D7, D8, D9     |
| C10, C11, C12  | D10, D11       |
| C13            |                |

## Tile Rendering

Each tile is a `custom:button-card` with a layered CSS background that simulates a solar panel:

1. **Color overlay** — dynamic green (page 1) or hue-shifted (page 2) based on performance percentage
2. **Glass reflection** — diagonal gradient from white highlight to dark shadow
3. **Cell grid lines** — horizontal + vertical `repeating-linear-gradient` at 20% intervals
4. **Dark base** — `#0a111a`

A dynamic `box-shadow` adds an inner glow whose intensity and color match the performance metric.

### Page 1 color formula (Energy)

```javascript
const max = parseFloat(states['sensor.tigo_max_daily_energy']?.state) || 1;
const pct = Math.min(Math.max(val, 0) / max, 1);
const alpha = pct * 0.7;
// Green overlay: rgba(0, 220, 50, alpha)
// Inner glow:    rgba(0, 255, 50, 0.8 * pct)
```

### Page 2 color formula (Connectivity)

```javascript
const max = parseFloat(states['sensor.tigo_max_readings_today']?.state) || 1;
const pct = Math.min(Math.max(val, 0) / max, 1);
const hue = Math.round(pct * 120);  // 0°=red → 120°=green
// Color overlay: hsla(hue, 80%, 50%, 0.55)
// Inner glow:    hsla(hue, 80%, 50%, 0.7)
```

## YAML Structure

YAML anchors keep the file DRY:

| Anchor | View | Orientation | Purpose |
|--------|------|-------------|---------|
| `&tile_landscape` | Energy | Landscape (3:2) | C2, C3 tiles |
| `&tile_portrait` | Energy | Portrait (2:3) | All other tiles |
| `&conn_tile_landscape` | Connectivity | Landscape (3:2) | C2, C3 tiles (red→green) |
| `&conn_tile_portrait` | Connectivity | Portrait (2:3) | All other tiles (red→green) |

Both views use the same structural approach:

1. **Top roof** — `custom:layout-card` with `grid-template-columns: repeat(26, 1fr)`, 6px gap
2. **Spacer** — blank `button-card` (20px height)
3. **Bottom roof** — second `custom:layout-card` with the same 26-column grid

Each panel card specifies its exact position via `view_layout: { grid-row, grid-column }`, with portrait tiles spanning 2 columns and landscape tiles spanning 3.

## tigo.yaml — Template Sensors

Server-side Jinja2 template sensors that aggregate all 24 panel values:

```yaml
template:
  - sensor:
      - name: "Tigo Max Daily Energy"         # sensor.tigo_max_daily_energy
        state: "{{ [all 24 _daily_energy states] | map('float', 0) | max }}"

      - name: "Tigo Max Readings Today"       # sensor.tigo_max_readings_today
        state: "{{ [all 24 _readings_today states] | map('float', 0) | max }}"
```

This offloads the max-value computation from the frontend (previously each of the 48 button-cards scanned all 24 sensors in JS) to a single HA template sensor that updates automatically.
