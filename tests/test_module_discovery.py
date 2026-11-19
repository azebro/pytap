"""Passive module admission, persistence, live entities, and metadata edits."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap import async_migrate_entry
from custom_components.pytap.config_flow import PyTapOptionsFlow
from custom_components.pytap.const import CONF_AUTO_DISCOVERY, CONF_EXCLUDED_BARCODES, DOMAIN
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.events import InfrastructureEvent, PowerReportEvent

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}


def _entry(hass, *, automatic=True, modules=None, excluded=None, version=7):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        unique_id="stable-entry",
        data={
            "host": "gateway",
            "port": 502,
            "connection_type": "tcp",
            "modules": modules or [],
            CONF_AUTO_DISCOVERY: automatic,
            CONF_EXCLUDED_BARCODES: excluded or [],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, **kwargs):
    entry = _entry(hass, **kwargs)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._schedule_save = MagicMock()
    entry.runtime_data = coordinator
    entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    return entry, coordinator


def _infra(*barcodes):
    return InfrastructureEvent(
        gateways={1: {"address": "gateway"}},
        nodes={i: {"barcode": barcode} for i, barcode in enumerate(barcodes, 1)},
        timestamp=dt_util.now(),
    )


def _power(barcode=BARCODE, power=120):
    return PowerReportEvent(
        gateway_id=1,
        node_id=1 if barcode == BARCODE else 2,
        barcode=barcode,
        voltage_in=30,
        voltage_out=30,
        current_in=power / 30,
        temperature=25,
        dc_dc_duty_cycle=0.5,
        rssi=-60,
        timestamp=dt_util.now(),
    )


async def _new_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "tcp"})
    with patch("custom_components.pytap.config_flow.validate_connection", return_value={}):
        return await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "gateway"})


async def test_finish_empty_auto_setup_creates_entry_immediately(hass):
    with (
        patch("custom_components.pytap.async_setup_entry", return_value=True),
        patch("custom_components.pytap.async_unload_entry", return_value=True),
    ):
        result = await _new_flow(hass)
        assert "discovery" in result["menu_options"]
        assert result["description_placeholders"]["discovery_mode"] == "Automatic"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["modules"] == []
        assert result["data"][CONF_AUTO_DISCOVERY] is True


async def test_finish_empty_manual_setup_shows_visible_error(hass):
    result = await _new_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discovery"})
    assert next(iter(result["data_schema"].schema)).default() is True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTO_DISCOVERY: False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {"base": "no_modules"}


def test_infrastructure_admits_all_modules_once_without_entry_writes(hass):
    entry, coordinator = _coordinator(hass)
    original = dict(entry.data)
    coordinator._process_event(_infra(BARCODE, OTHER))
    coordinator._process_event(_infra(BARCODE, OTHER))
    assert coordinator.module_count == 2
    assert {m["barcode"] for m in coordinator.modules} == {BARCODE, OTHER}
    assert entry.data == original  # No update-listener reload per discovery.
    assert coordinator.modules[0]["name"] == f"Optimizer {BARCODE}"
    assert coordinator.modules[0]["string"] == "Discovered"
    assert coordinator.modules[0]["peak_power"] == 0
    assert coordinator._state["nodes"][BARCODE]["node_id"] == 1


def test_first_power_reading_is_not_dropped_and_rating_is_unknown(hass):
    _, coordinator = _coordinator(hass)
    assert coordinator._process_event(_power())
    assert coordinator.module_count == 1
    node = coordinator._state["nodes"][BARCODE]
    assert node["power"] == 120
    assert node["readings_today"] == 1
    assert node["performance"] is None
    assert node["peak_power"] == 0


def test_unknown_rating_is_safe_in_averaged_mode(hass):
    entry, coordinator = _coordinator(hass)
    coordinator._live_reporting = False
    coordinator._process_event(_power())
    snapshot = coordinator._build_averaged_snapshot(1_000_000)
    assert snapshot["nodes"][BARCODE]["power"] == 120
    assert snapshot["nodes"][BARCODE]["performance"] is None


def test_manual_mode_only_records_unconfigured_barcodes(hass):
    _, coordinator = _coordinator(hass, automatic=False)
    coordinator._process_event(_infra(BARCODE))
    coordinator._process_event(_power())
    assert coordinator.modules == []
    assert coordinator._state["nodes"] == {}
    assert BARCODE in coordinator._state["discovered_barcodes"]


async def test_auto_modules_and_energy_restore_when_discovery_disabled(hass):
    entry, coordinator = _coordinator(hass)
    coordinator._process_event(_power())
    coordinator._energy_state[BARCODE].total_energy_wh = 1234
    coordinator._store.async_save = AsyncMock()
    await coordinator._async_save_coordinator_state()
    saved = coordinator._store.async_save.call_args.args[0]
    assert saved["auto_modules"][0]["barcode"] == BARCODE
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AUTO_DISCOVERY: False})
    restored = PyTapDataUpdateCoordinator(hass, entry)
    restored._schedule_save = MagicMock()
    restored._store.async_load = AsyncMock(return_value=saved)
    await restored._async_load_coordinator_state()
    assert restored.module_count == 1
    assert restored._state["nodes"][BARCODE]["total_energy_wh"] == 1234
    assert restored._state["nodes"][BARCODE]["power"] == 120
    restored._process_event(_infra(BARCODE, OTHER))
    assert restored.module_count == 1


async def test_enabling_discovery_admits_previously_logged_modules(hass):
    _, coordinator = _coordinator(hass)
    coordinator._store.async_load = AsyncMock(
        return_value={
            "discovered_barcodes": [BARCODE],
            "barcode_to_node": {BARCODE: 5},
        }
    )
    await coordinator._async_load_coordinator_state()
    assert coordinator.module_count == 1
    assert coordinator._state["nodes"][BARCODE]["node_id"] == 5


async def test_options_edit_metadata_merges_late_discoveries(hass):
    entry, coordinator = _coordinator(hass)
    coordinator._process_event(_infra(BARCODE))
    flow = PyTapOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_edit_module({"barcode": BARCODE})
    result = await flow.async_step_module_details({"name": "East Panel", "string": "East", "peak_power": 500})
    assert result["step_id"] == "init"
    coordinator._process_event(_infra(BARCODE, OTHER))
    await flow.async_step_done()
    assert {m["barcode"] for m in entry.data["modules"]} == {BARCODE, OTHER}
    edited = next(m for m in entry.data["modules"] if m["barcode"] == BARCODE)
    assert edited == MODULE
    assert entry.unique_id == "stable-entry"
    restored = PyTapDataUpdateCoordinator(hass, entry)
    restored._schedule_save = MagicMock()
    restored._store.async_load = AsyncMock(return_value={"auto_modules": coordinator.modules})
    await restored._async_load_coordinator_state()
    assert next(m for m in restored.modules if m["barcode"] == BARCODE) == MODULE
    restored._process_event(_power())
    assert restored._state["nodes"][BARCODE]["performance"] == 24


async def test_removed_module_is_excluded_until_explicitly_added(hass):
    entry, coordinator = _coordinator(hass)
    coordinator._process_event(_infra(BARCODE))
    flow = PyTapOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_remove_module({"remove_barcode": BARCODE})
    await flow.async_step_done()
    assert entry.data["modules"] == []
    assert entry.data[CONF_EXCLUDED_BARCODES] == [BARCODE]
    restored = PyTapDataUpdateCoordinator(hass, entry)
    restored._schedule_save = MagicMock()
    restored._store.async_load = AsyncMock(return_value={"auto_modules": coordinator.modules})
    await restored._async_load_coordinator_state()
    restored._process_event(_power())
    assert restored.module_count == 0
    assert BARCODE not in restored._state["nodes"]
    entry.runtime_data = restored
    flow = PyTapOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_add_module(MODULE)
    await flow.async_step_done()
    assert entry.data[CONF_EXCLUDED_BARCODES] == []
    assert entry.data["modules"] == [MODULE]


async def test_remove_a_late_discovery_does_not_merge_it_back(hass):
    entry, coordinator = _coordinator(hass)
    flow = PyTapOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_init()
    coordinator._process_event(_power())
    # The user explicitly selects and then removes a barcode which wasn't in
    # the options flow's starting snapshot.
    await flow.async_step_add_module(MODULE)
    await flow.async_step_remove_module({"remove_barcode": BARCODE})
    await flow.async_step_done()
    assert entry.data["modules"] == []
    assert entry.data[CONF_EXCLUDED_BARCODES] == [BARCODE]


async def test_cleared_rating_cannot_restore_an_old_percentage(hass):
    entry = _entry(hass, modules=[{**MODULE, "peak_power": 0}])

    def no_network(self):
        pass

    old_percentage = MagicMock(native_value=75.0)
    with (
        patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network),
        patch(
            "custom_components.pytap.sensor.RestoreSensor.async_get_last_sensor_data",
            return_value=old_percentage,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        for unique_id in (
            f"{DOMAIN}_{BARCODE}_performance",
            f"{DOMAIN}_{entry.entry_id}_installation_performance",
        ):
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            # No rating: the entity is disabled, so no old percentage can appear.
            assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(entity_id) is None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_manual_empty_requires_module_or_automatic_mode(hass):
    entry, _ = _coordinator(hass, automatic=False)
    flow = PyTapOptionsFlow(entry)
    flow.hass = hass
    await flow.async_step_init()
    result = await flow.async_step_done()
    assert result["errors"] == {"base": "no_modules"}
    await flow.async_step_change_discovery({CONF_AUTO_DISCOVERY: True})
    await flow.async_step_done()
    assert entry.data[CONF_AUTO_DISCOVERY] is True


async def test_v6_migration_keeps_existing_entry_manual_and_identity(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, version=6, data={"host": "gateway", "modules": [MODULE]}, unique_id="old"
    )
    entry.add_to_hass(hass)
    original_id = entry.entry_id
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 7
    assert entry.data[CONF_AUTO_DISCOVERY] is False
    assert entry.data["modules"] == [MODULE]
    assert entry.entry_id == original_id and entry.unique_id == "old"


async def test_live_discovery_creates_entities_and_updates_aggregates(hass: HomeAssistant):
    entry = _entry(hass)

    def no_network(self):
        pass

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        registry = er.async_get(hass)
        status_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_module_discovery"
        )
        assert hass.states.get(status_id).state == "waiting_for_modules"
        for barcode, power in [(BARCODE, 120), (OTHER, 150)]:
            await hass.async_add_executor_job(coordinator._process_event, _power(barcode, power))
            coordinator.async_set_updated_data(coordinator._snapshot())
            await hass.async_block_till_done()
        assert hass.states.get(status_id).state == "modules_discovered"
        assert hass.states.get(status_id).attributes["module_count"] == 2
        for barcode in (BARCODE, OTHER):
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{barcode}_power")
            assert hass.states.get(entity_id).state in ("120.0", "150.0")
        aggregate_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_power"
        )
        assert float(hass.states.get(aggregate_id).state) == 270
        performance_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_performance"
        )
        assert registry.async_get(performance_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        count = len(er.async_entries_for_config_entry(registry, entry.entry_id))
        assert count == 41  # 2 × (13 sensors + 3 binary) + 4 string + 4 installation + 1 status.
        coordinator._process_event(_infra(BARCODE, OTHER))
        coordinator.async_set_updated_data(coordinator._snapshot())
        await hass.async_block_till_done()
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == count
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_rating_without_power_blocks_aggregate_percentage(hass):
    entry = _entry(hass, modules=[MODULE])

    def no_network(self):
        pass

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator._process_event(_power())
        coordinator._process_event(_infra(BARCODE, OTHER))  # OTHER has no power or rating yet.
        coordinator.async_set_updated_data(coordinator._snapshot())
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        aggregate_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_installation_performance"
        )
        # OTHER joined unrated: the aggregate percentage is disabled, not guessed.
        assert registry.async_get(aggregate_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_performance_reenabled_once_rated_but_user_choice_kept(hass):
    entry = _entry(hass, modules=[{**MODULE, "peak_power": 0}])

    def no_network(self):
        pass

    registry = er.async_get(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        panel = registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{BARCODE}_performance")
        string = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_string_East_performance"
        )
        assert registry.async_get(panel).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        registry.async_update_entity(string, disabled_by=er.RegistryEntryDisabler.USER)
        assert await hass.config_entries.async_unload(entry.entry_id)

        hass.config_entries.async_update_entry(entry, data={**entry.data, "modules": [MODULE]})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(panel).disabled_by is None
        assert registry.async_get(string).disabled_by is er.RegistryEntryDisabler.USER
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_discovery_policy_options_rebuild_coordinator(hass):
    entry = _entry(hass, automatic=False, modules=[MODULE])

    def no_network(self):
        pass

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old = entry.runtime_data
        old._process_event(_infra(BARCODE, OTHER))
        flow = PyTapOptionsFlow(entry)
        flow.hass = hass
        await flow.async_step_init()
        await flow.async_step_change_discovery({CONF_AUTO_DISCOVERY: True})
        await flow.async_step_done()
        await hass.async_block_till_done()
        new = entry.runtime_data
        assert new is not old
        assert new.auto_discovery is True
        assert {m["barcode"] for m in new.modules} == {BARCODE, OTHER}
        # Removing a discovered module also applies its policy on reconstruction.
        flow = PyTapOptionsFlow(entry)
        flow.hass = hass
        await flow.async_step_init()
        await flow.async_step_remove_module({"remove_barcode": OTHER})
        await flow.async_step_done()
        await hass.async_block_till_done()
        newest = entry.runtime_data
        assert newest is not new
        newest._process_event(_infra(BARCODE, OTHER))
        assert {m["barcode"] for m in newest.modules} == {BARCODE}
        assert await hass.config_entries.async_unload(entry.entry_id)
