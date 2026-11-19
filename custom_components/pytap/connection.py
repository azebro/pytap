"""Connection settings validation and opening the gateway byte stream."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT
from .pytap.core.source import TcpSource

_LOGGER = logging.getLogger(__name__)


class InvalidConnectionSettings(ValueError):
    """Invalid settings, with a field and translation key safe for the UI."""

    def __init__(self, field: str, error: str) -> None:
        self.field = field
        self.error = error
        super().__init__(error)


def normalize_connection(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate connection data without opening a connection."""
    host = data.get(CONF_HOST, "")
    if not isinstance(host, str) or not host.strip():
        raise InvalidConnectionSettings(CONF_HOST, "invalid_host")
    port = data.get(CONF_PORT, DEFAULT_PORT)
    if type(port) is not int or not 1 <= port <= 65535:
        raise InvalidConnectionSettings(CONF_PORT, "invalid_port")
    return {CONF_HOST: host.strip(), CONF_PORT: port}


def open_source(data: Mapping[str, Any]) -> TcpSource:
    """Open the gateway connection (blocking), closing it again if connecting fails."""
    settings = normalize_connection(data)
    source = TcpSource(settings[CONF_HOST], settings[CONF_PORT])
    try:
        source.connect()
    except BaseException:
        try:
            source.close()
        except Exception as err:
            _LOGGER.warning("Failed-open source cleanup error (%s)", type(err).__name__)
        raise
    return source
