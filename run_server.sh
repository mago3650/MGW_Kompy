#!/usr/bin/env bash
# ==============================================================================
# Helper Script to Launch the Deployment & Key Harvester Server
# Part of MGW_Kompy
#
# Supports multiple subnet pools for flexible IP assignment across different
# network environments (e.g. 192.168.100.x, 10.10.35.x, 10.30.x.x, etc.)
#
# Default pool settings via environment variables:
#   DEPLOY_SUBNET, DEPLOY_START_IP, DEPLOY_MAX_IP, DEPLOY_GATEWAY,
#   DEPLOY_NETMASK, DEPLOY_CIDR, DEPLOY_DNS, DEPLOY_PREFIX
#
# Additional pools via EXTRA_POOLS array (see examples below):
#   Each entry follows the format:
#     "name=<label>,subnet=<x.y.z>,start=<n>,max=<m>,gw=<gateway>,mask=<netmask>,cidr=<n>,dns=<servers>,prefix=<hostprefix>"
# ==============================================================================

set -e

# Default pool settings (customize via environment variables or edit below)
SERVER_PORT="${DEPLOY_PORT:-8000}"
SUBNET="${DEPLOY_SUBNET:-192.168.1}"
START_IP="${DEPLOY_START_IP:-101}"
MAX_IP="${DEPLOY_MAX_IP:-250}"
GATEWAY="${DEPLOY_GATEWAY:-192.168.1.1}"
NETMASK="${DEPLOY_NETMASK:-255.255.255.0}"
CIDR="${DEPLOY_CIDR:-24}"
DNS="${DEPLOY_DNS:-1.1.1.1,8.8.8.8}"
PREFIX="${DEPLOY_PREFIX:-nuc-}"

# ---------------------------------------------------------------------------
# Additional subnet pools (uncomment/edit as needed)
# Format: "name=<label>,subnet=<x.y.z>,start=<n>,max=<m>,gw=<gw>,mask=<mask>,cidr=<n>,dns=<dns>,prefix=<pfx>"
# ---------------------------------------------------------------------------
EXTRA_POOLS=(
    # "name=Lab-A,subnet=192.168.100,start=1,max=250,gw=192.168.100.1,mask=255.255.255.0,cidr=24,dns=1.1.1.1,prefix=lab-a-"
    # "name=Lab-B,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,mask=255.255.255.0,cidr=24,dns=1.1.1.1,prefix=lab-b-"
    # "name=Office,subnet=10.30.1,start=1,max=250,gw=10.30.1.1,mask=255.255.255.0,cidr=24,dns=8.8.8.8,prefix=off-"
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Build CLI args for extra pools
EXTRA_POOL_ARGS=()
for pool_cfg in "${EXTRA_POOLS[@]}"; do
    EXTRA_POOL_ARGS+=(--subnet-pool "$pool_cfg")
done

echo "Starting MGW_Kompy Deployment Server on port $SERVER_PORT..."
echo "  Default pool: ${SUBNET}.${START_IP}-${MAX_IP} (GW: ${GATEWAY})"
if [ ${#EXTRA_POOLS[@]} -gt 0 ]; then
    echo "  Extra pools: ${#EXTRA_POOLS[@]} configured"
fi

python3 "$SCRIPT_DIR/server/inventory_server.py" \
    --port "$SERVER_PORT" \
    --subnet "$SUBNET" \
    --start-ip "$START_IP" \
    --max-ip "$MAX_IP" \
    --gateway "$GATEWAY" \
    --netmask "$NETMASK" \
    --cidr "$CIDR" \
    --dns "$DNS" \
    --prefix "$PREFIX" \
    "${EXTRA_POOL_ARGS[@]}" \
    --output "$SCRIPT_DIR/server/inventory.csv" \
    --pool "$SCRIPT_DIR/server/ip_pool.csv"
