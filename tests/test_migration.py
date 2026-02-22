"""Tests for PyTap entity migration (v1 → v2).

Validates that legacy ``voltage`` and ``current`` entity unique IDs from
pre-v0.2.0 are removed from the entity registry when the integration loads.
"""

import pytest

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap import (
    _async_cleanup_legacy_entities,
    async_migrate_entry,
    CONFIG_ENTRY_VERSION,
)
from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_STRING_NAME,
    DEFAULT_PORT,
    DOMAIN,
)


MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
    },
    {
        CONF_MODULE_STRING: "B",
        CONF_MODULE_NAME: "Panel_02",
        CONF_MODULE_BARCODE: "C-2345678D",
    },
]


def _make_entry(
    hass: HomeAssistant,
    version: int = 1,
    modules: list[dict[str, str]] | None = None,
) -> MockConfigEntry:
    """Create and register a MockConfigEntry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: DEFAULT_PORT,
            CONF_MODULES: modules if modules is not None else MOCK_MODULES,
        },
        version=version,
        entry_id="test_entry_migration",
    )
    entry.add_to_hass(hass)
    return entry


class TestLegacyEntityCleanup:
    """Test _async_cleanup_legacy_entities removes old unique IDs."""

    async def test_removes_old_voltage_and_current_entities(
        self, hass: HomeAssistant
    ) -> None:
        """Legacy 'voltage' and 'current' entities should be removed."""
        entry = _make_entry(hass)
        ent_reg = er.async_get(hass)

        # Simulate legacy entities created by v0.1.0
        for barcode in ("A-1234567B", "C-2345678D"):
            for old_key in ("voltage", "current"):
                ent_reg.async_get_or_create(
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_{barcode}_{old_key}",
                    config_entry=entry,
                )

        # Verify they exist before cleanup
        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_voltage"
            )
            is not None
        )
        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_C-2345678D_current"
            )
            is not None
        )

        await _async_cleanup_legacy_entities(hass, entry)

        # All legacy entities should be gone
        for barcode in ("A-1234567B", "C-2345678D"):
            for old_key in ("voltage", "current"):
                assert (
                    ent_reg.async_get_entity_id(
                        "sensor", DOMAIN, f"{DOMAIN}_{barcode}_{old_key}"
                    )
                    is None
                )

    async def test_does_not_touch_new_entities(self, hass: HomeAssistant) -> None:
        """New voltage_in/voltage_out/current_in/current_out should not be removed."""
        entry = _make_entry(hass)
        ent_reg = er.async_get(hass)

        # Create new-style entities
        new_keys = ("voltage_in", "voltage_out", "current_in", "current_out", "power")
        for key in new_keys:
            ent_reg.async_get_or_create(
                domain="sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_A-1234567B_{key}",
                config_entry=entry,
            )

        await _async_cleanup_legacy_entities(hass, entry)

        # All new entities should still exist
        for key in new_keys:
            assert (
                ent_reg.async_get_entity_id(
                    "sensor", DOMAIN, f"{DOMAIN}_A-1234567B_{key}"
                )
                is not None
            )

    async def test_no_op_when_no_legacy_entities(self, hass: HomeAssistant) -> None:
        """Should not error when no legacy entities exist."""
        entry = _make_entry(hass)
        # Should complete without error
        await _async_cleanup_legacy_entities(hass, entry)


class TestConfigEntryMigration:
    """Test async_migrate_entry version and data migrations."""

    async def test_migrates_v1_to_v2(self, hass: HomeAssistant) -> None:
        """Config entry version should be bumped from 1 to current version."""
        entry = _make_entry(hass, version=1)

        result = await async_migrate_entry(hass, entry)

        assert result is True
        assert entry.version == CONFIG_ENTRY_VERSION

    async def test_already_current_version(self, hass: HomeAssistant) -> None:
        """No migration needed if already at current version."""
        entry = _make_entry(hass, version=CONFIG_ENTRY_VERSION)
        original_version = entry.version

        result = await async_migrate_entry(hass, entry)

        assert result is True
        assert entry.version == original_version

    async def test_migrate_v2_to_v3_empty_strings(self, hass: HomeAssistant) -> None:
        """Modules with missing/empty string should get default label."""
        entry = _make_entry(
            hass,
            version=2,
            modules=[
                {
                    CONF_MODULE_STRING: "",
                    CONF_MODULE_NAME: "Panel_01",
                    CONF_MODULE_BARCODE: "A-1234567B",
                },
                {
                    CONF_MODULE_NAME: "Panel_02",
                    CONF_MODULE_BARCODE: "C-2345678D",
                },
            ],
        )

        result = await async_migrate_entry(hass, entry)

        assert result is True
        assert entry.version == CONFIG_ENTRY_VERSION
        assert entry.data[CONF_MODULES][0][CONF_MODULE_STRING] == DEFAULT_STRING_NAME
        assert entry.data[CONF_MODULES][1][CONF_MODULE_STRING] == DEFAULT_STRING_NAME

    async def test_migrate_v2_to_v3_existing_strings(self, hass: HomeAssistant) -> None:
        """Existing string labels should be preserved."""
        entry = _make_entry(hass, version=2)
        original_modules = list(entry.data[CONF_MODULES])

        result = await async_migrate_entry(hass, entry)

        assert result is True
        assert entry.version == CONFIG_ENTRY_VERSION
        assert entry.data[CONF_MODULES] == original_modules

    async def test_migrate_v2_to_v3_mixed(self, hass: HomeAssistant) -> None:
        """Only missing string labels should be defaulted in mixed lists."""
        entry = _make_entry(
            hass,
            version=2,
            modules=[
                {
                    CONF_MODULE_STRING: "A",
                    CONF_MODULE_NAME: "Panel_01",
                    CONF_MODULE_BARCODE: "A-1234567B",
                },
                {
                    CONF_MODULE_STRING: "",
                    CONF_MODULE_NAME: "Panel_02",
                    CONF_MODULE_BARCODE: "C-2345678D",
                },
            ],
        )

        result = await async_migrate_entry(hass, entry)

        assert result is True
        assert entry.version == CONFIG_ENTRY_VERSION
        assert entry.data[CONF_MODULES][0][CONF_MODULE_STRING] == "A"
        assert entry.data[CONF_MODULES][1][CONF_MODULE_STRING] == DEFAULT_STRING_NAME
