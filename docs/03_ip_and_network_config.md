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

### Mode 1: Web Dashboard Pool Editor (Recommended)
`server/inventory_server.py` exposes a **Network Settings** panel on `http://<master>:8000/`.
Each pool is one editable row:

| Column | Meaning |
|---|---|
| Pool | Name used by `{"pool": "..."}` on the API and by the reassign dropdown |
| Subnet (CIDR) | Any IPv4 network: `192.168.1.0/24`, `10.10.35.0/24`, `10.30.0.0/16` |
| Range start / end | Host offsets inside the network (`101`-`250` on a /24 == the classic last-octet range) |
| Default / next IP | The address handed to the *next* newly registered device. Accepts a full IP (`192.168.1.140`) or an offset (`140`). If it falls outside **Range start/end**, that range is widened to include it and the response says which end moved |
| Gateway / DNS | Blank gateway = `.1` of the subnet; DNS is a comma-separated list (max 3) |
| Host prefix | Hostname = prefix + zero-padded offset (`nuc-101`, `off-007`) |

Buttons per row: **Save** (create/rename/reconfigure), **★** (make it the default pool for devices that
do not name one), **↻** (restart the range at its first free host), **Delete** (asks for confirmation
when devices still use the pool). Global fields: master/server IP, port and the fleet target size.

Everything is validated server-side, applies to the next registration, and is written to
`server/network_config.json`:

* allocation never reuses an address that is already in `inventory.csv` or `ip_pool.csv` - if you
  rewind the cursor, used addresses are skipped;
* netmask/CIDR are always derived from the subnet, so they cannot drift apart;
* addresses outside the pool network are refused, so a typo cannot silently hand a NUC an IP that
  belongs to another VLAN;
* the server IP/port/default pool are injected into `client/extract_and_register.sh` and
  `client/configure_network.sh` while they are downloaded, so the Clonezilla side needs no edits;
* `run_server.sh` / CLI flags only seed the **first** run. After that the JSON file wins
  (`NO_DEPLOY_CONFIG=1` or `--no-config`, or delete the file, to re-seed; `--read-only` locks editing).

**Curl equivalents** (the same API the dashboard uses - handy over SSH):

```bash
curl -s http://localhost:8000/api/config                       # pools + settings + config path
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"name":"office","network":"10.10.35.0/24","start":1,"max":250,"prefix":"off-"}' \
  http://localhost:8000/api/pool/save                          # create (or update with original_name)
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"pool":"office","next_ip":"10.10.35.140"}' \
  http://localhost:8000/api/pool/next-ip                       # move the default/next IP
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"original_name":"office","force":true}' \
  http://localhost:8000/api/pool/delete                        # delete a pool
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"default_pool":"office","server_ip":"10.10.35.1","target_count":150}' \
  http://localhost:8000/api/config/save                        # global settings
```

### Mode 2: Automatic Sequential Allocation (Single Pool)
No prior configuration required - one pool called `default` is enough. It allocates sequentially from
the **Default / next IP** field in the dashboard (seeded by `--start-ip`, default 101):
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

This is the only addressing table that still lives in a file (it is a per-device map rather than a pool
setting), and it is read once at server start — restart the server after editing it. Those addresses are
counted as "in use", so pool allocation will skip them.

---

## API Reference: Subnet Pools

### `GET /api/pools`
Returns all configured subnet pools with their status:
```json
[
  {"name": "default", "network": "192.168.100.0/24", "subnet": "192.168.100", "cidr": "24",
   "start": 101, "max": 250, "next_ip": "192.168.100.141", "available": 110, "in_use": 40, ...},
  {"name": "office",  "network": "10.10.35.0/24", "subnet": "10.10.35", "cidr": "24",
   "start": 1, "max": 250, "next_ip": "10.10.35.100", "available": 151, "in_use": 99, ...}
]
```

### `GET /api/config` / `POST /api/config/save`
Read or change the whole configuration (pools are re-sent by the dashboard when a pool row is saved).
`legacy` keys `start_ip`, `max_ip`, `current_ip_counter` and `subnet` are still returned for
compatibility, `start`/`max`/`network` are the canonical names.

### `POST /api/pool/save`
Create (`{"name": ..., "network": ...}`) or update (add `"original_name": "<old name>"`) a pool.
Blank fields fall back to their defaults; the response contains the effective pool plus all pools.

### `POST /api/pool/next-ip`
`{"pool": "office", "next_ip": "10.10.35.140"}` - moves the default/next address. `"offset"` is
accepted as a synonym. Rejected when the address is not inside the pool's network (or is not a usable
host number); when it is inside the network but beyond `start`/`max`, the range is widened to include
it and the adjustment is reported in the `notes` array of the response.

### `POST /api/pool/delete`
`{"original_name": "office", "force": true}` - refuses (HTTP 400) while devices still use the pool
unless `force` is set; the last remaining pool can never be deleted. Already-assigned devices keep
their addresses either way.

### `POST /api/pools/replace`
Bulk atomic replacement: `{"pools": [ ... ], "settings": { ... }}` - validated as a whole.

### `POST /api/reassign`
Move a device to a different pool (assigns a new IP from that pool), or pin it to one address:
```json
{"mac": "aa:bb:cc:dd:ee:01", "pool": "office"}
{"mac": "aa:bb:cc:dd:ee:01", "pool": "office", "ip": "10.10.35.140"}
```

### `POST /api/register`
Register a device; optionally specify which pool:
```json
{"mac": "aa:bb:cc:dd:ee:02", "serial": "SN002", "pool": "depot", ...}
```
The response carries everything `configure_network.sh` needs (`assigned_ip`, `assigned_hostname`,
`netmask`, `cidr`, `gateway`, `dns`) - so a pool change on the dashboard is picked up by the next
batch without touching any file.

### `GET /api/hooks`
The ready-to-paste Clonezilla `ocs_prerun` / `ocs_postrun` commands for the current server IP,
port and default pool (also shown at the bottom of the Network Settings panel).
