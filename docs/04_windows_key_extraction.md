# 04. Windows Product Key Extraction Technical Guide

## Where Windows Product Keys Live on Intel NUCs

Depending on how Windows was installed, the license key is located in one of two places:

### 1. The UEFI ACPI MSDM Table (Factory OEM Keys)
Almost all Intel NUCs purchased with pre-installed Windows have a hardware-bound OEM digital license stored in the motherboard's ACPI (Advanced Configuration and Power Interface) **MSDM** (Microsoft Data Management) table.

- **Key Feature:** This key remains burned into the motherboard NVRAM even if the hard drive is completely wiped or replaced!
- **Format:** 29 characters in 5x5 format (`XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`).
- **How Linux reads it:**
  In Linux (and Clonezilla Live environment):
  ```bash
  cat /sys/firmware/acpi/tables/MSDM | tail -c 29
  ```
  Or via `strings`:
  ```bash
  strings /sys/firmware/acpi/tables/MSDM | grep -E '[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}'
  ```

### 2. Windows Registry Hive (Non-OEM / Volume / Retail Keys)
If the NUC was manually installed from a generic Windows ISO without an OEM BIOS key:
- The key or digital license is stored in `C:\Windows\System32\config\SOFTWARE`.
- Specifically, the string `BackupProductKeyDefault` or the binary blob `DigitalProductId`.
- `client/extract_and_register.sh` automatically mounts any NTFS partitions on the NUC's internal drive in read-only mode to extract this key before Clonezilla wipes the drive.

---

## Standalone Extraction from Windows (Alternative Option)

If you prefer to extract keys from Windows directly before shutting them down, use `client/extract_key_windows.ps1`.

### Option A: Run from a USB Drive
1. Copy `extract_key_windows.ps1` to a USB drive.
2. Plug into the NUC, open PowerShell as Administrator, and run:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\extract_key_windows.ps1 -CsvPath "D:\nuc_keys.csv"
   ```
   Each NUC will append its MAC, Serial Number, Model, and Windows Key to `nuc_keys.csv` on the USB drive.

### Option B: Run over LAN and POST to the MGW_Kompy Server
If the NUCs are booted in Windows and on the network:
```powershell
.\extract_key_windows.ps1 -ServerUrl "http://192.168.1.100:8000"
```
The key and hardware information will appear immediately on the web dashboard.
