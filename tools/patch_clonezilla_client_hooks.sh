#!/usr/bin/env bash
# ==============================================================================
# Inject the MGW_Kompy pre-run / post-run hooks into the boot menu that the
# Clonezilla *lite server* hands to the PXE clients.
#
# Run this ON THE MASTER, inside Clonezilla live, AFTER the lite server has been
# started (lite-server -> Start -> massive-deployment -> ... -> the "now wait for
# client to connect" prompt). Restarting the lite server regenerates both files
# below, so re-run this afterwards.
#
#   sudo ./patch_clonezilla_client_hooks.sh                        # defaults
#   sudo ./patch_clonezilla_client_hooks.sh http://192.168.169.2:8000
#   sudo ./patch_clonezilla_client_hooks.sh --self-test            # parse check only
#   sudo ./patch_clonezilla_client_hooks.sh --restore              # undo
#   sudo ./patch_clonezilla_client_hooks.sh --tftpdir /tmp/fake    # try it on a copy
#
# What it patches, and why both files:
#   $TFTPDIR/grub/grub.cfg            uEFI netboot clients (the menuentry
#                                     --id clonezilla-live-client, i.e. the line
#                                     carrying ocs_live_run="ocs-live-get-img <srv>")
#   $TFTPDIR/pxelinux.cfg/default     legacy BIOS netboot clients (the
#                                     "label Clonezilla-live" APPEND line)
# A NUC set to "UEFI Network Boot" reads the first one, a NUC set to "PXE IPv4"
# reads the second. Patching only grub.cfg is the usual reason a batch "does
# nothing": part of it booted a menu that has no hooks.
#
# The edit is idempotent - running it twice never duplicates the hooks, and a
# re-run replaces the previous copy instead of growing the line.
# ==============================================================================
set -uo pipefail

SERVER_URL="${SERVER_URL:-http://192.168.169.2:8000}"
TFTPDIR="${TFTPDIR:-/tftpboot/nbi_img}"
POWEROFF=1
BLIND=0
DO_RESTORE=0
SELF_TEST=0
DRY_RUN=0
QUIET=0
MARKER='MGW_Kompy_hooks_v2'

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  URL | -u, --url URL   server the hooks call (default: $SERVER_URL)
  -t, --tftpdir DIR     nbi_img directory (default: $TFTPDIR)
  --blind-poweroff      power off even when the post-run hook failed
                        (the classic "sleep 3; poweroff" behaviour)
  --no-poweroff         never power off from the hook; the unit stays at a prompt
  --restore             put the .orig files back
  --self-test           build the boot parameters, parse them the way Clonezilla
                        does on the client, and exit (touches no file)
  -n, --dry-run         patch a copy in /tmp and show the diff; changes nothing
  -q, --quiet           less output
  -h, --help            this text
EOF
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -u|--url)           SERVER_URL="$2"; shift 2 ;;
        -t|--tftpdir)       TFTPDIR="$2"; shift 2 ;;
        --blind-poweroff)   BLIND=1; shift ;;
        --no-poweroff)      POWEROFF=0; shift ;;
        --restore)          DO_RESTORE=1; shift ;;
        --self-test)        SELF_TEST=1; shift ;;
        -n|--dry-run)       DRY_RUN=1; shift ;;
        -q|--quiet)         QUIET=1; shift ;;
        -h|--help)          usage 0 ;;
        http://*|https://*) SERVER_URL="$1"; shift ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

SERVER_URL="${SERVER_URL%/}"
GRUB_CFG="$TFTPDIR/grub/grub.cfg"
PXE_CFG="$TFTPDIR/pxelinux.cfg/default"
CLIENT_CONF="/var/www/html/ocs-client-run.conf"

say() { [ "$QUIET" = 1 ] || echo "$@"; }
die() { echo "[!] $*" >&2; exit 1; }

# ---------------------------------------------------------------- the hook text
# NOTE the quoting. Clonezilla's own generated menu writes ocs_live_run=\"...\",
# and parse_cmdline_option (drbl-functions) accepts both that form and the plain
# "..." form, so either works. What does NOT work is a value with spaces and no
# quotes at all: grub then hands the kernel ocs_prerun=curl and everything after
# the first space becomes unrelated boot parameters.
PRERUN="curl -s $SERVER_URL/client/extract_and_register.sh | bash"
POSTRUN=""
POSTRUN1=""
if [ "$POWEROFF" = 1 ]; then
    if [ "$BLIND" = 1 ]; then
        POSTRUN="curl -s $SERVER_URL/client/configure_network.sh | bash"
        POSTRUN1="sleep 3; poweroff"
    else
        # "curl -s ... | bash" is the trap this script exists to avoid: when the
        # master is unreachable curl prints nothing, bash runs nothing, and the
        # exit status is 0 - so the unit powers off looking "done" with no
        # network configured and nothing on the console. Fetch the hook to a
        # file instead (-f makes curl fail on 404/500 too), run it, and only
        # power off when it really succeeded. A failure leaves the machine up
        # for 10 minutes with a message on the console.
        POSTRUN1="curl -fsS --max-time 8 --connect-timeout 3 $SERVER_URL/client/configure_network.sh"
        POSTRUN1="$POSTRUN1 -o /tmp/mgw_hook.sh && bash /tmp/mgw_hook.sh; mgw_rc=\$?"
        POSTRUN1="$POSTRUN1; if [ \$mgw_rc -eq 0 ]; then sleep 3; poweroff"
        POSTRUN1="$POSTRUN1; else echo MGW_Kompy post-run FAILED - not powering off; sleep 600; fi"
    fi
fi

hooks_line() {
    printf '%s' "ocs_prerun=\"$PRERUN\""
    [ -n "$POSTRUN" ]  && printf '%s' " ocs_postrun=\"$POSTRUN\""
    [ -n "$POSTRUN1" ] && printf '%s' " ocs_postrun1=\"$POSTRUN1\""
    printf '%s' " $MARKER"
}
HOOKS="$(hooks_line)"

# -------------------------------------------------------------------- self test
# Parse the generated line exactly the way Clonezilla does on the client
# (parse_cmdline_option, drbl-functions), so a value that would arrive empty or
# truncated is caught here on the master instead of on 20 dark NUCs.
self_test() {
    local cmdline k tmp val rc=0
    cmdline="$(mktemp)"
    printf 'initrd=Clonezilla-live-initrd.img boot=live ocs_live_run="ocs-live-get-img 10.0.0.1" %s\n' \
        "$HOOKS" > "$cmdline"
    echo "Kernel command line the client would get:"
    cat "$cmdline"
    echo ""
    for k in ocs_prerun ocs_postrun ocs_postrun1 ocs_postrun2; do
        grep -q "$k=" "$cmdline" || continue
        tmp="$(mktemp)"
        if LC_ALL=C grep -Eq "($k=\"|$k=\\\\\")" "$cmdline"; then
            LC_ALL=C grep -oE -- "$k=(\"|\\\\\")[^\"]*(\"|\\\\\")([[:space:]]|$)+" "$cmdline" \
                | sed -r -e "s|=\\\\\"|=\"|g" -e "s@\\\\\"([[:space:]]|$)+@\"@g" \
                | sed -r -e 's|^\"(.*)=|\1=\"|g' > "$tmp"
        else
            LC_ALL=C grep -oE -- "$k=[^[:space:]]*([[:space:]]|$)+" "$cmdline" > "$tmp"
        fi
        # parse_cmdline_option protects "$" before sourcing the temp file
        perl -pi -e 's|\$|\\\$|g' "$tmp"
        # shellcheck disable=SC1090
        . "$tmp" 2>/dev/null
        eval "val=\${$k}"
        if [ -z "$val" ]; then
            echo "  [FAIL] $k parsed to an empty value"
            rc=1
        else
            echo "  [ok]   $k -> $val"
        fi
        rm -f "$tmp"
    done
    rm -f "$cmdline"
    echo ""
    [ "$rc" -eq 0 ] && echo "[+] every hook survives grub -> /proc/cmdline -> parse_cmdline_option"
    exit "$rc"
}
[ "$SELF_TEST" = 1 ] && self_test

# ------------------------------------------------------------------- utilities
targets() {
    [ -f "$GRUB_CFG" ] && echo "$GRUB_CFG"
    [ -f "$PXE_CFG" ]  && echo "$PXE_CFG"
    return 0
}

# The line that actually boots the multicast client. In grub.cfg it starts with
# $linux_cmd (a grub variable, literal in the file); in pxelinux.cfg it is the
# APPEND line of the "label Clonezilla-live" block.
target_line_regex() {
    case "$1" in
        *grub.cfg) echo '\$linux_cmd.*ocs-live-get-img|ocs-live-get-img.*\$linux_cmd' ;;
        *)         echo 'ocs-live-get-img' ;;
    esac
}

backup_once() {
    local f="$1"
    [ -e "$f.orig" ] || cp -a "$f" "$f.orig" || die "cannot back up $f"
}

restore_one() {
    local f="$1"
    if [ -e "$f.orig" ]; then
        cp -a "$f.orig" "$f" && say "[*] restored $f from $f.orig"
    else
        say "[!] no $f.orig - nothing to restore for $f"
    fi
}

# Remove a previous copy of our own hooks so the boot line never grows twice.
strip_old_hooks() {
    local f="$1"
    # This version tags its hooks with $MARKER, so everything from the last
    # ocs_prerun= up to the marker is ours and goes as one block.
    MGW_MARKER="$MARKER" perl -i -pe 's{[[:space:]]*ocs_prerun=.*\Q$ENV{MGW_MARKER}\E}{}' "$f"
    # Anything an earlier, unmarked hand-made one-liner left behind. Quoted with
    # {} because the values contain both " and / characters.
    perl -i -pe 's{[[:space:]]*ocs_prerun="curl -s [^"]*extract_and_register\.sh[^"]*"}{};' \
                -e 's{[[:space:]]*ocs_postrun="curl -[^"]*configure_network\.sh[^"]*"}{};' \
                -e 's{[[:space:]]*ocs_postrun1="[^"]*"}{};' \
                -e 's{[[:space:]]*ocs_postrun2="[^"]*"}{}' "$f"
}

patch_one() {
    local f="$1" rx n
    rx="$(target_line_regex "$f")"
    n="$(grep -Ec "$rx" "$f")"
    say ""
    say "[*] $f"
    if [ "$n" -eq 0 ]; then
        say "    NO line in this file boots the multicast client, so there is"
        say "    nothing to patch here. Menu entries found:"
        grep -Eo '^[[:space:]]*menuentry[[:space:]]+[^{]*|--id [A-Za-z0-9+_-]+|^[[:space:]]*label .*' "$f" \
            | sed 's/^/      /' | head -20
        say "    ocs_live_run values found:"
        grep -Eo 'ocs_live_run=\\?"?[^ "\\]*' "$f" | sed 's/^/      /' | head -10
        say "    -> start the lite server in massive-deployment mode, then re-run."
        return 1
    fi
    say "    $n client boot line(s) match"
    strip_old_hooks "$f"
    backup_once "$f"
    MGW_HOOKS="$HOOKS" MGW_RX="$rx" perl -i -pe '
        if (m{^(?!\s*#)} && /$ENV{MGW_RX}/ && !/extract_and_register\.sh/) {
            chomp; $_ .= " $ENV{MGW_HOOKS}\n";
        }
    ' "$f"
    if grep -q 'extract_and_register\.sh' "$f"; then
        if [ "$QUIET" != 1 ]; then
            say "    patched with:"
            grep -o 'ocs_prerun="[^"]*"\|ocs_postrun[0-9]*="[^"]*"' "$f" | sed 's/^/      /'
        fi
        return 0
    fi
    say "    [!] PATCH DID NOT LAND - see the diagnostics above"
    return 1
}

verify_client_conf() {
    [ -f "$CLIENT_CONF" ] || return 0
    say ""
    say "[*] $CLIENT_CONF - what each client runs once it has booted:"
    sed 's/^/      /' "$CLIENT_CONF"
    if grep -Eq -- '-p[= ](poweroff|reboot)\b' "$CLIENT_CONF"; then
        say ""
        say "    The lite server was started with a poweroff/reboot final action."
        say "    That is carried to the client inside ocs_sr_opt. Clonezilla skips"
        say "    its own poweroff when ocs_postrun is on the kernel command line"
        say "    (ocs-functions, task_postprocessing), so the hooks still get their"
        say "    turn - but ocs-live-final-action powers the unit off again straight"
        say "    after them, whatever they did."
    fi
}

# ------------------------------------------------------------------------ main
[ -n "$(targets)" ] || die "no boot menu found under $TFTPDIR (looked for $GRUB_CFG and $PXE_CFG).
    Start the Clonezilla lite server first (lite-server -> massive-deployment),
    or pass --tftpdir /path/to/nbi_img."

say "=============================================================="
say " MGW_Kompy: injecting client hooks"
say "   server : $SERVER_URL"
say "   tftpdir: $TFTPDIR"
say "   prerun : $PRERUN"
[ -n "$POSTRUN" ]  && say "   postrun: $POSTRUN"
[ -n "$POSTRUN1" ] && say "   postrun1: $POSTRUN1"
say "=============================================================="

if [ "$DRY_RUN" = 1 ] && [ "$DO_RESTORE" != 1 ]; then
    # Work on a copy so the live boot menu is never touched.
    ORIG_GRUB="$GRUB_CFG"; ORIG_PXE="$PXE_CFG"
    TMPDIR_MGW="$(mktemp -d /tmp/mgw_dryrun.XXXXXX)"
    mkdir -p "$TMPDIR_MGW/grub" "$TMPDIR_MGW/pxelinux.cfg"
    [ -f "$ORIG_GRUB" ] && cp -a "$ORIG_GRUB" "$TMPDIR_MGW/grub/grub.cfg"
    [ -f "$ORIG_PXE" ]  && cp -a "$ORIG_PXE"  "$TMPDIR_MGW/pxelinux.cfg/default"
    GRUB_CFG="$TMPDIR_MGW/grub/grub.cfg"
    PXE_CFG="$TMPDIR_MGW/pxelinux.cfg/default"
    say "[*] dry run - patching copies under $TMPDIR_MGW"
fi

rc=0
for f in $(targets); do
    if [ "$DO_RESTORE" = 1 ]; then
        restore_one "$f"
    else
        patch_one "$f" || rc=1
    fi
done

if [ "$DRY_RUN" = 1 ] && [ "$DO_RESTORE" != 1 ]; then
    say ""
    say "[*] diff against the real files:"
    for pair in "$ORIG_GRUB:$TMPDIR_MGW/grub/grub.cfg" "$ORIG_PXE:$TMPDIR_MGW/pxelinux.cfg/default"; do
        real="${pair%%:*}"; copy="${pair##*:}"
        [ -f "$real" ] || continue
        diff -u "$real" "$copy" | sed 's/^/    /'
    done
    say ""
    say "[*] copy left in $TMPDIR_MGW - re-run without -n to apply for real."
    exit "$rc"
fi

if [ "$DO_RESTORE" != 1 ]; then
    verify_client_conf
    say ""
    if [ "$rc" -eq 0 ]; then
        say "[+] Done. Power on ONE NUC and watch two things before the next batch:"
        say "      1. the dashboard at $SERVER_URL - the unit must show up during the"
        say "         PRE-run, before the multicast restore starts. If it never"
        say "         appears, the hooks did not reach the kernel command line, or"
        say "         the NUC cannot reach $SERVER_URL (check the master IP in the"
        say "         dashboard's Network Settings and any firewall on port"
        say "         ${SERVER_URL##*:})."
        say "      2. the NUC console - configure_network.sh prints the IP, hostname"
        say "         and 'files changed' before it finishes. A poweroff with no"
        say "         output at all means the hook never ran."
    else
        say "[!] Not every boot menu could be patched - read the diagnostics above."
        say "    The usual causes: patching before massive-deployment was chosen, or"
        say "    a lite server that was restarted after the patch (it regenerates"
        say "    both files from scratch)."
    fi
fi
exit "$rc"
