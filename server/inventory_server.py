#!/usr/bin/env python3
"""
NUC Fleet Deployment & Inventory Server
Part of MGW_Kompy

Provides:
- Centralized Windows Key collection (MSDM ACPI & Registry)
- Automated Sequential or Custom IP/Hostname allocation
- Multiple Subnet Pool support (192.168.1.0/24, 10.10.35.0/24, 10.30.0.0/16 ...)
- Subnet pools, gateways, DNS and the next ("default") IP are edited live in the
  Web Dashboard and persisted to network_config.json - no file/CLI editing needed
- Real-time Web Dashboard for tracking 150 NUC deployments
- Static file delivery for Clonezilla pre-run and post-run scripts
  (client scripts get the current server IP/port/pool/boot-agent auto-injected)
- Records the address each check-in really came from, and warns when a pool
  is not on the same network as this PC
"""

import sys
import os
import re
import json
import csv
import argparse
import threading
import ipaddress
import socket
import struct
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
from html import escape as html_escape

try:
    import fcntl
except ImportError:  # Windows has no ioctl; detection falls back to "nothing found"
    fcntl = None

# Re-entrant: adopt_bench() holds the lock and then calls apply_config(), which
# takes it again. A plain Lock would deadlock that path.
DATA_LOCK = threading.RLock()

CONFIG_VERSION = 1
CONFIG_FILENAME = "network_config.json"

CSV_FIELDS = [
    "timestamp", "status", "assigned_hostname", "assigned_ip",
    "mac", "serial", "model", "windows_key", "key_source",
    "notes", "pool", "gateway", "netmask", "dns", "pinned",
    "seen_ip", "seen_at",
]

# What a hand-edited identity can be pinned to. Kept as a CSV column so a restart
# reproduces exactly the same names/addresses the technician typed.
PIN_IP = "ip"
PIN_NAME = "name"

# A plain RFC-1123 style hostname: letters, digits and '-', 1-63 chars, no leading or
# trailing '-' (dots are refused so a unit can never be named like a domain).
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$")

DEFAULT_SETTINGS = {
    "server_ip": "",          # Address clients use to reach this server
    "port": 8000,             # HTTP port for the dashboard + client hooks
    "hostname_prefix": "nuc-",  # Default prefix for newly created pools
    "target_count": 150,      # Fleet size shown on the dashboard
    "default_pool": "default",  # Pool used when a device asks for no pool
    # Install mgw-identity.service so a dashboard rename/IP lands at next boot.
    "boot_agent": True,
}


def is_ipv4(text):
    """True when `text` is a dotted-quad IPv4 address."""
    try:
        ipaddress.IPv4Address(str(text).strip())
        return True
    except Exception:
        return False


def netmask_to_cidr(netmask, fallback="24"):
    """'255.255.255.0' -> '24' (used for predefined rows that carry a bare netmask)."""
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{str(netmask).strip()}").prefixlen)
    except Exception:
        return fallback


def to_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clean_hostname(value):
    """Validate a computer name typed by a human. Returns it, raises ValueError otherwise."""
    name = str(value or "").strip()
    if not name:
        raise ValueError("hostname is empty")
    if len(name) > 63:
        raise ValueError("hostname is too long (max 63 characters)")
    if not HOSTNAME_RE.match(name):
        raise ValueError(
            f"hostname '{name}' is invalid: use 1-63 letters, digits or '-' "
            f"(no spaces, dots or underscores, and no leading/trailing '-')")
    return name


def clean_mac(value):
    """Normalise 'AA-BB-..' / 'aA:bB:..' to lower-case colon notation."""
    mac = str(value or "").strip().lower().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError(f"MAC address '{value}' is invalid (expected aa:bb:cc:dd:ee:ff)")
    return mac


def parse_pinned(value):
    """'ip,name' -> {'ip', 'name'}; tolerant of the legacy/empty forms."""
    items = {x.strip().lower() for x in str(value or "").replace(";", ",").split(",")}
    return {p for p in (PIN_IP, PIN_NAME) if p in items}


def format_pinned(pins):
    """{'name', 'ip'} -> 'ip,name' (stable order for the CSV / JSON output)."""
    return ",".join(p for p in (PIN_IP, PIN_NAME) if p in (pins or set()))


def coerce_bool(value, default=False):
    """JSON true/false, 1/0, yes/no — what the dashboard checkbox and the CLI send."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _run_cmd(cmd, timeout=3):
    """stdout of a short system command, or '' if it is missing or fails."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def parse_ip_addr(text):
    """Parse `ip -4 -o addr` lines into [{iface, address, prefix}]. Loopback is skipped."""
    found = []
    for line in str(text or "").splitlines():
        match = re.search(
            r"^\d+:\s+(\S+?)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line.strip())
        if not match:
            continue
        iface = match.group(1).rstrip(":")
        if iface == "lo" or iface.startswith("lo"):
            continue
        address = match.group(2)
        if address.startswith("127."):
            continue
        found.append({"iface": iface, "address": address, "prefix": int(match.group(3))})
    return found


def parse_ip_route(text):
    """Parse `ip -4 route` for default gateways. iface -> address, plus '*' for the first."""
    gateways = {}
    for line in str(text or "").splitlines():
        match = re.search(
            r"^default\s+via\s+(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)", line.strip())
        if not match:
            continue
        gateways[match.group(2)] = match.group(1)
        gateways.setdefault("*", match.group(1))
    return gateways


def parse_proc_net_route(text):
    """Parse /proc/net/route (little-endian hex) for the default gateway per iface."""
    gateways = {}
    for line in str(text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        iface, dest, gateway, flags_s = parts[0], parts[1], parts[2], parts[3]
        try:
            flags = int(flags_s, 16)
            if dest != "00000000" or not (flags & 0x2):
                continue
            ip = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
        except (ValueError, OSError, struct.error):
            continue
        if ip == "0.0.0.0":
            continue
        gateways[iface] = ip
        gateways.setdefault("*", ip)
    return gateways


def _addrs_from_ioctl():
    """IPv4 addresses via SIOCGIFADDR / SIOCGIFNETMASK. stdlib only, Linux."""
    if fcntl is None:
        return []
    found = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except OSError:
        return found
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if not name or name == "lo":
                continue
            try:
                packed = struct.pack("16s16s", name.encode()[:15], b"\0" * 16)
                res = fcntl.ioctl(sock.fileno(), 0x8915, packed)       # SIOCGIFADDR
                ip = socket.inet_ntoa(res[20:24])
                maskres = fcntl.ioctl(sock.fileno(), 0x891b, packed)    # SIOCGIFNETMASK
                mask = socket.inet_ntoa(maskres[20:24])
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            except OSError:
                continue
            if ip.startswith("127."):
                continue
            found.append({"iface": name, "address": ip, "prefix": prefix})
    finally:
        sock.close()
    return found


def _gateways_from_proc():
    try:
        with open("/proc/net/route", "r", encoding="utf-8", errors="replace") as handle:
            return parse_proc_net_route(handle.read())
    except OSError:
        return {}


def detect_local_networks():
    """IPv4 networks of this PC.

    Order: `ip -4 -o addr`, then ioctl; gateway from `ip route`, then /proc/net/route.
    Never raises — a bench server with no `ip` still starts.
    """
    try:
        addrs = parse_ip_addr(_run_cmd(["ip", "-4", "-o", "addr"])) or _addrs_from_ioctl()
        gateways = parse_ip_route(_run_cmd(["ip", "-4", "route"])) or _gateways_from_proc()
    except Exception:
        return []
    found = []
    for item in addrs:
        try:
            network = ipaddress.IPv4Network(
                f"{item['address']}/{int(item['prefix'])}", strict=False)
        except (ValueError, TypeError):
            continue
        gateway = gateways.get(item["iface"]) or gateways.get("*") or ""
        found.append({
            "iface": item["iface"],
            "address": item["address"],
            "prefix": int(item["prefix"]),
            "network": str(network),
            "gateway": gateway,
        })
    return found


def primary_network(networks):
    """The network that owns the default route, else the first one detected."""
    for net in networks or []:
        if net.get("gateway"):
            return net
    return (networks or [None])[0]


def describe_networks(networks):
    if not networks:
        return "no IPv4 network"
    return ", ".join(
        f"{net['network']} ({net.get('iface') or '?'} {net['address']})"
        for net in networks)


def format_startup_warning(warning):
    text = str(warning or "").strip()
    if not text:
        return ""
    return text if text.startswith("[!]") else f"[!] {text}"


class SubnetPool:
    """A named IPv4 address pool that hands out sequential addresses.

    A pool is defined by its network in CIDR notation (``192.168.1.0/24``), so any
    size works. ``start_ip`` / ``max_ip`` / ``current_ip_counter`` are host offsets
    counted from the network address - on a /24 that is simply the last octet, which
    keeps the classic "subnet 192.168.1, start 101" mental model working unchanged.
    """

    NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,31}$")
    PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")

    def __init__(self, name, network, start_ip=None, max_ip=None, gateway=None,
                 dns="1.1.1.1,8.8.8.8", hostname_prefix="nuc-", cidr=None,
                 next_offset=None, reserve=None):
        self.name = self._clean_name(name)
        self.network = self.parse_network(network, cidr)
        self.host_limit = max(1, self.network.num_addresses - 2)  # skip network+broadcast
        # /24 keeps the classic 101-250 defaults; smaller subnets start at the
        # first usable host so the whole range stays allocatable.
        default_start = 101 if self.host_limit >= 101 else 1
        default_max = min(250, self.host_limit)
        self.start_ip = self._clean_offset(default_start if start_ip is None else start_ip,
                                          1, self.host_limit, default_start)
        self.max_ip = self._clean_offset(default_max if max_ip is None else max_ip,
                                         1, self.host_limit, default_max)
        if self.max_ip < self.start_ip:
            raise ValueError(
                f"pool '{self.name}': max host ({self.max_ip}) is below start host ({self.start_ip})")

        gw = str(gateway).strip() if gateway else ""
        if not gw or gw.lower() == "auto":
            gw = str(self.network.network_address + 1)
        if not is_ipv4(gw):
            raise ValueError(f"pool '{self.name}': '{gw}' is not a valid gateway address")
        self.gateway = gw

        self.dns = self._clean_dns(dns)
        self.hostname_prefix = self._clean_prefix(hostname_prefix)
        self.reserve = self._clean_reserve(reserve)
        self.adjustments = []
        self.current_ip_counter = self._clean_offset(
            self.start_ip if next_offset is None else next_offset,
            1, self.host_limit + 1, self.start_ip)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _clean_name(name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("pool name is required")
        if not SubnetPool.NAME_RE.match(name):
            raise ValueError(
                f"pool name '{name}' is invalid: use 1-32 letters, digits, space, '.', '_' or '-'")
        return name

    @staticmethod
    def _clean_offset(value, lo, hi, fallback):
        return max(lo, min(hi, to_int(value, fallback)))

    @staticmethod
    def _clean_dns(dns):
        items = [x.strip() for x in str(dns or "").replace(";", ",").split(",") if x.strip()]
        if not items:
            items = ["1.1.1.1", "8.8.8.8"]
        for item in items:
            if not is_ipv4(item):
                raise ValueError(f"'{item}' is not a valid DNS server address")
        return ",".join(items[:3])

    @staticmethod
    def _clean_reserve(reserve):
        """Addresses that must never be handed to a NUC (this PC, the gateway)."""
        if isinstance(reserve, str):
            reserve = reserve.replace(";", ",").split(",")
        items = []
        for item in reserve or []:
            text = str(item).strip()
            if is_ipv4(text) and text not in items:
                items.append(text)
        return items

    @staticmethod
    def _clean_prefix(prefix):
        prefix = str(prefix or "").strip() or "nuc-"
        if not SubnetPool.PREFIX_RE.match(prefix):
            raise ValueError(
                f"hostname prefix '{prefix}' is invalid: use 1-15 letters, digits, '-', or '_'")
        return prefix

    @staticmethod
    def parse_network(value, cidr=None):
        """Accept '192.168.100', '192.168.100.1', '192.168.100.0/24' or '10.30/16'."""
        text = str(value or "").strip()
        if not text:
            raise ValueError("subnet is required (e.g. 192.168.1 or 192.168.1.0/24)")
        if "/" in text:
            text, _, suffix = text.partition("/")
            prefix_len = to_int(suffix, to_int(cidr, 24))
        else:
            prefix_len = to_int(cidr, 24)
        parts = text.split(".")
        if len(parts) < 4 and all(p.isdigit() for p in parts):
            text = text + ".0" * (4 - len(parts))   # '10.30' -> '10.30.0.0'
        text = f"{text}/{prefix_len}"
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a valid subnet ({exc})")
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"'{value}': only IPv4 pools are supported")
        if network.num_addresses < 4:
            raise ValueError(f"{network} is too small to allocate addresses from")
        return network

    # ------------------------------------------------------------- derived data
    @property
    def cidr(self):
        return str(self.network.prefixlen)

    @property
    def netmask(self):
        return str(self.network.netmask)

    @property
    def subnet(self):
        """Legacy human-friendly prefix: '192.168.1' for a /24, '10.30' for a /16."""
        octets = str(self.network.network_address).split(".")
        plen = self.network.prefixlen
        if plen == 24:
            return ".".join(octets[:3])
        if plen == 16:
            return ".".join(octets[:2])
        if plen == 8:
            return octets[0]
        return str(self.network)

    def ip_at(self, offset):
        return str(self.network.network_address + int(offset))

    def contains(self, ip):
        try:
            return ipaddress.IPv4Address(str(ip).strip()) in self.network
        except Exception:
            return False

    def offset_of(self, ip):
        """Host offset of an address inside this pool, or None."""
        try:
            addr = ipaddress.IPv4Address(str(ip).strip())
        except Exception:
            return None
        if addr not in self.network:
            return None
        return int(addr) - int(self.network.network_address)

    # -------------------------------------------------------------- allocation
    def allocate_next(self, taken=None):
        """Return (ip, hostname) for the next free address, skipping `taken` offsets."""
        taken = taken or set()
        limit = min(self.max_ip, self.host_limit)
        offset = max(self.current_ip_counter, self.start_ip)
        while offset <= limit and offset in taken:
            offset += 1
        if offset > limit:
            raise ValueError(
                f"Pool '{self.name}' exhausted ({self.ip_at(self.start_ip)}"
                f"-{self.ip_at(limit)}, {limit - self.start_ip + 1} addresses)")
        self.current_ip_counter = offset + 1
        return self.ip_at(offset), self.hostname_for(offset)

    def hostname_for(self, offset):
        return f"{self.hostname_prefix}{int(offset):03d}"

    def set_next(self, value, strict=False):
        """Point the allocator at the next address to hand out.

        `value` is either a host offset (101) or a full address (192.168.1.140). With
        `strict` (explicit human input) an address inside the subnet but outside the
        current start-max range widens that range - reporting a "widen the range" error
        instead would be a dead end for someone who just wants to start at .10 - while
        anything outside the subnet is rejected. Adjustments are collected in
        `self.adjustments` so the caller can surface them.
        """
        text = str(value).strip()
        self.adjustments = []
        offset = None
        if "." in text or "/" in text:
            offset = self.offset_of(text.split("/")[0])
            if offset is None:
                raise ValueError(f"{text} is not inside {self.network} (pool '{self.name}')")
        if offset is None:
            offset = to_int(text, None)
        if offset is None:
            if strict:
                raise ValueError(f"'{text}' is not a next IP: give an address inside {self.network} "
                                 f"or a host number")
            offset = self.start_ip
        if strict:
            if offset < 1 or offset > self.host_limit:
                raise ValueError(f"host number {offset} is not usable inside {self.network} "
                                 f"(usable range 1-{self.host_limit})")
            if offset < self.start_ip:
                self.start_ip = offset
                self.adjustments.append(f"range start widened to {self.ip_at(offset)}")
            if offset > self.max_ip:
                self.max_ip = offset
                self.adjustments.append(f"range end widened to {self.ip_at(offset)}")
            self.current_ip_counter = offset
            return self.current_ip_counter
        self.current_ip_counter = self._clean_offset(offset, 1, self.host_limit + 1, self.start_ip)
        return self.current_ip_counter

    # ------------------------------------------------------------ serialisation
    def to_config_dict(self):
        """Keys accepted by from_config_dict() - this is what network_config.json holds."""
        data = {
            "name": self.name,
            "network": str(self.network),
            "start": self.start_ip,
            "max": self.max_ip,
            "gateway": self.gateway,
            "dns": self.dns,
            "prefix": self.hostname_prefix,
            "next_offset": self.current_ip_counter,
        }
        if self.reserve:
            data["reserve"] = list(self.reserve)
        return data

    def to_dict(self):
        data = self.to_config_dict()
        limit = min(self.max_ip, self.host_limit)
        data.update({
            # legacy key aliases kept for the docs / external scripts
            "start_ip": self.start_ip,
            "max_ip": self.max_ip,
            "subnet": self.subnet,
            "cidr": self.cidr,
            "netmask": self.netmask,
            "current_ip_counter": self.current_ip_counter,
            "next_ip": self.ip_at(min(self.current_ip_counter, self.host_limit)),
            "next_ip_ready": self.current_ip_counter <= limit,
            "available": max(0, limit - self.current_ip_counter + 1),
            "size": max(0, limit - self.start_ip + 1),
        })
        return data

    @classmethod
    def from_config_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("pool definition must be an object")
        data = {k.lower().strip(): v for k, v in data.items()}
        network = data.get("network") or data.get("subnet") or data.get("cidr_block")
        pool = cls(
            name=data.get("name") or "",
            network=network,
            start_ip=data.get("start", data.get("start_ip")),
            max_ip=data.get("max", data.get("max_ip")),
            gateway=data.get("gateway", data.get("gw")),
            dns=data.get("dns", "1.1.1.1,8.8.8.8"),
            hostname_prefix=data.get("prefix", data.get("hostname_prefix", "nuc-")),
            cidr=data.get("cidr"),
        )
        # The next address may arrive as an offset ("140") or a full IP ("192.168.1.140").
        # An explicit next_ip comes from the web UI, so it must fit inside start-max;
        # a restored next_offset is persisted state and is only clamped.
        explicit = data.get("next_ip")
        if explicit is not None and str(explicit).strip() != "":
            pool.set_next(explicit, strict=True)
        else:
            for key in ("next_offset", "current_ip_counter"):
                raw = data.get(key)
                if raw is not None and str(raw).strip() != "":
                    pool.set_next(raw)
                    break
        pool.reserve = cls._clean_reserve(data.get("reserve", data.get("reserved")))
        return pool

    @staticmethod
    def from_config_string(config_str):
        """Parse 'name=Lab,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,...' format."""
        params = {}
        for part in str(config_str).split(","):
            part = part.strip()
            if "=" in part:
                key, val = part.split("=", 1)
                params[key.strip().lower()] = val.strip()
        if not (params.get("subnet") or params.get("network")):
            raise ValueError(f"Missing 'subnet' in pool config: {config_str}")
        # 'a.b.c' + 'dns=1.1.1.1,8.8.8.8' style DNS lists cannot survive the comma split,
        # so a semicolon is accepted as the separator inside --subnet-pool strings.
        params["dns"] = params.get("dns", "1.1.1.1;8.8.8.8").replace(";", ",")
        return SubnetPool.from_config_dict(params)


class InventoryManager:
    """Owns the records on disk, the subnet pools and their persisted configuration."""

    def __init__(self, output_csv="inventory.csv", pool_csv="ip_pool.csv",
                 config_path=None, settings=None, pools=None, read_only=False):
        self.output_csv = output_csv
        self.pool_csv = pool_csv
        self.config_path = config_path
        self.read_only = read_only

        self.settings = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update(settings)
        self.settings["port"] = to_int(self.settings.get("port"), 8000)
        self.settings["target_count"] = max(1, to_int(self.settings.get("target_count"), 150))

        self.subnet_pools = list(pools) if pools else []
        if not self.subnet_pools:
            raise ValueError("at least one subnet pool must be configured")

        # In-memory store: mac -> record
        self.records = {}
        # Predefined pool mappings: mac -> {ip, hostname, gateway, netmask, dns}
        self.predefined_pool = {}

        # Backwards-compatible aliases pointing at the default pool
        self._sync_aliases()

        self.load_predefined_pool()
        self.load_existing_inventory()

    # ------------------------------------------------------------------ helpers
    def _sync_aliases(self):
        pool = self.default_pool
        self.base_subnet = pool.subnet
        self.current_ip_counter = pool.current_ip_counter
        self.start_ip = pool.start_ip
        self.max_ip = pool.max_ip
        self.gateway = pool.gateway
        self.netmask = pool.netmask
        self.cidr = pool.cidr
        self.dns = pool.dns
        self.hostname_prefix = pool.hostname_prefix

    @property
    def default_pool(self):
        named = self.get_pool_by_name(self.settings.get("default_pool"))
        return named or self.subnet_pools[0]

    def get_pool_by_name(self, pool_name):
        """Find a pool by name (case-insensitive). Returns None if not found."""
        if not pool_name:
            return None
        for pool in self.subnet_pools:
            if pool.name.lower() == str(pool_name).strip().lower():
                return pool
        return None

    def get_pool_for_ip(self, ip):
        for pool in self.subnet_pools:
            if pool.contains(ip):
                return pool
        return None

    def taken_offsets(self, pool):
        """Host offsets already used in `pool` by registered or predefined devices."""
        taken = set()
        sources = [r.get("assigned_ip", "") for r in self.records.values()]
        sources += [e.get("ip", "") for e in self.predefined_pool.values()]
        for ip in sources:
            offset = pool.offset_of(ip)
            if offset is not None:
                taken.add(offset)
        # Addresses reserved when the pool was adopted onto this PC's network
        # (the master itself, and the bench gateway) are never handed to a NUC.
        for ip in getattr(pool, "reserve", None) or []:
            offset = pool.offset_of(ip)
            if offset is not None:
                taken.add(offset)
        return taken

    def pool_usage(self, pool_name):
        return sum(1 for r in self.records.values() if str(r.get("pool", "")).lower() == str(pool_name).lower())

    # ------------------------------------------------------- identity lookups
    def address_holder(self, ip, excluding=None):
        """Who (if anyone) already owns `ip`: another device or a predefined entry."""
        excluding = str(excluding or "").lower()
        wanted = str(ip or "").strip()
        for mac, rec in self.records.items():
            if mac != excluding and str(rec.get("assigned_ip", "")).strip() == wanted:
                return rec.get("assigned_hostname") or mac
        for mac, entry in self.predefined_pool.items():
            if mac != excluding and str(entry.get("ip", "")).strip() == wanted:
                return f"pre-assigned to {mac}"
        return None

    def hostname_holder(self, hostname, excluding=None):
        """Who (if anyone) already uses `hostname` (case-insensitive)."""
        excluding = str(excluding or "").lower()
        wanted = str(hostname or "").strip().lower()
        for mac, rec in self.records.items():
            if mac != excluding and str(rec.get("assigned_hostname", "")).strip().lower() == wanted:
                return rec.get("assigned_ip") or mac
        return None

    def auto_hostname_for(self, pool, ip):
        """The name a device gets when nobody typed one: prefix + host offset."""
        offset = pool.offset_of(ip) if pool else None
        if offset is None:
            return ""
        return pool.hostname_for(offset)

    def device_payload(self, mac, pool=None):
        """Everything the client hook / the dashboard needs for one device."""
        record = self.records.get(str(mac or "").lower(), {})
        pool = pool or self.get_pool_by_name(record.get("pool", "")) or self.default_pool
        return {
            "mac": record.get("mac", ""),
            "assigned_ip": record.get("assigned_ip", ""),
            "assigned_hostname": record.get("assigned_hostname", ""),
            "netmask": record.get("netmask") or pool.netmask,
            "cidr": record.get("cidr") or pool.cidr,
            "gateway": record.get("gateway") or pool.gateway,
            "dns": record.get("dns") or pool.dns,
            "pool": record.get("pool", pool.name),
            "pinned": record.get("pinned", ""),
            "status": record.get("status", ""),
            "notes": record.get("notes", ""),
        }

    # ------------------------------------------------------------------- loading
    def load_predefined_pool(self):
        if not self.pool_csv or not os.path.exists(self.pool_csv):
            return
        try:
            with open(self.pool_csv, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#")]
            if lines and "mac" not in [c.strip().lower() for c in lines[0].split(",")]:
                lines.insert(0, "mac,ip,hostname,gateway,netmask,dns")  # tolerated headerless file
            for row in csv.DictReader(lines):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                try:
                    mac = clean_mac(row.get("mac"))
                except ValueError:
                    continue
                self.predefined_pool[mac] = {
                    "ip": row.get("ip", ""),
                    "hostname": row.get("hostname", ""),
                    "gateway": row.get("gateway", "") or self.gateway,
                    "netmask": row.get("netmask", "") or self.netmask,
                    "dns": row.get("dns", "") or self.dns,
                }
            print(f"[*] Loaded {len(self.predefined_pool)} predefined entries from {self.pool_csv}")
        except Exception as e:
            print(f"[!] Warning: Could not load {self.pool_csv}: {e}")

    def load_existing_inventory(self):
        if not os.path.exists(self.output_csv):
            self.init_csv_header()
            return
        try:
            with open(self.output_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mac = (row.get("mac") or "").strip().lower()
                    if not mac:
                        continue
                    self.records[mac] = row
            print(f"[*] Loaded {len(self.records)} existing records from {self.output_csv}")
        except Exception as e:
            print(f"[!] Warning reading existing {self.output_csv}: {e}")
            self.init_csv_header()

    def clamp_counters(self):
        """Keep every cursor inside its (possibly just edited) pool range."""
        for pool in self.subnet_pools:
            pool.current_ip_counter = pool._clean_offset(
                pool.current_ip_counter, pool.start_ip, pool.host_limit + 1, pool.start_ip)
        self._sync_aliases()

    def sync_counters(self):
        """Advance each pool past the highest address already handed out.

        Only meaningful when the pool set was rebuilt from scratch (no saved config),
        so a restart with an existing inventory.csv keeps counting where it left off.
        """
        for pool in self.subnet_pools:
            used = [o for o in self.taken_offsets(pool) if pool.start_ip <= o <= pool.max_ip]
            if used and max(used) + 1 > pool.current_ip_counter:
                pool.current_ip_counter = max(pool.start_ip, max(used) + 1)
        self.clamp_counters()
        dp = self.default_pool
        print(f"[*] Next IP for pool '{dp.name}': {dp.ip_at(min(dp.current_ip_counter, dp.host_limit))}")

    def init_csv_header(self):
        with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_FIELDS)

    # ------------------------------------------------------------------ persisted
    def config_dict(self):
        return {
            "version": CONFIG_VERSION,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "settings": dict(self.settings),
            "pools": [p.to_config_dict() for p in self.subnet_pools],
        }

    def save_config(self):
        if not self.config_path:
            return None
        payload = self.config_dict()
        temp_file = self.config_path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.config_path)) or ".", exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(temp_file, self.config_path)
        return payload

    @classmethod
    def load_config(cls, config_path):
        """Return the stored config dict, or None when missing/unreadable."""
        if not config_path or not os.path.exists(config_path):
            return None
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config root must be an object")
            return data
        except Exception as e:
            print(f"[!] Warning: could not read {config_path}: {e} (falling back to CLI arguments)")
            return None

    def apply_config(self, data, persist=True):
        """Validate + swap the whole pool set from a config-shaped dict."""
        with DATA_LOCK:
            raw_pools = data.get("pools")
            if not isinstance(raw_pools, list) or not raw_pools:
                raise ValueError("at least one pool is required")

            previous = {p.name.lower(): p for p in self.subnet_pools}
            pools, seen = [], set()
            for item in raw_pools:
                pool = SubnetPool.from_config_dict(item)
                key = pool.name.lower()
                if key in seen:
                    raise ValueError(f"duplicate pool name '{pool.name}'")
                seen.add(key)
                # Keep the live counter unless the client explicitly moved it.
                if item.get("next_offset") is None and item.get("next_ip") is None:
                    old = previous.get(key)
                    if old:
                        pool.current_ip_counter = pool._clean_offset(
                            old.current_ip_counter, 1, pool.host_limit + 1, pool.start_ip)
                pools.append(pool)

            settings = dict(self.settings)
            if isinstance(data.get("settings"), dict):
                settings.update(self.clean_settings(data["settings"]))
            settings["default_pool"] = next(
                (p.name for p in pools
                 if p.name.lower() == str(settings.get("default_pool", "")).lower()),
                pools[0].name)
            if not is_ipv4(settings.get("server_ip", "")):
                settings["server_ip"] = pools[0].gateway

            self.subnet_pools = pools
            self.settings = settings
            self.clamp_counters()
            if persist:
                self.save_config()
            return {"pools": [p.to_dict() for p in self.subnet_pools],
                    "settings": dict(self.settings)}

    def clean_settings(self, raw):
        clean = {}
        for key in DEFAULT_SETTINGS:
            if key not in raw:
                continue
            value = raw[key]
            if key == "port":
                port = to_int(value, 8000)
                if not (1 <= port <= 65535):
                    raise ValueError("port must be between 1 and 65535")
                clean[key] = port
            elif key == "target_count":
                count = to_int(value, 150)
                if count < 1:
                    raise ValueError("target_count must be at least 1")
                clean[key] = count
            elif key == "server_ip":
                value = str(value).strip()
                if value and not is_ipv4(value):
                    raise ValueError(f"'{value}' is not a valid server IP address")
                clean[key] = value
            elif key == "hostname_prefix":
                clean[key] = SubnetPool._clean_prefix(value)
            elif key == "boot_agent":
                clean[key] = coerce_bool(value)
            else:
                clean[key] = str(value).strip()
        return clean

    def mutate_pool(self, payload, mode="save"):
        """Add / update / delete a single pool from the web UI, then persist."""
        with DATA_LOCK:
            previous = {p.name.lower(): p for p in self.subnet_pools}
            requested = str(payload.get("original_name") or "").strip()
            target = previous.get(requested.lower()) if requested else None
            if requested and not target:
                raise ValueError(f"pool '{requested}' not found")

            if mode == "delete":
                if not target:
                    raise ValueError("specify the pool to delete")
                used = self.pool_usage(target.name)
                if used and not payload.get("force"):
                    raise ValueError(
                        f"pool '{target.name}' is used by {used} device(s); "
                        f"reassign them first or confirm the forced delete")
                if len(self.subnet_pools) == 1:
                    raise ValueError("refusing to delete the last remaining pool")
                remaining = [p for p in self.subnet_pools if p is not target]
                if target.name.lower() == str(self.settings.get("default_pool", "")).lower():
                    self.settings["default_pool"] = remaining[0].name
                self.subnet_pools = remaining
                self._sync_aliases()
                self.save_config()
                print(f"[-] Pool '{target.name}' deleted from the web dashboard")
                return {"status": "deleted", "name": target.name,
                        "pools": [p.to_dict() for p in self.subnet_pools],
                        "settings": dict(self.settings)}

            if not target and previous.get(str(payload.get("name", "")).strip().lower()):
                raise ValueError(f"a pool named '{str(payload.get('name')).strip()}' already exists")
            data = dict(target.to_config_dict()) if target else {}
            for key, val in payload.items():
                if key == "original_name" or isinstance(val, bool):
                    continue
                if isinstance(val, str) and not val.strip():
                    if key == "gateway":
                        data[key] = "auto"   # blank == ".1 of (the new) subnet"
                    else:
                        data.pop(key, None)  # blank = fall back to the documented default
                    continue
                data[key] = val
            if not str(data.get("name", "")).strip():
                raise ValueError("pool name is required")
            if not target and not (data.get("network") or data.get("subnet")):
                raise ValueError("subnet is required, e.g. 192.168.1.0/24")
            pool = SubnetPool.from_config_dict(data)
            clash = previous.get(pool.name.lower())
            if clash and clash is not target:
                raise ValueError(f"a pool named '{pool.name}' already exists "
                                 f"(rename would collide)")

            pools = list(self.subnet_pools)
            if target:
                pools[pools.index(target)] = pool
            else:
                pools.append(pool)
            self.subnet_pools = pools
            # A renamed pool must keep owning the devices that were assigned from it
            if target and pool.name != target.name:
                moved = 0
                for rec in self.records.values():
                    if str(rec.get("pool", "")).lower() == target.name.lower():
                        rec["pool"] = pool.name
                        moved += 1
                if moved:
                    self.save_record_to_csv()
                    print(f"[*] Re-tagged {moved} device(s) from pool '{target.name}' to '{pool.name}'")
            if not self.get_pool_by_name(self.settings.get("default_pool")):
                self.settings["default_pool"] = pools[0].name
            self._sync_aliases()
            self.save_config()
            notes = list(getattr(pool, "adjustments", []))
            if ipaddress.IPv4Address(pool.gateway) not in pool.network:
                notes.append(f"gateway {pool.gateway} is outside {pool.network} - left as is, "
                             f"clear the field to use .1 of the subnet")
            action = "updated" if target else "created"
            print(f"[+] Pool '{pool.name}' {action} from the web dashboard -> "
                  f"{pool.network} hosts {pool.start_ip}-{pool.max_ip} "
                  f"(next: {pool.ip_at(min(pool.current_ip_counter, pool.host_limit))})")
            return {"status": action, "pool": pool.to_dict(), "notes": notes,
                    "pools": [p.to_dict() for p in self.subnet_pools],
                    "settings": dict(self.settings)}

    # ------------------------------------------------------------------ CSV I/O
    def save_record_to_csv(self, record=None):
        # Rewrite the entire CSV from memory so both stores stay in sync
        temp_file = self.output_csv + ".tmp"
        with open(temp_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for r in self.records.values():
                writer.writerow({k: r.get(k, "default" if k == "pool" else "") for k in CSV_FIELDS})
        os.replace(temp_file, self.output_csv)

    # ----------------------------------------------------------------- register
    def _note_seen(self, record, seen_ip):
        """Remember the address a check-in really came from. Returns True if it changed."""
        seen = str(seen_ip or "").strip()
        if not seen or not record:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if record.get("seen_ip") == seen and record.get("seen_at"):
            # still refresh the timestamp: a repeat check-in is new evidence
            record["seen_at"] = now
            return True
        record["seen_ip"] = seen
        record["seen_at"] = now
        return True

    def register_device(self, data, seen_ip=None):
        with DATA_LOCK:
            mac = data.get("mac", "").strip().lower()
            if not mac:
                return {"error": "Missing MAC address"}, 400
            seen_ip = seen_ip or data.get("seen_ip") or data.get("_seen_ip")

            # Determine which subnet pool to use
            requested_pool_name = str(data.get("pool", "")).strip()
            target_pool = self.get_pool_by_name(requested_pool_name) or self.default_pool

            # If device already registered, return existing data
            if mac in self.records:
                record = self.records[mac]
                # Update key if previously missing and now found
                changed = False
                if not record.get("windows_key") and data.get("windows_key"):
                    record["windows_key"] = data.get("windows_key")
                    record["key_source"] = data.get("key_source", "UPDATED")
                    record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
                if self._note_seen(record, seen_ip):
                    changed = True
                if changed:
                    self.save_record_to_csv(record)
                # Look up the pool this device was assigned to
                device_pool = self.get_pool_by_name(record.get("pool", "")) or self.default_pool
                return {
                    "status": "already_registered",
                    "assigned_ip": record.get("assigned_ip", ""),
                    "assigned_hostname": record.get("assigned_hostname", ""),
                    "netmask": record.get("netmask") or device_pool.netmask,
                    "cidr": record.get("cidr") or device_pool.cidr,
                    "gateway": record.get("gateway") or device_pool.gateway,
                    "dns": record.get("dns") or device_pool.dns,
                    "windows_key": record.get("windows_key", ""),
                    "pool": record.get("pool", device_pool.name),
                    "seen_ip": record.get("seen_ip", ""),
                    "seen_at": record.get("seen_at", ""),
                }, 200

            # Check if MAC has predefined static IP in pool CSV
            if mac in self.predefined_pool:
                pool_entry = self.predefined_pool[mac]
                assigned_ip = pool_entry["ip"]
                assigned_hostname = pool_entry["hostname"]
                gateway = pool_entry.get("gateway") or target_pool.gateway
                netmask = pool_entry.get("netmask") or target_pool.netmask
                dns = pool_entry.get("dns") or target_pool.dns
                cidr = netmask_to_cidr(netmask)
                owner = self.get_pool_for_ip(assigned_ip)
                if owner:
                    gateway = gateway or owner.gateway
                    dns = dns or owner.dns
                pool_name_used = "predefined"
                # a MAC reservation is a pin by definition - show it as such
                pins = {PIN_IP} | ({PIN_NAME} if assigned_hostname else set())
            else:
                # Dynamic sequential allocation from the selected pool
                try:
                    assigned_ip, assigned_hostname = target_pool.allocate_next(self.taken_offsets(target_pool))
                except ValueError as e:
                    return {"error": str(e)}, 409
                gateway = target_pool.gateway
                netmask = target_pool.netmask
                dns = target_pool.dns
                cidr = target_pool.cidr
                pool_name_used = target_pool.name
                pins = set()
                # Keep default-pool convenience aliases in sync
                if target_pool is self.subnet_pools[0]:
                    self.current_ip_counter = target_pool.current_ip_counter
                self.save_config()  # persist the moved cursor

            record = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "REGISTERED",
                "assigned_hostname": assigned_hostname,
                "assigned_ip": assigned_ip,
                "mac": mac,
                "serial": str(data.get("serial", "UNKNOWN")).strip(),
                "model": str(data.get("model", "Intel NUC")).strip(),
                "windows_key": str(data.get("windows_key", "NOT_FOUND")).strip(),
                "key_source": str(data.get("key_source", "UNKNOWN")).strip(),
                "notes": str(data.get("notes", "")).strip(),
                "pool": pool_name_used,
                "gateway": gateway,
                "netmask": netmask,
                "cidr": cidr,
                "dns": dns,
                "pinned": format_pinned(pins),
                "seen_ip": "",
                "seen_at": "",
            }
            self._note_seen(record, seen_ip)

            self.records[mac] = record
            self.save_record_to_csv(record)

            print(f"[+] NEW DEVICE [{len(self.records)}/{self.settings['target_count']}] "
                  f"Host: {assigned_hostname} | IP: {assigned_ip} | Pool: {pool_name_used} "
                  f"| MAC: {mac} | S/N: {record['serial']} | WinKey: {record['windows_key']}")

            return {
                "status": "success",
                "assigned_ip": assigned_ip,
                "assigned_hostname": assigned_hostname,
                "netmask": netmask,
                "cidr": cidr,
                "gateway": gateway,
                "dns": dns,
                "windows_key": record["windows_key"],
                "pool": pool_name_used,
                "seen_ip": record.get("seen_ip", ""),
                "seen_at": record.get("seen_at", ""),
            }, 200

    def reassign_device(self, mac, new_pool_name, new_ip=None):
        """Move a device to a different subnet pool (optionally a specific IP) for a new lease."""
        with DATA_LOCK:
            mac = mac.strip().lower()
            if mac not in self.records:
                return {"error": f"Device {mac} not found"}, 404

            target_pool = self.get_pool_by_name(new_pool_name)
            if not target_pool:
                return {"error": f"Pool '{new_pool_name}' not found"}, 404

            record = self.records[mac]
            old_ip = record.get("assigned_ip", "")

            taken = self.taken_offsets(target_pool)
            if target_pool.contains(old_ip):
                # The device's current address is free as soon as we move it
                taken.discard(target_pool.offset_of(old_ip))

            if new_ip:
                offset = target_pool.offset_of(new_ip)
                if offset is None:
                    return {"error": f"{new_ip} is outside pool '{target_pool.name}' ({target_pool.network})"}, 400
                if offset < target_pool.start_ip or offset > target_pool.max_ip:
                    return {"error": f"{new_ip} is outside the allocatable range of pool '{target_pool.name}' "
                                    f"({target_pool.ip_at(target_pool.start_ip)}-"
                                    f"{target_pool.ip_at(target_pool.max_ip)})"}, 400
                wanted = target_pool.ip_at(offset)
                if offset in taken:
                    holder = next((r.get("assigned_hostname") or r.get("mac")
                                   for r in self.records.values()
                                   if r.get("mac") != mac and r.get("assigned_ip") == wanted), "another device")
                    return {"error": f"{wanted} is already in use by {holder}"}, 409
                assigned_ip, assigned_hostname = wanted, target_pool.hostname_for(offset)
            else:
                try:
                    assigned_ip, assigned_hostname = target_pool.allocate_next(taken)
                except ValueError as e:
                    return {"error": str(e)}, 409

            if assigned_ip:
                # Keep the cursor ahead of an explicitly chosen address
                off = target_pool.offset_of(assigned_ip)
                if off is not None and off + 1 > target_pool.current_ip_counter:
                    target_pool.current_ip_counter = target_pool._clean_offset(
                        off + 1, 1, target_pool.host_limit + 1, target_pool.start_ip)
                    self.save_config()

            record["assigned_ip"] = assigned_ip
            record["assigned_hostname"] = assigned_hostname
            record["pool"] = target_pool.name
            record["gateway"] = target_pool.gateway
            record["netmask"] = target_pool.netmask
            record["cidr"] = target_pool.cidr
            record["dns"] = target_pool.dns
            record["status"] = "REASSIGNED"
            record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["notes"] = f"Reassigned from {old_ip} to {assigned_ip} (pool: {target_pool.name})"
            self.save_record_to_csv(record)

            # Keep default-pool convenience aliases in sync
            if target_pool is self.subnet_pools[0]:
                self.current_ip_counter = target_pool.current_ip_counter

            print(f"[*] REASSIGNED {mac}: {old_ip} -> {assigned_ip} (pool: {target_pool.name})")

            return {
                "status": "success",
                "old_ip": old_ip,
                "assigned_ip": assigned_ip,
                "assigned_hostname": assigned_hostname,
                "netmask": target_pool.netmask,
                "cidr": target_pool.cidr,
                "gateway": target_pool.gateway,
                "dns": target_pool.dns,
                "pool": target_pool.name,
            }, 200

    def set_device_identity(self, mac, hostname=None, ip=None, pool_name=None,
                            auto_ip=False, auto_hostname=False):
        """Pin an exact IP and/or a custom computer name to one unit.

        The dashboard sends all of these on every save, so the rules are explicit:

        * ``ip`` set        - that exact address is pinned (it must live inside a pool);
        * ``auto_ip`` set   - the next free address of the target pool is allocated;
        * neither           - the address the unit already has is kept;
        * ``hostname`` set  - that exact name is used (validated, must be unique);
        * ``auto_hostname`` - the name is derived again from prefix + host offset;
        * neither           - a name that was never pinned follows the address.

        Returns ``(payload, http_code)`` like the other mutating helpers.
        """
        with DATA_LOCK:
            mac = str(mac or "").strip().lower()
            if not mac:
                return {"error": "Missing MAC address"}, 400
            record = self.records.get(mac)
            if not record:
                return {"error": f"Device {mac} is not registered yet - "
                                 f"pre-assign its MAC address instead"}, 404

            notes = []
            old_ip = str(record.get("assigned_ip", "")).strip()
            old_name = str(record.get("assigned_hostname", "")).strip()
            old_pool = str(record.get("pool", "")).strip()
            pins = parse_pinned(record.get("pinned"))

            # ------------------------------------------------------------ the pool
            target_pool = self.get_pool_by_name(old_pool) or self.default_pool
            if pool_name:
                target_pool = self.get_pool_by_name(pool_name)
                if not target_pool:
                    return {"error": f"Pool '{pool_name}' not found"}, 404

            # -------------------------------------------------------------- the IP
            wanted_ip = str(ip or "").strip()
            allocated_host = ""
            if wanted_ip:
                if not is_ipv4(wanted_ip):
                    return {"error": f"'{wanted_ip}' is not a valid IPv4 address"}, 400
                owner = self.get_pool_for_ip(wanted_ip)
                if not owner:
                    return {"error": f"{wanted_ip} is not inside any configured pool "
                                     f"({', '.join(str(p.network) for p in self.subnet_pools)})"}, 400
                if pool_name and owner is not target_pool:
                    return {"error": f"{wanted_ip} belongs to pool '{owner.name}', "
                                     f"not '{target_pool.name}'"}, 400
                offset = owner.offset_of(wanted_ip)
                if offset is None or not (1 <= offset <= owner.host_limit):
                    return {"error": f"{wanted_ip} is not a usable host address in "
                                     f"{owner.network} (reserved/broadcast address)"}, 400
                holder = self.address_holder(wanted_ip, excluding=mac)
                if holder:
                    return {"error": f"{wanted_ip} is already used by {holder}"}, 409
                target_pool = owner
                if offset < target_pool.start_ip or offset > target_pool.max_ip:
                    # keep the pool bookkeeping honest when a hand-picked address
                    # lands outside the configured range
                    target_pool.start_ip = min(target_pool.start_ip, offset)
                    target_pool.max_ip = max(target_pool.max_ip, offset)
                    notes.append(f"pool '{target_pool.name}' range widened to "
                                 f"{target_pool.ip_at(target_pool.start_ip)}-"
                                 f"{target_pool.ip_at(target_pool.max_ip)}")
                assigned_ip = target_pool.ip_at(offset)
                pins.add(PIN_IP)
            elif auto_ip or not target_pool.contains(old_ip):
                taken = self.taken_offsets(target_pool)
                if target_pool.contains(old_ip):
                    taken.discard(target_pool.offset_of(old_ip))  # our own lease is free again
                try:
                    assigned_ip, allocated_host = target_pool.allocate_next(taken)
                except ValueError as exc:
                    return {"error": str(exc)}, 409
                pins.discard(PIN_IP)
            else:
                assigned_ip = old_ip

            # -------------------------------------------------------- the hostname
            given_name = None if hostname is None else str(hostname).strip()
            if given_name:
                try:
                    assigned_hostname = clean_hostname(given_name)
                except ValueError as exc:
                    return {"error": str(exc)}, 400
                pins.add(PIN_NAME)
            elif auto_hostname:
                assigned_hostname = self.auto_hostname_for(target_pool, assigned_ip)
                pins.discard(PIN_NAME)
            elif PIN_NAME in pins:
                assigned_hostname = old_name          # keep the name that was typed before
            else:
                assigned_hostname = (allocated_host
                                     or self.auto_hostname_for(target_pool, assigned_ip)
                                     or old_name)
            if not assigned_hostname:
                return {"error": "no hostname could be derived for this address - "
                                 "type one explicitly"}, 400
            clash = self.hostname_holder(assigned_hostname, excluding=mac)
            if clash:
                return {"error": f"hostname '{assigned_hostname}' is already used by {clash} "
                                 f"- pick another name"}, 409

            changed = (assigned_ip != old_ip or assigned_hostname != old_name
                       or target_pool.name != old_pool)
            record.update({
                "assigned_ip": assigned_ip,
                "assigned_hostname": assigned_hostname,
                "pool": target_pool.name,
                "gateway": target_pool.gateway,
                "netmask": target_pool.netmask,
                "cidr": target_pool.cidr,
                "dns": target_pool.dns,
                "pinned": format_pinned(pins),
            })
            if changed:
                bits = []
                if assigned_ip != old_ip:
                    bits.append(f"IP {old_ip or '-'} -> {assigned_ip}")
                if assigned_hostname != old_name:
                    bits.append(f"name {old_name or '-'} -> {assigned_hostname}")
                if target_pool.name != old_pool:
                    bits.append(f"pool {old_pool or '-'} -> {target_pool.name}")
                record["status"] = "ASSIGNED"
                record["notes"] = "Set in the dashboard: " + ", ".join(bits)
                record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Never hand the same address out twice: keep the cursor ahead of a pin.
            offset = target_pool.offset_of(assigned_ip)
            if offset is not None and offset + 1 > target_pool.current_ip_counter:
                target_pool.current_ip_counter = target_pool._clean_offset(
                    offset + 1, 1, target_pool.host_limit + 1, target_pool.start_ip)
            self._sync_aliases()

            # A MAC that is also in ip_pool.csv must not contradict the dashboard.
            if mac in self.predefined_pool:
                self.predefined_pool[mac].update({
                    "ip": assigned_ip, "hostname": assigned_hostname,
                    "gateway": target_pool.gateway, "netmask": target_pool.netmask,
                    "dns": target_pool.dns})
                self.save_predefined_pool()

            self.save_record_to_csv()
            self.save_config()
            print(f"[*] IDENTITY SET {mac}: host={assigned_hostname} ip={assigned_ip} "
                  f"pool={target_pool.name} pinned=[{format_pinned(pins)}]")

            device = self.device_payload(mac, target_pool)
            payload = dict(device)
            payload.update({"status": "success", "result": "success", "notes": notes,
                            "device": device,
                            "pools": [p.to_dict() for p in self.subnet_pools],
                            "settings": dict(self.settings)})
            return payload, 200

    # --------------------------------------------------- MAC pre-assignments
    def predefined_payload(self):
        """The ip_pool.csv table as the dashboard shows it."""
        items = []
        for mac, entry in sorted(self.predefined_pool.items()):
            owner = self.get_pool_for_ip(entry.get("ip"))
            items.append({
                "mac": mac,
                "ip": entry.get("ip", ""),
                "hostname": entry.get("hostname", ""),
                "gateway": entry.get("gateway", ""),
                "netmask": entry.get("netmask", ""),
                "dns": entry.get("dns", ""),
                "pool": owner.name if owner else "",
                "registered": mac in self.records,
            })
        return items

    def save_predefined_pool(self):
        """Rewrite the MAC table file from memory so the pre-assignments survive a restart."""
        if not self.pool_csv:
            return None
        header = [
            "# MAC-to-IP Predefined Allocation Table - written by the MGW_Kompy dashboard",
            "# A MAC listed here always gets the IP/hostname below, and that address is",
            "# reserved, so the automatic allocator will never hand it to another unit.",
            "mac,ip,hostname,gateway,netmask,dns",
        ]
        temp_file = self.pool_csv + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.pool_csv)) or ".", exist_ok=True)
        with open(temp_file, "w", newline="", encoding="utf-8") as f:
            f.write("\n".join(header) + "\n")
            writer = csv.writer(f)
            for mac, entry in sorted(self.predefined_pool.items()):
                writer.writerow([mac, entry.get("ip", ""), entry.get("hostname", ""),
                                 entry.get("gateway", ""), entry.get("netmask", ""),
                                 entry.get("dns", "")])
        os.replace(temp_file, self.pool_csv)
        return True

    def set_predefined_entry(self, payload, remove=False):
        """Create/replace - or delete - a 'this MAC always gets this IP and name' row."""
        with DATA_LOCK:
            try:
                mac = clean_mac(payload.get("mac"))
            except ValueError as exc:
                return {"error": str(exc),
                        "predefined": self.predefined_payload()}, 400

            if remove:
                if mac not in self.predefined_pool:
                    return {"error": f"{mac} is not pre-assigned",
                            "predefined": self.predefined_payload()}, 404
                self.predefined_pool.pop(mac)
                persisted = bool(self.save_predefined_pool())
                print(f"[-] Pre-assignment for {mac} removed")
                return {"status": "deleted", "mac": mac, "persisted": persisted,
                        "predefined": self.predefined_payload()}, 200

            ip = str(payload.get("ip", "")).strip()
            if not is_ipv4(ip):
                return {"error": f"'{ip}' is not a valid IPv4 address",
                        "predefined": self.predefined_payload()}, 400
            raw_name = str(payload.get("hostname", "") or "").strip()
            if raw_name:
                try:
                    raw_name = clean_hostname(raw_name)
                except ValueError as exc:
                    return {"error": str(exc),
                            "predefined": self.predefined_payload()}, 400
            if mac in self.records:
                return {"error": f"{mac} is already registered as "
                                 f"{self.records[mac].get('assigned_ip', '')} - edit that device "
                                 f"instead of pre-assigning it",
                        "predefined": self.predefined_payload()}, 409
            holder = self.address_holder(ip, excluding=mac)
            if holder:
                return {"error": f"{ip} is already used by {holder}",
                        "predefined": self.predefined_payload()}, 409

            owner = self.get_pool_for_ip(ip)
            netmask = str(payload.get("netmask", "") or "").strip()
            gateway = str(payload.get("gateway", "") or "").strip()
            dns = str(payload.get("dns", "") or "").strip()
            if not is_ipv4(netmask):
                netmask = owner.netmask if owner else "255.255.255.0"
            try:
                network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
            except ValueError:
                return {"error": f"'{netmask}' is not a valid netmask for {ip}",
                        "predefined": self.predefined_payload()}, 400
            address = ipaddress.IPv4Address(ip)
            if address in (network.network_address, network.broadcast_address):
                return {"error": f"{ip} is a network/broadcast address, not a host address",
                        "predefined": self.predefined_payload()}, 400
            if not is_ipv4(gateway):
                gateway = owner.gateway if owner else str(network.network_address + 1)
            if not dns:
                dns = owner.dns if owner else self.default_pool.dns

            self.predefined_pool[mac] = {"ip": ip, "hostname": raw_name,
                                         "gateway": gateway, "netmask": netmask, "dns": dns}
            persisted = bool(self.save_predefined_pool())
            notes = []
            if owner and not (owner.start_ip <= owner.offset_of(ip) <= owner.max_ip):
                notes.append(f"{ip} is outside pool '{owner.name}' range "
                             f"{owner.ip_at(owner.start_ip)}-{owner.ip_at(owner.max_ip)} "
                             f"(reserved here anyway)")
            print(f"[+] Pre-assigned {mac} -> {ip}{' (' + raw_name + ')' if raw_name else ''}"
                  f"{' [pool ' + owner.name + ']' if owner else ''}")
            return {"status": "success", "mac": mac, "ip": ip, "hostname": raw_name,
                    "pool": owner.name if owner else "", "persisted": persisted,
                    "notes": notes, "predefined": self.predefined_payload()}, 200

    def set_pool_next_ip(self, pool_name, value):
        """Move a pool's allocator ('default IP' for the next device)."""
        with DATA_LOCK:
            pool = self.get_pool_by_name(pool_name)
            if not pool:
                raise ValueError(f"pool '{pool_name}' not found")
            pool.set_next(value, strict=True)
            self._sync_aliases()
            self.save_config()
            print(f"[*] Pool '{pool.name}' next IP set to "
                  f"{pool.ip_at(min(pool.current_ip_counter, pool.host_limit))}")
            return {"status": "success", "pool": pool.to_dict(),
                    "notes": list(pool.adjustments),
                    "pools": [p.to_dict() for p in self.subnet_pools],
                    "settings": dict(self.settings)}

    def update_status(self, mac, status, notes="", seen_ip=None):
        with DATA_LOCK:
            mac = mac.strip().lower()
            if mac in self.records:
                self.records[mac]["status"] = status
                if notes:
                    self.records[mac]["notes"] = notes
                self.records[mac]["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._note_seen(self.records[mac], seen_ip)
                self.save_record_to_csv(self.records[mac])
                print(f"[*] STATUS UPDATE: {self.records[mac]['assigned_hostname']} ({mac}) -> {status}")
                return True
            return False

    # --------------------------------------------------------- dashboard helpers
    def ip_mismatches(self):
        """Units whose last check-in did not come from the address we assigned."""
        found = []
        for rec in self.records.values():
            seen = str(rec.get("seen_ip") or "").strip()
            assigned = str(rec.get("assigned_ip") or "").strip()
            if seen and assigned and seen != assigned:
                found.append({
                    "mac": rec.get("mac", ""),
                    "hostname": rec.get("assigned_hostname", ""),
                    "assigned_ip": assigned,
                    "seen_ip": seen,
                    "seen_at": rec.get("seen_at", ""),
                })
        return found

    def bench_status(self, networks=None):
        """Compare configured pools with the networks this PC is actually on.

        A pool "works" only when at least one address of this PC sits inside it —
        otherwise a NUC given that pool's address can never reach the server, and
        a dashboard edit can never reach the NUC. No detected networks means we
        cannot judge (a container, Windows, or `ip` missing) and we stay quiet.
        """
        if networks is None:
            networks = detect_local_networks()
        warnings = []
        if networks:
            where = describe_networks(networks)
            local = {net["address"] for net in networks}
            for pool in self.subnet_pools:
                reachable = False
                for net in networks:
                    try:
                        if ipaddress.IPv4Address(net["address"]) in pool.network:
                            reachable = True
                            break
                    except (ValueError, TypeError):
                        continue
                if not reachable:
                    warnings.append(
                        f"Pool '{pool.name}' uses {pool.network}, but this PC is only on {where}")
            server_ip = str(self.settings.get("server_ip") or "").strip()
            if server_ip and is_ipv4(server_ip) and server_ip not in local:
                warnings.append(
                    f"Master / server IP {server_ip} is not an address of this PC "
                    f"(this PC is {', '.join(sorted(local))})")
        return {
            "ok": not warnings,
            "networks": list(networks or []),
            "warnings": warnings,
            "primary": primary_network(networks),
            "mismatches": self.ip_mismatches(),
        }

    def adopt_bench(self, spec=None):
        """Rewrite the default pool onto the network this PC is actually on.

        Keeps the old range numbers (101-250, say) when they still fit, moves the
        master/server IP to this PC's address, and reserves that address plus the
        gateway so neither is ever handed to a NUC. Holds DATA_LOCK and then calls
        apply_config(), which takes the same lock — DATA_LOCK is therefore an RLock.
        """
        spec = spec or {}
        with DATA_LOCK:
            address = str(spec.get("address") or "").strip()
            prefix = spec.get("prefix", spec.get("cidr"))
            gateway = str(spec.get("gateway") or "").strip()
            iface = str(spec.get("iface") or "").strip()
            if not address:
                primary = primary_network(detect_local_networks())
                if not primary:
                    raise ValueError("this PC has no detected IPv4 network to adopt")
                address = primary["address"]
                prefix = primary["prefix"]
                gateway = gateway or primary.get("gateway") or ""
                iface = iface or primary.get("iface") or ""
            if "/" in address and prefix in (None, ""):
                address, _, prefix = address.partition("/")
            if not is_ipv4(address):
                raise ValueError(f"'{address}' is not a valid IPv4 address")
            try:
                prefix = int(prefix)
            except (TypeError, ValueError):
                raise ValueError("prefix (CIDR length) is required, e.g. 24")
            if not (1 <= prefix <= 30):
                raise ValueError(f"prefix /{prefix} is not a usable IPv4 network")
            network = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
            address = str(ipaddress.IPv4Address(address))
            if gateway and not is_ipv4(gateway):
                raise ValueError(f"'{gateway}' is not a valid gateway address")
            if not gateway:
                gateway = str(network.network_address + 1)

            pool = self.default_pool
            host_limit = max(1, network.num_addresses - 2)
            # Keep the numbers the technician already chose (101-250) when the new
            # network is large enough; otherwise clamp to what actually fits.
            if 1 <= pool.start_ip <= host_limit:
                start = pool.start_ip
            else:
                start = 1
            if 1 <= pool.max_ip <= host_limit:
                end = pool.max_ip
            else:
                end = host_limit
            if end < start:
                start, end = 1, host_limit
            cursor = pool.current_ip_counter
            if cursor < start or cursor > host_limit:
                cursor = start
            if cursor > end:
                cursor = start

            reserve = []
            for ip in (address, gateway):
                try:
                    host = ipaddress.IPv4Address(ip)
                except ValueError:
                    continue
                if host not in network or ip in reserve:
                    continue
                offset = int(host) - int(network.network_address)
                if 1 <= offset <= host_limit and ip not in reserve:
                    reserve.append(ip)
            reserved_offsets = {
                int(ipaddress.IPv4Address(ip)) - int(network.network_address)
                for ip in reserve
            }
            while cursor in reserved_offsets and cursor <= end:
                cursor += 1
            if cursor > end:
                cursor = end + 1

            new_pool = {
                "name": pool.name,
                "network": str(network),
                "start": start,
                "max": end,
                "gateway": gateway,
                "dns": pool.dns,
                "prefix": pool.hostname_prefix,
                "next_offset": cursor,
                "reserve": reserve,
            }
            pools = []
            for existing in self.subnet_pools:
                if existing.name.lower() == pool.name.lower():
                    pools.append(new_pool)
                else:
                    pools.append(existing.to_config_dict())
            settings = dict(self.settings)
            settings["server_ip"] = address
            result = self.apply_config({"pools": pools, "settings": settings})
            adopted = self.default_pool
            notes = [
                f"default pool '{adopted.name}' is now {adopted.network}",
                f"kept host range {adopted.start_ip}-{adopted.max_ip}",
                f"master / server IP is {self.settings.get('server_ip')}",
            ]
            if reserve:
                notes.append("reserved (never handed to a NUC): " + ", ".join(reserve))
            if iface:
                notes.append(f"taken from {iface}")
            next_ip = adopted.ip_at(min(adopted.current_ip_counter, adopted.host_limit))
            result.update({
                "status": "adopted",
                "network": str(adopted.network),
                "server_ip": self.settings.get("server_ip"),
                "gateway": adopted.gateway,
                "next_ip": next_ip,
                "notes": notes,
                "reserve": list(adopted.reserve),
            })
            print(f"[+] Adopted {adopted.network} for pool '{adopted.name}' "
                  f"(master {self.settings.get('server_ip')}, gateway {adopted.gateway}, "
                  f"next {next_ip})")
            return result

    def hook_commands(self):
        """Ready-to-paste Clonezilla hook commands for the current settings."""
        server = self.settings.get("server_ip") or self.default_pool.gateway
        port = self.settings.get("port", 8000)
        base = f"http://{server}:{port}"
        pool = self.settings.get("default_pool") or ""
        pool_arg = f" {pool}" if pool and pool != "default" else ""
        return {
            "server": f"{server}:{port}",
            "prerun": f"curl -s {base}/client/extract_and_register.sh | bash{pool_arg}",
            "postrun": f"curl -s {base}/client/configure_network.sh | bash",
            "injected": (
                "# These values are injected automatically when the NUC downloads the script:\n"
                f"DEPLOY_SERVER={server} DEPLOY_PORT={port} DEPLOY_POOL={pool or 'default'} "
                f"DEPLOY_BOOT_AGENT={'1' if coerce_bool(self.settings.get('boot_agent'), True) else '0'}"
            ),
        }


inventory_mgr: InventoryManager = None


def client_script_preamble(server, port, pool_name, boot_agent):
    """Shell snippet injected when a NUC downloads a client hook.

    DEPLOY_BOOT_AGENT is what configure_network.sh reads to install (1) or remove
    (0) the on-boot identity agent. The `:=` form leaves an explicit override alone.
    """
    boot = "1" if coerce_bool(boot_agent, True) else "0"
    pool_name = pool_name or "default"
    return (
        "# ---- MGW_Kompy live configuration (managed in the web dashboard) ----\n"
        f'if [ -z "${{DEPLOY_SERVER:-}}" ] && [ -z "${{1:-}}" ]; then DEPLOY_SERVER="{server}"; fi\n'
        f': "${{DEPLOY_PORT:={port}}}"\n'
        f': "${{DEPLOY_POOL:={pool_name}}}"\n'
        f': "${{DEPLOY_BOOT_AGENT:={boot}}}"\n'
        "export DEPLOY_SERVER DEPLOY_PORT DEPLOY_POOL DEPLOY_BOOT_AGENT\n"
        "# ----------------------------------------------------------------------\n"
    )


class DeploymentRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress verbose standard HTTP access logs for cleaner terminal output
        if "/api/" not in self.path:
            return
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    # ------------------------------------------------------------- tiny helpers
    def send_json(self, payload, code=200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:
            return {}

    def read_only(self):
        return bool(inventory_mgr and inventory_mgr.read_only)

    def client_ip(self):
        """The address this check-in really came from (the socket, not a header)."""
        try:
            return str(self.client_address[0] or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            html = self.render_dashboard()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif parsed.path == "/api/inventory":
            with DATA_LOCK:
                data = list(inventory_mgr.records.values())
            self.send_json(data)
            return

        elif parsed.path == "/api/pools":
            with DATA_LOCK:
                data = self.pools_payload()
            self.send_json(data)
            return

        elif parsed.path == "/api/config":
            with DATA_LOCK:
                payload = {
                    "version": CONFIG_VERSION,
                    "config_path": os.path.abspath(inventory_mgr.config_path or ""),
                    "read_only": inventory_mgr.read_only,
                    "settings": dict(inventory_mgr.settings),
                    "pools": self.pools_payload(),
                    "predefined": inventory_mgr.predefined_payload(),
                    "pool_file": os.path.abspath(inventory_mgr.pool_csv or ""),
                }
            self.send_json(payload)
            return

        elif parsed.path == "/api/dashboard":
            # Everything the page needs to update itself *without* a reload: the counters,
            # the pool cards, the NUC table and the bench / IP-mismatch banners.
            self.send_json(self.dashboard_payload())
            return

        elif parsed.path == "/api/bench":
            with DATA_LOCK:
                payload = inventory_mgr.bench_status()
            self.send_json(payload)
            return

        elif parsed.path == "/api/hooks":
            self.send_json(inventory_mgr.hook_commands())
            return

        elif parsed.path == "/export.csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=nuc_inventory.csv")
            self.end_headers()
            with DATA_LOCK:
                if os.path.exists(inventory_mgr.output_csv):
                    with open(inventory_mgr.output_csv, "rb") as f:
                        self.wfile.write(f.read())
            return

        elif parsed.path.startswith("/client/") and parsed.path.endswith(".sh"):
            self.serve_client_script(parsed.path)
            return

        # Serve everything else statically from the repo root
        super().do_GET()

    def dashboard_payload(self):
        """Counters, tables and the bench / mismatch banners, safe to poll in place."""
        with DATA_LOCK:
            records = list(inventory_mgr.records.values())
            pools_data = self.pools_payload()
            predefined = inventory_mgr.predefined_payload()
            bench = inventory_mgr.bench_status()
            banners = self.render_alert_banners(bench)
        return {
            "stats": self.dashboard_stats(records),
            "pool_cards": self.render_pool_cards(pools_data),
            "rows": self.render_device_rows(records, pools_data),
            "predefined_rows": self.render_predefined_rows(predefined),
            "predefined": predefined,
            "banners": banners,
            "bench": bench,
            "mismatches": bench.get("mismatches", []),
        }

    def pools_payload(self):
        pools = []
        for pool in inventory_mgr.subnet_pools:
            data = pool.to_dict()
            data["in_use"] = inventory_mgr.pool_usage(pool.name)
            data["is_default"] = (pool.name.lower() ==
                                  str(inventory_mgr.settings.get("default_pool", "")).lower())
            pools.append(data)
        return pools

    def serve_client_script(self, path):
        """Deliver a client hook with the current web-configured values injected,
        so the Clonezilla side never needs a hand-edited file."""
        try:
            with open(path[1:], "r", encoding="utf-8") as f:
                script = f.read()
        except Exception:
            self.send_error(404, "script not found")
            return
        lines = script.split("\n")
        head = lines[0] if lines and lines[0].startswith("#!") else "#!/bin/bash"
        rest = "\n".join(lines[1:]) if lines and lines[0].startswith("#!") else script
        hooks = inventory_mgr.hook_commands()
        server, _, port = hooks["server"].rpartition(":")
        pool_name = inventory_mgr.settings.get("default_pool", "default") or "default"
        block = client_script_preamble(
            server, port, pool_name, inventory_mgr.settings.get("boot_agent", True))
        body = (head + "\n" + block + rest).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        parsed = urlparse(self.path)
        body = self.read_json_body()

        if parsed.path not in ("/api/register", "/api/status") and self.read_only():
            self.send_json({"error": "server started with --read-only: web editing disabled"}, 403)
            return

        try:
            if parsed.path == "/api/register":
                resp, code = inventory_mgr.register_device(body, seen_ip=self.client_ip())
                self.send_json(resp, code)
                return

            elif parsed.path == "/api/status":
                success = inventory_mgr.update_status(body.get("mac", ""),
                                                       body.get("status", "UNKNOWN"),
                                                       body.get("notes", ""),
                                                       seen_ip=self.client_ip())
                self.send_json({"success": success}, 200 if success else 404)
                return

            elif parsed.path == "/api/bench/adopt":
                resp = inventory_mgr.adopt_bench(body)
                self.send_json(resp)
                return

            elif parsed.path == "/api/reassign":
                resp, code = inventory_mgr.reassign_device(
                    body.get("mac", ""), body.get("pool", ""), body.get("ip"))
                self.send_json(resp, code)
                return

            elif parsed.path in ("/api/device/update", "/api/device/identity"):
                # Pin a custom computer name and/or an exact IP from the pool to one unit.
                resp, code = inventory_mgr.set_device_identity(
                    body.get("mac", ""),
                    hostname=body.get("hostname"),
                    ip=body.get("ip"),
                    pool_name=body.get("pool") or None,
                    auto_ip=bool(body.get("auto_ip")),
                    auto_hostname=bool(body.get("auto_hostname")),
                )
                self.send_json(resp, code)
                return

            elif parsed.path in ("/api/predefine/save", "/api/predefine/delete"):
                resp, code = inventory_mgr.set_predefined_entry(
                    body, remove=parsed.path.endswith("delete"))
                self.send_json(resp, code)
                return

            elif parsed.path in ("/api/pool/save", "/api/pool/add"):
                resp = inventory_mgr.mutate_pool(body, "save" if parsed.path.endswith("save") else "create")
                self.send_json(resp)
                return

            elif parsed.path == "/api/pool/delete":
                resp = inventory_mgr.mutate_pool(body, "delete")
                self.send_json(resp)
                return

            elif parsed.path == "/api/pool/next-ip":
                resp = inventory_mgr.set_pool_next_ip(body.get("pool") or body.get("name"),
                                                      body.get("next_ip", body.get("offset")))
                self.send_json(resp)
                return

            elif parsed.path == "/api/pools/replace":
                resp = inventory_mgr.apply_config({"pools": body.get("pools"),
                                                   "settings": body.get("settings")})
                self.send_json(resp)
                return

            elif parsed.path == "/api/config/save":
                resp = inventory_mgr.apply_config({"pools": self.pools_payload(),
                                                   "settings": body})
                self.send_json(resp)
                return
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
            return
        except Exception as e:  # keep the deployment loop alive above all
            self.send_json({"error": f"internal error: {e}"}, 500)
            return

        self.send_json({"error": "not found"}, 404)

    # ---------------------------------------------------------------- dashboard
    def dashboard_stats(self, records=None):
        records = list(inventory_mgr.records.values()) if records is None else records
        settings = inventory_mgr.settings
        total = len(records)
        keys_found = sum(1 for r in records
                         if r.get("windows_key") and r.get("windows_key") not in ("NOT_FOUND", "UNKNOWN", ""))
        completed = sum(1 for r in records if r.get("status") in ("CONFIGURED", "FINISHED", "COMPLETED"))
        pinned = sum(1 for r in records if r.get("pinned"))
        target = max(1, to_int(settings.get("target_count"), 150))
        return {
            "total": total,
            "target": target,
            "keys_found": keys_found,
            "completed": completed,
            "pinned": pinned,
            "pools": len(inventory_mgr.subnet_pools),
            "default_pool": settings.get("default_pool", "default"),
            "progress_total": round(min(100, total / target * 100), 1),
            "progress_keys": round(min(100, keys_found / max(total, 1) * 100), 1),
            "progress_completed": round(min(100, completed / max(total, 1) * 100), 1),
        }

    def render_pool_cards(self, pools_data):
        html = ""
        for p in pools_data:
            pct = int((p["current_ip_counter"] - p["start"]) / max(p["size"], 1) * 100)
            star = " &#9733;" if p["is_default"] else ""
            html += f"""
            <div class="col-md-4 col-sm-6">
                <div class="card stat-card bg-white p-3">
                    <div class="d-flex justify-content-between align-items-start">
                        <span class="text-muted small text-uppercase fw-semibold">{html_escape(p['name'])}{star}</span>
                        <span class="badge bg-light text-dark border">{html_escape(p['network'])}</span>
                    </div>
                    <h4 class="fw-bold my-1 text-dark fs-4">{html_escape(p['next_ip']) if p['next_ip_ready'] else '<span class="text-danger">exhausted</span>'}</h4>
                    <small class="text-muted">Next IP &middot; {p['available']} free &middot; {p['in_use']} in use</small>
                    <div class="progress mt-2" style="height: 4px;">
                        <div class="progress-bar bg-secondary" style="width: {min(max(pct, 0), 100)}%"></div>
                    </div>
                    <small class="text-muted mt-1">GW: {html_escape(p['gateway'])} &middot; /{html_escape(p['cidr'])}
                        &middot; {html_escape(p['prefix'])}XXX</small>
                </div>
            </div>
            """
        return html or '<div class="col-12 text-muted">No pools configured.</div>'

    def render_predefined_rows(self, predefined):
        if not predefined:
            return ("<tr><td colspan='5' class='text-center text-muted py-3'>Nothing reserved yet - "
                    "every unit gets the next free address of its pool. Add a MAC above to fix "
                    "an exact IP / computer name for one machine.</td></tr>")
        rows = ""
        for item in predefined:
            badge = (" <span class='badge bg-light text-dark border'>registered</span>"
                     if item.get("registered") else "")
            name = (html_escape(item.get("hostname", "")) or
                    '<span class="text-muted">(from pool prefix)</span>')
            rows += f"""
            <tr>
                <td><code>{html_escape(item['mac'])}</code>{badge}</td>
                <td><code>{html_escape(item['ip'])}</code></td>
                <td>{name}</td>
                <td><span class="badge bg-light text-dark border">{html_escape(item['pool'] or 'outside pools')}</span>
                    <small class="text-muted">{html_escape(item['gateway'])} / {html_escape(item['netmask'])}</small></td>
                <td class="text-end">
                    <button class="btn btn-outline-danger btn-sm pre-delete"
                            data-mac="{html_escape(item['mac'], quote=True)}">Remove</button>
                </td>
            </tr>
            """
        return rows

    def render_device_rows(self, records, pools_data):
        """The NUC table plus a hidden per-device editor (name / exact IP) under each row."""
        total = len(records)
        pool_names = [p["name"] for p in pools_data]
        rows_html = ""
        for idx, r in enumerate(reversed(records), 1):
            mac = r.get("mac", "")
            key_val = r.get("windows_key", "NOT_FOUND")
            key_badge = (f"<span class='badge bg-success font-monospace'>{html_escape(key_val)}</span>"
                         if key_val != "NOT_FOUND"
                         else "<span class='badge bg-secondary'>None</span>")
            status = r.get("status", "REGISTERED")
            status_cls = "bg-primary"
            if status in ("CONFIGURED", "COMPLETED", "FINISHED"):
                status_cls = "bg-success"
            elif status == "CLONING":
                status_cls = "bg-warning text-dark"
            elif status in ("REASSIGNED", "ASSIGNED"):
                status_cls = "bg-info text-dark"
            elif "ERROR" in status:
                status_cls = "bg-danger"

            pool_name = r.get("pool", "default")
            hostname = r.get("assigned_hostname", "")
            ip = r.get("assigned_ip", "")
            pins = parse_pinned(r.get("pinned"))
            pin_badges = ""
            if PIN_NAME in pins:
                pin_badges += (" <span class='badge bg-light text-dark border' title='computer name was "
                               "typed by hand - it will not follow the pool prefix'>name</span>")
            if PIN_IP in pins:
                pin_badges += (" <span class='badge bg-light text-dark border' title='IP address was "
                               "picked by hand - the allocator will never reuse or renumber it'>ip</span>")
            pool_badge = f"<span class='badge bg-light text-dark border'>{html_escape(pool_name)}</span>"

            options = ""
            for p in pools_data:
                selected = " selected" if p["name"] == pool_name else ""
                options += (f"<option value='{html_escape(p['name'], quote=True)}'{selected} "
                            f"data-next='{html_escape(p['next_ip'], quote=True)}'>"
                            f"{html_escape(p['name'])} ({html_escape(p['network'])})</option>")
            if pool_name not in pool_names:
                options = (f"<option value='{html_escape(pool_name, quote=True)}' selected>"
                           f"{html_escape(pool_name)} (missing)</option>") + options

            rows_html += f"""
            <tr data-device="{html_escape(mac, quote=True)}">
                <td>{total - idx + 1}</td>
                <td><strong>{html_escape(hostname)}</strong>{pin_badges}</td>
                <td><code>{html_escape(ip)}</code></td>
                <td>{self.render_seen(r)}</td>
                <td>{pool_badge}</td>
                <td><code class='text-muted'>{html_escape(mac)}</code></td>
                <td>{html_escape(r.get('serial', ''))}</td>
                <td>{html_escape(r.get('model', ''))}</td>
                <td>{key_badge}</td>
                <td><small>{html_escape(r.get('key_source', ''))}</small></td>
                <td><span class='badge {status_cls}'>{html_escape(status)}</span></td>
                <td><small>{html_escape(r.get('timestamp', ''))}</small></td>
                <td class="text-nowrap">
                    <button class="btn btn-outline-primary btn-sm edit-device-btn"
                            data-mac="{html_escape(mac, quote=True)}">Edit name / IP</button>
                </td>
            </tr>
            <tr class="editor-row d-none" data-editor="{html_escape(mac, quote=True)}">
                <td colspan="13">
                    <div class="row g-2 align-items-end">
                        <div class="col-md-3">
                            <label class="form-label small text-muted mb-0">Computer name (hostname)</label>
                            <input type="text" class="form-control form-control-sm" data-f="hostname"
                                   value="{html_escape(hostname, quote=True)}" placeholder="nuc-101">
                            <label class="form-check-label small text-muted mt-1">
                                <input class="form-check-input me-1" type="checkbox" data-f="auto_hostname">
                                auto name from the pool prefix
                            </label>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label small text-muted mb-0">IP address</label>
                            <input type="text" class="form-control form-control-sm" data-f="ip"
                                   value="{html_escape(ip, quote=True)}" placeholder="192.168.1.140">
                            <label class="form-check-label small text-muted mt-1">
                                <input class="form-check-input me-1" type="checkbox" data-f="auto_ip">
                                give the next free IP of the pool
                            </label>
                        </div>
                        <div class="col-md-2">
                            <label class="form-label small text-muted mb-0">Pool</label>
                            <select class="form-select form-select-sm" data-f="pool">{options}</select>
                            <small class="text-muted pool-next">next free: -</small>
                        </div>
                        <div class="col-md-4 text-md-end">
                            <button class="btn btn-primary btn-sm" data-act="save-identity">Save name / IP</button>
                            <button class="btn btn-outline-secondary btn-sm" data-act="auto-identity"
                                    title="Reassign the next free address of the selected pool and let the name follow it">Reset to auto</button>
                            <button class="btn btn-outline-dark btn-sm" data-act="cancel-edit">Cancel</button>
                            <div class="small identity-msg mt-1"></div>
                        </div>
                    </div>
                </td>
            </tr>
            """
        if not rows_html:
            rows_html = ("<tr><td colspan='13' class='text-center py-4 text-muted'>"
                         "No devices connected yet. Start PXE booting your NUCs!</td></tr>")
        return rows_html

    def render_seen(self, record):
        """Green check when the unit checked in from its assigned address, red cross otherwise."""
        seen = str(record.get("seen_ip") or "").strip()
        assigned = str(record.get("assigned_ip") or "").strip()
        when = html_escape(record.get("seen_at") or "")
        if not seen:
            return '<span class="text-muted" title="has not checked in yet">&mdash;</span>'
        if seen == assigned:
            return (f'<span class="text-success seen-ok" title="checked in from {html_escape(seen)}'
                    f' at {when}">&#10003;</span>')
        return (f'<span class="text-danger seen-bad" title="assigned {html_escape(assigned)}, '
                f'seen at {when}">&#10007; {html_escape(seen)}</span>')

    def render_alert_banners(self, bench=None):
        """Read-only note, IP-mismatch warning, and the 'wrong network' bench warning."""
        bench = bench if bench is not None else inventory_mgr.bench_status()
        parts = []
        if inventory_mgr.read_only:
            parts.append(
                '<div class="alert alert-warning py-2 small mb-2">'
                "Server is running with <code>--read-only</code>: pool editing is disabled. "
                "Restart without that flag to edit subnets from this page."
                "</div>")
        mismatches = bench.get("mismatches") if bench.get("mismatches") is not None else inventory_mgr.ip_mismatches()
        if mismatches:
            lines = []
            for item in mismatches:
                unit = html_escape(item.get("hostname") or item.get("mac") or "a unit")
                seen = html_escape(item.get("seen_ip") or "")
                assigned = html_escape(item.get("assigned_ip") or "")
                lines.append(
                    f"<strong>{unit}</strong> checked in from <code>{seen}</code> "
                    f"instead of its assigned address <code>{assigned}</code>")
            parts.append(
                '<div class="alert alert-danger py-2 small mb-2" id="mismatch-banner">'
                "<strong>A NUC is not using its assigned address.</strong> "
                + "<br>".join(lines)
                + ". The bench router (not this server, and not FileZilla) is the usual "
                "cause: its DHCP handed the unit a different address."
                "</div>")
        primary = bench.get("primary") or {}
        warned_pool = False
        for warning in bench.get("warnings") or []:
            button = ""
            if (not inventory_mgr.read_only and primary and not warned_pool
                    and str(warning).startswith("Pool ")):
                warned_pool = True
                button = (
                    ' <button type="button" class="btn btn-sm btn-warning adopt-bench ms-2"'
                    f' data-address="{html_escape(primary.get("address", ""), quote=True)}"'
                    f' data-prefix="{html_escape(str(primary.get("prefix", "")), quote=True)}"'
                    f' data-gateway="{html_escape(primary.get("gateway") or "", quote=True)}"'
                    f' data-iface="{html_escape(primary.get("iface") or "", quote=True)}">'
                    "Use this PC's network</button>")
            parts.append(
                '<div class="alert alert-warning py-2 small mb-2" id="bench-banner">'
                "<strong>The pool and this PC are not on the same network.</strong> "
                f"{html_escape(warning)}.{button}"
                "</div>")
        return "\n".join(parts)

    def render_dashboard(self):
        with DATA_LOCK:
            records = list(inventory_mgr.records.values())
            pools_data = self.pools_payload()
            settings = dict(inventory_mgr.settings)
            predefined = inventory_mgr.predefined_payload()

        stats = self.dashboard_stats(records)
        total, target = stats["total"], stats["target"]
        keys_found, completed = stats["keys_found"], stats["completed"]

        pool_cards_html = self.render_pool_cards(pools_data)
        rows_html = self.render_device_rows(records, pools_data)
        predefined_rows = self.render_predefined_rows(predefined)

        read_only = inventory_mgr.read_only
        banner = self.render_alert_banners()

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NUC Deployment & Windows Key Registry</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>{DASHBOARD_CSS}</style>
</head>
<body class="py-4">
    <div class="container-fluid px-4">
        <div class="d-flex justify-content-between align-items-center mb-4 pb-2 border-bottom">
            <div>
                <h2 class="h3 fw-bold mb-1">NUC Deployment & Key Vault</h2>
                <p class="text-muted mb-0">Automated Clonezilla PXE Deployment & Windows Key Harvester
                    ({target} Target Units)</p>
            </div>
            <div class="text-end">
                <span id="refresh-state" class="badge bg-light text-dark border me-2">auto-refresh off</span>
                <button id="auto-refresh-toggle" class="btn btn-outline-secondary btn-sm me-2"
                        title="Auto-refresh is OFF by default. Turn it on to poll the server every 5s - only the tables below are updated, the page is never reloaded.">
                    Auto-refresh: off</button>
                <button id="refresh-now" class="btn btn-secondary btn-sm me-2">Refresh</button>
                <a href="/export.csv" class="btn btn-outline-primary btn-sm">Download CSV</a>
            </div>
        </div>

        <div id="banners">{banner}</div>

        <div class="row g-3 mb-4">
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Total Registered</span>
                    <h3 class="display-6 fw-bold my-1 text-primary"><span id="stat-total">{total}</span> <small class="text-muted fs-6">/ <span id="stat-target">{target}</span></small></h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div id="stat-bar-total" class="progress-bar bg-primary" style="width: {stats['progress_total']}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Windows Keys Saved</span>
                    <h3 class="display-6 fw-bold my-1 text-success" id="stat-keys">{keys_found}</h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div id="stat-bar-keys" class="progress-bar bg-success" style="width: {stats['progress_keys']}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Completed / Configured</span>
                    <h3 class="display-6 fw-bold my-1 text-info" id="stat-completed">{completed}</h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div id="stat-bar-completed" class="progress-bar bg-info" style="width: {stats['progress_completed']}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Subnet Pools Active</span>
                    <h3 class="display-6 fw-bold my-1 text-dark" id="stat-pools">{len(pools_data)}</h3>
                    <small class="text-muted">Default pool: <strong id="stat-default-pool">{html_escape(settings.get('default_pool', 'default'))}</strong></small>
                </div>
            </div>
        </div>

        <!-- ============ NETWORK SETTINGS (edit subnets & IPs right here) ============ -->
        <div class="card stat-card bg-white mb-4" id="network-settings">
            <div class="card-header bg-white py-3 border-bottom d-flex justify-content-between align-items-center">
                <h5 class="mb-0 fw-semibold">Network Settings &mdash; subnets, ranges &amp; default IPs</h5>
                <div>
                    <span class="badge bg-light text-dark border me-2" id="config-path" title="Where these settings are saved">config: -</span>
                    <button class="btn btn-outline-secondary btn-sm" id="reload-config">Reload</button>
                </div>
            </div>
            <div class="card-body">
                <div class="row g-3 mb-3 align-items-end">
                    <div class="col-md-2">
                        <label class="form-label small text-muted mb-0">Master / server IP</label>
                        <input type="text" class="form-control form-control-sm" id="set-server-ip" placeholder="192.168.1.1">
                    </div>
                    <div class="col-md-1">
                        <label class="form-label small text-muted mb-0">Port</label>
                        <input type="number" class="form-control form-control-sm" id="set-port" min="1" max="65535">
                    </div>
                    <div class="col-md-2">
                        <label class="form-label small text-muted mb-0">Fleet target size</label>
                        <input type="number" class="form-control form-control-sm" id="set-target" min="1">
                    </div>
                    <div class="col-md-7 text-md-end">
                        <button class="btn btn-sm btn-primary" id="save-settings" {'disabled' if read_only else ''}>Save global settings</button>
                    </div>
                </div>
                <div class="form-check mb-3">
                    <input class="form-check-input" type="checkbox" id="set-boot-agent" {'checked' if settings.get('boot_agent', True) else ''} {'disabled' if read_only else ''}>
                    <label class="form-check-label" for="set-boot-agent">
                        <strong>Keep NUCs in sync on every boot</strong>
                        <span class="text-muted small"> — install an on-boot agent so an IP or computer name
                        changed here is applied the next time that unit boots
                        (injected into the client script as <code>DEPLOY_BOOT_AGENT</code>).</span>
                    </label>
                </div>

                <div class="table-responsive">
                    <table class="table table-sm align-middle pool-table mb-2">
                        <thead class="table-light">
                            <tr>
                                <th>Pool</th>
                                <th>Subnet (CIDR)</th>
                                <th title="First host offset (last octet on a /24)">Range start</th>
                                <th title="Last host offset">Range end</th>
                                <th title="The IP the next new device will receive">Default / next IP</th>
                                <th>Gateway</th>
                                <th>DNS</th>
                                <th>Host prefix</th>
                                <th>In use</th>
                                <th class="text-end">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="pool-rows"><!-- filled by JS from /api/config --></tbody>
                        <tfoot id="pool-new-row"><!-- add-new-pool form --></tfoot>
                    </table>
                </div>

                <div class="d-flex flex-wrap gap-3 align-items-center">
                    <span class="small text-muted">
                        &#9733; marks the default pool (used for devices that do not name one) &middot;
                        &#8634; restarts a range at its first free host &middot;
                        changes are written to <code>network_config.json</code> and apply to the very next device.
                    </span>
                    <span id="config-msg" class="small"></span>
                </div>

                <hr class="my-3">
                <div class="row g-3">
                    <div class="col-lg-6">
                        <label class="form-label small text-muted mb-1">Clonezilla ocs_prerun hook</label>
                        <div class="input-group input-group-sm">
                            <input type="text" readonly class="form-control font-monospace" id="hook-prerun" value="-">
                            <button class="btn btn-outline-secondary copy-btn" data-target="hook-prerun">Copy</button>
                        </div>
                    </div>
                    <div class="col-lg-6">
                        <label class="form-label small text-muted mb-1">Clonezilla ocs_postrun hook</label>
                        <div class="input-group input-group-sm">
                            <input type="text" readonly class="form-control font-monospace" id="hook-postrun" value="-">
                            <button class="btn btn-outline-secondary copy-btn" data-target="hook-postrun">Copy</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============ Subnet Pools Overview ============ -->
        <div class="card stat-card bg-white mb-4">
            <div class="card-header bg-white py-3 border-bottom">
                <h5 class="mb-0 fw-semibold">Subnet Pools Overview</h5>
            </div>
            <div class="card-body">
                <div class="row g-3" id="pool-cards">
                    {pool_cards_html}
                </div>
            </div>
        </div>

        <div class="card stat-card bg-white">
            <div class="card-header bg-white py-3 border-bottom d-flex justify-content-between align-items-center">
                <div>
                    <h5 class="mb-0 fw-semibold">Connected NUC Systems</h5>
                    <small class="text-muted">
                        <strong>Edit name / IP</strong> pins an exact address and/or a custom computer name to
                        one unit - it is applied by the post-run hook and survives a restart.
                    </small>
                </div>
                <div class="text-end">
                    <span class="badge bg-light text-dark border" id="last-updated">not refreshed yet</span>
                    <div class="small" id="device-msg"></div>
                </div>
            </div>
            <div class="table-responsive">
                <table class="table table-hover align-middle mb-0">
                    <thead class="table-light">
                        <tr>
                            <th>#</th>
                            <th>Hostname</th>
                            <th>IP Address</th>
                            <th title="The address the unit actually checked in from">Seen as</th>
                            <th>Pool</th>
                            <th>MAC Address</th>
                            <th>Serial Number</th>
                            <th>Model</th>
                            <th>Windows Key</th>
                            <th>Key Source</th>
                            <th>Status</th>
                            <th>Timestamp</th>
                            <th>Name / IP</th>
                        </tr>
                    </thead>
                    <tbody id="device-rows">
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ============ Fixed IP / name pre-assignments (ip_pool.csv) ============ -->
        <div class="card stat-card bg-white mt-4">
            <div class="card-header bg-white py-3 border-bottom">
                <h5 class="mb-0 fw-semibold">Fixed IP &amp; name per unit (MAC pre-assignments)</h5>
                <small class="text-muted">
                    Reserve an exact IP + computer name for a NUC <em>before</em> it boots. A MAC listed here
                    always receives that address and name, and the pool allocator skips the address.
                    Saved to <code>{html_escape(os.path.basename(inventory_mgr.pool_csv or 'ip_pool.csv'))}</code>.
                </small>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-sm align-middle mb-2 fixed-table">
                        <thead class="table-light">
                            <tr>
                                <th>MAC address</th>
                                <th>Fixed IP</th>
                                <th>Computer name</th>
                                <th>Pool / gateway</th>
                                <th class="text-end">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="predefined-rows">{predefined_rows}</tbody>
                        <tfoot>
                            <tr>
                                <td><input class="form-control form-control-sm" id="pre-mac" placeholder="aa:bb:cc:dd:ee:ff"></td>
                                <td><input class="form-control form-control-sm" id="pre-ip" placeholder="192.168.1.140"></td>
                                <td><input class="form-control form-control-sm" id="pre-host" placeholder="workshop-01"></td>
                                <td class="text-muted small">the IP must be inside one of the pool subnets</td>
                                <td class="text-end">
                                    <button class="btn btn-outline-primary btn-sm" id="pre-save" {'disabled' if read_only else ''}>
                                        Add / update</button>
                                </td>
                            </tr>
                        </tfoot>
                    </table>
                </div>
                <div id="pre-msg" class="small"></div>
            </div>
        </div>
    </div>

    <script>{DASHBOARD_JS}</script>
</body>
</html>
"""
        return html


DASHBOARD_CSS = """
        body { background-color: #f8f9fa; font-family: system-ui, -apple-system, sans-serif; }
        .stat-card { border-radius: 10px; border: none; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
        .edit-device-btn, .pre-delete { font-size: 0.72rem; white-space: nowrap; }
        .pool-table input, .pool-table select { font-size: .8rem; }
        .pool-table input { border-color: #e3e6ea; padding: .2rem .4rem; width: 100%; min-width: 74px; }
        .pool-table input:focus { border-color: #86b7fe; box-shadow: none; }
        .pool-table td { vertical-align: middle; }
        .pool-table .row-name { max-width: 120px; }
        .pool-table tfoot td { background: #fbfcfd; border-top: 2px solid #e9ecef; }
        .pool-row-actions { white-space: nowrap; text-align: right; }
        .pool-row-actions .btn { font-size: .72rem; padding: .15rem .4rem; }
        .exhausted { color: #dc3545 !important; }
        /* per-device name / IP editor */
        .editor-row > td { background: #f1f5f9 !important; border-top: 2px solid #dee2e6 !important; }
        .editor-row .form-check-label { font-size: .72rem; }
        .editor-row input[disabled] { background: #eceff3; }
        .identity-msg { min-height: 1em; }
        .fixed-table input, .fixed-table td { font-size: .8rem; }
        .fixed-table code { font-size: .78rem; }
        .seen-ok { font-weight: 700; }
        .seen-bad { font-weight: 700; white-space: nowrap; }
        #banners:empty { display: none; }
"""

DASHBOARD_JS = """
(function () {
    var REFRESH_MS = 5000;               // poll interval while auto-refresh is ON
    var AUTO_KEY = 'mgw.autoRefresh';    // remembers the on/off choice in this browser
    var EDIT_GRACE_MS = 90000;           // after this long an untouched field stops blocking the refresh
    var READ_ONLY = false;
    var editing = false;
    var editingSince = 0;
    var autoRefresh = false;
    var FIELDS = ['name', 'network', 'start', 'max', 'next_ip', 'gateway', 'dns', 'prefix'];

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v === null || v === undefined ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function setText(id, v) { var el = $(id); if (el) el.textContent = v; }
    function setWidth(id, pct) { var el = $(id); if (el) el.style.width = pct + '%'; }

    function msg(text, ok) {
        var el = $('config-msg');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'small ' + (ok === false ? 'text-danger' : 'text-success');
    }
    function deviceMsg(text, ok) {
        var el = $('device-msg');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'small ' + (ok === false ? 'text-danger' : 'text-success');
    }
    function preMsg(text, ok) {
        var el = $('pre-msg');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'small ' + (ok === false ? 'text-danger' : 'text-success');
    }

    function api(path, body) {
        var opts = {headers: {'Content-Type': 'application/json'}};
        if (body !== undefined) { opts.method = 'POST'; opts.body = JSON.stringify(body); }
        return fetch(path, opts).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) {
                if (r.status >= 400) throw new Error(j.error || ('HTTP ' + r.status));
                return j;
            });
        });
    }

    // ======================================================= auto-refresh control
    function readAutoRefreshPreference() {
        var m = location.search.match(/[?&]refresh=([^&]*)/);
        if (m) return /^(1|on|yes|true|enabled?)$/i.test(decodeURIComponent(m[1]));
        try { return localStorage.getItem(AUTO_KEY) === 'on'; } catch (e) { return false; }
    }
    function renderAutoRefresh() {
        var btn = $('auto-refresh-toggle');
        if (btn) {
            btn.textContent = 'Auto-refresh: ' + (autoRefresh ? 'on (5s)' : 'off');
            btn.className = (autoRefresh ? 'btn btn-success btn-sm me-2'
                                         : 'btn btn-outline-secondary btn-sm me-2');
        }
        var badge = $('refresh-state');
        if (badge) {
            badge.textContent = autoRefresh
                ? (editing ? 'auto-refresh on - paused while you type' : 'auto-refresh on')
                : 'auto-refresh off';
            badge.className = autoRefresh ? 'badge bg-success me-2'
                                          : 'badge bg-light text-dark border me-2';
        }
    }
    function setAutoRefresh(on) {
        autoRefresh = !!on;
        try { localStorage.setItem(AUTO_KEY, autoRefresh ? 'on' : 'off'); } catch (e) {}
        renderAutoRefresh();
        if (!autoRefresh) deviceMsg('auto-refresh off - press "Refresh" to update the tables', true);
        else refreshLive();
    }
    function setEditing(on) {
        editing = on;
        if (on) editingSince = Date.now();
        renderAutoRefresh();
    }

    // ========================================================= live table updates
    function applyStats(s) {
        if (!s) return;
        setText('stat-total', s.total);
        setText('stat-target', s.target);
        setText('stat-keys', s.keys_found);
        setText('stat-completed', s.completed);
        setText('stat-pools', s.pools);
        setText('stat-default-pool', s.default_pool);
        setWidth('stat-bar-total', s.progress_total);
        setWidth('stat-bar-keys', s.progress_keys);
        setWidth('stat-bar-completed', s.progress_completed);
    }

    /* Refresh the numbers, the pool cards and the NUC table *in place* - the page is
       never reloaded, so the settings panel you are editing stays exactly as it is. */
    function refreshLive() {
        return api('/api/dashboard').then(function (d) {
            applyStats(d.stats);
            if (typeof d.pool_cards === 'string') $('pool-cards').innerHTML = d.pool_cards;
            if (typeof d.rows === 'string') $('device-rows').innerHTML = d.rows;
            if (typeof d.predefined_rows === 'string') $('predefined-rows').innerHTML = d.predefined_rows;
            if (typeof d.banners === 'string' && $('banners')) $('banners').innerHTML = d.banners;
            setText('last-updated', 'refreshed ' + new Date().toLocaleTimeString());
            return d;
        }).catch(function (e) {
            setText('last-updated', 'refresh failed');
            throw e;
        });
    }

    function openEditorRow() {
        var rows = document.querySelectorAll('#device-rows .editor-row');
        for (var i = 0; i < rows.length; i++) {
            if (!rows[i].classList.contains('d-none')) return rows[i];
        }
        return null;
    }
    /* True while a refresh would destroy something the technician is working on. */
    function busy() {
        if (openEditorRow()) return true;
        var el = document.activeElement;
        if (el && el.closest && el.closest('#network-settings, #device-rows')) return true;
        if (editing && (Date.now() - editingSince) < EDIT_GRACE_MS) return true;
        return false;
    }

    // ============================================================ subnet pool rows
    function cellHtml(field, p) {
        var type = (field === 'start' || field === 'max') ? 'number' : 'text';
        var val = p[field];
        if (val === undefined || val === null) val = '';
        var title = {
            name: 'Pool name (used by the API as "pool": "name")',
            network: 'Any CIDR works: 192.168.1.0/24, 10.30.0.0/16, ... (a bare 192.168.1 means /24)',
            start: 'First host offset (last octet on a /24)',
            max: 'Last host offset handed out',
            next_ip: 'Next address for a new device: full IP (192.168.1.140) or host number (140). Outside start-max it widens the range.',
            gateway: 'Blank = .1 of the subnet',
            dns: 'Comma-separated IPv4 servers',
            prefix: 'Hostname prefix; the offset is appended, e.g. nuc-101'
        }[field];
        var ph = {network: '192.168.1.0/24', next_ip: 'auto', gateway: 'auto', dns: '1.1.1.1,8.8.8.8'}[field] || '';
        return '<td class="f-' + field + '"><input data-f="' + field + '" data-initial="' + esc(val) +
            '" type="' + type + '"' +
            ' class="form-control form-control-sm" value="' + esc(val) + '"' +
            ' placeholder="' + esc(ph) + '" title="' + esc(title) + '"' +
            (READ_ONLY ? ' disabled' : '') + '></td>';
    }

    function row(p, isNew) {
        var body = FIELDS.slice(1).map(function (f) { return cellHtml(f, p); }).join('');
        var nameCell = cellHtml('name', p).replace('<td class="f-name">', '<td class="f-name">' +
            (isNew || !p.is_default ? '' : '<span class="text-warning" title="default pool">&#9733;</span> '));
        var actions = '';
        if (!READ_ONLY) {
            actions = '<button class="btn btn-outline-primary btn-sm" data-act="save">' +
                      (isNew ? 'Add pool' : 'Save') + '</button>';
            if (!isNew) {
                actions += ' <button class="btn btn-outline-secondary btn-sm" data-act="default"' +
                           ' title="Use this pool for devices that do not name one">&#9733;</button>' +
                           ' <button class="btn btn-outline-dark btn-sm" data-act="reset"' +
                           ' title="Restart the range at its first free host">&#8634;</button>' +
                           ' <button class="btn btn-outline-danger btn-sm" data-act="delete">Delete</button>';
            }
        }
        var usage = isNew ? '<span class="text-muted">-</span>'
                          : (esc(p.in_use) + ' <span class="text-muted">/ ' + esc(p.available) + ' free</span>');
        return '<tr data-orig="' + esc(isNew ? '' : p.name) + '" data-inuse="' + (p.in_use || 0) + '">' +
            nameCell + body +
            '<td class="text-nowrap">' + usage + '</td>' +
            '<td class="pool-row-actions">' + actions + '</td></tr>';
    }

    function readRow(tr) {
        var out = {};
        tr.querySelectorAll('input').forEach(function (inp) {
            var v = inp.value.trim();
            var f = inp.getAttribute('data-f');
            if (v === '') return;
            // Do not resend an untouched cursor: a fully-used pool legitimately reports
            // one address past its end, which must not bounce back as user input.
            if (f === 'next_ip' && v === (inp.getAttribute('data-initial') || '')) return;
            out[f] = v;
        });
        var orig = tr.getAttribute('data-orig');
        if (orig) out.original_name = orig;
        return out;
    }

    function render(cfg) {
        READ_ONLY = !!cfg.read_only;
        $('config-path').textContent = cfg.config_path ? ('saved in: ' + cfg.config_path) : 'not persisted';
        $('set-server-ip').value = cfg.settings.server_ip || '';
        $('set-port').value = cfg.settings.port || '';
        $('set-target').value = cfg.settings.target_count || '';
        if ($('set-boot-agent')) $('set-boot-agent').checked = !!cfg.settings.boot_agent;
        ['set-server-ip', 'set-port', 'set-target', 'set-boot-agent', 'save-settings', 'pre-save'].forEach(function (id) {
            if ($(id)) $(id).disabled = READ_ONLY;
        });
        $('pool-rows').innerHTML = cfg.pools.map(function (p) { return row(p, false); }).join('');
        $('pool-new-row').innerHTML = READ_ONLY ? '' : row({
            name: '', network: '', start: '', max: '', next_ip: '',
            gateway: '', dns: '', prefix: cfg.settings.hostname_prefix || ''
        }, true);
    }

    function afterWrite(text) {
        return function (data) {
            setEditing(false);
            if (data && data.notes && data.notes.length) { text += ' (' + data.notes.join(', ') + ')'; }
            msg(text + ' \u2713 (saved to network_config.json)');
            return api('/api/config').then(function (cfg) {
                render(cfg);
                return api('/api/hooks').then(function (h) {
                    $('hook-prerun').value = h.prerun;
                    $('hook-postrun').value = h.postrun;
                });
            }).then(function () { return refreshLive(); }).catch(function () {});
        };
    }

    function handle(act, tr) {
        var payload = readRow(tr);
        var label = payload.original_name || payload.name || 'pool';
        if (act === 'delete') {
            var used = tr.getAttribute('data-inuse');
            if (!confirm('Delete pool "' + label + '"?' + (used > 0
                ? ' ' + used + ' device(s) keep their current IPs.' : ''))) return;
            api('/api/pool/delete', {original_name: label, name: label, force: true})
                .then(afterWrite('pool deleted')).catch(function (e) { msg(e.message, false); });
            return;
        }
        if (act === 'default') {
            api('/api/config/save', {default_pool: label})
                .then(afterWrite('default pool = ' + label)).catch(function (e) { msg(e.message, false); });
            return;
        }
        if (act === 'reset') {
            if (!confirm('Restart the "' + label + '" range so the next device gets the first free host?')) return;
            api('/api/pool/next-ip', {pool: label, offset: payload.start || 1})
                .then(afterWrite('next IP reset')).catch(function (e) { msg(e.message, false); });
            return;
        }
        if (!payload.name) { msg('pool name is required', false); return; }
        if (!payload.original_name && !payload.network) { msg('subnet is required (e.g. 192.168.1.0/24)', false); return; }
        api('/api/pool/save', payload)
            .then(afterWrite(payload.original_name ? 'pool saved' : 'pool added'))
            .catch(function (e) { msg(e.message, false); });
    }

    // ==================================================== per-device name / IP editor
    function editorFor(mac) {
        return document.querySelector('#device-rows .editor-row[data-editor="' + mac + '"]');
    }
    function editorButton(mac) {
        return document.querySelector('#device-rows .edit-device-btn[data-mac="' + mac + '"]');
    }
    function closeEditors() {
        document.querySelectorAll('#device-rows .editor-row').forEach(function (r) {
            r.classList.add('d-none');
        });
        document.querySelectorAll('#device-rows .edit-device-btn').forEach(function (b) {
            b.textContent = 'Edit name / IP';
        });
    }
    function syncEditorInputs(row) {
        var autoName = row.querySelector('[data-f="auto_hostname"]');
        var autoIp = row.querySelector('[data-f="auto_ip"]');
        var nameEl = row.querySelector('[data-f="hostname"]');
        var ipEl = row.querySelector('[data-f="ip"]');
        nameEl.disabled = autoName.checked;
        ipEl.disabled = autoIp.checked;
        updatePoolNext(row);
    }
    function updatePoolNext(row) {
        var sel = row.querySelector('[data-f="pool"]');
        var hint = row.querySelector('.pool-next');
        if (!sel || !hint || !sel.options.length) return;
        var next = sel.options[sel.selectedIndex].getAttribute('data-next');
        hint.textContent = next ? ('next free: ' + next) : 'next free: -';
    }
    function toggleEditor(mac) {
        var row = editorFor(mac);
        var btn = editorButton(mac);
        if (!row) return;
        var wasHidden = row.classList.contains('d-none');
        closeEditors();
        if (wasHidden) {
            row.classList.remove('d-none');
            if (btn) btn.textContent = 'Close editor';
            syncEditorInputs(row);
            var note = row.querySelector('.identity-msg');
            if (note) { note.textContent = ''; note.className = 'small identity-msg'; }
            row.querySelector('[data-f="hostname"]').focus();
        }
    }
    function collectIdentity(row) {
        var autoName = row.querySelector('[data-f="auto_hostname"]').checked;
        var autoIp = row.querySelector('[data-f="auto_ip"]').checked;
        var sel = row.querySelector('[data-f="pool"]');
        return {
            mac: row.getAttribute('data-editor'),
            auto_hostname: autoName,
            hostname: autoName ? '' : row.querySelector('[data-f="hostname"]').value.trim(),
            auto_ip: autoIp,
            ip: autoIp ? '' : row.querySelector('[data-f="ip"]').value.trim(),
            pool: sel ? sel.value : ''
        };
    }
    function saveIdentity(row) {
        var payload = collectIdentity(row);
        var note = row.querySelector('.identity-msg');
        if (note) { note.textContent = 'saving...'; note.className = 'small identity-msg text-muted'; }
        api('/api/device/update', payload).then(function (d) {
            var text = 'saved: ' + d.assigned_hostname + ' / ' + d.assigned_ip + ' (pool ' + d.pool + ')' +
                       (d.notes && d.notes.length ? ' - ' + d.notes.join(', ') : '');
            closeEditors();
            deviceMsg(text + ' \u2713', true);
            setEditing(false);
            return refreshLive();
        }).catch(function (e) {
            if (note) { note.textContent = e.message; note.className = 'small identity-msg text-danger'; }
            deviceMsg('not saved: ' + e.message, false);
        });
    }
    function autoIdentity(row) {
        var sel = row.querySelector('[data-f="pool"]');
        var pool = sel ? sel.value : '';
        if (!confirm('Give this unit the next free IP of pool "' + pool + '" and let its computer name ' +
                     'follow the pool prefix?')) return;
        row.querySelector('[data-f="auto_hostname"]').checked = true;
        row.querySelector('[data-f="auto_ip"]').checked = true;
        syncEditorInputs(row);
        saveIdentity(row);
    }

    // ==================================================== MAC pre-assignments table
    function savePredefined() {
        var mac = $('pre-mac').value.trim();
        var ip = $('pre-ip').value.trim();
        var host = $('pre-host').value.trim();
        if (!mac || !ip) { preMsg('a MAC address and a fixed IP are both required', false); return; }
        api('/api/predefine/save', {mac: mac, ip: ip, hostname: host}).then(function (d) {
            preMsg('reserved ' + d.ip + (d.hostname ? ' / ' + d.hostname : '') +
                   (d.pool ? ' in pool ' + d.pool : '') +
                   (d.notes && d.notes.length ? ' (' + d.notes.join(', ') + ')' : '') +
                   (d.persisted ? ' \u2713 (saved)' : ' \u2713 (not persisted - server runs with --no-config)'),
                   true);
            $('pre-mac').value = '';
            $('pre-ip').value = '';
            $('pre-host').value = '';
            return refreshLive();
        }).catch(function (e) { preMsg(e.message, false); });
    }
    function removePredefined(mac) {
        if (!confirm('Remove the fixed IP / name reservation for ' + mac + '?')) return;
        api('/api/predefine/delete', {mac: mac}).then(function () {
            preMsg('reservation for ' + mac + ' removed \u2713', true);
            return refreshLive();
        }).catch(function (e) { preMsg(e.message, false); });
    }

    // ================================================================== wiring up
    var panel = $('network-settings');
    panel.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('[data-act]') : null;
        if (!btn || btn.closest('#device-rows')) return;
        var tr = btn.closest('tr');
        if (tr) handle(btn.getAttribute('data-act'), tr);
    });
    panel.addEventListener('focusin', function (ev) {
        if (ev.target.matches && ev.target.matches('input,select,textarea')) setEditing(true);
    });
    panel.addEventListener('input', function () { setEditing(true); });

    // The NUC table is re-rendered on every refresh, so its buttons are delegated.
    document.addEventListener('click', function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var editBtn = t.closest('.edit-device-btn');
        if (editBtn) { toggleEditor(editBtn.getAttribute('data-mac')); return; }
        var act = t.closest('#device-rows [data-act]');
        if (act) {
            var row = act.closest('.editor-row');
            var which = act.getAttribute('data-act');
            if (which === 'save-identity') saveIdentity(row);
            else if (which === 'auto-identity') autoIdentity(row);
            else if (which === 'cancel-edit') closeEditors();
            return;
        }
        var del = t.closest('.pre-delete');
        if (del) { removePredefined(del.getAttribute('data-mac')); return; }
        var adopt = t.closest('.adopt-bench');
        if (adopt) {
            if (!confirm('Move the default pool onto the network of this PC (' +
                         (adopt.getAttribute('data-address') || '') + ')? ' +
                         'Units already registered keep their current addresses.')) return;
            api('/api/bench/adopt', {
                address: adopt.getAttribute('data-address'),
                prefix: adopt.getAttribute('data-prefix'),
                gateway: adopt.getAttribute('data-gateway') || '',
                iface: adopt.getAttribute('data-iface') || ''
            }).then(function (d) {
                msg('Using the network of this PC' +
                    (d && d.network ? (': pool is now ' + d.network + ', master ' + (d.server_ip || '')) : '') +
                    ' \\u2713');
                setEditing(false);
                return load();
            }).catch(function (e) { msg(e.message, false); });
        }
    });
    document.addEventListener('change', function (ev) {
        var row = ev.target.closest ? ev.target.closest('#device-rows .editor-row') : null;
        var field = ev.target.getAttribute ? ev.target.getAttribute('data-f') : null;
        if (row && (field === 'auto_hostname' || field === 'auto_ip' || field === 'pool')) {
            syncEditorInputs(row);
        }
    });
    document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') {
            closeEditors();
            setEditing(false);
            return;
        }
        if (ev.key === 'Enter' && ev.target.closest && ev.target.closest('#device-rows .editor-row')) {
            ev.preventDefault();
            saveIdentity(ev.target.closest('.editor-row'));
        }
    });

    $('save-settings').addEventListener('click', function () {
        api('/api/config/save', {
            server_ip: $('set-server-ip').value.trim(),
            port: $('set-port').value.trim(),
            target_count: $('set-target').value.trim(),
            boot_agent: $('set-boot-agent') ? $('set-boot-agent').checked : true
        }).then(afterWrite('global settings saved')).catch(function (e) { msg(e.message, false); });
    });
    $('reload-config').addEventListener('click', function () { setEditing(false); load(); msg('reloaded'); });

    $('auto-refresh-toggle').addEventListener('click', function () { setAutoRefresh(!autoRefresh); });
    $('refresh-now').addEventListener('click', function () {
        refreshLive().then(function () { deviceMsg('tables refreshed \u2713', true); })
                     .catch(function (e) { deviceMsg('refresh failed: ' + e.message, false); });
    });
    $('pre-save').addEventListener('click', savePredefined);
    ['pre-mac', 'pre-ip', 'pre-host'].forEach(function (id) {
        $(id).addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); savePredefined(); } });
    });

    document.querySelectorAll('.copy-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var el = $(btn.getAttribute('data-target'));
            el.select();
            try { document.execCommand('copy'); btn.textContent = 'Copied'; }
            catch (e) { btn.textContent = 'Ctrl+C'; }
            setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
        });
    });

    function load() {
        api('/api/config').then(render).catch(function (e) { msg('config load failed: ' + e.message, false); });
        api('/api/hooks').then(function (h) {
            $('hook-prerun').value = h.prerun;
            $('hook-postrun').value = h.postrun;
        }).catch(function () {});
        refreshLive().catch(function () {});
    }

    autoRefresh = readAutoRefreshPreference();   // OFF unless it was switched on before
    renderAutoRefresh();
    load();

    setInterval(function () {
        if (editing && (Date.now() - editingSince) > EDIT_GRACE_MS) setEditing(false);
        if (!autoRefresh || busy()) return;
        refreshLive().catch(function () {});
    }, REFRESH_MS);
})();
"""


def build_pools_from_args(args):
    pools = []
    default_pool = SubnetPool(
        name="default",
        network=args.subnet,
        cidr=args.cidr,
        start_ip=args.start_ip,
        max_ip=args.max_ip,
        gateway=args.gateway,
        dns=args.dns,
        hostname_prefix=args.prefix,
    )
    pools.append(default_pool)
    for pool_str in (args.extra_pools or []):
        try:
            pool = SubnetPool.from_config_string(pool_str)
        except Exception as e:
            print(f"[!] Error parsing --subnet-pool '{pool_str}': {e}")
            sys.exit(1)
        if pools and pool.name.lower() == default_pool.name.lower():
            print(f"[!] Extra pool cannot be named 'default': {pool_str}")
            sys.exit(1)
        pools.append(pool)
        print(f"[*] Added subnet pool: '{pool.name}' -> {pool.network} "
              f"(hosts {pool.start_ip}-{pool.max_ip}, GW: {pool.gateway})")
    return pools, default_pool


def main():
    parser = argparse.ArgumentParser(
        description="NUC Mass Deployment & Key Registry Server (subnets editable in the web UI)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--subnet", default="192.168.1", help="Initial subnet for the default pool "
                        "(accepts '192.168.1' or CIDR '10.30.0.0/16'). Only used on the first run; "
                        "afterwards the pools come from --config / the web dashboard.")
    parser.add_argument("--start-ip", type=int, default=None,
                        help="First host number of the default pool "
                             "(default: 101 on a /24, first usable host on smaller subnets)")
    parser.add_argument("--max-ip", type=int, default=None,
                        help="Last host number of the default pool (default: 250, or the last "
                             "usable host of a smaller subnet)")
    parser.add_argument("--gateway", default=None, help="Default gateway (default: <subnet>.1)")
    parser.add_argument("--netmask", default="255.255.255.0", help=argparse.SUPPRESS)  # derived from --subnet
    parser.add_argument("--cidr", default="24", help="CIDR prefix of --subnet (default: 24)")
    parser.add_argument("--dns", default="1.1.1.1,8.8.8.8", help="DNS servers comma-separated")
    parser.add_argument("--prefix", default="nuc-", help="Hostname prefix (default: nuc-)")
    parser.add_argument("--server-ip", default=None, help="Address clients use to reach this server "
                        "(shown in the hook commands and injected into the client scripts). "
                        "Default: the gateway of the default pool")
    parser.add_argument("--output", default="inventory.csv", help="Output inventory CSV filename")
    parser.add_argument("--pool", default="ip_pool.csv", help="Custom MAC-to-IP pool CSV filename")
    parser.add_argument("--config", default=None, help=f"Network config JSON (default: {CONFIG_FILENAME} "
                        "next to the inventory CSV). Written by the web dashboard.")
    parser.add_argument("--no-config", action="store_true", help="Do not read/write any config file "
                        "(pools live only as long as the process)")
    parser.add_argument("--target", type=int, default=None, help="Fleet size shown on the dashboard "
                        "(default: 150). Later changed from the web UI (Network Settings).")
    parser.add_argument("--read-only", action="store_true", help="Disable editing of pools from the web UI")
    parser.add_argument("--subnet-pool", action="append", dest="extra_pools", metavar="CONFIG",
                        help="Additional subnet pool. Format: "
                             "'name=LabA,subnet=10.10.35.0/24,start=1,max=250,gw=10.10.35.1,dns=1.1.1.1;8.8.8.8,prefix=lab-'. "
                             "Use ';' between DNS servers because ',' separates the fields. Can be repeated.")
    args = parser.parse_args()

    output_csv = os.path.abspath(args.output)
    config_path = None if args.no_config else os.path.abspath(args.config or
                            os.path.join(os.path.dirname(output_csv), CONFIG_FILENAME))

    global inventory_mgr
    try:
        pools, default_pool = build_pools_from_args(args)
    except ValueError as e:
        print(f"[!] Invalid pool configuration: {e}")
        sys.exit(1)

    settings = {
        "server_ip": args.server_ip or default_pool.gateway,
        "port": args.port,
        "hostname_prefix": args.prefix,
        "target_count": args.target or 150,
        "default_pool": "default",
    }

    inventory_mgr = InventoryManager(
        output_csv=output_csv,
        pool_csv=os.path.abspath(args.pool),
        config_path=config_path,
        settings=settings,
        pools=pools,
        read_only=args.read_only,
    )

    # The persisted web configuration wins over the CLI defaults (CLI = first-run seed),
    # so subnets and default IPs survive a restart without touching any file.
    stored = InventoryManager.load_config(config_path) if config_path else None
    using_saved_config = False
    if stored and (stored.get("pools") or stored.get("settings")):
        try:
            stored_settings = dict(stored.get("settings") or {})
            if args.server_ip:
                stored_settings["server_ip"] = args.server_ip
            stored_settings["port"] = args.port  # the listening port always comes from the CLI
            if args.target:
                stored_settings["target_count"] = args.target
            inventory_mgr.apply_config({"pools": stored.get("pools") or [],
                                       "settings": stored_settings}, persist=False)
            using_saved_config = bool(inventory_mgr.subnet_pools)
            print(f"[*] Restored {len(inventory_mgr.subnet_pools)} subnet pool(s) from {config_path}")
        except ValueError as e:
            print(f"[!] Ignored {config_path}: {e} (using command line configuration)")

    if using_saved_config:
        # Addresses already handed out must never be offered again, even if the
        # saved cursor was rewound (e.g. by hand or by shrinking a range).
        inventory_mgr.sync_counters()
    else:
        # First run (or --no-config): continue after the highest address already deployed
        inventory_mgr.sync_counters()
        if config_path:
            inventory_mgr.save_config()
            print(f"[*] Wrote initial network configuration to {config_path} "
                  f"(edit subnets & default IPs from the web dashboard)")
    inventory_mgr._sync_aliases()

    # Change working directory to repo root so client files can be served
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(repo_root)

    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, DeploymentRequestHandler)

    print("=" * 78)
    print(" NUC MASS DEPLOYMENT & KEY HARVESTER SERVER RUNNING ")
    print("=" * 78)
    print(f" Web Dashboard & API : http://{args.host}:{args.port}/  (pools are editable there)")
    print(f" Inventory File      : {output_csv}")
    print(f" Network Config      : {config_path or '(disabled - --no-config)'}"
          f"{' [read-only]' if args.read_only else ''}")
    for pool in inventory_mgr.subnet_pools:
        tag = " (default)" if pool.name.lower() == str(inventory_mgr.settings['default_pool']).lower() else ""
        nxt = pool.ip_at(min(pool.current_ip_counter, pool.host_limit))
        print(f"   Pool '{pool.name}'{tag}: {pool.network} hosts {pool.start_ip}-{pool.max_ip} "
              f"| GW {pool.gateway} | DNS {pool.dns} | next IP {nxt}")
    print(f" Client Hook Target  : {inventory_mgr.hook_commands()['server']}")
    boot = "on" if coerce_bool(inventory_mgr.settings.get("boot_agent"), True) else "off"
    print(f" Boot identity agent : {boot}  (Keep NUCs in sync on every boot)")
    try:
        bench = inventory_mgr.bench_status()
    except Exception as exc:
        print(f"[!] Bench network check failed: {exc}")
        bench = {"warnings": [], "networks": []}
    if bench.get("networks"):
        print(f" This PC is on       : {describe_networks(bench['networks'])}")
    else:
        print(" This PC is on       : (no IPv4 network detected - bench check skipped)")
    for warning in bench.get("warnings") or []:
        print(format_startup_warning(warning))
    print("=" * 78)
    print("Waiting for NUCs to connect via Clonezilla / PXE ...\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        inventory_mgr.save_config()
        httpd.server_close()


if __name__ == "__main__":
    main()
