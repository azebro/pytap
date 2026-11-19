"""Stale-reading actions, status entities, faults and aggregate energy."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import (
    CONF_AUTO_DISCOVERY,
    CONF_STALE_ACTION,
    CONF_STALE_TIMEOUT,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
)
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator, reading_faults
from custom_components.pytap.pytap.core.events import PowerReportEvent
from custom_components.pytap.pytap.core.types import PowerReport

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}
MODULE_B = {"barcode": OTHER, "name": "West Panel", "string": "West", "peak_power": 500}


def _event(barcode=BARCODE, *, vin=31.0, vout=30.7, iin=1.9, temp=18.0, status=0x64, high=True, low=False):
    return PowerReportEvent(
        gateway_id=1,
        node_id=10 if barcode == BARCODE else 11,
        barcode=barcode,
        voltage_in=vin,
        voltage_out=vout,
        current_in=iin,
        temperature=temp,
        dc_dc_duty_cycle=1.0,
        rssi=99,
        timestamp=dt_util.now(),
        high_output_mode=high,
        status_code=status,
        low_voltage_flag=low,
    )


# The corrupt node 10 report captured at 2026-11-18 10:02:44 UTC.
IMPOSSIBLE = {"vin": 69.7, "vout": 154.3, "iin": 7.87, "status": 0x50, "high": True, "low": False}


def test_plausible_readings_have_no_faults() -> None:
    assert reading_faults(_event()) == []
    # Dawn readings: low voltage, output just under input.
    assert reading_faults(_event(vin=11.3, vout=5.1, iin=0.02, status=0xE4, low=True)) == []
    # Rounding can put output fractionally above input.
    assert reading_faults(_event(vin=30.0, vout=31.5)) == []


def test_impossible_readings_are_flagged() -> None:
    assert reading_faults(_event(**IMPOSSIBLE)) == [
        "voltage_out_out_of_range",
        "voltage_out_above_input",
    ]
    assert reading_faults(_event(temp=150.0)) == ["temperature_out_of_range"]
    assert reading_faults(_event(vin=-1.0, vout=0.0)) == ["voltage_in_out_of_range"]


def test_power_report_status_bits() -> None:
    def report(byte7: int, byte9: int) -> PowerReport:
        return PowerReport.from_bytes(
            bytes([0x26, 0x41, 0x33, 0xFF, 0x17, 0x60, 0xB4, byte7, 0, byte9, 0, 0, 99])
        )

    production = report(0x93, 0x64)
    assert production.high_output_mode is True
    assert production.status_code == 0x64
    assert production.low_voltage_flag is False

    dawn = report(0x10, 0xE4)
    assert dawn.high_output_mode is False
    assert dawn.status_code == 0xE4
    assert dawn.low_voltage_flag is True


async def _setup(
    hass: HomeAssistant, modules=None, **data
) -> tuple[MockConfigEntry, PyTapDataUpdateCoordinator]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        unique_id="status-entry",
        data={
            "host": "gateway",
            "port": 502,
            "connection_type": "tcp",
            "modules": modules or [MODULE],
            CONF_AUTO_DISCOVERY: False,
            **data,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


async def _feed(
    hass: HomeAssistant, coordinator: PyTapDataUpdateCoordinator, event: PowerReportEvent
) -> None:
    """Process like the worker does, then publish through the real live path."""
    await hass.async_add_executor_job(coordinator._process_event, event)
    coordinator._ha_update_pending = True
    coordinator._push_live_update(MagicMock(counters={}))
    await hass.async_block_till_done()


def _age(coordinator: PyTapDataUpdateCoordinator, barcode: str, seconds: float) -> None:
    """Pretend the last report arrived `seconds` ago, then run the timer check."""
    reported = time.monotonic() - seconds
    with coordinator._state_lock:
        node = coordinator._state["nodes"][barcode]
        coordinator._state["nodes"][barcode] = {**node, "report_monotonic": reported}
    nodes = dict(coordinator.data["nodes"])
    nodes[barcode] = {**nodes[barcode], "report_monotonic": reported}
    coordinator.data = {**coordinator.data, "nodes": nodes}
    coordinator._async_check_freshness()


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return hass.states.get(entity_id)


def _uid(key: str, barcode: str = BARCODE) -> str:
    return f"{DOMAIN}_{barcode}_{key}"


async def test_default_holds_values_with_120s_timeout(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    assert coordinator.stale_action == "hold" and coordinator.stale_timeout == 120
    await _feed(hass, coordinator, _event())
    power = _state(hass, "sensor", _uid("power")).state
    _age(coordinator, BARCODE, DEFAULT_STALE_TIMEOUT + 1)
    await hass.async_block_till_done()
    assert coordinator.is_reporting(BARCODE) is False
    assert _state(hass, "sensor", _uid("power")).state == power
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_after_120s_fault_and_recovery(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, **{CONF_STALE_ACTION: "unavailable"})
    assert coordinator.stale_action == "unavailable" and coordinator.stale_timeout == 120
    installation_power = f"{DOMAIN}_{entry.entry_id}_installation_power"

    # No report yet this session: live values unavailable, fault unknown.
    assert _state(hass, "sensor", _uid("power")).state == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_UNAVAILABLE

    await _feed(hass, coordinator, _event())
    assert float(_state(hass, "sensor", _uid("power")).state) > 0
    assert _state(hass, "sensor", _uid("status_code")).state == "0x64"
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_ON
    assert _state(hass, "binary_sensor", _uid("low_voltage_flag")).state == STATE_OFF
    assert _state(hass, "binary_sensor", _uid("reading_fault")).state == STATE_OFF

    # Just inside the timeout: still live.
    _age(coordinator, BARCODE, DEFAULT_STALE_TIMEOUT - 5)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", _uid("power")).state != STATE_UNAVAILABLE

    # Reports stop (darkness): live values go unavailable without new data;
    # energy and the fault entity remain.
    _age(coordinator, BARCODE, DEFAULT_STALE_TIMEOUT + 1)
    await hass.async_block_till_done()
    assert coordinator.is_reporting(BARCODE) is False
    for domain, unique_id in (
        ("sensor", _uid("power")),
        ("sensor", _uid("temperature")),
        ("sensor", _uid("status_code")),
        ("binary_sensor", _uid("high_output_mode")),
        ("binary_sensor", _uid("low_voltage_flag")),
        ("sensor", installation_power),
    ):
        assert _state(hass, domain, unique_id).state == STATE_UNAVAILABLE, unique_id
    energy_before = _state(hass, "sensor", _uid("total_energy")).state
    assert energy_before != STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", _uid("reading_fault")).state == STATE_OFF

    # An impossible reading raises the fault but does not replace the last good
    # measurement, add energy, or count as a fresh report.
    good_voltage = coordinator._state["nodes"][BARCODE]["voltage_in"]
    await _feed(hass, coordinator, _event(**IMPOSSIBLE))
    fault = _state(hass, "binary_sensor", _uid("reading_fault"))
    assert fault.state == STATE_ON
    assert fault.attributes["fault_reasons"] == ["voltage_out_out_of_range", "voltage_out_above_input"]
    assert fault.attributes["last_fault_at"] is not None
    assert coordinator._state["nodes"][BARCODE]["voltage_in"] == good_voltage
    assert _state(hass, "sensor", _uid("power")).state == STATE_UNAVAILABLE
    assert _state(hass, "sensor", _uid("total_energy")).state == energy_before

    # The next valid reading clears the fault and restores live values.
    await _feed(hass, coordinator, _event(vin=13.2, vout=9.9, iin=0.03, status=0xE4, high=False, low=True))
    fault = _state(hass, "binary_sensor", _uid("reading_fault"))
    assert fault.state == STATE_OFF
    assert fault.attributes["fault_reasons"] == []
    assert fault.attributes["last_fault_at"] is not None
    assert _state(hass, "sensor", _uid("power")).state != STATE_UNAVAILABLE
    assert _state(hass, "sensor", _uid("status_code")).state == "0xE4"
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_OFF
    assert _state(hass, "binary_sensor", _uid("low_voltage_flag")).state == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fault_is_unknown_before_first_report(hass: HomeAssistant) -> None:
    stored = {"barcode_to_node": {BARCODE: 10}, "node_snapshots": {BARCODE: {"power": 100.0}}}
    with patch(
        "custom_components.pytap.coordinator._MigratingStore.async_load", new=AsyncMock(return_value=stored)
    ):
        entry, _ = await _setup(hass)
    assert _state(hass, "binary_sensor", _uid("reading_fault")).state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_zero_action(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, **{CONF_STALE_ACTION: "zero", CONF_STALE_TIMEOUT: 60})
    installation_power = f"{DOMAIN}_{entry.entry_id}_installation_power"
    await _feed(hass, coordinator, _event())
    _age(coordinator, BARCODE, 61)
    await hass.async_block_till_done()

    for key in ("power", "voltage_in", "voltage_out", "current_in", "current_out", "performance"):
        assert float(_state(hass, "sensor", _uid(key)).state) == 0.0, key
    for key in ("temperature", "dc_dc_duty_cycle", "rssi", "status_code"):
        assert _state(hass, "sensor", _uid(key)).state == STATE_UNAVAILABLE, key
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_UNAVAILABLE
    assert float(_state(hass, "sensor", installation_power).state) == 0.0
    assert _state(hass, "sensor", _uid("total_energy")).state != STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hold_action_keeps_values_and_restores_after_restart(hass: HomeAssistant) -> None:
    stored = {
        "barcode_to_node": {BARCODE: 10},
        "node_snapshots": {BARCODE: {"power": 123.0, "temperature": 20.0}},
    }
    with patch(
        "custom_components.pytap.coordinator._MigratingStore.async_load", new=AsyncMock(return_value=stored)
    ):
        entry, coordinator = await _setup(hass, **{CONF_STALE_ACTION: "hold"})
    # Upstream behaviour: restored values are shown before any report.
    assert float(_state(hass, "sensor", _uid("power")).state) == 123.0
    await _feed(hass, coordinator, _event())
    power = _state(hass, "sensor", _uid("power")).state
    _age(coordinator, BARCODE, 600)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", _uid("power")).state == power
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_averaged_mode_never_publishes_raw_values(hass: HomeAssistant) -> None:
    """Review #1: freshness transitions must re-render published averages only."""
    entry, coordinator = await _setup(hass, [MODULE, MODULE_B], live_reporting=False, write_interval=30)
    parser = MagicMock(counters={})
    await hass.async_add_executor_job(coordinator._process_event, _event(iin=1.0))
    await hass.async_add_executor_job(coordinator._process_event, _event(OTHER, iin=1.0))
    coordinator._ha_update_pending = True
    coordinator._maybe_flush_interval(parser, time.monotonic())
    await hass.async_block_till_done()
    published_power = float(_state(hass, "sensor", _uid("power")).state)

    # A large raw reading arrives mid-interval; then another panel goes stale,
    # which re-renders every entity.
    await hass.async_add_executor_job(coordinator._process_event, _event(iin=9.0))
    _age(coordinator, OTHER, 1000)
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", _uid("power")).state) == published_power
    assert coordinator.data["nodes"][BARCODE]["power"] == pytest.approx(published_power, rel=0.01)

    # Allowance is timeout + write interval: no flapping between flushes.
    _age(coordinator, BARCODE, DEFAULT_STALE_TIMEOUT + 20)
    await hass.async_block_till_done()
    assert coordinator.is_reporting(BARCODE) is True
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_averaged_mode_publishes_faults_without_new_readings(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, live_reporting=False, write_interval=30)
    parser = MagicMock(counters={})
    await hass.async_add_executor_job(coordinator._process_event, _event())
    coordinator._ha_update_pending = True
    coordinator._maybe_flush_interval(parser, time.monotonic())
    await hass.async_block_till_done()
    await hass.async_add_executor_job(coordinator._process_event, _event(**IMPOSSIBLE))
    coordinator._ha_update_pending = True
    coordinator._last_ha_update = 0
    coordinator._maybe_flush_interval(parser, time.monotonic())
    await hass.async_block_till_done()
    assert _state(hass, "binary_sensor", _uid("reading_fault")).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_status_entities_only_write_on_change(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, _event())
    status_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, _uid("status_code"))
    flag_id = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, _uid("high_output_mode"))
    power_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, _uid("power"))
    before = {eid: hass.states.get(eid).last_reported for eid in (status_id, flag_id, power_id)}
    assert "last_update" not in hass.states.get(status_id).attributes

    await _feed(hass, coordinator, _event(iin=2.5))  # Same status values, new power.
    assert hass.states.get(status_id).last_reported == before[status_id]
    assert hass.states.get(flag_id).last_reported == before[flag_id]
    assert hass.states.get(power_id).last_reported != before[power_id]

    await _feed(hass, coordinator, _event(status=0x68, high=False))
    assert hass.states.get(status_id).state == "0x68"
    assert hass.states.get(flag_id).state == STATE_OFF
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fault_persists_but_live_timing_does_not(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, _event())
    await _feed(hass, coordinator, _event(**IMPOSSIBLE))

    saved = AsyncMock()
    with patch.object(coordinator._store, "async_save", saved):
        await coordinator._async_save_coordinator_state()
    snapshot = saved.call_args.args[0]["node_snapshots"][BARCODE]
    assert snapshot["reading_fault"] is True
    assert snapshot["fault_reasons"] == ["voltage_out_out_of_range", "voltage_out_above_input"]
    assert "report_monotonic" not in snapshot
    # Status comes from the last *valid* report, saved so "hold" can show it.
    assert snapshot["status_code"] == 0x64
    assert snapshot["high_output_mode"] is True

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_topology_survives_power_reports(hass: HomeAssistant) -> None:
    from custom_components.pytap.pytap.core.events import TopologyEvent

    entry, coordinator = await _setup(hass)
    coordinator._node_to_barcode[10] = BARCODE
    await _feed(hass, coordinator, _event())
    topology = TopologyEvent(gateway_id=1, node_id=10, data=b"\x01", timestamp=dt_util.now())
    await hass.async_add_executor_job(coordinator._process_event, topology)
    await _feed(hass, coordinator, _event())
    assert coordinator._state["nodes"][BARCODE]["topology"]["data"] == "01"
    assert await hass.config_entries.async_unload(entry.entry_id)


def _string_energy(barcodes: list[str], values: dict[str, float]):
    """A string total_energy aggregate fed with member lifetime totals."""
    from custom_components.pytap.sensor import STRING_SENSOR_DESCRIPTIONS, PyTapAggregateSensor

    coordinator = MagicMock(spec=PyTapDataUpdateCoordinator)
    coordinator.data = {"nodes": {b: {"total_energy_wh": v} for b, v in values.items()}}
    coordinator.stale_action = "unavailable"
    description = next(d for d in STRING_SENSOR_DESCRIPTIONS if d.key == "total_energy")
    sensor = PyTapAggregateSensor(coordinator, description, barcodes, MagicMock(), "uid")
    sensor.async_write_ha_state = MagicMock()
    return coordinator, sensor


def test_aggregate_energy_ignores_membership_changes() -> None:
    """Review #3: moving a panel into a string must not add its lifetime energy."""
    coordinator, sensor = _string_energy([BARCODE], {BARCODE: 1000.0, OTHER: 5000.0})
    sensor._handle_coordinator_update()
    assert sensor.native_value == 1000.0

    sensor.set_barcodes([BARCODE, OTHER])  # OTHER moved into this string.
    sensor._handle_coordinator_update()
    assert sensor.native_value == 1000.0

    coordinator.data = {"nodes": {BARCODE: {"total_energy_wh": 1010.0}, OTHER: {"total_energy_wh": 5020.0}}}
    sensor._handle_coordinator_update()
    assert sensor.native_value == 1030.0

    sensor.set_barcodes([OTHER])  # BARCODE moved out: no decrease.
    sensor._handle_coordinator_update()
    assert sensor.native_value == 1030.0


def test_aggregate_energy_restores_baselines() -> None:
    coordinator, sensor = _string_energy([BARCODE], {BARCODE: 1000.0})
    sensor._handle_coordinator_update()
    restored = sensor.extra_restore_state_data.as_dict()
    assert restored["baselines"] == {BARCODE: 1000.0}

    _, again = _string_energy([BARCODE], {BARCODE: 1005.0})
    again._energy_total = restored["native_value"]
    again._baselines = restored["baselines"]
    again._handle_coordinator_update()
    assert again.native_value == 1005.0


async def test_options_flow_stale_step(hass: HomeAssistant) -> None:
    entry, _ = await _setup(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "change_stale"}
        )
        assert result["step_id"] == "change_stale"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_STALE_ACTION: "zero", CONF_STALE_TIMEOUT: 300}
        )
        await hass.async_block_till_done()
    assert result["step_id"] == "init"
    assert entry.data[CONF_STALE_ACTION] == "zero"
    assert entry.data[CONF_STALE_TIMEOUT] == 300
    assert entry.runtime_data.stale_timeout == 300
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("timeout", [59, 601])
async def test_options_flow_stale_timeout_bounds(hass: HomeAssistant, timeout: int) -> None:
    from homeassistant.data_entry_flow import InvalidData

    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "change_stale"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_STALE_ACTION: "zero", CONF_STALE_TIMEOUT: timeout}
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hold_restores_status_entities_across_global_updates(hass: HomeAssistant) -> None:
    """Saved status values survive a restart and a global (e.g. gateway) update."""
    stored = {
        "barcode_to_node": {BARCODE: 10},
        "node_snapshots": {
            BARCODE: {"power": 50.0, "status_code": 0xE4, "high_output_mode": False, "low_voltage_flag": True}
        },
    }
    with patch(
        "custom_components.pytap.coordinator._MigratingStore.async_load", new=AsyncMock(return_value=stored)
    ):
        entry, coordinator = await _setup(hass)
    assert coordinator.stale_action == "hold"
    coordinator.async_set_updated_data(coordinator._snapshot())  # Global update, no report.
    await hass.async_block_till_done()
    assert _state(hass, "sensor", _uid("status_code")).state == "0xE4"
    assert _state(hass, "binary_sensor", _uid("high_output_mode")).state == STATE_OFF
    assert _state(hass, "binary_sensor", _uid("low_voltage_flag")).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)
