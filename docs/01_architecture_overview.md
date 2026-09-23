# 01. Architecture Overview: Fast 150-NUC Mass Deployment

## The Challenge
- **150 Intel NUCs** currently with Windows installed (unregistered/unactivated or factory OEM).
- Need to **extract and preserve each machine's original Windows product key** before wiping.
- Need to deploy a **custom Clonezilla Linux image** to all 150 machines.
- Need each machine to automatically get a **unique IP address** and **unique hostname** (no network collisions or manual typing).
- Work in batches of **20 computers at a time** using an isolated LAN / switch.

---

## The Solution: Automated Multicast PXE Workflow

Instead of booting 150 machines into Windows, logging in, writing down keys, and plugging 150 USB sticks, this toolkit uses **Clonezilla Lite Server (Multicast PXE)** combined with automated **Pre-Run** and **Post-Run** hooks.

```
                    +------------------------------------------+
                    |   Technician PC / Master Deployment Host |
                    |------------------------------------------|
                    | 1. Clonezilla Lite Server (PXE/Multicast)|
                    | 2. Inventory & Key Server (Port 8000)    |
                    | 3. Image Repository (Linux image)       |
                    +--------------------+---------------------+
                                         |
                       [ 24-Port Gigabit Ethernet Switch ]
                                         |
     +-------------------+---------------+-------------------+-------------------+
     | (Batch 1: 20 NUCs)| (Batch 2: 20 NUCs)| (Batch 3: 20 NUCs)| ... up to 150 NUCs
     v                   v                   v                   v
+---------+         +---------+         +---------+         +---------+
|  NUC #1 |         |  NUC #2 |         |  NUC #3 |         | NUC #20 |
+---------+         +---------+         +---------+         +---------+
```

---

## 4-Step Lifecycle per Batch (10–12 minutes total for 20 NUCs)

### Phase 1: PXE Boot (30 seconds)
1. 20 NUCs are plugged into power and the gigabit switch.
2. Power on -> NUCs boot via PXE network boot from the Clonezilla Lite Server.

### Phase 2: Pre-Run — License & Hardware Harvester (5 seconds)
1. The client executes `extract_and_register.sh` over HTTP:
   - Reads the OEM Windows Key directly from the motherboard's **ACPI MSDM** table (`/sys/firmware/acpi/tables/MSDM`).
   - If absent, checks the internal disk's Windows registry hive.
   - Reads the motherboard **Serial Number**, **UUID**, and **MAC address**.
   - Sends a JSON payload to the technician PC via `POST /api/register`.
2. The technician PC:
   - Saves the Windows key, Serial, and MAC to `inventory.csv`.
   - Allocates the next sequential unique IP (e.g., `192.168.1.101`) and Hostname (`nuc-101`).
   - Returns the configuration to the NUC.

### Phase 3: Multicast Disk Restore (5–7 minutes for all 20 NUCs)
1. Clonezilla restores the Linux system image to all 20 NUCs **simultaneously** via UDP Multicast.
2. Because it is multicast, writing to 20 computers takes the **exact same time** as writing to 1 computer!

### Phase 4: Post-Run — Unique Network Configuration (10 seconds)
1. The client executes `configure_network.sh`:
   - Mounts the newly restored Linux root partition.
   - Writes the unique static IP into Netplan / NetworkManager / interfaces.
   - Sets the assigned hostname in `/etc/hostname` and `/etc/hosts`.
   - **Clears `/etc/machine-id`**: Ensures systemd generates a fresh ID on first boot (prevents DHCP DUID conflicts).
   - Resets SSH host keys.
   - Reports `CONFIGURED` back to the server.
   - Powers off the NUC automatically.

---

## Time Comparison: Manual vs. Automated

| Task | Manual (Per Machine) | 150 Machines Manual | Multicast PXE (Batches of 20) |
|---|---|---|---|
| Windows Key Extraction | 3–5 min | 7.5 – 12.5 hrs | **Automated in background** |
| USB Disk Imaging | 10–15 min | 25 – 37.5 hrs | **5–7 min per batch of 20** |
| IP & Hostname Configuration | 3–5 min | 7.5 – 12.5 hrs | **Automated in background** |
| **Total Operator Time** | ~20 min | **40 – 60 Hours** | **~1.5 to 2 Hours total!** |
