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

### Mode 1: Automatic Sequential Allocation (Default)
No prior configuration required. The server allocates sequential IPs starting from `--start-ip` (default: 101):
- First NUC connected: `192.168.1.101` (`nuc-101`)
- Second NUC connected: `192.168.1.102` (`nuc-102`)
- ...
- 150th NUC connected: `192.168.1.250` (`nuc-250`)

### Mode 2: Pre-defined MAC Mapping Table (`ip_pool.csv`)
If your organization requires specific IP addresses bound to specific physical locations or MAC addresses, edit `server/ip_pool.csv`:

```csv
mac,ip,hostname,gateway,netmask,dns
00:1a:2b:3c:4d:01,10.0.50.101,lab-nuc-01,10.0.50.1,255.255.255.0,10.0.50.2,1.1.1.1
00:1a:2b:3c:4d:02,10.0.50.102,lab-nuc-02,10.0.50.1,255.255.255.0,10.0.50.2,1.1.1.1
```

If a MAC is found in this file, the server returns that explicit assignment. If a MAC is not found, it gracefully falls back to dynamic sequential allocation.
