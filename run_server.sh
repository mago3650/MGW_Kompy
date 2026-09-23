#!/usr/bin/env bash
# ==============================================================================
# Helper Script to Launch the Deployment & Key Harvester Server
# Part of MGW_Kompy
# ==============================================================================

set -e

# Default settings (customize as needed or pass as environment variables)
SERVER_PORT="${DEPLOY_PORT:-8000}"
SUBNET="${DEPLOY_SUBNET:-192.168.1}"
START_IP="${DEPLOY_START_IP:-101}"
GATEWAY="${DEPLOY_GATEWAY:-192.168.1.1}"
NETMASK="${DEPLOY_NETMASK:-255.255.255.0}"
CIDR="${DEPLOY_CIDR:-24}"
DNS="${DEPLOY_DNS:-1.1.1.1,8.8.8.8}"
PREFIX="${DEPLOY_PREFIX:-nuc-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting MGW_Kompy Deployment Server on port $SERVER_PORT..."
python3 "$SCRIPT_DIR/server/inventory_server.py" \
    --port "$SERVER_PORT" \
    --subnet "$SUBNET" \
    --start-ip "$START_IP" \
    --gateway "$GATEWAY" \
    --netmask "$NETMASK" \
    --cidr "$CIDR" \
    --dns "$DNS" \
    --prefix "$PREFIX" \
    --output "$SCRIPT_DIR/server/inventory.csv" \
    --pool "$SCRIPT_DIR/server/ip_pool.csv"
