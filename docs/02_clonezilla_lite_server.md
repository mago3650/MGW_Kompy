# 02. Clonezilla Lite Server Setup & Multicast Guide

Clonezilla Live comes with a built-in feature called **Clonezilla Lite Server**. It turns any PC or laptop into an automated PXE boot and Multicast deployment server without needing a dedicated Linux server installation.

---

## 1. Physical Hardware Setup

1. **Master / Technician Computer:**
   - Laptop or desktop PC with an Ethernet port.
   - Connected to the switch.
   - Holds your Clonezilla Linux image (on internal drive, external SSD, or USB 3.0 drive).
   - Set a **fixed** IP on this machine (Netmask to match the bench, e.g. `192.168.169.250/24`).
     That address is baked into every hook command. A DHCP lease on the master will go stale and
     the next batch will call a server that is no longer there. Reserve it on the router, or set it
     statically and do not let the router hand it out.
2. **Gigabit Switch:**
   - 24-port Gigabit Ethernet switch (recommended: unmanaged or managed with IGMP snooping enabled).
   - **Crucial:** Keep this switch disconnected from your company/office LAN during imaging to prevent Clonezilla's PXE/DHCP server from interfering with existing office networks.
     If the switch stays attached to a router, take the NUC range (for example `192.168.169.101`–`250`)
     **out of that router's DHCP pool** first. Clonezilla's built-in DHCP only serves the PXE phase and
     stops afterwards. FileZilla, if you use it to copy the image, is an FTP server — it does not hand
     out addresses and it has no IP pool. A NUC that can still ask for DHCP after imaging will take
     whatever the router offers, which is the usual "weird IP".
3. **Target NUCs:**
   - Connect up to 20 NUCs to the switch via Cat5e/Cat6 patch cables.
   - Plug into power.

---

## 2. Launching the Inventory & Key Vault Server

On the master technician machine (running Linux or Windows with Python 3):

```bash
cd MGW_Kompy
./run_server.sh
```
The CLI flags below only seed the **first** run; after that the saved configuration wins:
```bash
python3 server/inventory_server.py \
    --port 8000 \
    --subnet 192.168.100.0/24 \
    --start-ip 101 \
    --gateway 192.168.100.1 \
    --dns 1.1.1.1,8.8.8.8 \
    --prefix nuc-
```

Now open `http://localhost:8000`. In the **Network Settings** panel you set everything else in the
browser - subnet per pool (any CIDR), range start/end, the **default (next) IP**, gateway, DNS,
hostname prefix, the default pool, plus the master IP/port the NUCs use to reach this server.
Nothing there requires editing a file or restarting the server, and the pool the next NUC is
registered into is taken from these values immediately. The panel also shows the exact
`ocs_prerun` / `ocs_postrun` commands to paste into Clonezilla (section 4), with the right
server address already filled in.

Two more panels matter for batch work:

* **Connected NUC Systems** - every unit that has checked in. Press **Edit name / IP** on a row to give
  that single machine a custom computer name and/or an exact IP out of its pool (nothing is random:
  you can decide which NUC becomes `reception-pc` on `192.168.100.140`). The post-run hook re-reads the
  assignment from the server, so a change made while the batch is imaging still lands on that unit.
* **Fixed IP & name per unit** - reserve `MAC -> IP + name` *before* booting a batch (stored in
  `server/ip_pool.csv`). Reserved addresses are skipped by the automatic allocator.

The page does not refresh itself: press **Refresh** for the latest state, or switch **Auto-refresh** on
to poll the tables in place every 5 s while you watch a batch run.

If the pool subnet is not a network this PC actually has an address on, the server prints a warning at
startup and the dashboard shows the same sentence, with a **Use this PC's network** button. That
rewrites the default pool (subnet, range numbers when they still fit, gateway, next address) and the
master IP from the detected interface. Do this before imaging — a pool of `192.168.100.0/24` on a
bench that is really `192.168.169.0/24` gives every NUC an address it can never use to reach you.

**Keep NUCs in sync on every boot** (on by default) is injected into the downloaded post-run script as
`DEPLOY_BOOT_AGENT`. Leave it on if you want a dashboard rename to land the next time the unit powers
on, without re-imaging.

---

## 3. Starting Clonezilla Lite Server

### Step A: Boot Clonezilla Live on the Master PC
1. Insert a Clonezilla Live USB (version 2.7+ or 3.0+ Debian-based or Ubuntu-based AMD64) and boot the Master PC.
2. Select default Clonezilla Live settings (Language: English, Keyboard: keep layout).
3. In the main menu, select:
   ```
   lite-server   Clonezilla lite server
   ```

### Step B: Configure Lite Server
1. **Mode**: Choose `Start` -> `massive-deployment`.
2. **DHCP Service**:
   - Choose: `Use built-in DHCP service`.
   - Clonezilla will automatically assign temporary IP addresses to the NUCs while they boot PXE.
   - That service ends when the PXE session ends. It is not the DHCP server of the deployed image,
     and it is not a substitute for a static assignment. FileZilla is not involved in addressing at all.
3. **Repository (Image Location)**:
   - Select `local_dev` -> choose the partition on your external SSD or USB where your Linux Clonezilla image is stored (e.g. `/home/partimag`).
4. **Action**:
   - Select `restore-disk` (Restore an image to local disk).
   - Select your saved Linux image from the list.
   - Select target disk (e.g., `nvme0n1` for M.2 NVMe SSDs in Intel NUCs, or `sda`).
5. **Deployment Mode**:
   - Choose `multicast`.
   - Choose client trigger mode:
     - `wait-client-numbers`: Set to **`20`** (Clonezilla starts restoring as soon as all 20 NUCs are booted).
     - OR `wait-time`: Set to **`180`** seconds (starts restoring 3 minutes after the first client connects).

---

## 4. Injecting the Pre-Run and Post-Run Hooks

To make Clonezilla automatically extract the Windows keys and configure the static IP, add the following parameters to the client boot command or Clonezilla expert options:

### In Clonezilla Expert Mode:
Use the two commands shown at the bottom of the dashboard's Network Settings panel (they already
contain your configured server IP), or the equivalents below:

When prompted for extra parameters, add:
- **Pre-run command**:
  ```bash
  ocs_prerun="curl -s http://192.168.100.1:8000/client/extract_and_register.sh | bash"
  ```
- **Post-run command**:
  ```bash
  ocs_postrun="curl -s http://192.168.100.1:8000/client/configure_network.sh | bash"
  ```
- **Final action**: Choose `-p poweroff` (automatically shuts down the NUC when completed).

---

## 5. Booting the 20 NUCs

1. Power on each of the 20 NUCs.
2. Press **F10** during boot to bring up the Boot Menu.
3. Select **UEFI Network Boot** (or **PXE IPv4 Boot**).
   *(Tip: In BIOS setup under Boot -> Boot Priority, you can set Network Boot as #1 priority so they boot automatically on power-on).*
4. The NUC boots Clonezilla over the network:
   - **Step 1**: It runs `extract_and_register.sh`:
     - Reads the Windows MSDM Key from BIOS.
     - Sends MAC + Serial + Key to `http://192.168.100.1:8000/api/register`.
     - Your web dashboard shows the NUC appear instantly.
   - **Step 2**: The NUC joins the multicast session and waits for the rest of the batch.
   - **Step 3**: Once all 20 NUCs are ready, Multicast restore runs at full Gigabit speed (~5–7 minutes for all 20).
   - **Step 4**: Clonezilla finishes, runs `configure_network.sh`:
     - Asks the server once more for this MAC's assignment (so dashboard edits win).
     - Injects unique IP (e.g., `192.168.100.101` to `192.168.100.120`), or the exact IP that was
       pinned for this unit in the dashboard.
     - Injects hostname (`nuc-101` to `nuc-120`), or the custom computer name pinned for this unit.
     - Neutralises DHCP in the image and writes the static assignment, matched to this NIC by MAC.
       This includes ConnMan, so an AV Linux image needs no modification before capture: the
       assignment is written as a ConnMan provisioning file plus per-service settings.
     - Resets `machine-id` and SSH host keys, with first-boot regeneration for both systemd and
       SysVinit (AV Linux / MX Linux can boot with either).
     - Installs the on-boot identity agent when that setting is on (as a systemd unit and as a
       SysVinit rc script; whichever init boots runs it).
     - Shuts down.
5. The dashboard turns green for all 20 units.
6. Unplug the 20 finished NUCs, box them, and plug in Batch 2!
