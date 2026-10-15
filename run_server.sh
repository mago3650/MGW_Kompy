#!/usr/bin/env bash
# ==============================================================================
# Helper Script to Launch the Deployment & Key Harvester Server
# Part of MGW_Kompy
#
# Subnet pools and the "default" (next) IP no longer need to be edited here:
# open http://localhost:8000 and use the "Network Settings" panel. Everything is
# persisted to server/network_config.json and reloaded on the next start.
#
# The settings below only SEED the first run (or a run with NO_DEPLOY_CONFIG=1).
# Once network_config.json exists, the web dashboard is the source of truth.
#
# Environment variables:
#   DEPLOY_PORT, DEPLOY_SERVER_IP (address clients use to reach this server),
#   DEPLOY_SUBNET, DEPLOY_START_IP, DEPLOY_MAX_IP, DEPLOY_GATEWAY,
#   DEPLOY_CIDR, DEPLOY_DNS, DEPLOY_PREFIX, DEPLOY_TARGET (fleet size),
#   DEPLOY_CONFIG (path of network_config.json), DEPLOY_READ_ONLY=1 (lock editing)
#
# Additional seed pools via EXTRA_POOLS (also editable later in the web UI):
#   "name=<label>,subnet=<cidr or a.b.c>,start=<n>,max=<m>,gw=<gateway>,dns=<a;b>,prefix=<pfx>"
#   (use ';' between DNS servers, ',' separates the fields)
# ==============================================================================

set -e

# Default pool settings (customize via environment variables or edit below)
SERVER_PORT="${DEPLOY_PORT:-8000}"
SERVER_IP="${DEPLOY_SERVER_IP:-}"
SUBNET="${DEPLOY_SUBNET:-192.168.1}"
START_IP="${DEPLOY_START_IP:-101}"
MAX_IP="${DEPLOY_MAX_IP:-250}"
GATEWAY="${DEPLOY_GATEWAY:-192.168.1.1}"
CIDR="${DEPLOY_CIDR:-24}"
DNS="${DEPLOY_DNS:-1.1.1.1;8.8.8.8}"
PREFIX="${DEPLOY_PREFIX:-nuc-}"
TARGET="${DEPLOY_TARGET:-150}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${DEPLOY_CONFIG:-$SCRIPT_DIR/server/network_config.json}"

# ---------------------------------------------------------------------------
# Seed pools (only applied when $CONFIG_FILE does not exist yet)
# ---------------------------------------------------------------------------
EXTRA_POOLS=(
    # "name=Lab-A,subnet=192.168.100.0/24,start=1,max=250,gw=192.168.100.1,dns=1.1.1.1,prefix=lab-a-"
    # "name=Lab-B,subnet=10.10.35.0/24,start=1,max=250,gw=10.10.35.1,dns=1.1.1.1;8.8.8.8,prefix=lab-b-"
    # "name=Office,subnet=10.30.0.0/16,start=1,max=60000,gw=10.30.1.1,dns=8.8.8.8,prefix=off-"
)

# Build CLI args for extra pools
EXTRA_POOL_ARGS=()
for pool_cfg in "${EXTRA_POOLS[@]}"; do
    [ -z "$pool_cfg" ] && continue
    EXTRA_POOL_ARGS+=(--subnet-pool "$pool_cfg")
done

CLI_ARGS=(
    --port "$SERVER_PORT"
    --subnet "$SUBNET"
    --start-ip "$START_IP"
    --max-ip "$MAX_IP"
    --gateway "$GATEWAY"
    --cidr "$CIDR"
    --dns "$DNS"
    --prefix "$PREFIX"
    --config "$CONFIG_FILE"
    --output "$SCRIPT_DIR/server/inventory.csv"
    --pool "$SCRIPT_DIR/server/ip_pool.csv"
    "${EXTRA_POOL_ARGS[@]}"
)
[ -n "$SERVER_IP" ] && CLI_ARGS+=(--server-ip "$SERVER_IP")
[ -n "${NO_DEPLOY_CONFIG:-}" ] && CLI_ARGS+=(--no-config)
[ -n "${DEPLOY_READ_ONLY:-}" ] && CLI_ARGS+=(--read-only)
[ -n "$TARGET" ] && CLI_ARGS+=(--target "$TARGET")

echo "Starting MGW_Kompy Deployment Server on port $SERVER_PORT..."
if [ -f "$CONFIG_FILE" ] && [ -z "${NO_DEPLOY_CONFIG:-}" ]; then
    echo "  Pools come from $CONFIG_FILE - edit them in the web dashboard."
    echo "  (delete that file, or run with NO_DEPLOY_CONFIG=1, to re-seed from this script)"
else
    echo "  Seeding pools from run_server.sh defaults; they will be saved to $CONFIG_FILE."
fi

exec python3 "$SCRIPT_DIR/server/inventory_server.py" "${CLI_ARGS[@]}"
