"""Second review: stale default, partial publishes, sequencing and saves."""

import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import (
    CONF_STALE_ACTION,
    DEFAULT_STALE_ACTION,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
    STALE_ACTION_HOLD,
)
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.events import InfrastructureEvent, PowerReportEvent
from custom_components.pytap.sensor import STRING_SENSOR_DESCRIPTIONS, PyTapAggregateSensor

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}
MODULE_B = {"barcode": OTHER, "name": "West Panel", "string": "West", "peak_power": 500}
LISTEN = "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen"


def _power(barcode: str = BARCODE, *, iin: float = 1.9, node_id: int | None = None):
    return PowerReportEvent(
        gateway_id=1,
        node_id=node_id if node_id is not None else (10 if barcode == BARCODE else 11),
        barcode=barcode,
        voltage_in=31.0,
        voltage_out=30.7,
        current_in=iin,
        temperature=18.0,
        dc_dc_duty_cycle=1.0,
        rssi=99,
        timestamp=dt_util.now(),
    )


def _infra(*barcodes: str, gateways=None) -> InfrastructureEvent:
    return InfrastructureEvent(
        gateways=gateways if gateways is not None else {1: {"address": "gateway"}},
        nodes={i: {"barcode": barcode} for i, barcode in enumerate(barcodes, 1)},
        timestamp=dt_util.now(),
    )


def _entry(
    hass: HomeAssistant, modules=None, *, unique_id="review2", **data
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        unique_id=unique_id,
        data={
            "host": data.pop("host", "gateway"),
            "port": 502,
            "modules": [MODULE, MODULE_B] if modules is None else modules,
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, modules=None, **data):
    entry = _entry(hass, modules, **data)
    with patch(LISTEN, return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


async def _feed(hass: HomeAssistant, coordinator: PyTapDataUpdateCoordinator, event) -> None:
    """Process like the worker does, then publish through the real live path."""
    await hass.async_add_executor_job(coordinator._process_event, event)
    coordinator._ha_update_pending = True
    coordinator._push_live_update(MagicMock(counters={}))
    await hass.async_block_till_done()


def _bare(hass: HomeAssistant, modules=None, **data) -> PyTapDataUpdateCoordinator:
    coordinator = PyTapDataUpdateCoordinator(hass, _entry(hass, modules, **data))
    coordinator._schedule_save = MagicMock()
    return coordinator


def _record_writes(written: list[str]):
    original = Entity.async_write_ha_state

    def spy(self: Entity) -> None:
        written.append(self.entity_id)
        original(self)

    return patch.object(Entity, "async_write_ha_state", spy)


def _unique_ids(hass: HomeAssistant, entity_ids: list[str]) -> set[str]:
    registry = er.async_get(hass)
    return {registry.async_get(entity_id).unique_id for entity_id in entity_ids}


# --- 1. Default stale action -------------------------------------------------


async def test_default_stale_action_is_hold_for_existing_entries(hass: HomeAssistant) -> None:
    assert DEFAULT_STALE_ACTION == STALE_ACTION_HOLD
    # An entry saved before the option existed has no stale_action key.
    coordinator = _bare(hass)
    assert CONF_STALE_ACTION not in coordinator.config_entry.data
    assert coordinator.stale_action == "hold"
    assert coordinator.stale_timeout == DEFAULT_STALE_TIMEOUT == 120
    assert _bare(hass, unique_id="x", **{CONF_STALE_ACTION: "bogus"}).stale_action == "hold"
    assert _bare(hass, unique_id="y", **{CONF_STALE_ACTION: "zero"}).stale_action == "zero"


# --- 3. Partial publishes ----------------------------------------------------


async def test_live_publish_rerenders_only_changed_panel_and_its_aggregates(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, _power(BARCODE))
    await _feed(hass, coordinator, _power(OTHER))

    written: list[str] = []
    with _record_writes(written):
        await _feed(hass, coordinator, _power(BARCODE, iin=2.5))
    unique_ids = _unique_ids(hass, written)
    assert f"{DOMAIN}_{BARCODE}_power" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_string_East_power" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_installation_power" in unique_ids
    assert all(OTHER not in uid and "string_West" not in uid for uid in unique_ids), unique_ids
    # Each entity renders once per publish.
    assert len(written) == len(set(written))

    # Unchanged infrastructure (same gateways and mappings) re-renders nothing.
    await _feed(hass, coordinator, _infra(BARCODE, OTHER))
    written.clear()
    with _record_writes(written):
        await _feed(hass, coordinator, _infra(BARCODE, OTHER))
    assert written == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_gateway_change_is_a_global_update(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, _power(BARCODE))
    await _feed(hass, coordinator, _power(OTHER))
    await _feed(hass, coordinator, _infra(BARCODE, OTHER))
    written: list[str] = []
    with _record_writes(written):
        await _feed(hass, coordinator, _infra(BARCODE, OTHER, gateways={2: {"address": "new"}}))
    unique_ids = _unique_ids(hass, written)
    assert any(BARCODE in uid for uid in unique_ids) and any(OTHER in uid for uid in unique_ids)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_interval_publish_notifies_only_changed_panels(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, live_reporting=False, write_interval=30)
    parser = MagicMock(counters={})

    async def flush() -> None:
        coordinator._ha_update_pending = True
        coordinator._last_ha_update = 0
        coordinator._maybe_flush_interval(parser, time.monotonic())
        await hass.async_block_till_done()

    for barcode in (BARCODE, OTHER):
        await hass.async_add_executor_job(coordinator._process_event, _power(barcode))
    await flush()
    written: list[str] = []
    with _record_writes(written):
        await hass.async_add_executor_job(coordinator._process_event, _power(OTHER, iin=3.0))
        await flush()
    unique_ids = _unique_ids(hass, written)
    assert f"{DOMAIN}_{OTHER}_power" in unique_ids
    assert all(BARCODE not in uid and "string_East" not in uid for uid in unique_ids), unique_ids
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- 4. Averaged-mode disconnect ---------------------------------------------


async def test_averaged_disconnect_publishes_averages_and_counters(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, [MODULE], live_reporting=False, write_interval=30)
    await hass.async_add_executor_job(coordinator._process_event, _power(iin=1.0))
    await hass.async_add_executor_job(coordinator._process_event, _power(iin=9.0))
    coordinator._ha_update_pending = True
    coordinator._maybe_flush_interval(MagicMock(counters={"frames_received": 5}), time.monotonic())
    await hass.async_block_till_done()
    averaged = coordinator.data["nodes"][BARCODE]["power"]

    # Buffers are empty but an update is pending; the raw worker value
    # differs from the published average.
    assert coordinator._reading_buffers == {}
    with coordinator._state_lock:
        node = coordinator._state["nodes"][BARCODE]
        coordinator._state["nodes"][BARCODE] = {**node, "power": 999.0}
    coordinator._ha_update_pending = True
    coordinator._counter_source = MagicMock(counters={"frames_received": 7})
    await hass.async_add_executor_job(coordinator._flush_pending_on_disconnect)
    await hass.async_block_till_done()
    node = coordinator.data["nodes"][BARCODE]
    assert node["power"] == averaged
    assert coordinator.data["counters"] == {"frames_received": 7}
    assert coordinator._ha_update_pending is False
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- 5. Midnight reset vs queued snapshots ------------------------------------


async def test_queued_snapshot_cannot_undo_midnight_reset(hass: HomeAssistant) -> None:
    coordinator = _bare(hass, [MODULE])
    coordinator._process_event(_power())
    acc = coordinator._energy_state[BARCODE]
    acc.daily_energy_wh, acc.daily_reset_date = 5000.0, "2000-01-01"
    with coordinator._state_lock:
        node = coordinator._state["nodes"][BARCODE]
        coordinator._state["nodes"][BARCODE] = {
            **node,
            "daily_energy_wh": 5000.0,
            "daily_reset_date": "2000-01-01",
        }
    coordinator.async_set_updated_data(coordinator._snapshot())
    calls: list[int] = []
    coordinator.async_add_data_listener([BARCODE], lambda: calls.append(1))

    coordinator._publish(coordinator._snapshot())  # Queued by the worker, not yet applied.
    coordinator._perform_midnight_reset()
    today = dt_util.now().date().isoformat()
    assert coordinator.data["nodes"][BARCODE]["daily_energy_wh"] == 0.0
    await hass.async_block_till_done()
    node = coordinator.data["nodes"][BARCODE]
    assert node["daily_energy_wh"] == 0.0
    assert node["daily_reset_date"] == today
    assert calls == [1]  # Only the reset re-rendered; the stale snapshot was dropped.

    # Its changes are notified with the next applied snapshot.
    coordinator._publish(coordinator._snapshot())
    await hass.async_block_till_done()
    assert calls == [1, 1]
    # Direct publication of a fresh snapshot keeps working.
    coordinator.async_set_updated_data(coordinator._snapshot())
    assert coordinator.data["nodes"][BARCODE]["daily_reset_date"] == today


# --- 6. Save scheduling --------------------------------------------------------


async def test_schedule_save_posts_once_per_cycle(hass: HomeAssistant) -> None:
    coordinator = PyTapDataUpdateCoordinator(hass, _entry(hass, [MODULE]))
    saved = AsyncMock()
    coordinator._async_save_coordinator_state = saved

    def burst() -> None:
        for _ in range(500):
            coordinator._schedule_save()

    def run_worker_burst() -> None:
        worker = threading.Thread(target=burst)
        worker.start()
        worker.join()

    with patch.object(hass.loop, "call_soon_threadsafe", wraps=hass.loop.call_soon_threadsafe) as post:

        def posts() -> int:
            return sum(1 for c in post.call_args_list if c.args[0].__name__ == "_do_schedule")

        run_worker_burst()
        await hass.async_block_till_done()
        assert posts() == 1
        assert coordinator._save_task is not None

        coordinator._save_task.cancel()  # Run the save now instead of in 10 s.
        await coordinator._do_save()
        saved.assert_awaited_once()
        run_worker_burst()
        await hass.async_block_till_done()
        assert posts() == 2

    coordinator._cancel_scheduled_save()
    assert coordinator._save_task is None and coordinator._save_requested is False

    # A request that arrives while stopping doesn't block later cycles.
    coordinator._stop_event.set()
    coordinator._schedule_save()
    await hass.async_block_till_done()
    assert coordinator._save_task is None and coordinator._save_requested is False


# --- 7. Aggregate performance --------------------------------------------------


def _aggregate(description, barcodes, nodes, *, action, reporting):
    coordinator = MagicMock(spec=PyTapDataUpdateCoordinator)
    coordinator.data = {"nodes": nodes}
    coordinator.stale_action = action
    coordinator.is_reporting.side_effect = lambda barcode: barcode in reporting
    sensor = PyTapAggregateSensor(coordinator, description, barcodes, MagicMock(), "uid")
    sensor.async_write_ha_state = MagicMock()
    return coordinator, sensor


@pytest.mark.parametrize(
    ("action", "expected"),
    [("zero", 25.0), ("unavailable", 50.0), ("hold", 65.0)],
)
def test_aggregate_performance_by_stale_action(action: str, expected: float) -> None:
    description = next(d for d in STRING_SENSOR_DESCRIPTIONS if d.key == "performance")
    nodes = {
        BARCODE: {"power": 250.0, "peak_power": 500},
        OTHER: {"power": 400.0, "peak_power": 500},  # Stale: last reading before dark.
    }
    _, sensor = _aggregate(description, [BARCODE, OTHER], nodes, action=action, reporting={BARCODE})
    sensor._handle_coordinator_update()
    assert sensor.native_value == expected
