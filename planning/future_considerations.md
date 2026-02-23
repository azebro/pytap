# Future Considerations

## 1. Per Sensor Cumulatives

Status: ✅ Implemented in Feature 1.

Each sensor now implements the following meters:

1.1 **Daily generated power** — This meter will aggregate power during the day and zero at midnight. Need to take care of the potential time differences between readings. Should use HA trapezoid calculations not to invent the wheel. Important is not to take simple averages as time between readings may vary. This meter should be in Wh.

1.2 **Total power generated** — Always-growing lifetime counter (`total_energy`) implemented in Wh (`SensorStateClass.TOTAL_INCREASING`).

## 2. Total Cumulatives

Status: ✅ Implemented in Feature 2.

Aggregate entities now expose power, daily energy, and total energy at two levels:

2.1 **String**

2.2 **Total installation**

## 3. Performance

Status: ✅ Implemented in Feature 3.

3.1 **Peak panel power configuration** — Panel configuration extended to add peak panel power. Based on that, each sensor shows performance in % representing `Power / Peak Power`. Default value set to 455 W for migrations or when user does not input a value.

3.2 **Aggregate performance** — The per-optimizer performance concept extended to per-string and installation-wide totals.

## 4. Energy Dashboard Integration

Status: ✅ Implemented per optimizer.

PyTap now exposes native `SensorDeviceClass.ENERGY` sensors:
- `daily_energy` in Wh with `SensorStateClass.TOTAL` and `last_reset`
- `total_energy` in Wh with `SensorStateClass.TOTAL_INCREASING`

These are compatible with the Home Assistant energy dashboard without requiring a user-side Riemann sum helper.

## 5. Diagnostics Platform

Status: ✅ Implemented in Feature 5.

5.1 **Diagnostics download** — Exposes parser counters (`frames_received`, `crc_errors`, `noise_bytes`, `runts`, `giants`), infrastructure state, barcode↔node mappings, discovered barcodes, connection state, and per-node summaries via Home Assistant's integration diagnostics download.

5.2 **Per-sensor readings counter** — Each optimizer now has a `readings_today` daily meter (`SensorStateClass.TOTAL`, diagnostic category) to support per-module connectivity troubleshooting.

## 6. Binary Sensor Platform

Add binary sensors for node connectivity (available/unavailable based on `last_update` age) and gateway online status.

## 7. HACS Distribution

Package for distribution via [HACS](https://hacs.xyz/) with a `hacs.json` manifest for one-click installation.

## 8. Configuration
- Add ability to bulk load devices
- Add ability to modify barcodes
- Make energy gap threshold configurable via options flow (currently hardcoded at 120 s)
