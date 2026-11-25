"""Second review: stale default, barcode claims, partial publishes, sequencing and saves."""

from datetime import date, datetime, timedelta
from datetime import time as dt_time
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.pytap.const import (
    CONF_AUTO_DISCOVERY,
    CONF_STALE_ACTION,
    DEFAULT_STALE_ACTION,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
    STALE_ACTION_HOLD,
)
from custom_components.pytap.coordinator import (
    BarcodeClaims,
    PyTapDataUpdateCoordinator,
    barcode_claims,
)
from custom_components.pytap.pytap.core.events import InfrastructureEvent, PowerReportEvent
from custom_components.pytap.sensor import STRING_SENSOR_DESCRIPTIONS, PyTapAggregateSensor

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}
MODULE_B = {"barcode": OTHER, "name": "West Panel", "string": "West", "peak_power": 500}
LISTEN = "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen"
STORE_LOAD = "custom_components.pytap.coordinator._MigratingStore.async_load"


def _power(barcode: str = BARCODE, *, iin: float = 1.9, node_id: int | None = None, vout: float = 30.7):
    return PowerReportEvent(
        gateway_id=1,
        node_id=node_id if node_id is not None else (10 if barcode == BARCODE else 11),
        barcode=barcode,
        voltage_in=31.0,
        voltage_out=vout,
        current_in=iin,
        temperature=18.0,
        dc_dc_duty_cycle=1.0,
        rssi=99,
        timestamp=dt_util.now(),
        high_output_mode=True,
        status_code=0x64,
        low_voltage_flag=False,
    )


def _infra(*barcodes: str, gateways=None) -> InfrastructureEvent:
    return InfrastructureEvent(
        gateways=gateways if gateways is not None else {1: {"address": "gateway"}},
        nodes={i: {"barcode": barcode} for i, barcode in enumerate(barcodes, 1)},
        timestamp=dt_util.now(),
    )


def _entry(
    hass: HomeAssistant, modules=None, *, entry_id=None, unique_id="review2", **data
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        unique_id=unique_id,
        entry_id=entry_id,
        data={
            "host": data.pop("host", "gateway"),
            "port": 502,
            "connection_type": "tcp",
            "modules": [MODULE, MODULE_B] if modules is None else modules,
            CONF_AUTO_DISCOVERY: data.pop("auto", False),
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


# --- 2. Barcode claims -------------------------------------------------------


def test_claims_are_atomic_across_threads() -> None:
    claims = BarcodeClaims()
    barcodes = [f"B-{i:07d}" for i in range(2000)]
    won: dict[str, set[str]] = {"a": set(), "b": set()}
    barrier = threading.Barrier(2)

    def worker(entry_id: str, order: list[str]) -> None:
        barrier.wait()
        for barcode in order:
            if claims.claim(barcode, entry_id):
                won[entry_id].add(barcode)

    threads = [
        threading.Thread(target=worker, args=("a", barcodes)),
        threading.Thread(target=worker, args=("b", list(reversed(barcodes)))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not won["a"] & won["b"]
    assert won["a"] | won["b"] == set(barcodes)
    # Same-entry re-claims succeed; releasing hands barcodes to others.
    assert all(claims.claim(barcode, "a") for barcode in won["a"])
    claims.release_entry("a")
    assert all(claims.claim(barcode, "b") for barcode in barcodes)


async def test_two_entries_discovering_concurrently_never_share_a_barcode(hass: HomeAssistant) -> None:
    first = _bare(hass, [], auto=True, unique_id="first")
    second = _bare(hass, [], auto=True, unique_id="second", host="other")
    barcodes = [f"B-{i:07d}" for i in range(300)]
    barrier = threading.Barrier(2)

    def worker(coordinator: PyTapDataUpdateCoordinator, order: list[str]) -> None:
        barrier.wait()
        coordinator._process_event(_infra(*order))

    # The worker must never touch loop-only config entry APIs.
    with patch.object(hass.config_entries, "async_entries", side_effect=AssertionError("loop only")):
        threads = [
            threading.Thread(target=worker, args=(first, barcodes)),
            threading.Thread(target=worker, args=(second, list(reversed(barcodes)))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    tracked_first = {m["barcode"] for m in first.modules}
    tracked_second = {m["barcode"] for m in second.modules}
    assert not tracked_first & tracked_second
    assert tracked_first | tracked_second == set(barcodes)


async def test_manual_modules_are_claimed_at_init(hass: HomeAssistant) -> None:
    manual = _bare(hass, [MODULE], unique_id="manual")
    automatic = _bare(hass, [], auto=True, unique_id="auto", host="other")
    assert barcode_claims(hass).claim(BARCODE, manual.config_entry.entry_id)
    assert automatic._add_discovered_module(BARCODE, 1) is False
    assert automatic._add_discovered_module(OTHER, 2) is True

    # Released on unload/removal: the other entry may now track it.
    manual.release_barcode_claims()
    assert automatic._add_discovered_module(BARCODE, 1) is True


async def test_same_entry_reclaims_after_reload(hass: HomeAssistant) -> None:
    coordinator = _bare(hass, [], auto=True)
    coordinator._process_event(_infra(BARCODE))
    assert coordinator.module_count == 1
    # A new (or parked) coordinator for the same entry restores its modules.
    restored = PyTapDataUpdateCoordinator(hass, coordinator.config_entry)
    restored._schedule_save = MagicMock()
    restored._store.async_load = AsyncMock(return_value={"auto_modules": coordinator.modules})
    await restored._async_load_coordinator_state()
    assert restored.module_count == 1


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


async def test_discovery_still_adds_entities(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, [], auto=True)
    await _feed(hass, coordinator, _power(OTHER, node_id=7))
    registry = er.async_get(hass)
    power_id = registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{OTHER}_power")
    flag_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{DOMAIN}_{OTHER}_high_output_mode")
    assert power_id is not None and flag_id is not None
    assert float(hass.states.get(power_id).state) > 0
    installation = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert float(hass.states.get(installation).state) > 0

    # Later reports for the discovered panel update through its data listener.
    before = float(hass.states.get(power_id).state)
    await _feed(hass, coordinator, _power(OTHER, node_id=7, iin=3.0))
    assert float(hass.states.get(power_id).state) > before
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert coordinator._data_listeners == {}


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

    # A fault leaves the buffers empty but marks an update pending; the raw
    # worker value differs from the published average.
    await hass.async_add_executor_job(coordinator._process_event, _power(vout=154.3))
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
    assert node["reading_fault"] is True
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


# --- 7. Aggregate performance and energy ----------------------------------------


def _aggregate(description, barcodes, nodes, *, action, reporting):
    coordinator = MagicMock(spec=PyTapDataUpdateCoordinator)
    coordinator.data = {"nodes": nodes}
    coordinator.stale_action = action
    coordinator.is_reporting.side_effect = lambda barcode: barcode in reporting
    coordinator.module_peak_power.return_value = 500
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


def test_daily_aggregate_rolls_over_with_its_cycle_date() -> None:
    description = next(d for d in STRING_SENSOR_DESCRIPTIONS if d.key == "daily_energy")
    coordinator, sensor = _aggregate(
        description, [BARCODE], {BARCODE: {"daily_energy_wh": 400.0}}, action="hold", reporting={BARCODE}
    )
    today = dt_util.now(dt_util.UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    sensor._energy_total, sensor._baselines, sensor._cycle_date = 500.0, {BARCODE: 390.0}, yesterday

    # The member reset at midnight: a new cycle starts at zero, no negative step.
    coordinator.data = {"nodes": {BARCODE: {"daily_energy_wh": 20.0}}}
    sensor._handle_coordinator_update()
    assert sensor.native_value == 0.0
    assert sensor.extra_restore_state_data.as_dict()["cycle_date"] == today.isoformat()
    assert sensor.last_reset == datetime.combine(today, dt_time.min, tzinfo=dt_util.UTC)

    coordinator.data = {"nodes": {BARCODE: {"daily_energy_wh": 30.0}}}
    sensor._handle_coordinator_update()
    assert sensor.native_value == 10.0
    assert date.fromisoformat(sensor._cycle_date) == today


ENTRY_ID = "aggregate-restore"


def _installation_uid(key: str) -> str:
    return f"{DOMAIN}_{ENTRY_ID}_installation_{key}"


async def _setup_with_restore(hass: HomeAssistant, restored: dict[str, tuple[str, dict]]):
    """Set up with saved member totals and pre-seeded aggregate restore data."""
    entry = _entry(hass, [MODULE], entry_id=ENTRY_ID)
    registry = er.async_get(hass)
    cache = []
    for key, (state, extra) in restored.items():
        registry.async_get_or_create(
            "sensor", DOMAIN, _installation_uid(key), suggested_object_id=f"inst_{key}", config_entry=entry
        )
        cache.append((State(f"sensor.inst_{key}", state), extra))
    mock_restore_cache_with_extra_data(hass, cache)
    today = dt_util.now().date().isoformat()
    stored = {
        "energy_data": {
            BARCODE: {"total_energy_wh": 1010.0, "daily_energy_wh": 15.0, "daily_reset_date": today}
        },
        "node_snapshots": {BARCODE: {"total_energy_wh": 1010.0, "daily_energy_wh": 15.0}},
    }
    with patch(STORE_LOAD, new=AsyncMock(return_value=stored)), patch(LISTEN, return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, entry.runtime_data


def _sensor_entity(hass: HomeAssistant, key: str) -> PyTapAggregateSensor:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, _installation_uid(key))
    return hass.data["sensor"].get_entity(entity_id)


async def test_aggregate_restore_round_trip_with_baselines(hass: HomeAssistant) -> None:
    today = dt_util.now().date().isoformat()
    entry, coordinator = await _setup_with_restore(
        hass,
        {
            "total_energy": (
                "3000.0",
                {
                    "native_value": 3000.0,
                    "native_unit_of_measurement": "Wh",
                    "baselines": {BARCODE: 1000.0},
                    "cycle_date": None,
                },
            ),
            "daily_energy": (
                "40.0",
                {
                    "native_value": 40.0,
                    "native_unit_of_measurement": "Wh",
                    "baselines": {BARCODE: 10.0},
                    "cycle_date": today,
                },
            ),
        },
    )
    total = _sensor_entity(hass, "total_energy")
    daily = _sensor_entity(hass, "daily_energy")
    # Own totals continue, plus what members produced since they were last seen.
    assert float(hass.states.get(total.entity_id).state) == 3010.0
    assert float(hass.states.get(daily.entity_id).state) == 45.0
    saved = total.extra_restore_state_data.as_dict()
    assert saved["native_value"] == 3010.0
    assert saved["baselines"] == {BARCODE: 1010.0}
    assert daily.extra_restore_state_data.as_dict()["cycle_date"] == today
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_aggregate_restore_upgrades_plain_sum(hass: HomeAssistant) -> None:
    """Earlier versions stored only a sum: continue from it, rebase every member."""
    entry, coordinator = await _setup_with_restore(
        hass,
        {
            "total_energy": ("2500.0", {"native_value": 2500.0, "native_unit_of_measurement": "Wh"}),
            "daily_energy": ("99.0", {"native_value": 99.0, "native_unit_of_measurement": "Wh"}),
        },
    )
    total = _sensor_entity(hass, "total_energy")
    daily = _sensor_entity(hass, "daily_energy")
    assert float(hass.states.get(total.entity_id).state) == 2500.0
    assert total.extra_restore_state_data.as_dict()["baselines"] == {BARCODE: 1010.0}
    # A daily plain sum can't be trusted (unknown cycle): start from the members.
    assert float(hass.states.get(daily.entity_id).state) == 15.0

    await _feed(hass, coordinator, _power())
    with coordinator._state_lock:
        node = coordinator._state["nodes"][BARCODE]
        coordinator._state["nodes"][BARCODE] = {**node, "total_energy_wh": 1020.0}
        coordinator._mark_changed(BARCODE)
    coordinator._ha_update_pending = True
    coordinator._push_live_update(MagicMock(counters={}))
    await hass.async_block_till_done()
    assert float(hass.states.get(total.entity_id).state) == 2510.0
    assert await hass.config_entries.async_unload(entry.entry_id)
