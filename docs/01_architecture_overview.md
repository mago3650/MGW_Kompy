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
     The subnet, the range and that "next IP" are live settings of the web dashboard (see
     `03_ip_and_network_config.md`), not constants baked into the image or a file.
   - An address or computer name that was **pinned for that MAC in the dashboard** (or reserved before
     the batch booted) always wins over the sequential allocator.
   - Returns the configuration to the NUC.

### Phase 3: Multicast Disk Restore (5–7 minutes for all 20 NUCs)
1. Clonezilla restores the Linux system image to all 20 NUCs **simultaneously** via UDP Multicast.
2. Because it is multicast, writing to 20 computers takes the **exact same time** as writing to 1 computer!

### Phase 4: Post-Run — Unique Network Configuration (10 seconds)
1. The client executes `configure_network.sh`:
   - Re-reads the assignment from the server (`POST /api/register` by MAC), so an exact IP or a custom
     computer name that was pinned in the dashboard *while the NUC was imaging* is applied too
     (the pre-run cache is only the fallback when the server cannot be reached).
   - Mounts the newly restored Linux root partition.
   - **Neutralises every DHCP source in the image** (netplan, including cloud-init; systemd-networkd;
     NetworkManager `method=auto`; ifupdown and `interfaces.d`; cloud-init's network stage; and
     **ConnMan**, the network manager AV Linux / MX Linux ships) and writes a static config matched
     to this NIC by MAC address. Otherwise the bench router can hand the unit a different address on
     first boot, and the dashboard assignment never sticks. An AV Linux image is used exactly as
     captured - nothing in it has to be prepared or changed beforehand.
   - Sets the assigned hostname in `/etc/hostname` and `/etc/hosts`.
   - **Clears `/etc/machine-id`**: a fresh ID is generated on first boot (prevents DHCP DUID
     conflicts) - by systemd, or by an `mgw-firstboot` SysVinit script on images that boot SysVinit.
   - Resets SSH host keys (regenerated on first boot under either init system).
   - Optionally installs the on-boot identity agent (dashboard setting *Keep NUCs in sync on every
     boot*) as both `mgw-identity.service` and a SysVinit rc script, so an IP or name changed later
     is re-applied at the next boot whichever init the image boots with, without re-imaging.
   - Reports `CONFIGURED` back to the server. The server records the address that check-in came from.
   - Powers off the NUC automatically.

The pool the server allocates from has to be the network the master PC is actually plugged into. If it
is not, the dashboard banner and the server log say so, and **Use this PC's network** rewrites the
default pool onto the detected network. See `03_ip_and_network_config.md`.

---

## Time Comparison: Manual vs. Automated

| Task | Manual (Per Machine) | 150 Machines Manual | Multicast PXE (Batches of 20) |
|---|---|---|---|
| Windows Key Extraction | 3–5 min | 7.5 – 12.5 hrs | **Automated in background** |
| USB Disk Imaging | 10–15 min | 25 – 37.5 hrs | **5–7 min per batch of 20** |
| IP & Hostname Configuration | 3–5 min | 7.5 – 12.5 hrs | **Automated in background** |
| **Total Operator Time** | ~20 min | **40 – 60 Hours** | **~1.5 to 2 Hours total!** |
