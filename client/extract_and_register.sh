#!/bin/bash
# ==============================================================================
# NUC Windows Key Extractor & Deployment Pre-Run Hook
# Part of MGW_Kompy
#
# Runs in Clonezilla Live / PXE environment BEFORE restoring the Linux image.
# 1. Extracts OEM Windows Key from BIOS ACPI MSDM table (or disk registry).
# 2. Collects hardware details (MAC, Serial Number, Model).
# 3. Sends info to central deployment server.
# 4. Receives and caches assigned IP & Hostname for post-clone configuration.
# ==============================================================================

set -o pipefail

# Config / Server Discovery
CONFIG_CACHE="/tmp/deployment_config.json"
SERVER_PORT="${DEPLOY_PORT:-8000}"

# Auto-detect Server IP:
# 1. If passed via command line argument $1
# 2. Or environment variable DEPLOY_SERVER
# 3. Or Clonezilla boot parameter
# 4. Or the default gateway from DHCP
if [ -n "$1" ]; then
    SERVER_IP="$1"
elif [ -n "$DEPLOY_SERVER" ]; then
    SERVER_IP="$DEPLOY_SERVER"
else
    # Clonezilla client's gateway or PXE server
    SERVER_IP=$(ip route show default 2>/dev/null | awk '{print $3}' | head -n1)
    if [ -z "$SERVER_IP" ]; then
        # Try finding via resolv.conf or fall back to standard subnet .1
        SERVER_IP=$(grep -m1 nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}')
        [ -z "$SERVER_IP" ] && SERVER_IP="192.168.1.1"
    fi
fi

API_URL="http://${SERVER_IP}:${SERVER_PORT}/api/register"

echo "=========================================================="
echo "   NUC FLEET PRE-RUN: KEY EXTRACTION & REGISTRATION      "
echo "=========================================================="
echo "[*] Central Server Target: ${API_URL}"

# 1. Hardware Discovery
# Find active network interface
NET_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -n1)
if [ -z "$NET_IFACE" ]; then
    NET_IFACE=$(ls -1 /sys/class/net | grep -vE '^(lo|docker|veth)' | head -n1)
fi

MAC_ADDR=""
if [ -n "$NET_IFACE" ] && [ -f "/sys/class/net/${NET_IFACE}/address" ]; then
    MAC_ADDR=$(cat "/sys/class/net/${NET_IFACE}/address" | tr -d '\r\n ' | tr '[:upper:]' '[:lower:]')
fi

# Fallback MAC scan if default route not ready yet
if [ -z "$MAC_ADDR" ] || [ "$MAC_ADDR" = "00:00:00:00:00:00" ]; then
    for f in /sys/class/net/*/address; do
        dev=$(echo "$f" | cut -d/ -f5)
        if [ "$dev" != "lo" ]; then
            MAC_ADDR=$(cat "$f" | tr -d '\r\n ' | tr '[:upper:]' '[:lower:]')
            [ -n "$MAC_ADDR" ] && break
        fi
    done
fi

SERIAL_NUM=$(cat /sys/class/dmi/id/product_serial 2>/dev/null | tr -d '\r\n' || dmidecode -s system-serial-number 2>/dev/null | tr -d '\r\n')
[ -z "$SERIAL_NUM" ] && SERIAL_NUM="UNKNOWN"

SYS_MODEL=$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr -d '\r\n' || dmidecode -s system-product-name 2>/dev/null | tr -d '\r\n')
[ -z "$SYS_MODEL" ] && SYS_MODEL="Intel NUC"

SYS_UUID=$(cat /sys/class/dmi/id/product_uuid 2>/dev/null | tr -d '\r\n' || dmidecode -s system-uuid 2>/dev/null | tr -d '\r\n')

echo "[*] Detected Hardware:"
echo "    - Interface: ${NET_IFACE}"
echo "    - MAC      : ${MAC_ADDR}"
echo "    - Serial   : ${SERIAL_NUM}"
echo "    - Model    : ${SYS_MODEL}"

# 2. Extract Windows Product Key
WIN_KEY=""
KEY_SOURCE="NONE"

# Method A: ACPI MSDM Table in BIOS/UEFI (Factory OEM Windows license)
if [ -f /sys/firmware/acpi/tables/MSDM ]; then
    echo "[*] ACPI MSDM table found in BIOS/UEFI. Parsing..."
    # The key is 29 characters formatted as XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
    # Standard MSDM table has a 36-byte header, key is at the end
    RAW_KEY=$(tail -c 29 /sys/firmware/acpi/tables/MSDM 2>/dev/null | tr -cd 'A-Z0-9-')
    if [[ "$RAW_KEY" =~ ^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$ ]]; then
        WIN_KEY="$RAW_KEY"
        KEY_SOURCE="ACPI_MSDM"
    else
        # Try strings extraction
        RAW_KEY=$(strings /sys/firmware/acpi/tables/MSDM 2>/dev/null | grep -E -o '[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}' | head -n1)
        if [ -n "$RAW_KEY" ]; then
            WIN_KEY="$RAW_KEY"
            KEY_SOURCE="ACPI_MSDM"
        fi
    fi
fi

# Method B: Fallback to scanning existing Windows disk installation (if MSDM is absent)
if [ -z "$WIN_KEY" ]; then
    echo "[*] ACPI MSDM key not present. Checking existing Windows disk partitions..."
    mkdir -p /tmp/win_mnt_check
    NTFS_PARTS=$(blkid -o device -t TYPE=ntfs 2>/dev/null)
    for part in $NTFS_PARTS; do
        if mount -o ro "$part" /tmp/win_mnt_check 2>/dev/null; then
            # Search for Windows SOFTWARE hive
            SOFTWARE_HIVE=$(find /tmp/win_mnt_check -maxdepth 4 -ipath "*/system32/config/software" 2>/dev/null | head -n1)
            if [ -n "$SOFTWARE_HIVE" ]; then
                echo "[*] Found Windows registry hive on $part. Scanning for backup product key..."
                # Extract 5x5 product key pattern from hive
                DISK_KEY=$(strings "$SOFTWARE_HIVE" 2>/dev/null | grep -E -o '[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}' | head -n1)
                if [ -n "$DISK_KEY" ]; then
                    WIN_KEY="$DISK_KEY"
                    KEY_SOURCE="WIN_REGISTRY"
                fi
            fi
            umount /tmp/win_mnt_check 2>/dev/null
            [ -n "$WIN_KEY" ] && break
        fi
    done
    rmdir /tmp/win_mnt_check 2>/dev/null
fi

if [ -n "$WIN_KEY" ]; then
    echo "=========================================================="
    echo " [SUCCESS] Windows Key Found: ${WIN_KEY} (${KEY_SOURCE})"
    echo "=========================================================="
else
    echo "=========================================================="
    echo " [WARNING] No Windows Key found in BIOS or Disk."
    echo "=========================================================="
    WIN_KEY="NOT_FOUND"
    KEY_SOURCE="NONE"
fi

# 3. Register with Central Deployment Server
JSON_PAYLOAD=$(cat <<EOF
{
  "mac": "${MAC_ADDR}",
  "serial": "${SERIAL_NUM}",
  "model": "${SYS_MODEL}",
  "uuid": "${SYS_UUID}",
  "windows_key": "${WIN_KEY}",
  "key_source": "${KEY_SOURCE}",
  "notes": "Clonezilla PXE Batch"
}
EOF
)

echo "[*] Submitting hardware and license data to server..."
HTTP_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d "$JSON_PAYLOAD" \
    "$API_URL" 2>/dev/null)

HTTP_CODE=$(echo "$HTTP_RESP" | tail -n1)
RESP_BODY=$(echo "$HTTP_RESP" | sed '$d')

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "[SUCCESS] Registered with server! (HTTP $HTTP_CODE)"
    echo "$RESP_BODY" > "$CONFIG_CACHE"
    
    # Extract assigned parameters
    ASSIGNED_IP=$(grep -o '"assigned_ip": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
    ASSIGNED_HOST=$(grep -o '"assigned_hostname": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
    
    echo "=========================================================="
    echo " ASSIGNED TARGET CONFIGURATION FOR THIS UNIT:"
    echo "   Hostname   : ${ASSIGNED_HOST}"
    echo "   Static IP  : ${ASSIGNED_IP}"
    echo "   Windows Key: ${WIN_KEY}"
    echo "=========================================================="

    # Notify server that cloning is starting
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"mac\": \"${MAC_ADDR}\", \"status\": \"CLONING\"}" \
        "http://${SERVER_IP}:${SERVER_PORT}/api/status" >/dev/null 2>&1
else
    echo "[ERROR] Failed to communicate with server at $API_URL (HTTP $HTTP_CODE)"
    echo "Saving offline local backup to /tmp/local_nuc_key.txt"
    echo "${MAC_ADDR},${SERIAL_NUM},${WIN_KEY},${KEY_SOURCE}" >> /tmp/local_nuc_key.txt
fi

echo "[*] Pre-run hook completed. Handing over to Clonezilla disk restore..."
