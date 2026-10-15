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
  (client scripts get the current server IP/port/pool auto-injected on the fly)
"""

import sys
import os
import re
import json
import csv
import argparse
import threading
import ipaddress
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
from html import escape as html_escape

# Global lock for thread-safe state and CSV writes
DATA_LOCK = threading.Lock()

CONFIG_VERSION = 1
CONFIG_FILENAME = "network_config.json"

CSV_FIELDS = [
    "timestamp", "status", "assigned_hostname", "assigned_ip",
    "mac", "serial", "model", "windows_key", "key_source",
    "notes", "pool", "gateway", "netmask", "dns",
]

DEFAULT_SETTINGS = {
    "server_ip": "",          # Address clients use to reach this server
    "port": 8000,             # HTTP port for the dashboard + client hooks
    "hostname_prefix": "nuc-",  # Default prefix for newly created pools
    "target_count": 150,      # Fleet size shown on the dashboard
    "default_pool": "default",  # Pool used when a device asks for no pool
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
                 next_offset=None):
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
        return {
            "name": self.name,
            "network": str(self.network),
            "start": self.start_ip,
            "max": self.max_ip,
            "gateway": self.gateway,
            "dns": self.dns,
            "prefix": self.hostname_prefix,
            "next_offset": self.current_ip_counter,
        }

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
        return taken

    def pool_usage(self, pool_name):
        return sum(1 for r in self.records.values() if str(r.get("pool", "")).lower() == str(pool_name).lower())

    # ------------------------------------------------------------------- loading
    def load_predefined_pool(self):
        if not os.path.exists(self.pool_csv):
            return
        try:
            with open(self.pool_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mac = (row.get("mac") or "").strip().lower()
                    if mac:
                        self.predefined_pool[mac] = {
                            "ip": (row.get("ip") or "").strip(),
                            "hostname": (row.get("hostname") or "").strip(),
                            "gateway": (row.get("gateway") or "").strip() or self.gateway,
                            "netmask": (row.get("netmask") or "").strip() or self.netmask,
                            "dns": (row.get("dns") or "").strip() or self.dns,
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
    def register_device(self, data):
        with DATA_LOCK:
            mac = data.get("mac", "").strip().lower()
            if not mac:
                return {"error": "Missing MAC address"}, 400

            # Determine which subnet pool to use
            requested_pool_name = str(data.get("pool", "")).strip()
            target_pool = self.get_pool_by_name(requested_pool_name) or self.default_pool

            # If device already registered, return existing data
            if mac in self.records:
                record = self.records[mac]
                # Update key if previously missing and now found
                if not record.get("windows_key") and data.get("windows_key"):
                    record["windows_key"] = data.get("windows_key")
                    record["key_source"] = data.get("key_source", "UPDATED")
                    record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            }

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

    def update_status(self, mac, status, notes=""):
        with DATA_LOCK:
            mac = mac.strip().lower()
            if mac in self.records:
                self.records[mac]["status"] = status
                if notes:
                    self.records[mac]["notes"] = notes
                self.records[mac]["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.save_record_to_csv(self.records[mac])
                print(f"[*] STATUS UPDATE: {self.records[mac]['assigned_hostname']} ({mac}) -> {status}")
                return True
            return False

    # --------------------------------------------------------- dashboard helpers
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
                f"DEPLOY_SERVER={server} DEPLOY_PORT={port} DEPLOY_POOL={pool or 'default'}"
            ),
        }


inventory_mgr: InventoryManager = None


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
                }
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
        block = (
            "# ---- MGW_Kompy live configuration (managed in the web dashboard) ----\n"
            f'if [ -z "${{DEPLOY_SERVER:-}}" ] && [ -z "${{1:-}}" ]; then DEPLOY_SERVER="{server}"; fi\n'
            f': "${{DEPLOY_PORT:={port}}}"\n'
            f': "${{DEPLOY_POOL:={pool_name}}}"\n'
            "export DEPLOY_SERVER DEPLOY_PORT DEPLOY_POOL\n"
            "# ----------------------------------------------------------------------\n"
        )
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
                resp, code = inventory_mgr.register_device(body)
                self.send_json(resp, code)
                return

            elif parsed.path == "/api/status":
                success = inventory_mgr.update_status(body.get("mac", ""),
                                                       body.get("status", "UNKNOWN"),
                                                       body.get("notes", ""))
                self.send_json({"success": success}, 200 if success else 404)
                return

            elif parsed.path == "/api/reassign":
                resp, code = inventory_mgr.reassign_device(
                    body.get("mac", ""), body.get("pool", ""), body.get("ip"))
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
    def render_dashboard(self):
        with DATA_LOCK:
            records = list(inventory_mgr.records.values())
            pools_data = self.pools_payload()
            settings = dict(inventory_mgr.settings)

        total = len(records)
        target = settings.get("target_count", 150)
        keys_found = sum(1 for r in records
                         if r.get("windows_key") and r.get("windows_key") not in ("NOT_FOUND", "UNKNOWN", ""))
        completed = sum(1 for r in records if r.get("status") in ("CONFIGURED", "FINISHED", "COMPLETED"))

        # Build pool summary cards HTML
        pool_cards_html = ""
        for p in pools_data:
            pct = int((p["current_ip_counter"] - p["start"]) / max(p["size"], 1) * 100)
            star = " &#9733;" if p["is_default"] else ""
            pool_cards_html += f"""
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

        # Build table rows
        rows_html = ""
        for idx, r in enumerate(reversed(records), 1):
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
            elif status == "REASSIGNED":
                status_cls = "bg-info text-dark"
            elif "ERROR" in status:
                status_cls = "bg-danger"

            pool_name = r.get("pool", "default")
            pool_badge = f"<span class='badge bg-light text-dark border'>{html_escape(pool_name)}</span>"

            row_pool_options = "".join(
                f"<option value='{html_escape(p['name'], quote=True)}'>{html_escape(p['name'])} "
                f"({html_escape(p['network'])})</option>" for p in pools_data if p["name"] != pool_name)

            rows_html += f"""
            <tr>
                <td>{total - idx + 1}</td>
                <td><strong>{html_escape(r.get('assigned_hostname', ''))}</strong></td>
                <td><code>{html_escape(r.get('assigned_ip', ''))}</code></td>
                <td>{pool_badge}</td>
                <td><code class='text-muted'>{html_escape(r.get('mac', ''))}</code></td>
                <td>{html_escape(r.get('serial', ''))}</td>
                <td>{html_escape(r.get('model', ''))}</td>
                <td>{key_badge}</td>
                <td><small>{html_escape(r.get('key_source', ''))}</small></td>
                <td><span class='badge {status_cls}'>{html_escape(status)}</span></td>
                <td><small>{html_escape(r.get('timestamp', ''))}</small></td>
                <td>
                    <div class="input-group input-group-sm">
                        <select class="form-select form-select-sm reassign-pool" data-mac="{html_escape(r.get('mac', ''), quote=True)}" style="max-width:150px;">
                            {row_pool_options}
                        </select>
                        <button class="btn btn-outline-warning btn-sm reassign-btn" data-mac="{html_escape(r.get('mac', ''), quote=True)}" title="Reassign to selected pool">&#8635;</button>
                    </div>
                </td>
            </tr>
            """

        read_only = inventory_mgr.read_only
        banner = ("""
            <div class="alert alert-warning py-2 small mb-3">
                Server is running with <code>--read-only</code>: pool editing is disabled.
                Restart without that flag to edit subnets from this page.
            </div>""" if read_only else "")

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
                <span id="refresh-state" class="badge bg-light text-dark border me-2">live</span>
                <a href="/export.csv" class="btn btn-outline-primary btn-sm me-2">Download CSV</a>
                <button onclick="location.reload()" class="btn btn-secondary btn-sm">Refresh</button>
            </div>
        </div>

        {banner}

        <div class="row g-3 mb-4">
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Total Registered</span>
                    <h3 class="display-6 fw-bold my-1 text-primary">{total} <small class="text-muted fs-6">/ {target}</small></h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div class="progress-bar bg-primary" style="width: {(total/max(target,1))*100}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Windows Keys Saved</span>
                    <h3 class="display-6 fw-bold my-1 text-success">{keys_found}</h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div class="progress-bar bg-success" style="width: {(keys_found/max(total, 1))*100}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Completed / Configured</span>
                    <h3 class="display-6 fw-bold my-1 text-info">{completed}</h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div class="progress-bar bg-info" style="width: {(completed/max(total, 1))*100}%"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Subnet Pools Active</span>
                    <h3 class="display-6 fw-bold my-1 text-dark">{len(pools_data)}</h3>
                    <small class="text-muted">Default pool: <strong>{html_escape(settings.get('default_pool', 'default'))}</strong></small>
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
                <div class="row g-3">
                    {pool_cards_html or '<div class="col-12 text-muted">No pools configured.</div>'}
                </div>
            </div>
        </div>

        <div class="card stat-card bg-white">
            <div class="card-header bg-white py-3 border-bottom d-flex justify-content-between align-items-center">
                <h5 class="mb-0 fw-semibold">Connected NUC Systems</h5>
                <span class="badge bg-light text-dark border">auto-refresh every 5s (paused while editing)</span>
            </div>
            <div class="table-responsive">
                <table class="table table-hover align-middle mb-0">
                    <thead class="table-light">
                        <tr>
                            <th>#</th>
                            <th>Hostname</th>
                            <th>IP Address</th>
                            <th>Pool</th>
                            <th>MAC Address</th>
                            <th>Serial Number</th>
                            <th>Model</th>
                            <th>Windows Key</th>
                            <th>Key Source</th>
                            <th>Status</th>
                            <th>Timestamp</th>
                            <th>Reassign</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html if rows_html else "<tr><td colspan='12' class='text-center py-4 text-muted'>No devices connected yet. Start PXE booting your NUCs!</td></tr>"}
                    </tbody>
                </table>
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
        .reassign-btn { font-size: 0.75rem; }
        .pool-table input, .pool-table select { font-size: .8rem; }
        .pool-table input { border-color: #e3e6ea; padding: .2rem .4rem; width: 100%; min-width: 74px; }
        .pool-table input:focus { border-color: #86b7fe; box-shadow: none; }
        .pool-table td { vertical-align: middle; }
        .pool-table .row-name { max-width: 120px; }
        .pool-table tfoot td { background: #fbfcfd; border-top: 2px solid #e9ecef; }
        .pool-row-actions { white-space: nowrap; text-align: right; }
        .pool-row-actions .btn { font-size: .72rem; padding: .15rem .4rem; }
        .exhausted { color: #dc3545 !important; }
"""

DASHBOARD_JS = """
(function () {
    var REFRESH_MS = 5000;               // dashboard auto-refresh
    var EDIT_GRACE_MS = 90000;           // resume refreshing after 90s of inactivity
    var READ_ONLY = false;
    var editing = false;
    var editingSince = 0;
    var FIELDS = ['name', 'network', 'start', 'max', 'next_ip', 'gateway', 'dns', 'prefix'];

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v === null || v === undefined ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function setEditing(on) {
        editing = on;
        if (on) editingSince = Date.now();
        var badge = $('refresh-state');
        if (badge) {
            badge.textContent = on ? 'editing - auto-refresh paused' : 'live';
            badge.className = on ? 'badge bg-warning text-dark me-2' : 'badge bg-light text-dark border me-2';
        }
    }
    function msg(text, ok) {
        var el = $('config-msg');
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
        ['set-server-ip', 'set-port', 'set-target', 'save-settings'].forEach(function (id) {
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
            }).catch(function () {});
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

    var panel = $('network-settings');
    panel.addEventListener('click', function (ev) {
        var btn = ev.target.closest ? ev.target.closest('[data-act]') : null;
        if (!btn) return;
        var tr = btn.closest('tr');
        if (tr) handle(btn.getAttribute('data-act'), tr);
    });
    panel.addEventListener('focusin', function (ev) {
        if (ev.target.matches && ev.target.matches('input,select,textarea')) setEditing(true);
    });
    panel.addEventListener('input', function () { setEditing(true); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setEditing(false); });

    $('save-settings').addEventListener('click', function () {
        api('/api/config/save', {
            server_ip: $('set-server-ip').value.trim(),
            port: $('set-port').value.trim(),
            target_count: $('set-target').value.trim()
        }).then(afterWrite('global settings saved')).catch(function (e) { msg(e.message, false); });
    });
    $('reload-config').addEventListener('click', function () { setEditing(false); load(); msg('reloaded'); });

    document.querySelectorAll('.copy-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var el = $(btn.getAttribute('data-target'));
            el.select();
            try { document.execCommand('copy'); btn.textContent = 'Copied'; }
            catch (e) { btn.textContent = 'Ctrl+C'; }
            setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
        });
    });

    document.querySelectorAll('.reassign-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var mac = this.getAttribute('data-mac');
            var select = document.querySelector('.reassign-pool[data-mac="' + mac + '"]');
            var pool = select ? select.value : '';
            if (!pool) { alert('Select a target pool first.'); return; }
            if (!confirm('Reassign ' + mac + ' to pool "' + pool + '" (new IP)?')) return;
            api('/api/reassign', {mac: mac, pool: pool})
                .then(function (d) { alert('Reassigned to ' + d.assigned_ip + ' (pool: ' + d.pool + ')'); location.reload(); })
                .catch(function (e) { alert('Error: ' + e); });
        });
    });

    function load() {
        api('/api/config').then(render).catch(function (e) { msg('config load failed: ' + e.message, false); });
        api('/api/hooks').then(function (h) {
            $('hook-prerun').value = h.prerun;
            $('hook-postrun').value = h.postrun;
        }).catch(function () {});
    }
    load();

    setInterval(function () {
        if (editing) {
            var focused = document.activeElement;
            var stillTyping = focused && focused.closest && focused.closest('#pool-rows input, #network-settings input');
            if (stillTyping || (Date.now() - editingSince) < EDIT_GRACE_MS) return;
            setEditing(false);
        }
        location.reload();
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
