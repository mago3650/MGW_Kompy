# MGW_Kompy: Mass Deployment & Windows Key Harvester for 150 NUCs

Automated high-speed deployment pipeline for **150 Intel NUC computers**:
- **Extracts & archives Windows Product Keys** (from ACPI MSDM BIOS table or Windows disk registry) before wiping.
- **Deploys Clonezilla Linux Image** to **20 computers simultaneously** via Multicast PXE over LAN (takes ~5–7 minutes per batch of 20).
- **Assigns unique static IP addresses and hostnames** automatically to every machine.
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

---

## Repository Structure

```
MGW_Kompy/
├── README.md                          # Quick start and instructions
├── run_server.sh                      # One-click launcher for deployment server
├── server/
│   ├── inventory_server.py            # Central Key Vault, IP Allocator & Web UI
│   ├── ip_pool.csv.template           # (Optional) Predefined MAC-to-IP mapping
│   └── inventory.csv                  # Generated inventory file with all saved keys
├── client/
│   ├── extract_and_register.sh        # Runs in Clonezilla Pre-Run hook
│   ├── configure_network.sh           # Runs in Clonezilla Post-Run hook
│   └── extract_key_windows.ps1        # Alternative Windows PowerShell key extractor
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
3. Configure the Master PC with a static IP: `192.168.100.1 / 255.255.255.0`.

### Step 2: Start the Central Inventory Server
On the Master PC:
```bash
./run_server.sh
```
*(Or run `python3 server/inventory_server.py --port 8000 --subnet 192.168.100 --start-ip 101`)*

**Multiple subnet pools** are supported for different network environments:
```bash
python3 server/inventory_server.py \
    --subnet 192.168.100 --start-ip 1 --gateway 192.168.100.1 \
    --subnet-pool "name=office,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,prefix=off-" \
    --subnet-pool "name=depot,subnet=10.30.1,start=1,max=250,gw=10.30.1.1,prefix=dep-"
```
Edit the `EXTRA_POOLS` array in `run_server.sh` or use `--subnet-pool` on the command line. Devices can be assigned to any pool or reassigned between pools via the Web Dashboard.

Open your browser to: **`http://localhost:8000`** to view the live deployment dashboard.

### Step 3: Launch Clonezilla Lite Server (Multicast PXE)
1. Boot Clonezilla Live on your Master PC (or run it as a server).
2. Select **`lite-server`** -> **`Start`** -> **`massive-deployment`**.
3. Choose **`Use built-in DHCP service`**.
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
   - Each NUC receives its assigned unique IP (`192.168.100.101`, `102`, etc.) and Hostname (`nuc-101`, `nuc-102`, etc.).
   - All 20 NUCs stream the Clonezilla Linux image simultaneously.
   - When finished, network configs are injected and each NUC powers off automatically.

### Step 5: Repeat for Remaining Batches
- Disconnect the 20 completed units.
- Connect the next batch of 20 units.
- Repeat until all 150 units are deployed.
- Download `nuc_inventory.csv` from the dashboard to keep the complete archive of all 150 Windows product keys and hardware serial numbers.
