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
DEFAULT_SCAN_INTERVAL = 30
RECONNECT_TIMEOUT = 60
RECONNECT_DELAY = 5
RECONNECT_RETRIES = 0
UNAVAILABLE_TIMEOUT = 120
