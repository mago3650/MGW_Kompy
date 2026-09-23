#!/bin/bash
# ==============================================================================
# NUC Post-Clone Network & System Configuration Hook
# Part of MGW_Kompy
#
# Runs in Clonezilla Live environment AFTER image restoration finishes.
# 1. Mounts the restored Linux root filesystem on the NUC's internal drive.
# 2. Applies the assigned Unique Static IP, Gateway, DNS, and Hostname.
# 3. Resets /etc/machine-id and SSH host keys (prevents network ID collisions).
# 4. Reports completion to central server so the dashboard turns green.
# ==============================================================================

set -o pipefail

CONFIG_CACHE="/tmp/deployment_config.json"
SERVER_PORT="${DEPLOY_PORT:-8000}"

# Auto-detect Server IP if not cached
if [ -n "$1" ]; then
    SERVER_IP="$1"
elif [ -n "$DEPLOY_SERVER" ]; then
    SERVER_IP="$DEPLOY_SERVER"
else
    SERVER_IP=$(ip route show default 2>/dev/null | awk '{print $3}' | head -n1)
    [ -z "$SERVER_IP" ] && SERVER_IP="192.168.1.1"
fi

# Detect MAC Address
NET_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -n1)
if [ -z "$NET_IFACE" ]; then
    NET_IFACE=$(ls -1 /sys/class/net | grep -vE '^(lo|docker|veth)' | head -n1)
fi
MAC_ADDR=$(cat "/sys/class/net/${NET_IFACE}/address" 2>/dev/null | tr -d '\r\n ' | tr '[:upper:]' '[:lower:]')

echo "=========================================================="
echo "   NUC FLEET POST-RUN: UNIQUE NETWORK & IDENTITY SETUP   "
echo "=========================================================="

# If config cache is missing, fetch from server by MAC
if [ ! -s "$CONFIG_CACHE" ]; then
    echo "[*] Config cache missing, querying server for MAC ${MAC_ADDR}..."
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"mac\": \"${MAC_ADDR}\"}" \
        "http://${SERVER_IP}:${SERVER_PORT}/api/register" > "$CONFIG_CACHE"
fi

if [ ! -s "$CONFIG_CACHE" ]; then
    echo "[ERROR] Could not retrieve network configuration from server."
    exit 1
fi

ASSIGNED_IP=$(grep -o '"assigned_ip": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
ASSIGNED_HOST=$(grep -o '"assigned_hostname": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
NETMASK=$(grep -o '"netmask": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
CIDR=$(grep -o '"cidr": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
GATEWAY=$(grep -o '"gateway": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)
DNS_SERVERS=$(grep -o '"dns": *"[^"]*"' "$CONFIG_CACHE" | cut -d'"' -f4)

[ -z "$CIDR" ] && CIDR="24"
[ -z "$GATEWAY" ] && GATEWAY="192.168.1.1"
[ -z "$DNS_SERVERS" ] && DNS_SERVERS="1.1.1.1, 8.8.8.8"

echo "[*] Applying Target Parameters:"
echo "    - Hostname: ${ASSIGNED_HOST}"
echo "    - IP      : ${ASSIGNED_IP}/${CIDR}"
echo "    - Gateway : ${GATEWAY}"
echo "    - DNS     : ${DNS_SERVERS}"

# Find and mount target Linux root filesystem
MOUNT_DIR="/tmp/target_linux_root"
mkdir -p "$MOUNT_DIR"

ROOT_DEV=""
# Search all partitions on NVMe and SATA disks for root filesystem
for dev in $(ls /dev/nvme*p* /dev/sd[a-z][0-9]* 2>/dev/null); do
    if mount -o rw "$dev" "$MOUNT_DIR" 2>/dev/null; then
        if [ -f "$MOUNT_DIR/etc/os-release" ] && [ -d "$MOUNT_DIR/etc" ]; then
            ROOT_DEV="$dev"
            echo "[+] Found Linux root filesystem on: $ROOT_DEV"
            break
        fi
        umount "$MOUNT_DIR" 2>/dev/null
    fi
done

if [ -z "$ROOT_DEV" ]; then
    echo "[ERROR] Could not locate installed Linux root filesystem on disk!"
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"mac\": \"${MAC_ADDR}\", \"status\": \"ERROR_ROOT_NOT_FOUND\"}" \
        "http://${SERVER_IP}:${SERVER_PORT}/api/status" >/dev/null 2>&1
    exit 1
fi

# 1. Configure Hostname
echo "[*] Setting Hostname..."
echo "$ASSIGNED_HOST" > "$MOUNT_DIR/etc/hostname"

if [ -f "$MOUNT_DIR/etc/hosts" ]; then
    # Clean previous local hostname mappings and set new
    sed -i '/127.0.1.1/d' "$MOUNT_DIR/etc/hosts"
    echo "127.0.1.1    $ASSIGNED_HOST" >> "$MOUNT_DIR/etc/hosts"
fi

# 2. Reset machine-id (CRITICAL: prevents identical DHCP DUID collisions)
echo "[*] Resetting systemd machine-id..."
truncate -s 0 "$MOUNT_DIR/etc/machine-id"
rm -f "$MOUNT_DIR/var/lib/dbus/machine-id"

# 3. Regenerate SSH host keys on first boot
echo "[*] Resetting SSH host keys for security uniqueness..."
rm -f "$MOUNT_DIR/etc/ssh/ssh_host_"*"_key"* 2>/dev/null
cat << 'EOF' > "$MOUNT_DIR/etc/systemd/system/regenerate-ssh-keys.service"
[Unit]
Description=Regenerate SSH Host Keys on First Boot
Before=ssh.service sshd.service
ConditionFileNotEmpty=!/etc/ssh/ssh_host_rsa_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
ExecStartPost=/bin/systemctl disable regenerate-ssh-keys.service

[Install]
WantedBy=multi-user.target
EOF
# Enable service link
mkdir -p "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"
ln -sf "/etc/systemd/system/regenerate-ssh-keys.service" "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants/regenerate-ssh-keys.service" 2>/dev/null

# 4. Configure Static IP on Target OS
echo "[*] Configuring Static Network..."

# Detect Network Stack A: Netplan (Ubuntu / Debian modern)
if [ -d "$MOUNT_DIR/etc/netplan" ]; then
    echo "[*] Configuring Netplan static IP..."
    # Backup existing netplan files
    mkdir -p "$MOUNT_DIR/etc/netplan/backup"
    mv "$MOUNT_DIR/etc/netplan/"*.yaml "$MOUNT_DIR/etc/netplan/backup/" 2>/dev/null

    # Format DNS array for YAML
    YAML_DNS=$(echo "$DNS_SERVERS" | awk -F',' '{for(i=1;i<=NF;i++){gsub(/^[ \t]+|[ \t]+$/, "", $i); printf "\"%s\"%s", $i, (i==NF?"":", ")}}')

    cat <<EOF > "$MOUNT_DIR/etc/netplan/01-static-ip.yaml"
# Generated automatically by MGW_Kompy deployment hook
network:
  version: 2
  renderer: networkd
  ethernets:
    wired-all:
      match:
        name: "en*"
      dhcp4: false
      dhcp6: false
      addresses:
        - ${ASSIGNED_IP}/${CIDR}
      routes:
        - to: default
          via: ${GATEWAY}
      nameservers:
        addresses: [${YAML_DNS}]
EOF
    chmod 600 "$MOUNT_DIR/etc/netplan/01-static-ip.yaml"
fi

# Detect Network Stack B: NetworkManager (Fedora / RHEL / Rocky / Desktop distros)
if [ -d "$MOUNT_DIR/etc/NetworkManager/system-connections" ]; then
    echo "[*] Configuring NetworkManager static connection..."
    DNS_SEMI=$(echo "$DNS_SERVERS" | sed 's/ //g' | tr ',' ';')
    cat <<EOF > "$MOUNT_DIR/etc/NetworkManager/system-connections/static-wired.nmconnection"
[connection]
id=static-wired
type=ethernet
autoconnect=true

[ethernet]

[ipv4]
method=manual
addresses1=${ASSIGNED_IP};${CIDR};${GATEWAY};
dns=${DNS_SEMI};

[ipv6]
method=ignore
EOF
    chmod 600 "$MOUNT_DIR/etc/NetworkManager/system-connections/static-wired.nmconnection"
fi

# Detect Network Stack C: /etc/network/interfaces (Traditional Debian)
if [ -f "$MOUNT_DIR/etc/network/interfaces" ] && [ ! -d "$MOUNT_DIR/etc/netplan" ]; then
    echo "[*] Configuring /etc/network/interfaces..."
    cat <<EOF >> "$MOUNT_DIR/etc/network/interfaces"

# Auto-configured static IP by MGW_Kompy
auto eth0
iface eth0 inet static
    address ${ASSIGNED_IP}
    netmask ${NETMASK}
    gateway ${GATEWAY}
    dns-nameservers ${DNS_SERVERS}
EOF
fi

sync

# Unmount cleanly
echo "[*] Unmounting target filesystem..."
umount "$MOUNT_DIR" 2>/dev/null

# 5. Notify Server of Success
echo "[*] Reporting SUCCESS to central server..."
curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"mac\": \"${MAC_ADDR}\", \"status\": \"CONFIGURED\", \"notes\": \"IP: ${ASSIGNED_IP}, Host: ${ASSIGNED_HOST}\"}" \
    "http://${SERVER_IP}:${SERVER_PORT}/api/status" >/dev/null 2>&1

echo "=========================================================="
echo " [SUCCESS] UNIT DEPLOYMENT & CONFIGURATION COMPLETE!     "
echo " Hostname : ${ASSIGNED_HOST}                             "
echo " IP       : ${ASSIGNED_IP}/${CIDR}                       "
echo "=========================================================="
