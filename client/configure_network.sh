#!/bin/bash
# ==============================================================================
# NUC Post-Clone Network & System Configuration Hook
# Part of MGW_Kompy
#
# Image mode (default, Clonezilla ocs_postrun):
#   1. Mounts the restored Linux root (or uses DEPLOY_TARGET_ROOT).
#   2. Applies the assigned static IP, gateway, DNS and hostname.
#   3. Neutralises every DHCP source in the cloned image so the bench router
#      cannot hand the unit a different address on first boot.
#   4. Resets /etc/machine-id and SSH host keys (cloned identity must not leak).
#   5. Optionally installs an on-boot identity agent so a later dashboard edit
#      lands the next time the unit powers on.
#
# Live mode (DEPLOY_MODE=live) — re-apply on a unit that is already deployed:
#   Same network/hostname/DHCP work, idempotent ("files changed: 0" when the
#   tree already matches). Never touches machine-id or SSH host keys.
#
# Switches (environment):
#   DEPLOY_MODE=image|live     image = Clonezilla post-run (default)
#   DEPLOY_APPLY=0             print the plan, write nothing
#   DEPLOY_IFACE=eth0          interface name used by ifupdown
#   DEPLOY_MAC=aa:bb:cc:dd:ee:ff
#   DEPLOY_TARGET_ROOT=/mnt    already-mounted (or fake) root; skips disk search
#   DEPLOY_CONFIG_FILE=path    assignment JSON; skips the server fetch
#   DEPLOY_BOOT_AGENT=1|0      install or remove the on-boot identity agent
#                              (unset = leave whatever is already installed)
#   DEPLOY_SERVER / DEPLOY_PORT / DEPLOY_POOL
# ==============================================================================

set -o pipefail
shopt -s nullglob

CONFIG_CACHE="/tmp/deployment_config.json"
SERVER_PORT="${DEPLOY_PORT:-8000}"
DEPLOY_MODE=$(printf '%s' "${DEPLOY_MODE:-image}" | tr '[:upper:]' '[:lower:]')
APPLY=1
[ "${DEPLOY_APPLY:-1}" = "0" ] && APPLY=0
CHANGED=0

note_change() {
    CHANGED=$((CHANGED + 1))
}

# Consume stdin and write it to $1 only when the bytes differ.
write_file() {
    local dest="$1"
    local mode="${2:-644}"
    local tmp
    tmp=$(mktemp)
    cat > "$tmp"
    if [ "$APPLY" != "1" ]; then
        rm -f "$tmp"
        return 0
    fi
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
    else
        mkdir -p "$(dirname "$dest")"
        mv "$tmp" "$dest"
        note_change
    fi
    chmod "$mode" "$dest" 2>/dev/null || true
}

move_aside() {
    local src="$1"
    local dest="$2"
    [ "$APPLY" = "1" ] || return 0
    [ -e "$src" ] || return 0
    mkdir -p "$(dirname "$dest")"
    mv "$src" "$dest"
    note_change
}

ensure_link() {
    local target="$1"
    local link="$2"
    [ "$APPLY" = "1" ] || return 0
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
        return 0
    fi
    mkdir -p "$(dirname "$link")"
    ln -sfn "$target" "$link"
    note_change
}

remove_path() {
    local path="$1"
    [ "$APPLY" = "1" ] || return 0
    if [ -e "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        note_change
    fi
}

# ---------------------------------------------------------------- server / identity
if [ -n "${1:-}" ]; then
    SERVER_IP="$1"
elif [ -n "${DEPLOY_SERVER:-}" ]; then
    SERVER_IP="$DEPLOY_SERVER"
else
    SERVER_IP=$(ip route show default 2>/dev/null | awk '{print $3}' | head -n1)
    [ -z "$SERVER_IP" ] && SERVER_IP="192.168.1.1"
fi

if [ -n "${DEPLOY_IFACE:-}" ]; then
    NET_IFACE="$DEPLOY_IFACE"
else
    NET_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -n1)
    if [ -z "$NET_IFACE" ]; then
        NET_IFACE=$(ls -1 /sys/class/net 2>/dev/null | grep -vE '^(lo|docker|veth)' | head -n1)
    fi
fi
[ -z "$NET_IFACE" ] && NET_IFACE="eth0"

if [ -n "${DEPLOY_MAC:-}" ]; then
    MAC_ADDR=$(printf '%s' "$DEPLOY_MAC" | tr '[:upper:]' '[:lower:]' | tr -cd '0-9a-f:')
else
    MAC_ADDR=$(cat "/sys/class/net/${NET_IFACE}/address" 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cd '0-9a-f:')
fi

echo "=========================================================="
echo "   NUC FLEET POST-RUN: UNIQUE NETWORK & IDENTITY SETUP   "
echo "=========================================================="
echo "[*] Mode: ${DEPLOY_MODE}    apply: ${APPLY}"

if [ -n "${DEPLOY_CONFIG_FILE:-}" ] && [ -s "$DEPLOY_CONFIG_FILE" ]; then
    CONFIG_CACHE="$DEPLOY_CONFIG_FILE"
    echo "[*] Using assignment from ${CONFIG_CACHE}"
else
    echo "[*] Fetching the current assignment for ${MAC_ADDR}..."
    FRESH_CONFIG=$(curl -s --max-time 5 --connect-timeout 3 -X POST \
        -H "Content-Type: application/json" \
        -d "{\"mac\": \"${MAC_ADDR}\"}" \
        "http://${SERVER_IP}:${SERVER_PORT}/api/register" 2>/dev/null || true)
    if echo "$FRESH_CONFIG" | grep -q '"assigned_ip"'; then
        echo "$FRESH_CONFIG" > "$CONFIG_CACHE"
        echo "[*] Server returned the latest assignment (dashboard edits win)."
    elif [ -s "$CONFIG_CACHE" ]; then
        echo "[!] Server did not answer - using the configuration cached during the pre-run."
    else
        echo "[ERROR] Could not retrieve network configuration from server."
        exit 1
    fi
fi

ASSIGNED_IP=$(grep -o '"assigned_ip": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)
ASSIGNED_HOST=$(grep -o '"assigned_hostname": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)
NETMASK=$(grep -o '"netmask": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)
CIDR=$(grep -o '"cidr": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)
GATEWAY=$(grep -o '"gateway": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)
DNS_SERVERS=$(grep -o '"dns": *"[^"]*"' "$CONFIG_CACHE" | head -n1 | cut -d'"' -f4)

if [ -z "$ASSIGNED_IP" ]; then
    echo "[ERROR] The server returned no IP address for this unit - fix it in the dashboard."
    exit 1
fi
[ -z "$ASSIGNED_HOST" ] && ASSIGNED_HOST="nuc-$(echo "$ASSIGNED_IP" | awk -F. '{print $NF}')"
[ -z "$CIDR" ] && CIDR="24"
[ -z "$NETMASK" ] && NETMASK="255.255.255.0"
[ -z "$GATEWAY" ] && GATEWAY="192.168.1.1"
[ -z "$DNS_SERVERS" ] && DNS_SERVERS="1.1.1.1, 8.8.8.8"

echo "[*] Applying Target Parameters:"
echo "    - Hostname: ${ASSIGNED_HOST}"
echo "    - IP      : ${ASSIGNED_IP}/${CIDR}"
echo "    - Gateway : ${GATEWAY}"
echo "    - DNS     : ${DNS_SERVERS}"
echo "    - MAC     : ${MAC_ADDR:-unknown}"
echo "    - Iface   : ${NET_IFACE}"

# ---------------------------------------------------------------- target root
MOUNTED=0
ROOT=""
if [ -n "${DEPLOY_TARGET_ROOT:-}" ]; then
    ROOT="${DEPLOY_TARGET_ROOT}"
    [ "$ROOT" != "/" ] && ROOT="${ROOT%/}"
elif [ "$DEPLOY_MODE" = "live" ]; then
    ROOT="/"
else
    MOUNT_DIR="/tmp/target_linux_root"
    mkdir -p "$MOUNT_DIR"
    ROOT_DEV=""
    for dev in /dev/nvme*p* /dev/sd[a-z][0-9]*; do
        [ -b "$dev" ] || continue
        if mount -o rw "$dev" "$MOUNT_DIR" 2>/dev/null; then
            if [ -f "$MOUNT_DIR/etc/os-release" ] && [ -d "$MOUNT_DIR/etc" ]; then
                ROOT_DEV="$dev"
                echo "[+] Found Linux root filesystem on: $ROOT_DEV"
                break
            fi
            umount "$MOUNT_DIR" 2>/dev/null || true
        fi
    done
    if [ -z "$ROOT_DEV" ]; then
        echo "[ERROR] Could not locate installed Linux root filesystem on disk!"
        if [ -z "${DEPLOY_CONFIG_FILE:-}" ] || [ -n "${DEPLOY_SERVER:-}" ]; then
            curl -s --max-time 5 --connect-timeout 3 -X POST -H "Content-Type: application/json" \
                -d "{\"mac\": \"${MAC_ADDR}\", \"status\": \"ERROR_ROOT_NOT_FOUND\"}" \
                "http://${SERVER_IP}:${SERVER_PORT}/api/status" >/dev/null 2>&1 || true
        fi
        exit 1
    fi
    ROOT="$MOUNT_DIR"
    MOUNTED=1
fi

if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
    echo "[ERROR] Target root '${ROOT:-}' does not exist."
    exit 1
fi
echo "[*] Target root: ${ROOT}"

# ---------------------------------------------------------------- hostname
echo "[*] Setting hostname..."
printf '%s\n' "$ASSIGNED_HOST" | write_file "$ROOT/etc/hostname" 644

HOSTS_TMP=$(mktemp)
if [ -f "$ROOT/etc/hosts" ]; then
    grep -v '^127\.0\.1\.1[[:space:]]' "$ROOT/etc/hosts" > "$HOSTS_TMP" || true
else
    printf '127.0.0.1 localhost\n' > "$HOSTS_TMP"
fi
# drop a trailing blank line we may have left, then append exactly one mapping
printf '127.0.1.1    %s\n' "$ASSIGNED_HOST" >> "$HOSTS_TMP"
write_file "$ROOT/etc/hosts" 644 < "$HOSTS_TMP"
rm -f "$HOSTS_TMP"

# ---------------------------------------------------------------- cloned identity (image mode only)
reset_cloned_identity() {
    echo "[*] Resetting systemd machine-id..."
    if [ "$APPLY" = "1" ]; then
        if [ -f "$ROOT/etc/machine-id" ] && [ ! -s "$ROOT/etc/machine-id" ]; then
            :
        else
            mkdir -p "$ROOT/etc"
            : > "$ROOT/etc/machine-id"
            note_change
        fi
        if [ -e "$ROOT/var/lib/dbus/machine-id" ] || [ -L "$ROOT/var/lib/dbus/machine-id" ]; then
            rm -f "$ROOT/var/lib/dbus/machine-id"
            note_change
        fi
    fi

    echo "[*] Resetting SSH host keys for security uniqueness..."
    if [ "$APPLY" = "1" ]; then
        local removed=0
        local key
        for key in "$ROOT"/etc/ssh/ssh_host_*_key "$ROOT"/etc/ssh/ssh_host_*_key.pub; do
            [ -e "$key" ] || [ -L "$key" ] || continue
            rm -f "$key"
            removed=1
        done
        [ "$removed" = "1" ] && note_change
    fi

    write_file "$ROOT/etc/systemd/system/regenerate-ssh-keys.service" 644 << 'EOF'
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
    ensure_link /etc/systemd/system/regenerate-ssh-keys.service \
        "$ROOT/etc/systemd/system/multi-user.target.wants/regenerate-ssh-keys.service"
}

if [ "$DEPLOY_MODE" != "live" ]; then
    reset_cloned_identity
else
    echo "[*] Live mode: leaving machine-id and SSH host keys untouched."
fi

# ---------------------------------------------------------------- DHCP neutralisation
echo "[*] Neutralising DHCP and writing the static assignment..."

YAML_DNS=$(echo "$DNS_SERVERS" | awk -F',' '{for(i=1;i<=NF;i++){gsub(/^[ \t]+|[ \t]+$/, "", $i); if ($i != "") printf "\"%s\"%s", $i, (i==NF?"":", ")}}')
[ -z "$YAML_DNS" ] && YAML_DNS='"1.1.1.1", "8.8.8.8"'
DNS_SEMI=$(echo "$DNS_SERVERS" | sed 's/ //g' | tr ',' ';')
DNS_SPACED=$(echo "$DNS_SERVERS" | sed 's/,/ /g' | tr -s ' ')

# --- 1. netplan (Ubuntu / modern Debian, including cloud-init) -----------------
if [ -d "$ROOT/etc/netplan" ] || { [ ! -d "$ROOT/etc/NetworkManager/system-connections" ] && [ ! -f "$ROOT/etc/network/interfaces" ]; }; then
    if [ "$APPLY" = "1" ]; then
        mkdir -p "$ROOT/etc/netplan/mgw-backup"
    fi
    for f in "$ROOT"/etc/netplan/*.yaml "$ROOT"/etc/netplan/*.yml; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        [ "$base" = "01-mgw-static.yaml" ] && continue
        echo "[*] Moving netplan $base aside (it must not keep DHCP)"
        move_aside "$f" "$ROOT/etc/netplan/mgw-backup/$base"
    done

    if [ -n "$MAC_ADDR" ]; then
        MATCH_BLOCK=$(printf '      match:\n        macaddress: "%s"\n' "$MAC_ADDR")
    else
        MATCH_BLOCK=$(printf '      match:\n        name: "%s"\n' "${NET_IFACE:-e*}")
    fi
    # $(...) strips the trailing newline from MATCH_BLOCK, so dhcp4 must start
    # on its own line or netplan sees one invalid scalar.
    write_file "$ROOT/etc/netplan/01-mgw-static.yaml" 600 << EOF
# Generated automatically by MGW_Kompy deployment hook
# Static config is matched to this unit's NIC so a renamed interface still gets it.
network:
  version: 2
  renderer: networkd
  ethernets:
    mgw-wired:
${MATCH_BLOCK}
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
fi

# --- 2. systemd-networkd -------------------------------------------------------
for f in "$ROOT"/etc/systemd/network/*.network \
         "$ROOT"/lib/systemd/network/*.network \
         "$ROOT"/usr/lib/systemd/network/*.network \
         "$ROOT"/run/systemd/network/*.network; do
    [ -f "$f" ] || continue
    if grep -Eiq '^[[:space:]]*DHCP[[:space:]]*=[[:space:]]*(yes|true|ipv4|v4)[[:space:]]*$' "$f"; then
        echo "[*] Disabling systemd-networkd DHCP file $(basename "$f")"
        move_aside "$f" "$f.disabled"
    fi
done

# --- 3. NetworkManager ---------------------------------------------------------
if [ -d "$ROOT/etc/NetworkManager/system-connections" ]; then
    for f in "$ROOT"/etc/NetworkManager/system-connections/*; do
        [ -f "$f" ] || continue
        grep -Eq '^[[:space:]]*method=(auto|dhcp)[[:space:]]*$' "$f" || continue
        if grep -Eq '^[[:space:]]*autoconnect=false[[:space:]]*$' "$f"; then
            continue
        fi
        echo "[*] Disabling NetworkManager DHCP profile $(basename "$f")"
        NM_TMP=$(mktemp)
        if grep -Eq '^[[:space:]]*autoconnect=' "$f"; then
            sed -E 's/^[[:space:]]*autoconnect=.*/autoconnect=false/' "$f" > "$NM_TMP"
        else
            awk 'BEGIN{done=0} {print} /^\[connection\]$/ && !done {print "autoconnect=false"; done=1}' "$f" > "$NM_TMP"
        fi
        write_file "$f" 600 < "$NM_TMP"
        rm -f "$NM_TMP"
    done

    MAC_NM=$(printf '%s' "$MAC_ADDR" | tr '[:lower:]' '[:upper:]')
    NM_MAC_LINE=""
    if [ -n "$MAC_NM" ]; then
        NM_MAC_LINE="mac-address=${MAC_NM}"
    fi
    write_file "$ROOT/etc/NetworkManager/system-connections/mgw-static.nmconnection" 600 << EOF
[connection]
id=mgw-static
type=ethernet
autoconnect=true
autoconnect-priority=100

[ethernet]
${NM_MAC_LINE}

[ipv4]
method=manual
address1=${ASSIGNED_IP}/${CIDR},${GATEWAY}
dns=${DNS_SEMI};

[ipv6]
method=ignore
EOF
fi

# --- 4 + 5. ifupdown and interfaces.d ------------------------------------------
strip_ifupdown_dhcp() {
    local f="$1"
    [ -f "$f" ] || return 0
    grep -Eq 'inet6?[[:space:]]+dhcp' "$f" || return 0
    echo "[*] Stripping DHCP stanzas from ${f#"$ROOT"}"
    local tmp
    tmp=$(mktemp)
    awk '
        FNR == NR {
            if ($1 == "iface" && ($3 == "inet" || $3 == "inet6") && $4 == "dhcp") bad[$2] = 1
            next
        }
        {
            if (($1 == "auto" || $1 == "allow-hotplug") && bad[$2]) next
            if ($1 == "iface" && bad[$2] && $4 == "dhcp") { drop = 1; next }
            if (drop) {
                if ($0 ~ /^[ \t]/ || $0 ~ /^$/) next
                drop = 0
            }
            print
        }
    ' "$f" "$f" > "$tmp"
    write_file "$f" 644 < "$tmp"
    rm -f "$tmp"
}

if [ -f "$ROOT/etc/network/interfaces" ]; then
    strip_ifupdown_dhcp "$ROOT/etc/network/interfaces"
    # A netplan/NM image must not also grow a second static stanza. ifupdown-only
    # images get the assignment here, matched to this NIC via hwaddress.
    if [ ! -d "$ROOT/etc/netplan" ] && [ ! -d "$ROOT/etc/NetworkManager/system-connections" ]; then
        if ! grep -q "address ${ASSIGNED_IP}" "$ROOT/etc/network/interfaces" 2>/dev/null; then
            IF_TMP=$(mktemp)
            cat "$ROOT/etc/network/interfaces" > "$IF_TMP"
            # make sure the file ends with a newline before we append
            [ -s "$IF_TMP" ] && [ -n "$(tail -c1 "$IF_TMP")" ] && printf '\n' >> "$IF_TMP"
            cat >> "$IF_TMP" << EOF

# Auto-configured static IP by MGW_Kompy
auto ${NET_IFACE}
iface ${NET_IFACE} inet static
    address ${ASSIGNED_IP}
    netmask ${NETMASK}
    gateway ${GATEWAY}
    dns-nameservers ${DNS_SPACED}
    hwaddress ether ${MAC_ADDR}
EOF
            write_file "$ROOT/etc/network/interfaces" 644 < "$IF_TMP"
            rm -f "$IF_TMP"
        fi
    fi
fi

if [ -d "$ROOT/etc/network/interfaces.d" ]; then
    for f in "$ROOT"/etc/network/interfaces.d/*; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        case "$base" in
            .*|mgw-backup) continue ;;
        esac
        if grep -Eq 'inet6?[[:space:]]+dhcp' "$f"; then
            echo "[*] Neutralising interfaces.d snippet $base"
            move_aside "$f" "$ROOT/etc/network/interfaces.d/mgw-backup/$base"
        fi
    done
fi

# --- cloud-init network stage --------------------------------------------------
# cloud-init rewrites netplan on boot unless its network stage is disabled.
# The drop-in is harmless on an image that never runs cloud-init.
write_file "$ROOT/etc/cloud/cloud.cfg.d/99-mgw-disable-network.cfg" 644 << 'EOF'
# Written by MGW_Kompy. cloud-init must not restore DHCP over the assigned address.
network: {config: disabled}
EOF

# ---------------------------------------------------------------- on-boot agent
install_boot_agent() {
    echo "[*] Installing on-boot identity agent (dashboard edits apply at next boot)..."
    write_file "$ROOT/etc/mgw-deploy.conf" 644 << EOF
# Written by MGW_Kompy configure_network.sh — sourced by mgw-identity.sh
DEPLOY_SERVER=${SERVER_IP}
DEPLOY_PORT=${SERVER_PORT}
DEPLOY_MAC=${MAC_ADDR}
DEPLOY_IFACE=${NET_IFACE}
DEPLOY_POOL=${DEPLOY_POOL:-default}
EOF
    write_file "$ROOT/usr/local/sbin/mgw-identity.sh" 755 << 'EOF'
#!/bin/bash
# MGW_Kompy on-boot identity agent.
# Re-reads this unit's IP and hostname from the dashboard and applies them.
set -o pipefail
CONF=/etc/mgw-deploy.conf
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
fi
export DEPLOY_MODE=live
# Leave DEPLOY_BOOT_AGENT unset so a downloaded hook follows the dashboard
# setting instead of uninstalling this agent on every boot.
unset DEPLOY_BOOT_AGENT
export DEPLOY_SERVER DEPLOY_PORT DEPLOY_MAC DEPLOY_IFACE DEPLOY_POOL
CACHED=/usr/local/sbin/mgw-configure-network.sh
SCRIPT="$CACHED"
if [ -n "${DEPLOY_SERVER:-}" ] && command -v curl >/dev/null 2>&1; then
    TMP=$(mktemp)
    if curl -fsS --max-time 8 --connect-timeout 3 \
        "http://${DEPLOY_SERVER}:${DEPLOY_PORT:-8000}/client/configure_network.sh" -o "$TMP"; then
        mv "$TMP" "$CACHED"
        chmod 755 "$CACHED"
        SCRIPT="$CACHED"
    else
        rm -f "$TMP"
    fi
fi
if [ ! -x "$SCRIPT" ]; then
    echo "[mgw-identity] no configure script available" >&2
    exit 1
fi
exec "$SCRIPT"
EOF
    # Cached copy so the first boot still works if the server is briefly down.
    if [ -f "$0" ] && [ "$APPLY" = "1" ]; then
        CACHE_TMP=$(mktemp)
        cat "$0" > "$CACHE_TMP"
        write_file "$ROOT/usr/local/sbin/mgw-configure-network.sh" 755 < "$CACHE_TMP"
        rm -f "$CACHE_TMP"
    fi
    write_file "$ROOT/etc/systemd/system/mgw-identity.service" 644 << 'EOF'
[Unit]
Description=MGW Kompy identity agent - re-apply dashboard IP and hostname
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/mgw-deploy.conf

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mgw-identity.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    ensure_link /etc/systemd/system/mgw-identity.service \
        "$ROOT/etc/systemd/system/multi-user.target.wants/mgw-identity.service"
}

uninstall_boot_agent() {
    echo "[*] Removing on-boot identity agent..."
    remove_path "$ROOT/etc/systemd/system/multi-user.target.wants/mgw-identity.service"
    remove_path "$ROOT/etc/systemd/system/mgw-identity.service"
    remove_path "$ROOT/usr/local/sbin/mgw-identity.sh"
    remove_path "$ROOT/usr/local/sbin/mgw-configure-network.sh"
    remove_path "$ROOT/etc/mgw-deploy.conf"
}

case "${DEPLOY_BOOT_AGENT:-}" in
    1|yes|true|on|YES|TRUE|ON) install_boot_agent ;;
    0|no|false|off|NO|FALSE|OFF) uninstall_boot_agent ;;
    *) ;;
esac

if [ "$MOUNTED" = "1" ]; then
    echo "[*] Unmounting target filesystem..."
    sync
    umount "$ROOT" 2>/dev/null || true
elif [ "$APPLY" = "1" ]; then
    sync 2>/dev/null || true
fi

# ---------------------------------------------------------------- report
if [ "$APPLY" = "1" ] && { [ -z "${DEPLOY_CONFIG_FILE:-}" ] || [ -n "${DEPLOY_SERVER:-}" ]; }; then
    echo "[*] Reporting SUCCESS to central server..."
    NOTES="IP: ${ASSIGNED_IP}, Host: ${ASSIGNED_HOST}"
    curl -s --max-time 5 --connect-timeout 3 -X POST -H "Content-Type: application/json" \
        -d "{\"mac\": \"${MAC_ADDR}\", \"status\": \"CONFIGURED\", \"notes\": \"${NOTES}\"}" \
        "http://${SERVER_IP}:${SERVER_PORT}/api/status" >/dev/null 2>&1 || true
fi

echo "=========================================================="
if [ "$DEPLOY_MODE" = "live" ]; then
    echo " [SUCCESS] LIVE IDENTITY RE-APPLIED                       "
else
    echo " [SUCCESS] UNIT DEPLOYMENT & CONFIGURATION COMPLETE!     "
fi
echo " Hostname : ${ASSIGNED_HOST}"
echo " IP       : ${ASSIGNED_IP}/${CIDR}"
echo "files changed: ${CHANGED}"
echo "=========================================================="
