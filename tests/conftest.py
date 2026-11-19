"""Fixtures for PyTap tests."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    yield


@pytest.fixture(autouse=True)
def mock_usb_hardware():
    """Keep native serial discovery/websocket setup, without host USB access."""
    with (
        patch(
            "homeassistant.components.usb.USBDiscovery._async_start_aiousbwatcher",
            new_callable=AsyncMock,
        ),
        patch("homeassistant.components.usb.scan_serial_ports", return_value=[]),
    ):
        yield
