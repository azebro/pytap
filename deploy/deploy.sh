#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/.deploy.env"

# Load config from .deploy.env if it exists
if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi

# Allow environment variables or flags to override
HA_HOST="${HA_HOST:-}"
HA_SSH_USER="${HA_SSH_USER:-}"
HA_SSH_PASSWORD="${HA_SSH_PASSWORD:-}"
HA_SSH_PORT="${HA_SSH_PORT:-22}"
HA_CONFIG_PATH="${HA_CONFIG_PATH:-/homeassistant}"
RESTART="${RESTART:-true}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Deploy pytap custom component to a Home Assistant instance via SSH.

Options:
  -h, --host HOST         HA host/IP address
  -u, --user USER         SSH username
  -p, --password PASS     SSH password (prefer config file or env var)
  -P, --port PORT         SSH port (default: 22)
  -c, --config-path PATH  HA config path (default: /homeassistant)
  --no-restart             Skip HA restart after deploy
  --help                   Show this help message

Environment variables:
  HA_HOST, HA_SSH_USER, HA_SSH_PASSWORD, HA_SSH_PORT, HA_CONFIG_PATH, RESTART

Config file:
  Place a .deploy.env file in the deploy/ directory with KEY=VALUE pairs.
  See .deploy.env.example for reference.

EOF
    exit 0
}

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--host)      HA_HOST="$2"; shift 2 ;;
        -u|--user)      HA_SSH_USER="$2"; shift 2 ;;
        -p|--password)  HA_SSH_PASSWORD="$2"; shift 2 ;;
        -P|--port)      HA_SSH_PORT="$2"; shift 2 ;;
        -c|--config-path) HA_CONFIG_PATH="$2"; shift 2 ;;
        --no-restart)   RESTART="false"; shift ;;
        --help)         usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# Validate required parameters
missing=()
[[ -z "$HA_HOST" ]] && missing+=("HA_HOST (--host)")
[[ -z "$HA_SSH_USER" ]] && missing+=("HA_SSH_USER (--user)")
[[ -z "$HA_SSH_PASSWORD" ]] && missing+=("HA_SSH_PASSWORD (--password)")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Error: Missing required parameters:"
    for m in "${missing[@]}"; do
        echo "  - $m"
    done
    echo ""
    echo "Provide via flags, environment variables, or ${CONFIG_FILE}"
    exit 1
fi

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p ${HA_SSH_PORT}"
SSH_CMD="sshpass -p '${HA_SSH_PASSWORD}' ssh ${SSH_OPTS} ${HA_SSH_USER}@${HA_HOST}"
COMPONENT_SRC="${PROJECT_ROOT}/custom_components/pytap"
COMPONENT_DEST="${HA_CONFIG_PATH}/custom_components"

# Check sshpass is available
if ! command -v sshpass &>/dev/null; then
    echo "Error: sshpass is required but not installed."
    echo "  Install with: sudo apt-get install sshpass  (Debian/Ubuntu)"
    echo "                brew install hudochenkov/sshpass/sshpass  (macOS)"
    exit 1
fi

# Verify source exists
if [[ ! -d "$COMPONENT_SRC" ]]; then
    echo "Error: Component source not found at ${COMPONENT_SRC}"
    exit 1
fi

echo "==> Deploying pytap to ${HA_SSH_USER}@${HA_HOST}:${COMPONENT_DEST}/pytap"

# Test connectivity
echo "    Testing SSH connection..."
if ! eval "$SSH_CMD 'echo ok'" &>/dev/null; then
    echo "Error: Cannot connect to ${HA_HOST}:${HA_SSH_PORT} as ${HA_SSH_USER}"
    exit 1
fi

# Deploy via tar-over-SSH (works with HA OS SSH add-on which lacks SCP/SFTP)
echo "    Uploading component..."
tar czf - -C "${PROJECT_ROOT}/custom_components" pytap \
    | eval "$SSH_CMD 'cd ${COMPONENT_DEST} && sudo rm -rf pytap && sudo tar xzf -'"

echo "    Upload complete."

# Verify deployment
echo "    Verifying..."
FILE_COUNT=$(eval "$SSH_CMD 'find ${COMPONENT_DEST}/pytap -type f | wc -l'" 2>/dev/null)
echo "    Deployed ${FILE_COUNT} files."

# Restart HA
if [[ "$RESTART" == "true" ]]; then
    echo "==> Restarting Home Assistant Core..."
    eval "$SSH_CMD 'ha core restart --api-token \$(sudo cat /run/s6/container_environment/SUPERVISOR_TOKEN)'" &>/dev/null || true

    echo "    Waiting for HA to come back up..."
    for i in $(seq 1 12); do
        sleep 10
        HTTP_CODE=$(curl -m 5 -s -o /dev/null -w '%{http_code}' "http://${HA_HOST}:8123" 2>/dev/null || echo "000")
        if [[ "$HTTP_CODE" == "200" ]]; then
            echo "    HA is up (attempt ${i})."
            break
        fi
        if [[ "$i" == "12" ]]; then
            echo "    Warning: HA did not respond after 120s. Check manually."
        fi
    done
else
    echo "==> Skipping restart (--no-restart). Restart HA manually to load changes."
fi

echo "==> Done."
