"""Tests for the PyTap config flow (menu-driven module list UX)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
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
            CONF_MODULE_STRING: "",
            CONF_MODULE_NAME: "",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_module"
    assert result["errors"] == {CONF_MODULE_NAME: "missing_name"}


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


async def test_already_configured(hass: HomeAssistant) -> None:
    """Test that the same host:port cannot be added twice."""
    # Complete the full flow to create the first entry
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
    with patch(
        "custom_components.pytap.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_MODULE_STRING: "A",
                CONF_MODULE_NAME: "Panel_01",
                CONF_MODULE_BARCODE: "A-1234567B",
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Try adding the same device again
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.pytap.config_flow.validate_connection",
        return_value={"title": f"PyTap ({MOCK_HOST})"},
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {"host": MOCK_HOST, "port": MOCK_PORT},
        )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"
