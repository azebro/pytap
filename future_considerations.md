# Future Considerations

## 1. Per Sensor Cumulatives

Each sensor should implement the following meters:

1.1 **Daily power meter** — This meter will aggregate power during the day and zero at midnight. Need to take care of the potential time differences between readings. Should use HA trapezoid calculations not to invent the wheel. Important is not to take simple averages as time between readings may vary. This meter should be in W.

1.2 **Total power generated** — Always growing meter that will show total power production over lifetime of the sensor in kWh.

## 2. Total Cumulatives

Integration should create entities to show daily and total power meters in kWh for:

2.1 String

2.2 Total installation

## 3. Performance

3.1 Panel configuration should be extended to add peak panel power. Based on that there should be entity in each sensor to show performance in % which will represent 'Power'/'Peak Power'.

3.2 The 3.1 concept should be extended to per string and total.

## 4. Energy Dashboard Integration

Expose `PowerReportEvent.power` as a `SensorDeviceClass.POWER` entity with `SensorStateClass.MEASUREMENT`. HA's energy dashboard can then track per-optimizer production when combined with a Riemann sum integration helper for energy (kWh).

## 5. Diagnostics Platform

Expose parser counters (`frames_received`, `crc_errors`, `noise_bytes`) and infrastructure state as a diagnostics download for troubleshooting.

## 6. Binary Sensor Platform

Add binary sensors for node connectivity (available/unavailable based on `last_update` age) and gateway online status.

## 7. HACS Distribution

Package for distribution via [HACS](https://hacs.xyz/) with a `hacs.json` manifest for one-click installation.
