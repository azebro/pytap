"""Transport settings and a passive serial adapter for the parser worker."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
import logging
import os
import posixpath
import re
import stat
from typing import Any, Protocol, TypeVar
from urllib.parse import parse_qs, parse_qsl, urlsplit

import serialx

from .const import (
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_HOST,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    CONNECTION_TIMEOUT,
    DEFAULT_BAUD_RATE,
    DEFAULT_PORT,
    SERIAL_READ_TIMEOUT,
)
from .pytap.core.source import TcpSource

CONNECTION_KEYS = {CONF_CONNECTION_TYPE, CONF_HOST, CONF_PORT, CONF_SERIAL_PORT, CONF_BAUD_RATE}
_PROXY_FILTERS = {
    "port_name",
    "port_manufacturer",
    "port_product",
    "port_serial_number",
    "port_usb_vid",
    "port_usb_pid",
    "port_usb_bcd_device",
    "port_usb_interface_num",
    "port_udev_id",
}
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)
# Entries under /dev that alias process file descriptors or memory, not ports.
_NON_PORT_DEVICES = ("/dev/fd", "/dev/shm", "/dev/mqueue")


class InvalidConnectionSettings(ValueError):
    """Invalid settings, with a field and translation key safe for the UI."""

    def __init__(self, field: str, error: str) -> None:
        self.field = field
        self.error = error
        super().__init__(error)


class ByteSource(Protocol):
    """Read-only interface consumed by the parser thread."""

    def read(self, size: int) -> bytes:
        """Read up to size bytes."""

    def close(self) -> None:
        """Release the connection."""


def normalize_connection(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate connection data without opening a port or consulting discovery."""
    connection_type = data.get(CONF_CONNECTION_TYPE, CONNECTION_TCP)
    if connection_type == CONNECTION_TCP:
        host = data.get(CONF_HOST, "")
        if not isinstance(host, str) or not host.strip():
            raise InvalidConnectionSettings(CONF_HOST, "invalid_host")
        port = data.get(CONF_PORT, DEFAULT_PORT)
        if type(port) is not int or not 1 <= port <= 65535:
            raise InvalidConnectionSettings(CONF_PORT, "invalid_port")
        return {
            CONF_CONNECTION_TYPE: CONNECTION_TCP,
            CONF_HOST: host.strip(),
            CONF_PORT: port,
        }
    if connection_type != CONNECTION_SERIAL:
        raise InvalidConnectionSettings("base", "invalid_connection_type")

    endpoint = data.get(CONF_SERIAL_PORT, "")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise InvalidConnectionSettings(CONF_SERIAL_PORT, "invalid_serial_port")
    endpoint = endpoint.strip()
    if re.search(r"[\x00-\x1f\x7f]", endpoint):
        raise InvalidConnectionSettings(CONF_SERIAL_PORT, "invalid_serial_port")
    # Local paths and HA-managed proxy URLs only. In particular, do not accept
    # credentials or protocol-mode overrides in manually entered proxy URLs.
    if "://" in endpoint:
        try:
            parsed = urlsplit(endpoint)
            filters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            valid = (
                parsed.scheme == "esphome-hass"
                and endpoint.startswith("esphome-hass://")
                and parsed.netloc == "esphome"
                and bool(parsed.path.strip("/"))
                and "/" not in parsed.path.strip("/")
                and not parsed.fragment
                and bool(filters)
                and filters.keys() <= _PROXY_FILTERS
                and all(len(values) == 1 and values[0] for values in filters.values())
                and not re.search(r"%(?![0-9a-fA-F]{2})", endpoint)
            )
            for key, values in filters.items():
                if key.startswith("port_usb_"):
                    valid = valid and int(values[0], 0) >= 0
        except ValueError:
            valid = False
        if not valid:
            raise InvalidConnectionSettings(CONF_SERIAL_PORT, "invalid_serial_port")
    else:
        # Only device nodes: never open arbitrary files (e.g. config) or
        # special devices outside /dev as a "serial port". Normalise first so
        # "/dev/../config/secrets.yaml" can't pass as a device path.
        endpoint = posixpath.normpath(endpoint)
        if not is_device_path(endpoint):
            raise InvalidConnectionSettings(CONF_SERIAL_PORT, "invalid_serial_port")
    baud_rate = data.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE)
    if type(baud_rate) is not int or not 1 <= baud_rate <= 4_000_000:
        raise InvalidConnectionSettings(CONF_BAUD_RATE, "invalid_baud_rate")
    return {
        CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
        CONF_SERIAL_PORT: endpoint,
        CONF_BAUD_RATE: baud_rate,
    }


def is_device_path(path: str) -> bool:
    """Whether a normalised absolute path names a device node that could be a port."""
    if not path.startswith("/dev/") or path.startswith("/dev/std"):
        return False
    return not any(path == prefix or path.startswith(f"{prefix}/") for prefix in _NON_PORT_DEVICES)


def check_device_node(endpoint: str) -> None:
    """Reject paths whose symlink target isn't a character device under /dev (blocking)."""
    if "://" in endpoint:
        return
    target = os.path.realpath(endpoint)
    if not is_device_path(target) or not stat.S_ISCHR(os.stat(target).st_mode):
        raise InvalidConnectionSettings(CONF_SERIAL_PORT, "invalid_serial_port")


def connection_title(data: Mapping[str, Any]) -> str:
    """Return a title without exposing proxy URLs or hardware identity filters."""
    if data.get(CONF_CONNECTION_TYPE, CONNECTION_TCP) == CONNECTION_TCP:
        return f"PyTap ({data[CONF_HOST]})"
    if data[CONF_SERIAL_PORT].startswith("esphome-hass://"):
        return "PyTap (Home Assistant serial proxy)"
    return "PyTap (Serial)"


def serial_endpoint_key(endpoint: str) -> str | tuple[str, tuple[tuple[str, str], ...]]:
    """Compare equivalent manual URLs without changing the saved endpoint."""
    if endpoint.startswith("esphome-hass://"):
        parsed = urlsplit(endpoint)
        return parsed.path, tuple(sorted(parse_qsl(parsed.query)))
    return posixpath.normpath(endpoint)


def resolved_endpoint_key(endpoint: str) -> str | tuple[str, tuple[tuple[str, str], ...]]:
    """Like serial_endpoint_key, but resolves symlinks (blocking: run in an executor)."""
    key = serial_endpoint_key(endpoint)
    return os.path.realpath(key) if isinstance(key, str) else key


class SerialProxySource:
    """Bridge serialx's async transport to the existing blocking parser worker.

    All serial operations run on HA's loop; only the parser thread blocks.
    The public async transport releases a proxy subscription without closing
    HA's shared ESPHome API client. The worker owns open/read/close, avoiding a
    concurrent close racing a read or stopping a serialx-owned event loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, data: Mapping[str, Any]) -> None:
        self._loop = loop
        self._serial = serialx.async_serial_for_url(
            data[CONF_SERIAL_PORT],
            baudrate=data[CONF_BAUD_RATE],
            byte_size=8,
            parity=serialx.Parity.NONE,
            stopbits=serialx.StopBits.ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            # Do not toggle a transceiver's direction or reset pins.
            dtr_on_open=serialx.PinState.UNDEFINED,
            rts_on_open=serialx.PinState.UNDEFINED,
            dtr_on_close=serialx.PinState.UNDEFINED,
            rts_on_close=serialx.PinState.UNDEFINED,
            connect_timeout=CONNECTION_TIMEOUT,
            close_timeout=SERIAL_READ_TIMEOUT,
        )

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Submit a bounded coroutine from the worker, never from HA's loop."""
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            coro.close()
            raise RuntimeError("Serial source must run in an executor")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            # Every coroutine is itself bounded; this only guards against a
            # stopped event loop leaving the worker waiting forever.
            return future.result(timeout=CONNECTION_TIMEOUT + 5)
        except TimeoutError:
            future.cancel()
            raise

    async def _async_connect(self) -> None:
        async with asyncio.timeout(CONNECTION_TIMEOUT):
            await self._serial.open()

    def connect(self) -> None:
        """Open the selected endpoint within the connection timeout."""
        self._run(self._async_connect())

    async def _async_read(self, size: int) -> bytes:
        idle_timeout = asyncio.timeout(SERIAL_READ_TIMEOUT)
        try:
            async with idle_timeout:
                data = await self._serial.read(size)
        except TimeoutError:
            if idle_timeout.expired():
                return b""
            # A transport-reported timeout is a connection failure, not idle.
            raise
        if not data:
            raise ConnectionResetError("Serial connection closed")
        return data

    def read(self, size: int = 4096) -> bytes:
        """Read bytes; idle timeout and EOF have distinct meanings."""
        return self._run(self._async_read(size))

    async def _async_close(self) -> None:
        try:
            async with asyncio.timeout(CONNECTION_TIMEOUT):
                await self._serial.close()
        except BaseException:
            self._serial.abort()
            raise

    def close(self) -> None:
        """Release the port; serialx's close is idempotent."""
        self._run(self._async_close())


def open_source(loop: asyncio.AbstractEventLoop, data: Mapping[str, Any]) -> ByteSource:
    """Open a source in the executor, closing failed/partially opened sources."""
    settings = normalize_connection(data)
    if settings[CONF_CONNECTION_TYPE] == CONNECTION_SERIAL:
        check_device_node(settings[CONF_SERIAL_PORT])
        source = SerialProxySource(loop, settings)
    else:
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
