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
Every existing netplan file, including cloud-init's `50-cloud-init.yaml`, is moved to
`/etc/netplan/mgw-backup/` and replaced by `/etc/netplan/01-mgw-static.yaml`. The NIC is matched by
MAC address, not by a name glob, so a renamed interface still gets the assignment:

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    mgw-wired:
      match:
        macaddress: "aa:bb:cc:dd:ee:01"
      dhcp4: false
      dhcp6: false
      addresses:
        - 192.168.169.101/24
      routes:
        - to: default
          via: 192.168.169.254
      nameservers:
        addresses: ["1.1.1.1", "8.8.8.8"]
```

The same hook also disables the other DHCP sources a cloned image commonly still has:

| Stack | What the hook does |
|---|---|
| systemd-networkd | Any `*.network` with `DHCP=yes` is renamed to `*.network.disabled` |
| NetworkManager | Profiles with `method=auto` get `autoconnect=false`. A `mgw-static.nmconnection` (method=manual, matched by MAC) is added when that directory exists |
| ifupdown | `inet dhcp` stanzas are stripped from `/etc/network/interfaces` |
| `interfaces.d` | Snippets that still say `inet dhcp` are moved to `interfaces.d/mgw-backup/` |
| cloud-init | `/etc/cloud/cloud.cfg.d/99-mgw-disable-network.cfg` sets `network: {config: disabled}` so cloud-init cannot write a DHCP netplan back on boot |

### Case B: NetworkManager Keyfile (Fedora, Rocky Linux, RHEL, Ubuntu Desktop)
Creates `/etc/NetworkManager/system-connections/mgw-static.nmconnection`:
```ini
[connection]
id=mgw-static
type=ethernet
autoconnect=true

[ethernet]
mac-address=AA:BB:CC:DD:EE:01

[ipv4]
method=manual
address1=192.168.169.101/24,192.168.169.254
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
| Host prefix | Hostname = prefix + zero-padded host offset (`nuc-101`, `off-007`; on a /16 the offset keeps running past 254) |

Buttons per row: **Save** (create/rename/reconfigure), **★** (make it the default pool for devices that
do not name one), **↻** (restart the range at its first free host), **Delete** (asks for confirmation
when devices still use the pool). Global fields: master/server IP, port and the fleet target size.

The page **never reloads itself**: auto-refresh starts **off** (press *Refresh* for the latest state) and
the *Auto-refresh* button polls `/api/dashboard` every 5 s when you want live tracking - counters, pool
cards and the NUC table are swapped in place, so an editor you have open is never wiped
(`?refresh=on` / `?refresh=off` overrides it for one visit; the choice is remembered in `localStorage`).

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
setting). You can edit the file directly (restart the server afterwards) **or** use the dashboard's
*Fixed IP & name per unit* panel, which writes exactly this file (`POST /api/predefine/save`,
`POST /api/predefine/delete`). Those addresses are counted as "in use", so pool allocation skips them.

### Mode 4: Pin one unit by hand from the NUC table (name and/or exact IP)

The *Connected NUC Systems* table has an **Edit name / IP** button on every row. It opens an inline
editor for that single machine (identified by its MAC) with three fields:

| Field | Meaning |
|---|---|
| Computer name | Anything up to 63 chars (`workshop-01`, `reception`, `lab-a-07`). Tick **auto name from the pool prefix** to leave it to `prefix + host offset` again (`nuc-101`) |
| IP address | The exact address of the pool you want for this unit (`192.168.1.140`). Tick **next free IP of the pool** to un-pin it and let the allocator choose |
| Pool | Which pool it belongs to — the editor shows that pool's *next free* address |

* **Save name / IP** applies the change immediately (validate → persist → no restart, no page reload).
* **Reset to auto** gives the unit the next free IP of the selected pool and lets the name follow it.
* The same address may only exist once: a duplicate name, a duplicate IP, an address belonging to
  another pool, a network/broadcast address or an invalid hostname are all refused (HTTP 400/409).
* Pinned values are stored in `inventory.csv` (the `pinned` column, `ip` / `name` / `ip,name`) and in
  `ip_pool.csv` when that MAC is also listed there, so a restart reproduces them exactly.
* `client/configure_network.sh` re-queries `POST /api/register` by MAC in the post-run hook, so a unit
  that is being imaged while you edit the dashboard still gets the **new** name/IP. Editing the identity
  of a unit that already reported `CONFIGURED` sets it back to `ASSIGNED` (it has to be re-imaged to pick
  the change up).


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
compatibility, `start`/`max`/`network` are the canonical names. The response also carries
`read_only`, `predefined` (the MAC pre-assignments), `pool_file` (where that table is stored) and
`config_path`.

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

### `POST /api/device/update` (alias `/api/device/identity`)
Set the computer name and/or the exact IP of one registered unit - this is what the *Edit name / IP*
button calls. Every field is optional:
```json
{"mac": "aa:bb:cc:dd:ee:01", "hostname": "workshop-01"}                 # custom computer name
{"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.140"}                     # exact IP (name follows it when it was never pinned)
{"mac": "aa:bb:cc:dd:ee:01", "hostname": "", "auto_hostname": true}     # back to prefix+offset
{"mac": "aa:bb:cc:dd:ee:01", "pool": "office", "auto_ip": true}         # next free IP of another pool
```
The response repeats everything the hook needs (`assigned_ip`, `assigned_hostname`, `netmask`, `cidr`,
`gateway`, `dns`, `pool`) plus `pinned` (`ip`, `name` or `ip,name`), the effective `device` record and a
`notes` array (e.g. `pool 'office' range widened to ...` when a hand-picked address sat outside the range).

### `POST /api/predefine/save` / `POST /api/predefine/delete`
Reserve an exact IP + computer name for a MAC that has not booted yet (this is the dashboard's
*Fixed IP & name per unit* panel, and it writes `ip_pool.csv`):
```json
{"mac": "aa:bb:cc:dd:ee:07", "ip": "192.168.1.140", "hostname": "reserved-07"}
{"mac": "aa:bb:cc:dd:ee:07"}                     # to /api/predefine/delete -> remove the reservation
```
`gateway`, `netmask` and `dns` are derived from the pool that owns the address (they can be sent
explicitly for an address outside every pool). Removing a reservation frees the address again.

### `GET /api/bench` / `POST /api/bench/adopt`
`GET /api/bench` reports the IPv4 networks detected on this PC (`ip -4 -o addr`, then ioctl;
gateway from `ip route`, then `/proc/net/route`) and a `warnings` list when a configured pool is not
one of those networks. `POST /api/bench/adopt` with

```json
{"address": "192.168.169.250", "prefix": 24, "gateway": "192.168.169.254"}
```

rewrites the **default** pool onto that network: subnet, gateway, cursor and the master/server IP.
The old range numbers (101–250) are kept when they still fit. The gateway and this PC's own address
are reserved, so they are never handed to a NUC. The result is persisted to `network_config.json`.
This is the **Use this PC's network** button. Already-registered units keep the addresses they have.

### `GET /api/dashboard`
The fragment endpoint the auto-refresh toggle uses - it returns the same numbers/tables the page was
rendered with, so the tables can be updated without reloading:
```json
{"stats": {"total": 12, "target": 150, "keys_found": 5, "completed": 3, "pinned": 2, "pools": 2,
           "default_pool": "office", "progress_total": 8.0, "progress_keys": 41.7, "progress_completed": 25.0},
 "pool_cards": "<div class=\"col-md-4 ...\">...</div>", "rows": "<tr>...</tr>",
 "predefined_rows": "<tr>...</tr>", "predefined": [ ... ],
 "banners": "<div class=\"alert ...\">...</div>",
 "bench": {"ok": false, "networks": [ ... ], "warnings": [ "Pool 'default' uses ..." ], "primary": { ... }},
 "mismatches": [{"hostname": "nuc-101", "assigned_ip": "192.168.100.101", "seen_ip": "192.168.169.45"}]}
```

`banners` is the bench warning plus any "checked in from the wrong address" warning, so auto-refresh
can replace them in place. `seen_ip` / `seen_at` are also columns of `inventory.csv`. The global
setting `boot_agent` (*Keep NUCs in sync on every boot*) is part of `POST /api/config/save` and is
injected into the downloaded client script as `DEPLOY_BOOT_AGENT`.

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

---

## The pool and the bench must be the same network

A NUC can only reach this server if the address it was given is on the same LAN as the master PC.
The server detects that LAN itself (`ip -4 -o addr`, ioctl if `ip` is missing, default gateway from
`ip route` or `/proc/net/route`) and compares it with every pool.

If they differ, startup prints a line like:

```text
[!] Pool 'default' uses 192.168.100.0/24, but this PC is only on 192.168.169.0/24 (eth0 192.168.169.250)
```

and the dashboard shows the same warning with **Use this PC's network**. That button (or
`POST /api/bench/adopt`) rewrites the default pool:

| | Before | After adopt of `192.168.169.250/24`, gateway `.254` |
|---|---|---|
| Subnet | `192.168.100.0/24` | `192.168.169.0/24` |
| Gateway | `192.168.100.1` | `192.168.169.254` |
| Master / server IP | `192.168.100.1` | `192.168.169.250` |
| Next NUC | `192.168.100.101` | `192.168.169.101` |

The old range numbers are kept when they fit (101–250 still fits a /24). The gateway and this PC's
own address are reserved, so the allocator will never give either of them to a unit. Other pools are
left alone. Units already in `inventory.csv` keep the addresses they were given — edit those by hand
if they were assigned on the old network.

Two things the software cannot do for you:

1. **Take the NUC range out of the router's DHCP pool** (or unplug the bench from that router).
   Otherwise the router can hand `192.168.169.101` to a phone while the dashboard thinks a NUC owns it.
2. **Keep the master on a fixed address.** It is written into every `ocs_prerun` / `ocs_postrun`
   command. A DHCP address on the master is a batch that cannot phone home tomorrow.

Clonezilla's built-in DHCP only covers the PXE boot and then stops. FileZilla is an FTP server: no
DHCP service, no IP pool. Neither of them is what assigns the address the deployed image keeps.

---

## Troubleshooting: the NUC keeps a different address

Symptom: the dashboard assigned `192.168.169.101` / `nuc-101`, but the unit comes up as something
else (often another `192.168.169.x` from the router). The **Seen as** column shows a red cross and
the address it actually used; the banner names the unit (`nuc-101 checked in from 192.168.169.45
instead of its assigned address 192.168.169.101`). A green check means the check-in came from the
assigned address.

Why it happens:

* The cloned image still had DHCP enabled, so on first boot the NIC asked the router and kept that
  lease. `configure_network.sh` now moves netplan files (including cloud-init's) to
  `/etc/netplan/mgw-backup`, disables systemd-networkd `DHCP=yes` profiles, sets NetworkManager
  `autoconnect=false` on `method=auto` profiles, strips ifupdown DHCP stanzas, neutralises
  `interfaces.d` snippets, and disables cloud-init's network stage. The static file matches the NIC
  by MAC, not by `en*`.
* The pool was a different network from the bench, so the "assigned" address was never reachable.
  Adopt this PC's network (section above) and re-image, or edit the unit.
* The router still owns the same range. Exclude it, or isolate the switch.

Re-applying without a full re-image:

```bash
# on the NUC, or against a mounted root with DEPLOY_TARGET_ROOT=
curl -fsS http://192.168.169.250:8000/client/configure_network.sh | \
  env DEPLOY_MODE=live bash
```

`DEPLOY_MODE=live` re-applies the assignment and the DHCP neutralisation, idempotently
(`files changed: 0` when nothing differs). It does **not** wipe `machine-id` or SSH host keys —
that only happens in image mode (the Clonezilla post-run). Other switches: `DEPLOY_APPLY=0` (print
only), `DEPLOY_IFACE=`, `DEPLOY_MAC=`, `DEPLOY_TARGET_ROOT=`, `DEPLOY_CONFIG_FILE=`.

With **Keep NUCs in sync on every boot** left on, the post-run hook installs:

* `/etc/mgw-deploy.conf` — server address, MAC, interface
* `/usr/local/sbin/mgw-identity.sh` — downloads the current hook and runs it in live mode
* `mgw-identity.service` — oneshot, `network-online.target`

An IP or computer name changed in the dashboard then lands at the next boot. Turn the setting off
and the next boot's downloaded script removes the agent (`DEPLOY_BOOT_AGENT=0`).
