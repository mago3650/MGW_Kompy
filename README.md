# MGW_Kompy: Mass Deployment & Windows Key Harvester for 150 NUCs

Automated high-speed deployment pipeline for **150 Intel NUC computers**:
- **Extracts & archives Windows Product Keys** (from ACPI MSDM BIOS table or Windows disk registry) before wiping.
- **Deploys Clonezilla Linux Image** to **20 computers simultaneously** via Multicast PXE over LAN (takes ~5–7 minutes per batch of 20).
- **Assigns unique static IP addresses and hostnames** automatically to every machine - and lets you
  **pin an exact IP and a custom computer name to any single NUC** (before or after it boots).
- **Stops DHCP from stealing that address.** The post-run hook neutralises netplan, systemd-networkd,
  NetworkManager, ifupdown and cloud-init DHCP in the cloned image, and can install an on-boot agent
  so a later dashboard edit lands at the next boot.
- **Wipes `/etc/machine-id` and resets SSH keys** to eliminate network ID conflicts.
- **Live Web Dashboard & CSV export** to monitor progress and archive license keys.

---

## Quick Workflow Summary

```
   [20 NUCs connected to 24-Port Switch]
                    │
                    ▼
           1. PXE Boot Clonezilla
                    │
                    ▼
  2. Pre-Run: Read ACPI MSDM Windows Key + Serial + MAC
     ──> Transmit to Server (Appends to inventory.csv)
                    │
                    ▼
  3. Multicast Restore of Linux Image (5–7 min for all 20)
                    │
                    ▼
  4. Post-Run: Inject unique IP, Hostname, reset machine-id
                    │
                    ▼
  5. Automatic Poweroff ──> Swap with next batch of 20!
```

**Total time for 150 NUCs: Under 2 hours** (compared to 40+ hours manually).

> **Addressing is a runtime setting.** Subnets, IP ranges and the default (next) IP are edited in the
> web dashboard and persisted to `server/network_config.json`; the CLI / `run_server.sh` only seed the
> first run. The dashboard has no login, so keep it on the isolated deployment switch - or start the
> server with `--read-only` (`DEPLOY_READ_ONLY=1 ./run_server.sh`) to lock editing.

---

## Repository Structure

```
MGW_Kompy/
├── README.md                          # Quick start and instructions
├── run_server.sh                      # One-click launcher for deployment server
├── server/
│   ├── inventory_server.py            # Central Key Vault, IP Allocator & Web UI
│   ├── ip_pool.csv.template           # MAC-to-IP/name pre-assignments (editable in the web UI)
│   ├── network_config.json            # Generated: subnets/pools & default IPs (edited on the web UI)
│   └── inventory.csv                  # Generated inventory file with all saved keys
├── client/
│   ├── extract_and_register.sh        # Runs in Clonezilla Pre-Run hook
│   ├── configure_network.sh           # Runs in Clonezilla Post-Run hook
│   └── extract_key_windows.ps1        # Alternative Windows PowerShell key extractor
├── tests/
│   └── test_network_config.py         # python3 tests/test_network_config.py (no dependencies)
└── docs/
    ├── 01_architecture_overview.md   # System architecture & timing
    ├── 02_clonezilla_lite_server.md   # Step-by-step Clonezilla Lite Server guide
    ├── 03_ip_and_network_config.md   # Netplan / NetworkManager IP injection details
    └── 04_windows_key_extraction.md   # ACPI MSDM & Registry key technical guide
```

---

## Step-by-Step Instructions

### Step 1: Equipment Setup
1. Connect a **24-port Gigabit switch** to an isolated workbench (keep it disconnected from the corporate network so DHCP doesn't conflict).
2. Connect your **Master / Technician PC** to the switch.
3. Configure the Master PC with a **fixed** IP on the bench switch, and put that same address in
   **Network Settings -> Master / server IP**. It is baked into every hook command, so a DHCP address
   on the master will break the next batch the moment the lease changes. Give it a reservation on
   the router, or set it statically and leave it alone.
4. The deployment pool **must be the same network the master is actually on**. If the dashboard still
   says `192.168.100.0/24` but `ip -4 addr` on the master shows `192.168.169.250/24`, the NUCs are
   handed addresses that do not exist on the bench: they can never reach this server, and a name/IP
   you change in the dashboard can never reach them. The server says so at startup and in a banner
   (`Pool 'default' uses 192.168.100.0/24, but this PC is only on ...`). Press **Use this PC's network**
   to rewrite the default pool, gateway and master IP from the address this PC really has.
   The shipped seed is `192.168.1.0/24`; a first run can also be seeded with
   `--subnet 192.168.169.0/24 --start-ip 101 --gateway 192.168.169.254 --server-ip 192.168.169.250`.

### Step 2: Start the Central Inventory Server
On the Master PC:
```bash
./run_server.sh
```
Then open **`http://localhost:8000`** and use the **Network Settings** panel: every addressing
detail is edited in the browser - no file editing, no restart, no re-deploy.

| Edit in the dashboard | Notes |
|---|---|
| Subnet of each pool | Any CIDR: `192.168.100.0/24`, `10.10.35.0/24`, `10.30.0.0/16` ... |
| Range start / end | Host offsets (`101`-`250` on a /24, `1`-`60000` on a /16) |
| **Default / next IP** | The address the *next* new NUC receives - type `192.168.100.10` or just `10` |
| Gateway, DNS | Netmask & CIDR are derived from the subnet you enter |
| Hostname prefix | `nuc-` -> `nuc-101`, `off-` -> `off-007` |
| Add / rename / delete pools | Deleting a pool that still has devices asks for confirmation |
| Default pool (star) | Used for devices that do not name a pool |
| Master/server IP, port | Used for the hook commands **and injected into the downloaded client scripts** |
| Fleet target size | Progress bar denominator (150 by default) |
| **Computer name of one unit** | **Edit name / IP** in the NUC table: type any name (`workshop-01`) instead of the pool prefix |
| **Exact IP of one unit** | Same editor: type the address you want (`192.168.1.140`) - or tick *next free IP* to un-pin it again |
| **Fixed IP + name per MAC** | "Fixed IP & name per unit": reserve an address *before* the NUC boots (saved to `ip_pool.csv`) |
| Auto-refresh | **Off by default**; the *Auto-refresh* button polls the tables in place (no page reload) |
| **Keep NUCs in sync on every boot** | On by default. Injected as `DEPLOY_BOOT_AGENT`; the post-run hook installs `mgw-identity.service` so a later rename/IP lands at the next boot |
| **Use this PC's network** | Shown when a pool is not on this PC's network. Rewrites the default pool, gateway and master IP |

Changes are validated (a malformed subnet, a bad gateway/DNS address, a duplicate pool name, an
address outside the pool's own network are all rejected with a clear message; a next IP that is valid
but beyond the configured range simply widens that range and says so), they take effect for the very
next device, and they are persisted to `server/network_config.json` so they survive a restart.

The `EXTRA_POOLS` array in `run_server.sh` and the CLI flags only **seed the first run**; afterwards
the saved file wins
(delete it, or start with `NO_DEPLOY_CONFIG=1`, to re-seed from the script).

Because the client scripts are served by this server, the configured **server IP, port and default
pool are injected on the fly** when a NUC downloads them - you never have to edit
`client/*.sh` either.

**CLI seeding is still supported** (useful for scripted/headless starts):
```bash
python3 server/inventory_server.py --port 8000 --subnet 192.168.100.0/24 --start-ip 101 --target 150
python3 server/inventory_server.py \
    --subnet-pool "name=office,subnet=10.10.35.0/24,start=1,max=250,gw=10.10.35.1,dns=1.1.1.1;8.8.8.8,prefix=off-" \
    --subnet-pool "name=depot,subnet=10.30.0.0/16,start=1,max=60000,gw=10.30.1.1,prefix=dep-"
```
Useful flags: `--config <file>` (where the pools live), `--no-config` (ephemeral),
`--read-only` (lock the web editor), `--server-ip`, `--target`.

Devices can be assigned to any pool or reassigned between pools (even to one specific IP) from the
dashboard or via `POST /api/reassign`. See `docs/03_ip_and_network_config.md` for the full API.

#### Give each NUC exactly the address and name you want (nothing is random)

Automatic allocation is sequential, but you stay in charge of every single unit:

1. **Edit name / IP** - click it on any row of the NUC table to open the editor for that machine:
   * **Computer name** - type anything (`workshop-01`, `reception-pc`, `lab-a-07`). Tick *auto name
     from the pool prefix* to hand the name back to `prefix + host` (`nuc-101`).
   * **IP address** - type the exact address from the pool (`192.168.100.140`). Tick *next free IP of
     the pool* to let the server pick the next free one again. **Reset to auto** does both at once.
   * Saving pins the unit: the address is never handed to another machine, and it stays pinned after a
     server restart. The post-run hook re-reads the assignment from the server, so a change made while
     a batch is imaging still lands on the right NUC.
2. **Fixed IP & name per unit** - reserve `MAC -> IP + name` *before* the NUC is even switched on
   (kept in `server/ip_pool.csv`, the classic MAC-to-IP table). The allocator skips those addresses,
   so a reserved address can never be handed out twice.

Every custom name/address is validated (a duplicate name, a duplicate address, an address outside
the pool, an invalid hostname or a network/broadcast address are all refused with a clear message).
A locked unit is flagged with a small `name` / `ip` badge in the table.

#### Auto-refresh

The dashboard **does not refresh itself by default** - press **Refresh** whenever you want the latest
data. If you would rather watch the deployment live, press **Auto-refresh** (it turns green and polls
every 5 s, updating the counters and tables *in place* - the page is never reloaded, and it pauses
while you are typing in the table). The choice is remembered per browser; `?refresh=on` / `?refresh=off`
in the URL forces it for one visit.

Sanity-check the pool, naming and pinning logic without any hardware:
```bash
python3 tests/test_network_config.py     # 28 checks, stdlib only (also runs under pytest)
python3 tests/test_configure_hook.py     # 6 hook checks: DHCP neutralisation, agent, idempotency
```

### Step 3: Launch Clonezilla Lite Server (Multicast PXE)
1. Boot Clonezilla Live on your Master PC (or run it as a server).
2. Select **`lite-server`** -> **`Start`** -> **`massive-deployment`**.
3. Choose **`Use built-in DHCP service`**.
   That DHCP server exists only for the PXE phase and stops afterwards. It does not keep handing
   addresses out once the NUCs boot the restored image. FileZilla (if you use it to copy images) is
   an FTP server: it has no DHCP and no IP pool. The address a deployed NUC keeps comes from the
   image, which is why the post-run hook has to remove every DHCP source it finds.
4. Select your Linux image from your storage device.
5. Target disk: `nvme0n1` (or `sda`).
6. Mode: **`multicast`** -> client wait: **`20`** (or wait-time: `180` seconds).
7. In Expert options, add the hooks:
   - **`ocs_prerun`**:
     ```bash
     curl -s http://192.168.100.1:8000/client/extract_and_register.sh | bash
     ```
   - **`ocs_postrun`**:
     ```bash
     curl -s http://192.168.100.1:8000/client/configure_network.sh | bash
     ```
   - Final action: `-p poweroff`

### Step 4: Plug & Boot 20 NUCs
1. Plug 20 NUCs into power and the switch.
2. Turn them on and press **F10** (Boot Menu) -> select **UEFI Network Boot / PXE**.
3. Watch the dashboard at `http://localhost:8000`:
   - Each NUC reports its **MAC address**, **Motherboard Serial Number**, and **Windows Product Key**.
   - Each NUC receives its assigned unique IP (`192.168.100.101`, `102`, etc.) and Hostname (`nuc-101`, `nuc-102`, etc.) -
     unless you pinned an exact IP and/or a custom computer name for that unit in the dashboard.
   - All 20 NUCs stream the Clonezilla Linux image simultaneously.
   - When finished, network configs are injected and each NUC powers off automatically.

### Step 5: Repeat for Remaining Batches
- Disconnect the 20 completed units.
- Connect the next batch of 20 units.
- Repeat until all 150 units are deployed.
- Download `nuc_inventory.csv` from the dashboard to keep the complete archive of all 150 Windows product keys and hardware serial numbers.

### Before the first batch: take the NUC range off the router

If the bench switch is still attached to a router, that router is a DHCP server of its own. Clonezilla's
built-in DHCP only covers PXE. After imaging, a NUC left able to ask for DHCP will take whatever the
router offers — often a `192.168.169.x` address that is not the one in the dashboard (the "weird IP").

1. **Take the deployment range out of the router's DHCP pool** before you image (for a pool of
   `192.168.169.101`–`192.168.169.250`, those addresses must not be free for the router to hand out),
   **or isolate the bench switch** from the office LAN entirely.
2. **Reserve the master PC's address** (or set it statically). It is written into every hook command.
3. Watch the **Seen as** column. A green check means the unit checked in from its assigned address; a
   red cross names the address it actually used. The banner names the unit. Details and the five DHCP
   stacks the hook disables are in `docs/03_ip_and_network_config.md`.
