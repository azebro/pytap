"""Tests for the PyTap config flow (menu-driven module list UX)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData, FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PEAK_POWER,
    DEFAULT_PORT,
    DOMAIN,
)


MOCK_HOST = "192.168.1.100"
MOCK_PORT = 502


async def test_step_user_shows_form(hass: HomeAssistant) -> None:
    """Test the initial user step shows the connection form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_proceeds_to_menu(hass: HomeAssistant) -> None:
    """Test that submitting host/port goes to the modules menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "modules_menu"


async def test_user_step_proceeds_even_without_connection(
    hass: HomeAssistant,
) -> None:
    """Test that a failed connection test still proceeds to modules menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        side_effect=Exception("Connection refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    # Should still proceed to modules menu (connection test is non-blocking)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "modules_menu"


async def test_full_flow_add_one_module(hass: HomeAssistant) -> None:
    """Test complete flow: user → menu → add_module → finish."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    assert result["type"] is FlowResultType.MENU

    # Choose "Add module"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "add_module"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"

    # Fill in the module
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )
    # Should return to the menu
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "modules_menu"

    # Choose "Finish"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "finish"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"PyTap ({MOCK_HOST})"
    assert result["data"]["host"] == MOCK_HOST
    assert result["data"]["port"] == MOCK_PORT
    assert len(result["data"][CONF_MODULES]) == 1
    assert result["data"][CONF_MODULES][0] == {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: DEFAULT_PEAK_POWER,
    }


async def test_full_flow_add_two_modules(hass: HomeAssistant) -> None:
    """Test adding multiple modules via the menu loop."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    # Add first module
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )
    assert result["type"] is FlowResultType.MENU

    # Add second module
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "B",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
        },
    )
    assert result["type"] is FlowResultType.MENU

    # Finish
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_MODULES]) == 2


async def test_add_module_invalid_barcode(hass: HomeAssistant) -> None:
    """Test that an invalid barcode shows an error on the add_module step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    assert result["step_id"] == "add_module"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "INVALID",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {CONF_MODULE_BARCODE: "invalid_barcode"}


async def test_add_module_missing_name(hass: HomeAssistant) -> None:
    """Test that a missing name shows an error on the add_module step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {CONF_MODULE_NAME: "missing_name"}


async def test_add_module_missing_string(hass: HomeAssistant) -> None:
    """Test that a missing string shows an error on the add_module step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {CONF_MODULE_STRING: "missing_string"}


async def test_add_module_duplicate_barcode(hass: HomeAssistant) -> None:
    """Test that adding a duplicate barcode shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    # Add first module
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )

    # Try adding same barcode again
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "B",
            CONF_MODULE_NAME: "Panel_Copy",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {CONF_MODULE_BARCODE: "duplicate_barcode"}


# ──────────────────────────────────────────────────────────
# Options flow: change connection settings
# ──────────────────────────────────────────────────────────


def _make_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a PyTap config entry for options-flow testing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title=f"PyTap ({MOCK_HOST})",
        data={
            "host": MOCK_HOST,
            "port": MOCK_PORT,
            CONF_MODULES: [
                {
                    CONF_MODULE_STRING: "A",
                    CONF_MODULE_NAME: "Panel_01",
                    CONF_MODULE_BARCODE: "A-1234567B",
                },
            ],
        },
        unique_id="pytap_test_entry",
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_flow_shows_change_connection(hass: HomeAssistant) -> None:
    """Test the options menu includes the change_connection option."""
    entry = _make_config_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert "change_connection" in result["menu_options"]


async def test_options_change_connection_shows_prefilled_form(
    hass: HomeAssistant,
) -> None:
    """Test that selecting change_connection shows a form with current values."""
    entry = _make_config_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "change_connection"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "change_connection"
    # Schema defaults should carry the current values
    schema = result["data_schema"]
    schema_dict = {str(k): k for k in schema.schema}
    host_key = schema_dict["host"]
    port_key = schema_dict["port"]
    assert host_key.default() == MOCK_HOST
    assert port_key.default() == MOCK_PORT


async def test_options_change_connection_updates_entry(
    hass: HomeAssistant,
) -> None:
    """Test that submitting new host/port updates entry data and title."""
    entry = _make_config_entry(hass)
    new_host = "10.0.0.50"
    new_port = 8502
    original_unique_id = entry.unique_id

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "change_connection"},
    )
    assert result["step_id"] == "change_connection"

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({new_host})"},
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"host": new_host, "port": new_port},
        )

    # Should return to the init menu
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    # Verify the config entry was updated
    assert entry.data["host"] == new_host
    assert entry.data["port"] == new_port
    assert entry.unique_id == original_unique_id  # unique_id must NOT change
    assert entry.title == f"PyTap ({new_host})"


async def test_options_change_connection_warn_only_on_failure(
    hass: HomeAssistant,
) -> None:
    """Test that a failed connection test still saves the new values."""
    entry = _make_config_entry(hass)
    new_host = "10.0.0.99"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "change_connection"},
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        side_effect=Exception("Connection refused"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"host": new_host, "port": MOCK_PORT},
        )

    # Should still return to menu despite connection failure
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    # Values should still be saved
    assert entry.data["host"] == new_host


async def test_options_change_connection_preserves_modules(
    hass: HomeAssistant,
) -> None:
    """Test that changing connection does not alter the module list."""
    entry = _make_config_entry(hass)
    original_modules = list(entry.data[CONF_MODULES])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "change_connection"},
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": "PyTap (10.0.0.1)"},
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"host": "10.0.0.1", "port": 503},
        )

    assert entry.data[CONF_MODULES] == original_modules


async def test_add_module_with_custom_peak_power(hass: HomeAssistant) -> None:
    """Custom peak power should be persisted in module config."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )
    assert result["step_id"] == "add_module"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
            CONF_MODULE_PEAK_POWER: 400,
        },
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODULES][0][CONF_MODULE_PEAK_POWER] == 400


async def test_add_module_peak_power_validation(hass: HomeAssistant) -> None:
    """Peak power must satisfy schema range constraints."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_module"}
    )

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_MODULE_STRING: "A",
                CONF_MODULE_NAME: "Panel_01",
                CONF_MODULE_BARCODE: "A-1234567B",
                CONF_MODULE_PEAK_POWER: 0,
            },
        )
