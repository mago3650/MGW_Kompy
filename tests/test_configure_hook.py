#!/usr/bin/env python3
"""
Exercise client/configure_network.sh against a fake root filesystem.

No root, no server, no third-party dependencies:
    python3 tests/test_configure_hook.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "client" / "configure_network.sh"

ASSIGNMENT = {
    "assigned_ip": "192.168.169.101",
    "assigned_hostname": "nuc-101",
    "netmask": "255.255.255.0",
    "cidr": "24",
    "gateway": "192.168.169.254",
    "dns": "1.1.1.1, 8.8.8.8",
}
MAC = "aa:bb:cc:dd:ee:01"
IFACE = "eth0"


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def plant_dhcp_image(root):
    """An image that still has DHCP in all five stacks, plus a cloned identity."""
    write(root / "etc/os-release", "ID=ubuntu\n")
    write(root / "etc/hostname", "clone\n")
    write(root / "etc/hosts", "127.0.0.1 localhost\n127.0.1.1 clone\n")
    write(root / "etc/machine-id", "0123456789abcdef0123456789abcdef\n")
    write(root / "var/lib/dbus/machine-id", "0123456789abcdef0123456789abcdef\n")
    write(root / "etc/ssh/ssh_host_rsa_key", "SECRET-RSA\n", 0o600)
    write(root / "etc/ssh/ssh_host_rsa_key.pub", "SECRET-RSA-PUB\n")
    write(root / "etc/ssh/ssh_host_ed25519_key", "SECRET-ED\n", 0o600)
    # 1. netplan, including cloud-init's file
    write(root / "etc/netplan/00-installer-config.yaml",
          "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n")
    write(root / "etc/netplan/50-cloud-init.yaml",
          "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n")
    # 2. systemd-networkd
    write(root / "etc/systemd/network/10-dhcp.network",
          "[Match]\nName=eth0\n\n[Network]\nDHCP=yes\n")
    write(root / "etc/systemd/network/20-static.network",
          "[Match]\nName=eth1\n\n[Network]\nAddress=10.0.0.5/24\nDHCP=no\n")
    # 3. NetworkManager
    write(root / "etc/NetworkManager/system-connections/Wired.nmconnection",
          "[connection]\nid=Wired\ntype=ethernet\nautoconnect=true\n\n"
          "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n", 0o600)
    write(root / "etc/NetworkManager/system-connections/already-manual.nmconnection",
          "[connection]\nid=manual\ntype=ethernet\nautoconnect=true\n\n"
          "[ipv4]\nmethod=manual\naddresses1=10.1.1.5/24,10.1.1.1\n", 0o600)
    # 4. ifupdown
    write(root / "etc/network/interfaces",
          "auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n    hostname clone\n\n")
    # 5. interfaces.d snippet
    write(root / "etc/network/interfaces.d/eth1",
          "auto eth1\niface eth1 inet dhcp\n")
    write(root / "etc/cloud/cloud.cfg", "network:\n  config: enabled\n")
    return root


def run_hook(root, mode="image", extra=None, assignment=None):
    cfg = root / "assignment.json"
    cfg.write_text(json.dumps(assignment or ASSIGNMENT))
    env = os.environ.copy()
    # The parent environment must not leak a server address or a mode into the hook.
    for key in ("DEPLOY_SERVER", "DEPLOY_PORT", "DEPLOY_POOL", "DEPLOY_BOOT_AGENT",
                "DEPLOY_MODE", "DEPLOY_APPLY", "DEPLOY_MAC", "DEPLOY_IFACE",
                "DEPLOY_TARGET_ROOT", "DEPLOY_CONFIG_FILE"):
        env.pop(key, None)
    env.update({
        "DEPLOY_MODE": mode,
        "DEPLOY_TARGET_ROOT": str(root),
        "DEPLOY_CONFIG_FILE": str(cfg),
        "DEPLOY_MAC": MAC,
        "DEPLOY_IFACE": IFACE,
        "DEPLOY_APPLY": "1",
    })
    if extra:
        env.update(extra)
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return proc


def active_files(root):
    """Files that a boot would still read (backups and *.disabled are not active)."""
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if "mgw-backup" in rel or rel.endswith(".disabled") or rel.endswith(".network.disabled"):
            continue
        if rel == "assignment.json":
            continue
        found.append(path)
    return found


def fingerprint(path):
    data = path.read_bytes() if path.exists() else b"<missing>"
    return hashlib.sha256(data).hexdigest(), data


def test_image_mode_neutralises_every_dhcp_source():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        proc = run_hook(root, mode="image")
        assert proc.returncode == 0, proc.stdout + proc.stderr

        static = (root / "etc/netplan/01-mgw-static.yaml").read_text()
        assert "192.168.169.101/24" in static
        assert "192.168.169.254" in static
        assert "dhcp4: false" in static
        assert "dhcp6: false" in static
        # the old DHCP yaml files survive only as backups
        assert not (root / "etc/netplan/00-installer-config.yaml").exists()
        assert not (root / "etc/netplan/50-cloud-init.yaml").exists()
        assert (root / "etc/netplan/mgw-backup/00-installer-config.yaml").is_file()
        assert (root / "etc/netplan/mgw-backup/50-cloud-init.yaml").is_file()

        assert not (root / "etc/systemd/network/10-dhcp.network").exists()
        assert (root / "etc/systemd/network/10-dhcp.network.disabled").is_file()
        # a DHCP=no file for another NIC is not our business
        assert "DHCP=no" in (root / "etc/systemd/network/20-static.network").read_text()

        wired = (root / "etc/NetworkManager/system-connections/Wired.nmconnection").read_text()
        assert "autoconnect=false" in wired
        assert "method=auto" in wired  # left in place, just not allowed to connect
        manual = (root / "etc/NetworkManager/system-connections/already-manual.nmconnection").read_text()
        assert "autoconnect=true" in manual and "method=manual" in manual

        interfaces = (root / "etc/network/interfaces").read_text()
        assert "inet dhcp" not in interfaces
        assert "inet loopback" in interfaces
        assert not (root / "etc/network/interfaces.d/eth1").exists()
        assert "inet dhcp" in (root / "etc/network/interfaces.d/mgw-backup/eth1").read_text()

        disabled = (root / "etc/cloud/cloud.cfg.d/99-mgw-disable-network.cfg").read_text()
        assert "network: {config: disabled}" in disabled

        assert (root / "etc/hostname").read_text().strip() == "nuc-101"
        assert "127.0.1.1    nuc-101" in (root / "etc/hosts").read_text()

        # Nothing a boot would still read may offer DHCP. The assignment is the survivor.
        for path in active_files(root):
            text = path.read_text(errors="replace")
            rel = str(path.relative_to(root))
            assert "dhcp4: true" not in text and "dhcp4: yes" not in text, rel
            assert "DHCP=yes" not in text and "DHCP=YES" not in text, rel
            assert "inet dhcp" not in text and "inet6 dhcp" not in text, rel
            if "method=auto" in text or "method=dhcp" in text:
                assert "autoconnect=false" in text, rel
    finally:
        shutil.rmtree(root)


def test_static_config_matches_the_nic_by_mac():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        proc = run_hook(root, mode="image")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        netplan = (root / "etc/netplan/01-mgw-static.yaml").read_text()
        assert 'macaddress: "%s"' % MAC in netplan
        assert "name: \"en*\"" not in netplan
        # command-substitution used to glue dhcp4 onto the macaddress line
        for line in netplan.splitlines():
            if "macaddress:" in line:
                assert "dhcp" not in line, line
        assert any(line.strip() == "dhcp4: false" for line in netplan.splitlines())
        nm = (root / "etc/NetworkManager/system-connections/mgw-static.nmconnection").read_text()
        assert "mac-address=%s" % MAC.upper() in nm
        assert "method=manual" in nm
        assert "192.168.169.101/24" in nm
        assert "autoconnect=true" in nm
    finally:
        shutil.rmtree(root)


def test_boot_agent_install_and_uninstall():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        proc = run_hook(root, mode="image", extra={
            "DEPLOY_BOOT_AGENT": "1",
            "DEPLOY_SERVER": "192.168.169.250",
            "DEPLOY_PORT": "8000",
        })
        assert proc.returncode == 0, proc.stdout + proc.stderr
        agent = root / "usr/local/sbin/mgw-identity.sh"
        unit = root / "etc/systemd/system/mgw-identity.service"
        wants = root / "etc/systemd/system/multi-user.target.wants/mgw-identity.service"
        conf = root / "etc/mgw-deploy.conf"
        assert agent.is_file() and os.access(agent, os.X_OK)
        assert "DEPLOY_MODE=live" in agent.read_text()
        assert unit.is_file()
        assert "ExecStart=/usr/local/sbin/mgw-identity.sh" in unit.read_text()
        assert "WantedBy=multi-user.target" in unit.read_text()
        assert wants.is_symlink()
        assert os.readlink(wants) == "/etc/systemd/system/mgw-identity.service"
        text = conf.read_text()
        assert "DEPLOY_SERVER=192.168.169.250" in text
        assert "DEPLOY_MAC=%s" % MAC in text
        assert (root / "usr/local/sbin/mgw-configure-network.sh").is_file()

        proc = run_hook(root, mode="live", extra={"DEPLOY_BOOT_AGENT": "0"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        for path in (agent, unit, wants, conf, root / "usr/local/sbin/mgw-configure-network.sh"):
            assert not path.exists(), path
        # uninstalling the agent must not undo the assignment
        assert "192.168.169.101/24" in (root / "etc/netplan/01-mgw-static.yaml").read_text()
    finally:
        shutil.rmtree(root)


def test_live_mode_is_idempotent():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        first = run_hook(root, mode="live", extra={"DEPLOY_BOOT_AGENT": "1",
                                                   "DEPLOY_SERVER": "192.168.169.250"})
        assert first.returncode == 0, first.stdout + first.stderr
        assert "files changed: 0" not in first.stdout.splitlines()[-3:], first.stdout
        second = run_hook(root, mode="live", extra={"DEPLOY_BOOT_AGENT": "1",
                                                    "DEPLOY_SERVER": "192.168.169.250"})
        assert second.returncode == 0, second.stdout + second.stderr
        assert "files changed: 0" in second.stdout, second.stdout
    finally:
        shutil.rmtree(root)


def test_live_mode_never_touches_machine_id_or_ssh_keys():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        watched = [
            root / "etc/machine-id",
            root / "var/lib/dbus/machine-id",
            root / "etc/ssh/ssh_host_rsa_key",
            root / "etc/ssh/ssh_host_rsa_key.pub",
            root / "etc/ssh/ssh_host_ed25519_key",
        ]
        before = {path: fingerprint(path) for path in watched}
        proc = run_hook(root, mode="live")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "leaving machine-id and SSH host keys untouched" in proc.stdout
        for path, (digest, data) in before.items():
            again, raw = fingerprint(path)
            assert again == digest, path
            assert raw == data
        # the regen-ssh unit is an image-mode artefact and must not appear
        assert not (root / "etc/systemd/system/regenerate-ssh-keys.service").exists()
    finally:
        shutil.rmtree(root)


def test_image_mode_resets_identity_and_dry_run_writes_nothing():
    root = Path(tempfile.mkdtemp())
    try:
        plant_dhcp_image(root)
        proc = run_hook(root, mode="image")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (root / "etc/machine-id").read_text() == ""
        assert not (root / "var/lib/dbus/machine-id").exists()
        assert not (root / "etc/ssh/ssh_host_rsa_key").exists()
        assert not (root / "etc/ssh/ssh_host_ed25519_key").exists()
        assert (root / "etc/systemd/system/regenerate-ssh-keys.service").is_file()

        # a second image, this time a dry run, must not be touched at all
        other = Path(tempfile.mkdtemp())
        plant_dhcp_image(other)
        snapshot = {p.relative_to(other): p.read_bytes()
                    for p in other.rglob("*") if p.is_file()}
        dry = run_hook(other, mode="image", extra={"DEPLOY_APPLY": "0"})
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "files changed: 0" in dry.stdout, dry.stdout
        assert "192.168.169.101" in dry.stdout
        after = {p.relative_to(other): p.read_bytes()
                 for p in other.rglob("*") if p.is_file() and p.name != "assignment.json"}
        # assignment.json is written by the test harness, not the hook
        snapshot.pop(Path("assignment.json"), None)
        assert after == snapshot
        shutil.rmtree(other)
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL  {name}: {exc}")
                raise
            print(f"ok    {name}")
            passed += 1
    print(f"\n{passed} hook tests passed")
