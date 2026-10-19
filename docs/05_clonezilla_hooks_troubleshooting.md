# 05. PXE hooks: what actually runs on a NUC, and why a unit can power off unconfigured

Everything here was read out of the Clonezilla/DRBL sources
(`stevenshiau/clonezilla` and `stevenshiau/drbl`, `master`) that ship inside
Clonezilla live **3.3.3-37-amd64** - the version in `clonezilla/Clonezilla-Live-Version`.
Line numbers are from those files.

---

## 1. `clonezilla/` in this repo has no `live/` - that is intentional

`clonezilla/` is the extracted Clonezilla live ISO **minus `live/`**. Tracked today:

| | |
|---|---|
| tracked files under `clonezilla/` | 335 |
| size on disk | 18 MB |
| largest tracked file | `clonezilla/boot/grub/efi.img`, 3.5 MB |
| `clonezilla/live/` | **absent, and never committed** (`git log --diff-filter=A -- 'clonezilla/live/*'` is empty) |

`clonezilla/b3sum.txt` lists what is missing - 10 files, all under `./live/`:
`vmlinuz`, `initrd.img`, `filesystem.squashfs`, `filesystem.packages`,
`filesystem.packages-remove`, `Clonezilla-Live-Version`, `freedos.img`,
`ipxe.efi`, `ipxe.lkn`, `mt86+x64` (lines 299-308 of that file).

`filesystem.squashfs` alone is hundreds of MB, past GitHub's 100 MB per-file
limit, so it could not be pushed anyway. **Nothing in this repo reads
`clonezilla/live/`** - there is no reference to it anywhere outside
`b3sum.txt` and the stock boot menus. It is kept as a reference of the boot
tree (grub modules, syslinux, EFI stubs) so the menu files can be inspected.

**The missing `live/` cannot be the reason a NUC fails to get its network.**
The PXE clients never fetch the payload from this repository. When the lite
server starts, `ocs-live-feed-img` copies the kernel, initrd and squashfs from
the *master's running Clonezilla media* (`$LIVE_MEDIA/live/...`) into
`/tftpboot/nbi_img` and `/var/www/html`
(`ocs-live-feed-img` lines 1765-1789), and the client menu it generates points
at those copies. If the master's own media were incomplete, the master would
not have booted in the first place.

---

## 2. The exact order of events on a client

```
PXE / uEFI netboot
  └─ grub reads /tftpboot/nbi_img/grub/grub.cfg
       menuentry --id clonezilla-live-client
       $linux_cmd ... ocs_live_run=\"ocs-live-get-img <master-ip>\" ...
                                    ▲ written by ocs-live-feed-img line 734
  └─ live-boot brings the NIC up (this is the only network the client has)
  └─ ocs-live-run-menu                        (sbin/ocs-live-run-menu)
       line  98  ocs-run-boot-param ocs_prerun     <-- extract_and_register.sh
       line 198  eval $ocs_live_run                <-- ocs-live-get-img <ip>
                     └─ wget http://<ip>/ocs-client-run.conf   (line 91)
                     └─ ocs-sr $ocs_sr_opt <img> <dev>          (line 228)
                          └─ task_postprocessing                (ocs-functions 4166)
       line 224  ocs-run-boot-param ocs_postrun    <-- configure_network.sh
       line 250  ocs-live-final-action $EXIT       <-- poweroff / reboot / prompt
```

Three consequences that matter:

* **`ocs_postrun` runs after the restore, and it runs even when the restore
  failed.** `ocs-live-run-menu` calls it at line 224, outside the
  `if [ "$EXIT" -eq 0 ]` block. A postrun that only makes sense after a good
  restore has to check for itself.
* **`-p poweroff` chosen in the lite-server UI does not kill the hooks.** It is
  baked into `ocs_sr_opt` on the master (`ocs-live-feed-img` line 1717) and
  carried to the client in `/var/www/html/ocs-client-run.conf`. But
  `task_postprocessing` only runs it when no postrun is on the kernel command
  line:

  ```sh
  # ocs-functions, lines 4343-4349
  if [ "$ocs_sr_mode" != "interactive" ] || ! pgrep -f ocs-live-run-menu >/dev/null; then
    # Only when not in interactive mode and no ocs_postrun in /proc/cmdline
    if [ -z "$(grep -Ewo "ocs_postrun[[:digit:]]*=" /proc/cmdline)" ]; then
      run_post_cmd_when_clone_end "$postaction"
    fi
  fi
  ```

  The poweroff you see afterwards is `ocs-live-final-action`, which reads the
  same `postaction` from `/var/lib/clonezilla/ocs-vars` (lines 145-148) - and it
  runs **after** the hooks. If the restore failed, `$EXIT` is non-zero and
  `ocs-live-final-action` sets `final_action="choose"` (line 203), i.e. it stops
  at a prompt instead of powering off.
* **A hook is skipped on a later boot if it already succeeded once.**
  `ocs-run-boot-param` touches `/var/lib/clonezilla/<name>` on success
  (line 123) and prints *"This boot parameter has been successfully run before,
  so skip it"* next time (lines 100-105). That only persists if something wrote
  it into the restored image - normally it does not.

---

## 3. Why a NUC can power off with no network configured

Ranked by how often it is the actual cause.

### 3.1 `curl -s ... | bash` cannot fail

This is the one that produces exactly "it powers off and nothing happened":

```sh
ocs_postrun="curl -s http://192.168.169.2:8000/client/configure_network.sh | bash"
```

When the master is unreachable, `curl -s` prints **nothing**, `bash` reads an
empty script, and the exit status of the pipeline is **0**. Verified here:

```
$ curl -s --max-time 3 http://192.0.2.1:8000/client/configure_network.sh | bash
curl|bash EXIT_CODE=0
```

So `ocs-run-boot-param` considers the hook a success, touches its state file,
and the machine proceeds to `ocs-live-final-action` and powers off. No output,
no error, no network. The same is true of the pre-run hook - if it silently
does nothing, the NUC never appears on the dashboard *and* never gets a cached
assignment, so the post-run has nothing to apply either.

`tools/patch_clonezilla_client_hooks.sh` therefore writes the post-run as

```sh
ocs_postrun1="curl -fsS --max-time 8 --connect-timeout 3 http://<srv>:8000/client/configure_network.sh \
              -o /tmp/mgw_hook.sh && bash /tmp/mgw_hook.sh; mgw_rc=$?; \
              if [ $mgw_rc -eq 0 ]; then sleep 3; poweroff; \
              else echo MGW_Kompy post-run FAILED - not powering off; sleep 600; fi"
```

`-f` makes curl fail on 404/500 too, and the poweroff only happens when the
hook really succeeded. Tested against a local stub server:

| case | result |
|---|---|
| server up, hook exits 0 | `>> BRANCH: poweroff (success path)` |
| server up, hook exits 1 | `MGW_Kompy post-run FAILED - not powering off` |
| HTTP 404 | `MGW_Kompy post-run FAILED - not powering off` |
| server unreachable | `MGW_Kompy post-run FAILED - not powering off` |

### 3.2 The patch never landed

`grep -c 'ocs-live-get-img' $F` is the tell. In a lite-server
massive-deployment menu there is exactly **one** such line - the
`--id clonezilla-live-client` entry. If it prints `0`, the perl substitution
changed nothing and the clients booted a menu with no hooks at all. That
happens when:

* the lite server was started in **interactive-client** mode instead of
  massive-deployment - then `ocs_live_run` is `ocs-live-general` and there is
  no `ocs-live-get-img` line to match;
* the file was edited **before** the lite server generated it;
* the lite server was **restarted** after the patch. `ocs-live-feed-img`
  regenerates `grub.cfg` from scratch every time it runs.

### 3.3 Only the uEFI menu was patched

`/tftpboot/nbi_img/grub/grub.cfg` serves uEFI netboot clients. Legacy BIOS
clients read `/tftpboot/nbi_img/pxelinux.cfg/default`, whose
`label Clonezilla-live` block carries the same `ocs_live_run` in its `APPEND`
line. A NUC whose boot menu entry says "PXE IPv4" rather than "UEFI Network
Boot" never sees a grub.cfg edit. Patch both.

### 3.4 The client cannot reach the server

The client's address comes from the lite server's own DHCP. In
`start-new-dhcpd` mode the range is derived from the master's own uplink
address (`ocs-live-feed-img` lines 965-968; the closed-LAN default there is
`192.168.169.250`, line 37), so a master on `192.168.169.2` hands out
`192.168.169.x` and the clients can reach it. In `use-existing-dhcpd` mode they
get whatever the office router gives them, which is often a different subnet.

Then check, on the master:

```sh
ip -4 addr                      # is 192.168.169.2 really on an interface?
ss -ltn | grep 8000             # the dashboard binds 0.0.0.0:8000 by default
iptables -L -n | head           # Clonezilla live normally has no firewall rules
```

and from any other machine on the bench switch:

```sh
curl -s http://192.168.169.2:8000/client/configure_network.sh | head -8
```

The first lines must be the injected preamble:

```sh
# ---- MGW_Kompy live configuration (managed in the web dashboard) ----
if [ -z "${DEPLOY_SERVER:-}" ] && [ -z "${1:-}" ]; then DEPLOY_SERVER="192.168.169.2"; fi
: "${DEPLOY_PORT:=8000}"
```

If `DEPLOY_SERVER` there is not the master's real address, fix **Master /
server IP** in the dashboard's Network Settings - it is what gets injected, and
without it both hooks fall back to the client's *default gateway*, which on a
Clonezilla bench is the lite server's DHCP gateway, not your server.

### 3.5 The hook ran but found no target root

`configure_network.sh` in `image` mode mounts the restored partitions looking
for `/etc/os-release` (`/dev/nvme*p*`, then `/dev/sd[a-z][0-9]*`). If it finds
nothing it prints `[ERROR] Could not locate installed Linux root filesystem on
disk!` and exits 1. Reproduced here with the server reachable:

```
[*] Server returned the latest assignment (dashboard edits win).
[*] Applying Target Parameters:
    - Hostname: nuc-101
    - IP      : 192.168.169.101/24
[ERROR] Could not locate installed Linux root filesystem on disk!
EXIT_CODE=1
```

On a real NUC that means the restore did not actually land on the disk the
script can see.

---

## 4. The one-minute diagnosis

| Observation | Conclusion |
|---|---|
| NUC appears on the dashboard **before** the restore starts | pre-run reached the server. Network path and hooks are fine; the problem is later. |
| NUC never appears | hooks are not on the kernel command line, or the NUC cannot reach the server. Check 3.2, 3.3, 3.4. |
| NUC powers off, console showed nothing | `curl -s \| bash` swallowed a failure (3.1) or the hooks are absent (3.2). |
| Console shows the `Applying Target Parameters` block, then `[ERROR] ... root filesystem` | the restore did not land (3.5). |
| Unit stays `REGISTERED` instead of turning `CONFIGURED` | the post-run never finished. `configure_network.sh` POSTs `CONFIGURED` to `/api/status` as its last step. |

On the NUC console, `cat /proc/cmdline` shows exactly what grub handed the
kernel - if `ocs_prerun=` is not in there, no amount of server-side fixing
helps.

---

## 5. Patching the menu the supported way

```sh
sudo tools/patch_clonezilla_client_hooks.sh --self-test    # parse check, touches nothing
sudo tools/patch_clonezilla_client_hooks.sh --dry-run      # diff against a copy
sudo tools/patch_clonezilla_client_hooks.sh                # patch both menus
sudo tools/patch_clonezilla_client_hooks.sh --restore      # undo
```

It patches **both** `grub/grub.cfg` and `pxelinux.cfg/default`, refuses with
diagnostics when the multicast line is missing instead of silently changing
nothing, is idempotent (tagged with `MGW_Kompy_hooks_v2`, re-running replaces
the previous copy), keeps a `.orig` next to each file, and echoes
`/var/www/html/ocs-client-run.conf` so you can see the `ocs_sr_opt` the clients
will run.

### Quoting, because it bites

Clonezilla's own generator writes `ocs_live_run=\"ocs-live-get-img <ip>\"` -
literal backslashes. `parse_cmdline_option` (`drbl-functions` line 3060)
documents all four forms it accepts (lines 3103-3112):

| in grub.cfg | in `/proc/cmdline` |
|---|---|
| `ocs_prerun="sleep 5"` | `"ocs_prerun=sleep 5"` |
| `ocs_prerun='sleep 5'` | `"ocs_prerun=sleep 5"` |
| `ocs_prerun=\"sleep 5\"` | `ocs_prerun=\"sleep 5\"` |
| `ocs_prerun=dhclient` | `ocs_prerun=dhclient` |

Both quoted forms parse to the same value, so the plain `"..."` form this repo
uses is fine - it is reproduced and checked by `--self-test`. What does **not**
work is a value containing spaces with no quotes at all: grub then hands the
kernel `ocs_prerun=curl` and everything after the first space becomes unrelated
boot parameters.

---

## 6. Source files consulted

| file | repo | what it settles |
|---|---|---|
| `sbin/ocs-live-feed-img` | clonezilla | generates the client menu (734), writes `ocs-client-run.conf` (1713-1718), DHCP range (965-968), squashfs/HTTP publishing (1765-1789) |
| `sbin/ocs-live-get-img` | clonezilla | client fetches its job over **HTTP port 80** (91), then runs `ocs-sr` (228) |
| `sbin/ocs-live-run-menu` | clonezilla | hook order: prerun 98, `$ocs_live_run` 198, postrun 224, final action 250 |
| `sbin/ocs-run-boot-param` | clonezilla | runs `ocs_prerun*` / `ocs_postrun*` from `/proc/cmdline`, state file at 123 |
| `sbin/ocs-live-final-action` | clonezilla | `postaction` from `ocs-vars` (145-148), failure -> `choose` (203) |
| `scripts/sbin/ocs-functions` | clonezilla | `task_postprocessing` 4166, poweroff skipped when `ocs_postrun` is present (4343-4349) |
| `scripts/sbin/drbl-functions` | drbl | `parse_cmdline_option` 3060, the four quoting cases 3103-3112, `add_opt_in_grub_efi_cfg_block` 4502 |
