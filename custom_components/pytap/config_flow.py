"""Config flow for PyTap integration.

Implements a menu-driven config flow where users add Tigo optimizer modules
one at a time via individual form fields (string group, name, barcode),
rather than a comma-separated text blob.

Flow: user (host/port) → modules_menu → add_module (repeat) → finish
Options: init (menu) → add_module / remove_module / change_connection /
         change_reporting / change_precision / change_last_update → done
"""

from __future__ import annotations

import logging
import re
from typing import Any
import uuid

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .const import (
    CONF_LAST_UPDATE_MODE,
    CONF_LAST_UPDATE_QUANTIZATION,
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_STATE_PRECISION,
    CONF_WRITE_INTERVAL,
    DEFAULT_LAST_UPDATE_MODE,
    DEFAULT_LAST_UPDATE_QUANTIZATION,
    DEFAULT_LIVE_REPORTING,
    DEFAULT_PEAK_POWER,
    DEFAULT_PORT,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    LAST_UPDATE_MODE_OFF,
    LAST_UPDATE_MODE_QUANTIZED,
    LAST_UPDATE_MODE_RAW,
    MAX_LAST_UPDATE_QUANTIZATION,
    MAX_WRITE_INTERVAL,
    MIN_LAST_UPDATE_QUANTIZATION,
    MIN_WRITE_INTERVAL,
    PRECISION_GROUP_CURRENT,
    PRECISION_GROUP_ENERGY,
    PRECISION_GROUP_PERCENT,
    PRECISION_GROUP_POWER,
    PRECISION_GROUP_TEMPERATURE,
    PRECISION_GROUP_VOLTAGE,
    STATE_PRECISION_OFF,
)
from .last_update import normalize_quantization
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

_LOGGER = logging.getLogger(__name__)

# Barcode format: X-NNNNNNN[C] where X is hex digit, N is hex, C is alpha
_BARCODE_PATTERN = re.compile(r"^[0-9A-Fa-f]-[0-9A-Fa-f]{1,7}[A-Za-z]$")

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)

ADD_MODULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODULE_STRING): str,
        vol.Required(CONF_MODULE_NAME): str,
        vol.Required(CONF_MODULE_BARCODE): str,
        vol.Optional(CONF_MODULE_PEAK_POWER, default=DEFAULT_PEAK_POWER): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
    }
)


def validate_barcode(barcode: str) -> None:
    """Validate a single barcode format."""
    if barcode and not _BARCODE_PATTERN.match(barcode):
        raise InvalidBarcodeFormat(
            f"Invalid barcode format: '{barcode}'. "
            "Expected format like A-1234567B (X-NNNNNNNC)."
        )


async def validate_connection(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, Any]:
    """Validate connection to the Tigo gateway."""
    host = data[CONF_HOST]
    port = data.get(CONF_PORT, DEFAULT_PORT)

    def _test_connection() -> None:
        from .pytap.core.source import TcpSource

        source = TcpSource(host, port)
        try:
            source.connect()
        finally:
            source.close()

    await hass.async_add_executor_job(_test_connection)
    return {"title": f"PyTap ({host})"}


def _modules_description(modules: list[dict[str, Any]]) -> str:
    """Build a human-readable summary of currently added modules."""
    if not modules:
        return "No modules added yet."
    lines = []
    for i, m in enumerate(modules, 1):
        parts = []
        if m.get(CONF_MODULE_STRING):
            parts.append(f"string={m[CONF_MODULE_STRING]}")
        parts.append(m[CONF_MODULE_NAME])
        parts.append(m.get(CONF_MODULE_BARCODE, ""))
        peak_power = m.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
        lines.append(f"  {i}. {' / '.join(parts)} ({peak_power}Wp)")
    return f"**Modules ({len(modules)}):**\n" + "\n".join(lines)


class PyTapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PyTap."""

    VERSION = 5

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_data: dict[str, Any] = {}
        self._modules: list[dict[str, str]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PyTapOptionsFlow:
        """Get the options flow for this handler."""
        return PyTapOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle step 1: connection parameters (host/port)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._user_data = user_input
            # Stable unique ID — not tied to mutable connection settings
            await self.async_set_unique_id(uuid.uuid4().hex)

            # Optional connection test — warn but don't block
            try:
                await validate_connection(self.hass, user_input)
            except Exception:
                _LOGGER.warning(
                    "Could not connect to %s:%s — proceeding anyway",
                    user_input[CONF_HOST],
                    user_input.get(CONF_PORT, DEFAULT_PORT),
                )

            return await self.async_step_modules_menu()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_modules_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the modules menu: add another or finish."""
        return self.async_show_menu(
            step_id="modules_menu",
            menu_options=["add_module", "finish"],
            description_placeholders={
                "modules_list": _modules_description(self._modules),
                "error": "",
            },
        )

    async def async_step_add_module(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle adding a single module (string, name, barcode)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            barcode = user_input.get(CONF_MODULE_BARCODE, "").strip().upper()
            name = user_input.get(CONF_MODULE_NAME, "").strip()
            string_group = user_input.get(CONF_MODULE_STRING, "").strip()

            if not string_group:
                errors[CONF_MODULE_STRING] = "missing_string"
            elif not name:
                errors[CONF_MODULE_NAME] = "missing_name"
            elif not barcode:
                errors[CONF_MODULE_BARCODE] = "missing_barcode"
            else:
                try:
                    validate_barcode(barcode)
                except InvalidBarcodeFormat:
                    errors[CONF_MODULE_BARCODE] = "invalid_barcode"

            # Check for duplicate barcode
            if not errors and any(
                m[CONF_MODULE_BARCODE] == barcode for m in self._modules
            ):
                errors[CONF_MODULE_BARCODE] = "duplicate_barcode"

            if not errors:
                self._modules.append(
                    {
                        CONF_MODULE_STRING: string_group,
                        CONF_MODULE_NAME: name,
                        CONF_MODULE_BARCODE: barcode,
                        CONF_MODULE_PEAK_POWER: user_input.get(
                            CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER
                        ),
                    }
                )
                return await self.async_step_modules_menu()

        return self.async_show_form(
            step_id="add_module",
            data_schema=ADD_MODULE_SCHEMA,
            errors=errors,
            description_placeholders={
                "modules_list": _modules_description(self._modules),
            },
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the config flow — create the entry."""
        if not self._modules:
            return await self.async_step_modules_menu()
        data = {**self._user_data, CONF_MODULES: self._modules}
        title = f"PyTap ({self._user_data[CONF_HOST]})"
        return self.async_create_entry(title=title, data=data)


def _precision_options(
    example: float, unit: str, decimals: list[int]
) -> list[SelectOptionDict]:
    """Build dropdown options showing the example value after rounding.

    E.g. for voltage 48.7654 V: "0.01 → 48.77 V". The -1 step follows
    Python round() semantics (round to tens).
    """
    options: list[SelectOptionDict] = [
        SelectOptionDict(
            value=STATE_PRECISION_OFF,
            label=f"off — raw value ({example} {unit})",
        )
    ]
    for dec in decimals:
        rounded = round(example, dec)
        if dec <= 0:
            shown = f"{int(rounded)}"
            step = {0: "1", -1: "10"}[dec]
        else:
            shown = f"{rounded:.{dec}f}"
            step = "0." + "0" * (dec - 1) + "1"
        options.append(
            SelectOptionDict(value=str(dec), label=f"{step} → {shown} {unit}")
        )
    return options


# Per-group dropdown options. Example values are chosen so every offered
# rounding step produces a visibly different result.
_PRECISION_STEPS = [-1, 0, 1, 2, 3]
_PRECISION_SELECTORS: dict[str, list[SelectOptionDict]] = {
    PRECISION_GROUP_VOLTAGE: _precision_options(48.7654, "V", _PRECISION_STEPS),
    PRECISION_GROUP_CURRENT: _precision_options(8.5472, "A", _PRECISION_STEPS),
    PRECISION_GROUP_POWER: _precision_options(1234.5678, "W", _PRECISION_STEPS),
    PRECISION_GROUP_TEMPERATURE: _precision_options(41.8712, "°C", _PRECISION_STEPS),
    PRECISION_GROUP_PERCENT: _precision_options(64.5345, "%", _PRECISION_STEPS),
    PRECISION_GROUP_ENERGY: _precision_options(4567.8912, "Wh", _PRECISION_STEPS),
}


class PyTapOptionsFlow(OptionsFlow):
    """Handle PyTap options — add/remove modules after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._modules: list[dict[str, str]] = list(
            config_entry.data.get(CONF_MODULES, [])
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu: change connection / add / remove / done."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "change_connection",
                "change_reporting",
                "change_precision",
                "change_last_update",
                "add_module",
                "remove_module",
                "done",
            ],
            description_placeholders={
                "modules_list": _modules_description(self._modules),
            },
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save modules and close the options flow."""
        new_data = {**self._config_entry.data, CONF_MODULES: self._modules}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(title="", data={})

    async def async_step_change_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the user to change the Tigo gateway IP address and port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_host = user_input[CONF_HOST].strip()
            new_port = user_input.get(CONF_PORT, DEFAULT_PORT)

            if not new_host:
                errors[CONF_HOST] = "cannot_connect"
            else:
                # Warn-only connection test (same as initial setup)
                try:
                    await validate_connection(
                        self.hass, {CONF_HOST: new_host, CONF_PORT: new_port}
                    )
                except Exception:
                    _LOGGER.warning(
                        "Could not connect to %s:%s — saving anyway",
                        new_host,
                        new_port,
                    )

                # Update entry data with new connection details
                new_data = {**self._config_entry.data}
                new_data[CONF_HOST] = new_host
                new_data[CONF_PORT] = new_port
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data=new_data,
                    title=f"PyTap ({new_host})",
                )
                return await self.async_step_init()

        # Pre-fill with current values
        current_host = self._config_entry.data.get(CONF_HOST, "")
        current_port = self._config_entry.data.get(CONF_PORT, DEFAULT_PORT)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current_host): str,
                vol.Optional(CONF_PORT, default=current_port): int,
            }
        )

        return self.async_show_form(
            step_id="change_connection",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_change_reporting(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Toggle live reporting.

        Live reporting is presented on its own step with just the checkbox.
        Home Assistant's static forms cannot grey out a field reactively, so
        the reporting interval is collected on a separate ``set_interval``
        step that is only reached when live reporting is turned off. When it
        is left on, the interval is irrelevant and never shown.
        """
        if user_input is not None:
            live = user_input.get(CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING)
            if live:
                # Live mode — clear any stored interval and finish.
                new_data = {**self._config_entry.data}
                new_data[CONF_LIVE_REPORTING] = True
                new_data.pop(CONF_WRITE_INTERVAL, None)
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return await self.async_step_init()
            # Interval mode — collect the interval on the next step.
            return await self.async_step_set_interval()

        current_live = self._config_entry.data.get(
            CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_LIVE_REPORTING, default=current_live): bool,
            }
        )

        return self.async_show_form(
            step_id="change_reporting",
            data_schema=schema,
        )

    async def async_step_set_interval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the reporting interval (only reached when live reporting is off)."""
        if user_input is not None:
            new_data = {**self._config_entry.data}
            new_data[CONF_LIVE_REPORTING] = False
            new_data[CONF_WRITE_INTERVAL] = user_input.get(
                CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL
            )
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return await self.async_step_init()

        current_write_interval = self._config_entry.data.get(
            CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WRITE_INTERVAL, default=current_write_interval
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_WRITE_INTERVAL, max=MAX_WRITE_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="set_interval",
            data_schema=schema,
        )

    async def async_step_change_precision(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure optional per-quantity rounding of float sensor states.

        One dropdown per physical quantity (voltage, current, power,
        temperature, percent, energy). ``off`` (default) keeps raw values —
        identical to versions before this option existed. The setting applies
        to per-module and aggregate sensors alike; aggregates always sum raw
        coordinator data, so per-module rounding never propagates into
        aggregate values. Display precision in the UI is a separate
        per-entity setting and is not changed here.
        """
        if user_input is not None:
            precision: dict[str, int] = {}
            for group in _PRECISION_SELECTORS:
                selection = user_input.get(group, STATE_PRECISION_OFF)
                if selection != STATE_PRECISION_OFF:
                    precision[group] = int(selection)
            new_data = {**self._config_entry.data}
            if precision:
                new_data[CONF_STATE_PRECISION] = precision
            else:
                new_data.pop(CONF_STATE_PRECISION, None)
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return await self.async_step_init()

        current = self._config_entry.data.get(CONF_STATE_PRECISION)
        if not isinstance(current, dict):
            current = {}

        schema_dict: dict[Any, Any] = {}
        for group, options in _PRECISION_SELECTORS.items():
            default = (
                str(int(current[group])) if group in current else STATE_PRECISION_OFF
            )
            schema_dict[vol.Optional(group, default=default)] = SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="change_precision",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_change_last_update(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the per-module ``last_update`` attribute (v2.2).

        HA records a sensor whenever anything about it changes, including
        attributes. The full-resolution per-reading timestamp therefore
        forces a recorder/InfluxDB write for every reading even when the
        (rounded) state is unchanged — on larger installations this
        attribute, not the measurement, drives write volume. Modes:

        * ``raw`` (default) — full-resolution timestamp, previous behaviour.
        * ``quantized`` — floored timestamp written once per interval and
          omitted in between (near-``off`` storage; the timestamp still
          advances once per interval). Live per-module outage automations
          that read the attribute stop firing between intervals — detection
          reads the last logged ``last_update`` (e.g. from InfluxDB) instead.
        * ``off`` — attribute not exposed. HA's built-in ``last_updated``
          advances with every coordinator update even for silent modules,
          so per-module outage detection needs ``raw`` or ``quantized``.

        In averaged reporting mode the quantization interval is clamped to
        at least the reporting interval and normalized to a multiple of it —
        the attribute cannot change more often than readings are delivered.
        """
        data = self._config_entry.data
        write_interval = int(data.get(CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL))
        live_reporting = bool(
            data.get(CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING)
        )

        if user_input is not None:
            mode = user_input.get(CONF_LAST_UPDATE_MODE, DEFAULT_LAST_UPDATE_MODE)
            new_data = {**data}
            if mode == LAST_UPDATE_MODE_RAW:
                new_data.pop(CONF_LAST_UPDATE_MODE, None)
                new_data.pop(CONF_LAST_UPDATE_QUANTIZATION, None)
            elif mode == LAST_UPDATE_MODE_OFF:
                new_data[CONF_LAST_UPDATE_MODE] = mode
                new_data.pop(CONF_LAST_UPDATE_QUANTIZATION, None)
            else:
                seconds = int(
                    user_input.get(
                        CONF_LAST_UPDATE_QUANTIZATION,
                        DEFAULT_LAST_UPDATE_QUANTIZATION,
                    )
                )
                seconds = normalize_quantization(
                    seconds, write_interval, live_reporting
                )
                new_data[CONF_LAST_UPDATE_MODE] = mode
                new_data[CONF_LAST_UPDATE_QUANTIZATION] = seconds
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return await self.async_step_init()

        current_mode = data.get(CONF_LAST_UPDATE_MODE, DEFAULT_LAST_UPDATE_MODE)
        current_quant = int(
            data.get(
                CONF_LAST_UPDATE_QUANTIZATION, DEFAULT_LAST_UPDATE_QUANTIZATION
            )
        )
        mode_options = [
            SelectOptionDict(
                value=LAST_UPDATE_MODE_RAW,
                label="raw — full timestamp, changes on every reading (default)",
            ),
            SelectOptionDict(
                value=LAST_UPDATE_MODE_QUANTIZED,
                label="quantized — written once per interval, absent in between",
            ),
            SelectOptionDict(
                value=LAST_UPDATE_MODE_OFF,
                label="off — attribute not exposed",
            ),
        ]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LAST_UPDATE_MODE, default=current_mode
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=mode_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_LAST_UPDATE_QUANTIZATION, default=current_quant
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_LAST_UPDATE_QUANTIZATION,
                        max=MAX_LAST_UPDATE_QUANTIZATION,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="change_last_update",
            data_schema=schema,
            description_placeholders={
                "reporting_hint": (
                    "live reporting is enabled — any value from "
                    f"{MIN_LAST_UPDATE_QUANTIZATION} s applies as-is"
                    if live_reporting
                    else (
                        "averaged mode with a "
                        f"{write_interval} s reporting interval — the value "
                        "is normalized to a multiple of it (at least 2×)"
                    )
                ),
            },
        )

    async def async_step_add_module(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a single module in options flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            barcode = user_input.get(CONF_MODULE_BARCODE, "").strip().upper()
            name = user_input.get(CONF_MODULE_NAME, "").strip()
            string_group = user_input.get(CONF_MODULE_STRING, "").strip()

            if not string_group:
                errors[CONF_MODULE_STRING] = "missing_string"
            elif not name:
                errors[CONF_MODULE_NAME] = "missing_name"
            elif not barcode:
                errors[CONF_MODULE_BARCODE] = "missing_barcode"
            else:
                try:
                    validate_barcode(barcode)
                except InvalidBarcodeFormat:
                    errors[CONF_MODULE_BARCODE] = "invalid_barcode"

            if not errors and any(
                m[CONF_MODULE_BARCODE] == barcode for m in self._modules
            ):
                errors[CONF_MODULE_BARCODE] = "duplicate_barcode"

            if not errors:
                self._modules.append(
                    {
                        CONF_MODULE_STRING: string_group,
                        CONF_MODULE_NAME: name,
                        CONF_MODULE_BARCODE: barcode,
                        CONF_MODULE_PEAK_POWER: user_input.get(
                            CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER
                        ),
                    }
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_module",
            data_schema=ADD_MODULE_SCHEMA,
            errors=errors,
            description_placeholders={
                "modules_list": _modules_description(self._modules),
            },
        )

    async def async_step_remove_module(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a module by selecting from a list."""
        if not self._modules:
            return await self.async_step_init()

        if user_input is not None:
            remove_barcode = user_input.get("remove_barcode")
            if remove_barcode:
                self._modules = [
                    m for m in self._modules if m[CONF_MODULE_BARCODE] != remove_barcode
                ]
            return await self.async_step_init()

        # Build selection list from current modules
        barcode_options = {
            m[CONF_MODULE_BARCODE]: f"{m[CONF_MODULE_NAME]} ({m[CONF_MODULE_BARCODE]})"
            for m in self._modules
            if m.get(CONF_MODULE_BARCODE)
        }

        schema = vol.Schema(
            {
                vol.Required("remove_barcode"): vol.In(barcode_options),
            }
        )

        return self.async_show_form(
            step_id="remove_module",
            data_schema=schema,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidModuleFormat(HomeAssistantError):
    """Error to indicate invalid module format."""


class InvalidBarcodeFormat(HomeAssistantError):
    """Error to indicate invalid barcode format."""
