# 03. IP Address & Network Configuration Details

When cloning a master Linux image across 150 machines, two major network issues must be addressed:

1. **Duplicate Machine-ID Conflict:**
   Modern Linux distributions (Ubuntu 18.04+, Debian 10+, Fedora, RHEL 8/9, Arch) use systemd's `/etc/machine-id`. Many DHCP servers (including `systemd-networkd` and `NetworkManager`) generate the client DUID from this ID. If 150 machines share the same `/etc/machine-id`, they may receive the **exact same IP address** or disrupt routing.
2. **Assigning a Unique Static IP to Every Machine:**
   Setting 150 static IPs by hand would take 5–10 hours of tedious typing. `configure_network.sh` automates this in under 2 seconds.

---

## How `configure_network.sh` Handles This Automatically

Directly after the Clonezilla image is written to the NUC's NVMe drive, `configure_network.sh`:

1. Mounts the restored Linux filesystem (`/dev/nvme0n1p2` or root partition).
2. Sets `/etc/hostname` to the assigned hostname (e.g. `nuc-105`).
3. Updates `/etc/hosts` to match.
4. **Wipes `/etc/machine-id`:**
   ```bash
   truncate -s 0 /mnt/target/etc/machine-id
   rm -f /mnt/target/var/lib/dbus/machine-id
   ```
   On first boot, systemd automatically detects the empty file and generates a brand-new, globally unique machine ID for each NUC.
5. Injects the static network configuration according to the distribution's network stack:

### Case A: Netplan (Ubuntu Server/Desktop & modern Debian)
Creates `/etc/netplan/01-static-ip.yaml`:
```yaml
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
        - 192.168.1.105/24
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: ["1.1.1.1", "8.8.8.8"]
```

### Case B: NetworkManager Keyfile (Fedora, Rocky Linux, RHEL, Ubuntu Desktop)
Creates `/etc/NetworkManager/system-connections/static-wired.nmconnection`:
```ini
[connection]
id=static-wired
type=ethernet
autoconnect=true

[ipv4]
method=manual
addresses1=192.168.1.105;24;192.168.1.1;
dns=1.1.1.1;8.8.8.8;

[ipv6]
method=ignore
```

---

## IP Allocation Modes

### Mode 1: Multiple Subnet Pools (Recommended)
The server supports **multiple subnet pools**, so you can assign different IP ranges to different batches or locations:

```bash
python3 server/inventory_server.py \
    --subnet 192.168.100 \
    --start-ip 1 \
    --gateway 192.168.100.1 \
    --subnet-pool "name=office,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,mask=255.255.255.0,cidr=24,dns=1.1.1.1,prefix=off-" \
    --subnet-pool "name=depot,subnet=10.30.1,start=1,max=250,gw=10.30.1.1,mask=255.255.255.0,cidr=24,dns=8.8.8.8,prefix=dep-"
```

Or via `run_server.sh` — uncomment and edit the `EXTRA_POOLS` array:

```bash
EXTRA_POOLS=(
    "name=office,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,mask=255.255.255.0,cidr=24,dns=1.1.1.1,prefix=off-"
    "name=depot,subnet=10.30.1,start=1,max=250,gw=10.30.1.1,mask=255.255.255.0,cidr=24,dns=8.8.8.8,prefix=dep-"
)
```

Each pool has its own:
- **Subnet range** (e.g. `10.10.35.x`, `10.30.1.x`)
- **IP range** (`start` to `max`)
- **Gateway, netmask, CIDR, DNS**
- **Hostname prefix** (e.g. `off-001`, `dep-001`)

The **default pool** is configured via `--subnet`, `--start-ip`, `--gateway`, etc. Devices that don't specify a pool get assigned from it.

**Choosing a pool:** Devices can specify which pool to use by sending `"pool": "office"` in their registration request, or you can reassign them via the Web Dashboard or the `/api/reassign` API endpoint.

**Dashboard:** The web UI shows all active pools, their usage, and provides a dropdown to reassign any device to a different pool with a new IP.

### Mode 2: Automatic Sequential Allocation (Single Pool)
No prior configuration required. The server allocates sequential IPs starting from `--start-ip` (default: 101):
- First NUC connected: `192.168.1.101` (`nuc-101`)
- Second NUC connected: `192.168.1.102` (`nuc-102`)
- ...
- 150th NUC connected: `192.168.1.250` (`nuc-250`)

### Mode 3: Pre-defined MAC Mapping Table (`ip_pool.csv`)
If your organization requires specific IP addresses bound to specific physical locations or MAC addresses, edit `server/ip_pool.csv`:

```csv
mac,ip,hostname,gateway,netmask,dns
00:1a:2b:3c:4d:01,10.0.50.101,lab-nuc-01,10.0.50.1,255.255.255.0,1.1.1.1
00:1a:2b:3c:4d:02,192.168.100.50,nuc-lab01,192.168.100.1,255.255.255.0,1.1.1.1
00:1a:2b:3c:4d:03,10.10.35.10,nuc-office01,10.10.35.1,255.255.255.0,1.1.1.1
```

You can mix subnets here freely — each row can use a completely different network. If a MAC is found in this file, the server returns that explicit assignment. If a MAC is not found, it gracefully falls back to dynamic sequential allocation from the selected pool.

---

## API Reference: Subnet Pools

### `GET /api/pools`
Returns all configured subnet pools with their status:
```json
[
  {"name": "default", "subnet": "192.168.100", "next_ip": "192.168.100.10", "available": 41, ...},
  {"name": "office",  "subnet": "10.10.35",     "next_ip": "10.10.35.1",   "available": 100, ...}
]
```

### `POST /api/reassign`
Move a device to a different pool (assigns a new IP from that pool):
```json
{"mac": "aa:bb:cc:dd:ee:01", "pool": "office"}
```

### `POST /api/register`
Register a device; optionally specify which pool:
```json
{"mac": "aa:bb:cc:dd:ee:02", "serial": "SN002", "pool": "depot", ...}
```
