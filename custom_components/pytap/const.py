"""Constants for the PyTap integration."""

DOMAIN = "pytap"
DEFAULT_NAME = "PyTap"

# Configuration keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MODULES = "modules"

# Module dict keys
CONF_MODULE_STRING = "string"
CONF_MODULE_NAME = "name"
CONF_MODULE_BARCODE = "barcode"
CONF_MODULE_PEAK_POWER = "peak_power"

# Defaults
DEFAULT_PORT = 502
DEFAULT_STRING_NAME = "Default"
DEFAULT_PEAK_POWER = 455
DEFAULT_SCAN_INTERVAL = 30
RECONNECT_TIMEOUT = 60
RECONNECT_DELAY = 5
RECONNECT_RETRIES = 0

# Energy accumulation tuning
ENERGY_GAP_THRESHOLD_SECONDS = 120
ENERGY_LOW_POWER_THRESHOLD_W = 1.0

# Stale-data handling
# When optimizers go to sleep (e.g. at night) they stop sending power reports.
# After this many seconds without an update, the instantaneous production
# readings for a node are zeroed so sensors no longer show frozen daytime
# values. Cumulative meters (daily/total energy) are left untouched.
STALE_DATA_TIMEOUT_SECONDS = 120
# How often the coordinator checks node freshness.
STALE_CHECK_INTERVAL_SECONDS = 30
# Instantaneous production fields zeroed once a node is considered stale.
STALE_ZERO_KEYS = (
    "power",
    "performance",
    "voltage_in",
    "voltage_out",
    "current_in",
    "current_out",
    "dc_dc_duty_cycle",
)
