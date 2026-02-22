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

# Defaults
DEFAULT_PORT = 502
DEFAULT_STRING_NAME = "Default"
DEFAULT_SCAN_INTERVAL = 30
RECONNECT_TIMEOUT = 60
RECONNECT_DELAY = 5
RECONNECT_RETRIES = 0

# Energy accumulation tuning
ENERGY_GAP_THRESHOLD_SECONDS = 120
ENERGY_LOW_POWER_THRESHOLD_W = 1.0
