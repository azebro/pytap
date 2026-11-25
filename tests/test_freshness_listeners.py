"""Freshness transitions re-render only the affected panels and aggregates."""

import time
from unittest.mock import MagicMock, patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import CONF_AUTO_DISCOVERY, DEFAULT_STALE_TIMEOUT, DOMAIN
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.events import PowerReportEvent

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}
MODULE_B = {"barcode": OTHER, "name": "West Panel", "string": "West", "peak_power": 500}


def _event(barcode: str) -> PowerReportEvent:
    return PowerReportEvent(
        gateway_id=1,
        node_id=10 if barcode == BARCODE else 11,
        barcode=barcode,
        voltage_in=31.0,
        voltage_out=30.7,
        current_in=1.9,
        temperature=18.0,
        dc_dc_duty_cycle=1.0,
        rssi=99,
        timestamp=dt_util.now(),
        high_output_mode=True,
        status_code=0x64,
        low_voltage_flag=False,
    )


async def _setup(hass: HomeAssistant) -> tuple[MockConfigEntry, PyTapDataUpdateCoordinator]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        unique_id="freshness-entry",
        data={
            "host": "gateway",
            "port": 502,
            "connection_type": "tcp",
            "modules": [MODULE, MODULE_B],
            CONF_AUTO_DISCOVERY: False,
            "stale_action": "unavailable",  # Opt-in; makes freshness visible.
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = getattr(entry, "runtime_data", None) or hass.data[DOMAIN][entry.entry_id]
    return entry, coordinator


async def _feed(hass: HomeAssistant, coordinator: PyTapDataUpdateCoordinator, barcode: str) -> None:
    await hass.async_add_executor_job(coordinator._process_event, _event(barcode))
    coordinator._ha_update_pending = True
    coordinator._push_live_update(MagicMock(counters={}))
    await hass.async_block_till_done()


def _set_age(coordinator: PyTapDataUpdateCoordinator, barcode: str, seconds: float) -> None:
    reported = time.monotonic() - seconds
    with coordinator._state_lock:
        node = coordinator._state["nodes"][barcode]
        coordinator._state["nodes"][barcode] = {**node, "report_monotonic": reported}
    nodes = dict(coordinator.data["nodes"])
    nodes[barcode] = {**nodes[barcode], "report_monotonic": reported}
    coordinator.data = {**coordinator.data, "nodes": nodes}


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


def _record_writes(written: list[str]):
    original = Entity.async_write_ha_state

    def spy(self: Entity) -> None:
        written.append(self.entity_id)
        original(self)

    return patch.object(Entity, "async_write_ha_state", spy)


async def test_stale_panel_rerenders_only_its_entities_and_aggregates(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, BARCODE)
    await _feed(hass, coordinator, OTHER)

    def uid(key: str, barcode: str) -> str:
        return _entity_id(hass, "sensor", f"{DOMAIN}_{barcode}_{key}")

    def aggregate(name: str) -> str:
        return _entity_id(hass, "sensor", f"{DOMAIN}_{entry.entry_id}_{name}_power")

    fresh_power, stale_power = uid("power", BARCODE), uid("power", OTHER)
    stale_flag = _entity_id(hass, "binary_sensor", f"{DOMAIN}_{OTHER}_high_output_mode")
    east, west, installation = aggregate("string_East"), aggregate("string_West"), aggregate("installation")
    before = {eid: hass.states.get(eid).last_reported for eid in (fresh_power, east, installation)}

    written: list[str] = []
    with _record_writes(written):
        _set_age(coordinator, OTHER, DEFAULT_STALE_TIMEOUT + 1)
        coordinator._async_check_freshness()
        await hass.async_block_till_done()

    assert hass.states.get(stale_power).state == STATE_UNAVAILABLE
    assert hass.states.get(stale_flag).state == STATE_UNAVAILABLE
    assert hass.states.get(west).state == STATE_UNAVAILABLE
    assert hass.states.get(installation).attributes["reporting_count"] == 1
    assert hass.states.get(installation).last_reported != before[installation]
    assert hass.states.get(fresh_power).last_reported == before[fresh_power]
    assert hass.states.get(east).last_reported == before[east]

    registry = er.async_get(hass)
    for entity_id in written:
        unique_id = registry.async_get(entity_id).unique_id
        assert OTHER in unique_id or "installation" in unique_id or "string_West" in unique_id, entity_id
    # Energy totals don't depend on freshness and are not re-rendered.
    assert uid("total_energy", OTHER) not in written

    # No transition: nothing is re-rendered.
    written.clear()
    with _record_writes(written):
        coordinator._async_check_freshness()
        await hass.async_block_till_done()
    assert written == []

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_one_call_per_registration_and_unsubscribe(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, BARCODE)
    await _feed(hass, coordinator, OTHER)

    calls: list[int] = []
    unsub = coordinator.async_add_freshness_listener([BARCODE, OTHER], lambda: calls.append(1))
    _set_age(coordinator, BARCODE, DEFAULT_STALE_TIMEOUT + 1)
    _set_age(coordinator, OTHER, DEFAULT_STALE_TIMEOUT + 1)
    coordinator._async_check_freshness()
    await hass.async_block_till_done()
    assert calls == [1]

    unsub()
    assert all(unsub not in listeners for listeners in coordinator._freshness_listeners.values())
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_aggregate_follows_membership_changes(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, BARCODE)
    await _feed(hass, coordinator, OTHER)
    east_id = _entity_id(hass, "sensor", f"{DOMAIN}_{entry.entry_id}_string_East_power")
    east = hass.data["sensor"].get_entity(east_id)

    east.set_barcodes([BARCODE, OTHER])  # The West panel moved into East.
    written: list[str] = []
    with _record_writes(written):
        _set_age(coordinator, OTHER, DEFAULT_STALE_TIMEOUT + 1)
        coordinator._async_check_freshness()
        await hass.async_block_till_done()
    assert east_id in written
    assert hass.states.get(east_id).attributes["reporting_count"] == 1

    east.set_barcodes([BARCODE])  # And back out again.
    await _feed(hass, coordinator, OTHER)
    written.clear()
    with _record_writes(written):
        _set_age(coordinator, OTHER, DEFAULT_STALE_TIMEOUT + 1)
        coordinator._async_check_freshness()
        await hass.async_block_till_done()
    assert written and east_id not in written

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_removes_all_freshness_listeners(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    assert set(coordinator._freshness_listeners) == {BARCODE, OTHER}
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._freshness_listeners == {}
