"""Constants for the PyTap integration."""

DOMAIN = "pytap"
DEFAULT_NAME = "PyTap"

# Configuration keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MODULES = "modules"
CONF_LIVE_REPORTING = "live_reporting"
CONF_WRITE_INTERVAL = "write_interval"
CONF_STATE_PRECISION = "state_precision"

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
DEFAULT_LIVE_REPORTING = True
DEFAULT_WRITE_INTERVAL = 30
MIN_WRITE_INTERVAL = 5
MAX_WRITE_INTERVAL = 300
RECONNECT_TIMEOUT = 60
RECONNECT_DELAY = 5
RECONNECT_RETRIES = 0

# State precision (optional rounding of float sensor states).
# Stored as a dict in entry data: {group: decimals}. A missing group (or a
# missing dict entirely) means off = raw values (pre-feature behaviour).
# Negative decimals follow Python round() semantics: -1 = tens.
STATE_PRECISION_OFF = "off"
PRECISION_GROUP_VOLTAGE = "voltage"
PRECISION_GROUP_CURRENT = "current"
PRECISION_GROUP_POWER = "power"
PRECISION_GROUP_TEMPERATURE = "temperature"
PRECISION_GROUP_PERCENT = "percent"
PRECISION_GROUP_ENERGY = "energy"
PRECISION_GROUPS = (
    PRECISION_GROUP_VOLTAGE,
    PRECISION_GROUP_CURRENT,
    PRECISION_GROUP_POWER,
    PRECISION_GROUP_TEMPERATURE,
    PRECISION_GROUP_PERCENT,
    PRECISION_GROUP_ENERGY,
)
MIN_STATE_PRECISION = -1
MAX_STATE_PRECISION = 3

# Energy accumulation tuning
ENERGY_GAP_THRESHOLD_SECONDS = 120
ENERGY_LOW_POWER_THRESHOLD_W = 1.0

# Last-update attribute handling (v2.2, write-trigger control).
# HA records a sensor whenever anything about it changes, including
# attributes. A full-resolution per-reading timestamp therefore forces a
# recorder/InfluxDB write for every reading even when the (rounded) state is
# unchanged. Modes: raw = previous behaviour (default), quantized = timestamp
# floored to a configurable interval, off = attribute not exposed.
CONF_LAST_UPDATE_MODE = "last_update_mode"
CONF_LAST_UPDATE_QUANTIZATION = "last_update_quantization"
LAST_UPDATE_MODE_RAW = "raw"
LAST_UPDATE_MODE_QUANTIZED = "quantized"
LAST_UPDATE_MODE_OFF = "off"
LAST_UPDATE_MODES = (
    LAST_UPDATE_MODE_RAW,
    LAST_UPDATE_MODE_QUANTIZED,
    LAST_UPDATE_MODE_OFF,
)
DEFAULT_LAST_UPDATE_MODE = LAST_UPDATE_MODE_RAW
DEFAULT_LAST_UPDATE_QUANTIZATION = 60
MIN_LAST_UPDATE_QUANTIZATION = 5
MAX_LAST_UPDATE_QUANTIZATION = 300
