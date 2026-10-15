#!/usr/bin/env python3
"""
NUC Fleet Deployment & Inventory Server
Part of MGW_Kompy

Provides:
- Centralized Windows Key collection (MSDM ACPI & Registry)
- Automated Sequential or Custom IP/Hostname allocation
- Multiple Subnet Pool support (192.168.100.x, 10.10.35.x, 10.30.x.x, etc.)
- Real-time Web Dashboard for tracking 150 NUC deployments
- Static file delivery for Clonezilla pre-run and post-run scripts
"""

import sys
import os
import json
import csv
import time
import argparse
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# Global lock for thread-safe state and CSV writes
DATA_LOCK = threading.Lock()


class SubnetPool:
    """Represents a single IP subnet pool with its own range and settings."""
    def __init__(self, name, subnet, start_ip=101, max_ip=250,
                 gateway=None, netmask="255.255.255.0", cidr="24",
                 dns="1.1.1.1,8.8.8.8", hostname_prefix="nuc-"):
        self.name = name
        self.subnet = subnet          # e.g. "192.168.100"
        self.start_ip = start_ip
        self.max_ip = max_ip
        self.gateway = gateway or f"{subnet}.1"
        self.netmask = netmask
        self.cidr = cidr
        self.dns = dns
        self.hostname_prefix = hostname_prefix
        self.current_ip_counter = start_ip

    def allocate_next(self):
        """Allocate the next available IP from this pool. Returns (ip, hostname) or raises."""
        if self.current_ip_counter > self.max_ip:
            raise ValueError(f"Pool '{self.name}' exhausted (max {self.max_ip})")
        ip = f"{self.subnet}.{self.current_ip_counter}"
        hostname = f"{self.hostname_prefix}{self.current_ip_counter:03d}"
        self.current_ip_counter += 1
        return ip, hostname

    def to_dict(self):
        return {
            "name": self.name,
            "subnet": self.subnet,
            "start_ip": self.start_ip,
            "max_ip": self.max_ip,
            "gateway": self.gateway,
            "netmask": self.netmask,
            "cidr": self.cidr,
            "dns": self.dns,
            "hostname_prefix": self.hostname_prefix,
            "current_ip_counter": self.current_ip_counter,
            "next_ip": f"{self.subnet}.{self.current_ip_counter}",
            "available": max(0, self.max_ip - self.current_ip_counter + 1),
        }

    @staticmethod
    def from_config_string(config_str):
        """Parse 'name=Lab,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,...' format."""
        params = {}
        for part in config_str.split(","):
            part = part.strip()
            if "=" in part:
                key, val = part.split("=", 1)
                params[key.strip().lower()] = val.strip()
        if "subnet" not in params:
            raise ValueError(f"Missing 'subnet' in pool config: {config_str}")
        return SubnetPool(
            name=params.get("name", params["subnet"]),
            subnet=params["subnet"],
            start_ip=int(params.get("start", params.get("start_ip", 101))),
            max_ip=int(params.get("max", params.get("max_ip", 250))),
            gateway=params.get("gw", params.get("gateway")),
            netmask=params.get("mask", params.get("netmask", "255.255.255.0")),
            cidr=params.get("cidr", "24"),
            dns=params.get("dns", "1.1.1.1,8.8.8.8"),
            hostname_prefix=params.get("prefix", params.get("hostname_prefix", "nuc-")),
        )


class InventoryManager:
    def __init__(self, output_csv="inventory.csv", pool_csv="ip_pool.csv",
                 base_subnet="192.168.1", start_ip=101, max_ip=250,
                 gateway="192.168.1.1", netmask="255.255.255.0", cidr="24",
                 dns="1.1.1.1,8.8.8.8", hostname_prefix="nuc-",
                 extra_pools=None):
        self.output_csv = output_csv
        self.pool_csv = pool_csv
        self.hostname_prefix = hostname_prefix

        # Build list of subnet pools — first pool is the "default"
        self.subnet_pools = []
        default_pool = SubnetPool(
            name="default",
            subnet=base_subnet,
            start_ip=start_ip,
            max_ip=max_ip,
            gateway=gateway,
            netmask=netmask,
            cidr=cidr,
            dns=dns,
            hostname_prefix=hostname_prefix,
        )
        self.subnet_pools.append(default_pool)

        if extra_pools:
            for pool in extra_pools:
                self.subnet_pools.append(pool)

        # Convenience aliases pointing to the default pool (index 0)
        self.base_subnet = default_pool.subnet
        self.current_ip_counter = default_pool.current_ip_counter
        self.start_ip = default_pool.start_ip
        self.max_ip = default_pool.max_ip
        self.gateway = default_pool.gateway
        self.netmask = default_pool.netmask
        self.cidr = default_pool.cidr
        self.dns = default_pool.dns

        # In-memory store: mac -> record
        self.records = {}
        # Predefined pool mappings: mac -> {ip, hostname, gateway, netmask, dns}
        self.predefined_pool = {}

        self.load_predefined_pool()
        self.load_existing_inventory()

    def load_predefined_pool(self):
        if not os.path.exists(self.pool_csv):
            return
        try:
            with open(self.pool_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mac = row.get("mac", "").strip().lower()
                    if mac:
                        self.predefined_pool[mac] = {
                            "ip": row.get("ip", "").strip(),
                            "hostname": row.get("hostname", "").strip(),
                            "gateway": row.get("gateway", self.gateway).strip(),
                            "netmask": row.get("netmask", self.netmask).strip(),
                            "dns": row.get("dns", self.dns).strip(),
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
                    mac = row.get("mac", "").strip().lower()
                    if mac:
                        self.records[mac] = row
                        # Sync counter for whichever pool this IP belongs to
                        ip = row.get("assigned_ip", "")
                        for pool in self.subnet_pools:
                            if ip.startswith(pool.subnet + "."):
                                try:
                                    last_octet = int(ip.split(".")[-1])
                                    if last_octet >= pool.current_ip_counter:
                                        pool.current_ip_counter = last_octet + 1
                                except ValueError:
                                    pass
                                break
            # Keep convenience aliases in sync with default pool
            dp = self.subnet_pools[0]
            self.current_ip_counter = dp.current_ip_counter
            print(f"[*] Loaded {len(self.records)} existing records from {self.output_csv}. Next IP: {dp.subnet}.{dp.current_ip_counter}")
        except Exception as e:
            print(f"[!] Warning reading existing {self.output_csv}: {e}")
            self.init_csv_header()

    def init_csv_header(self):
        with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "status", "assigned_hostname", "assigned_ip",
                "mac", "serial", "model", "windows_key", "key_source",
                "notes", "pool"
            ])

    def save_record_to_csv(self, record):
        # Rewrite the entire CSV with all updated records to maintain sync
        temp_file = self.output_csv + ".tmp"
        with open(temp_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "timestamp", "status", "assigned_hostname", "assigned_ip",
                "mac", "serial", "model", "windows_key", "key_source",
                "notes", "pool"
            ])
            writer.writeheader()
            for r in self.records.values():
                writer.writerow({
                    "timestamp": r.get("timestamp", ""),
                    "status": r.get("status", ""),
                    "assigned_hostname": r.get("assigned_hostname", ""),
                    "assigned_ip": r.get("assigned_ip", ""),
                    "mac": r.get("mac", ""),
                    "serial": r.get("serial", ""),
                    "model": r.get("model", ""),
                    "windows_key": r.get("windows_key", ""),
                    "key_source": r.get("key_source", ""),
                    "notes": r.get("notes", ""),
                    "pool": r.get("pool", "default"),
                })
        os.replace(temp_file, self.output_csv)

    def get_pool_by_name(self, pool_name):
        """Find a pool by name (case-insensitive). Returns None if not found."""
        if not pool_name:
            return None
        for pool in self.subnet_pools:
            if pool.name.lower() == pool_name.lower():
                return pool
        return None

    def register_device(self, data):
        with DATA_LOCK:
            mac = data.get("mac", "").strip().lower()
            if not mac:
                return {"error": "Missing MAC address"}, 400

            # Determine which subnet pool to use
            requested_pool_name = data.get("pool", "").strip()
            target_pool = self.get_pool_by_name(requested_pool_name) or self.subnet_pools[0]

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
                device_pool = self.get_pool_by_name(record.get("pool", "default")) or self.subnet_pools[0]
                return {
                    "status": "already_registered",
                    "assigned_ip": record["assigned_ip"],
                    "assigned_hostname": record["assigned_hostname"],
                    "netmask": record.get("netmask", device_pool.netmask),
                    "cidr": device_pool.cidr,
                    "gateway": record.get("gateway", device_pool.gateway),
                    "dns": record.get("dns", device_pool.dns),
                    "windows_key": record.get("windows_key", ""),
                    "pool": record.get("pool", device_pool.name),
                }, 200

            # Check if MAC has predefined static IP in pool CSV
            if mac in self.predefined_pool:
                pool_entry = self.predefined_pool[mac]
                assigned_ip = pool_entry["ip"]
                assigned_hostname = pool_entry["hostname"]
                gateway = pool_entry.get("gateway", target_pool.gateway)
                netmask = pool_entry.get("netmask", target_pool.netmask)
                dns = pool_entry.get("dns", target_pool.dns)
                pool_name_used = "predefined"
            else:
                # Dynamic sequential allocation from the selected pool
                try:
                    assigned_ip, assigned_hostname = target_pool.allocate_next()
                except ValueError as e:
                    return {"error": str(e)}, 409
                gateway = target_pool.gateway
                netmask = target_pool.netmask
                dns = target_pool.dns
                pool_name_used = target_pool.name
                # Keep default-pool convenience aliases in sync
                if target_pool is self.subnet_pools[0]:
                    self.current_ip_counter = target_pool.current_ip_counter

            record = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "REGISTERED",
                "assigned_hostname": assigned_hostname,
                "assigned_ip": assigned_ip,
                "mac": mac,
                "serial": data.get("serial", "UNKNOWN").strip(),
                "model": data.get("model", "Intel NUC").strip(),
                "windows_key": data.get("windows_key", "NOT_FOUND").strip(),
                "key_source": data.get("key_source", "UNKNOWN").strip(),
                "notes": data.get("notes", "").strip(),
                "pool": pool_name_used,
                "gateway": gateway,
                "dns": dns,
            }

            self.records[mac] = record
            self.save_record_to_csv(record)

            print(f"[+] NEW DEVICE [{len(self.records)}/150] Host: {assigned_hostname} | IP: {assigned_ip} | Pool: {pool_name_used} | MAC: {mac} | S/N: {record['serial']} | WinKey: {record['windows_key']}")

            return {
                "status": "success",
                "assigned_ip": assigned_ip,
                "assigned_hostname": assigned_hostname,
                "netmask": netmask,
                "cidr": target_pool.cidr,
                "gateway": gateway,
                "dns": dns,
                "windows_key": record["windows_key"],
                "pool": pool_name_used,
            }, 200

    def reassign_device(self, mac, new_pool_name):
        """Move a device to a different subnet pool, assigning a new IP."""
        with DATA_LOCK:
            mac = mac.strip().lower()
            if mac not in self.records:
                return {"error": f"Device {mac} not found"}, 404

            target_pool = self.get_pool_by_name(new_pool_name)
            if not target_pool:
                return {"error": f"Pool '{new_pool_name}' not found"}, 404

            try:
                new_ip, new_hostname = target_pool.allocate_next()
            except ValueError as e:
                return {"error": str(e)}, 409

            record = self.records[mac]
            old_ip = record["assigned_ip"]
            record["assigned_ip"] = new_ip
            record["assigned_hostname"] = new_hostname
            record["pool"] = target_pool.name
            record["gateway"] = target_pool.gateway
            record["dns"] = target_pool.dns
            record["status"] = "REASSIGNED"
            record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["notes"] = f"Reassigned from {old_ip} to {new_ip} (pool: {target_pool.name})"
            self.save_record_to_csv(record)

            # Keep default-pool convenience aliases in sync
            if target_pool is self.subnet_pools[0]:
                self.current_ip_counter = target_pool.current_ip_counter

            print(f"[*] REASSIGNED {mac}: {old_ip} -> {new_ip} (pool: {target_pool.name})")

            return {
                "status": "success",
                "old_ip": old_ip,
                "assigned_ip": new_ip,
                "assigned_hostname": new_hostname,
                "netmask": target_pool.netmask,
                "cidr": target_pool.cidr,
                "gateway": target_pool.gateway,
                "dns": target_pool.dns,
                "pool": target_pool.name,
            }, 200

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

inventory_mgr: InventoryManager = None

class DeploymentRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress verbose standard HTTP access logs for cleaner terminal output
        if "/api/" not in self.path:
            return
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = self.render_dashboard()
            self.wfile.write(html.encode("utf-8"))
            return

        elif parsed.path == "/api/inventory":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with DATA_LOCK:
                data = list(inventory_mgr.records.values())
            self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))
            return

        elif parsed.path == "/api/pools":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            pools_data = [p.to_dict() for p in inventory_mgr.subnet_pools]
            self.wfile.write(json.dumps(pools_data, indent=2).encode("utf-8"))
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

        # Serve static client scripts directly from repo root
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            body = json.loads(post_data.decode("utf-8"))
        except Exception:
            body = {}

        if parsed.path == "/api/register":
            resp, code = inventory_mgr.register_device(body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        elif parsed.path == "/api/status":
            mac = body.get("mac", "")
            status = body.get("status", "UNKNOWN")
            notes = body.get("notes", "")
            success = inventory_mgr.update_status(mac, status, notes)
            self.send_response(200 if success else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": success}).encode("utf-8"))
            return

        elif parsed.path == "/api/reassign":
            mac = body.get("mac", "")
            pool = body.get("pool", "")
            resp, code = inventory_mgr.reassign_device(mac, pool)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def render_dashboard(self):
        with DATA_LOCK:
            records = list(inventory_mgr.records.values())
            pools_data = [p.to_dict() for p in inventory_mgr.subnet_pools]

        total = len(records)
        keys_found = sum(1 for r in records if r.get("windows_key") and r.get("windows_key") not in ["NOT_FOUND", "UNKNOWN", ""])
        completed = sum(1 for r in records if r.get("status") in ["CONFIGURED", "FINISHED", "COMPLETED"])

        # Build pool summary cards HTML
        pool_cards_html = ""
        for p in pools_data:
            used = p["start_ip"] - 1 + (p["current_ip_counter"] - p["start_ip"])
            pool_size = p["max_ip"] - p["start_ip"] + 1
            pct = int((p["current_ip_counter"] - p["start_ip"]) / max(pool_size, 1) * 100)
            pool_cards_html += f"""
            <div class="col-md-4 col-sm-6">
                <div class="card stat-card bg-white p-3">
                    <div class="d-flex justify-content-between align-items-start">
                        <span class="text-muted small text-uppercase fw-semibold">{p['name']}</span>
                        <span class="badge bg-light text-dark border">{p['subnet']}.x</span>
                    </div>
                    <h4 class="fw-bold my-1 text-dark fs-4">{p['next_ip']}</h4>
                    <small class="text-muted">Next IP &middot; {p['available']} remaining</small>
                    <div class="progress mt-2" style="height: 4px;">
                        <div class="progress-bar bg-secondary" style="width: {min(pct, 100)}%"></div>
                    </div>
                    <small class="text-muted mt-1">GW: {p['gateway']} &middot; /{p['cidr']}</small>
                </div>
            </div>
            """

        # Build pool options for reassign dropdown
        pool_options_html = ""
        for p in pools_data:
            pool_options_html += f"<option value='{p['name']}'>{p['name']} ({p['subnet']}.x)</option>"

        # Build table rows
        rows_html = ""
        for idx, r in enumerate(reversed(records), 1):
            key_val = r.get("windows_key", "NOT_FOUND")
            key_badge = f"<span class='badge bg-success font-monospace'>{key_val}</span>" if key_val != "NOT_FOUND" else "<span class='badge bg-secondary'>None</span>"
            status = r.get("status", "REGISTERED")
            status_cls = "bg-primary"
            if status in ["CONFIGURED", "COMPLETED", "FINISHED"]:
                status_cls = "bg-success"
            elif status == "CLONING":
                status_cls = "bg-warning text-dark"
            elif status == "REASSIGNED":
                status_cls = "bg-info text-dark"
            elif "ERROR" in status:
                status_cls = "bg-danger"

            pool_name = r.get("pool", "default")
            pool_badge = f"<span class='badge bg-outline-secondary border'>{pool_name}</span>"

            # Build per-row reassign dropdown options (exclude current pool)
            row_pool_options = ""
            for p in pools_data:
                if p["name"] != pool_name:
                    row_pool_options += f"<option value='{p['name']}'>{p['name']} ({p['subnet']}.x)</option>"

            rows_html += f"""
            <tr>
                <td>{total - idx + 1}</td>
                <td><strong>{r.get('assigned_hostname', '')}</strong></td>
                <td><code>{r.get('assigned_ip', '')}</code></td>
                <td>{pool_badge}</td>
                <td><code class='text-muted'>{r.get('mac', '')}</code></td>
                <td>{r.get('serial', '')}</td>
                <td>{r.get('model', '')}</td>
                <td>{key_badge}</td>
                <td><small>{r.get('key_source', '')}</small></td>
                <td><span class='badge {status_cls}'>{status}</span></td>
                <td><small>{r.get('timestamp', '')}</small></td>
                <td>
                    <div class="input-group input-group-sm">
                        <select class="form-select form-select-sm reassign-pool" data-mac="{r.get('mac', '')}" style="max-width:120px;">
                            {row_pool_options}
                        </select>
                        <button class="btn btn-outline-warning btn-sm reassign-btn" data-mac="{r.get('mac', '')}" title="Reassign to selected pool">↻</button>
                    </div>
                </td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NUC Deployment & Windows Key Registry</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <meta http-equiv="refresh" content="5">
    <style>
        body {{ background-color: #f8f9fa; font-family: system-ui, -apple-system, sans-serif; }}
        .stat-card {{ border-radius: 10px; border: none; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .reassign-btn {{ font-size: 0.75rem; }}
    </style>
</head>
<body class="py-4">
    <div class="container-fluid px-4">
        <div class="d-flex justify-content-between align-items-center mb-4 pb-2 border-bottom">
            <div>
                <h2 class="h3 fw-bold mb-1">NUC Deployment & Key Vault</h2>
                <p class="text-muted mb-0">Automated Clonezilla PXE Deployment & Windows Key Harvester (150 Target Units)</p>
            </div>
            <div>
                <a href="/export.csv" class="btn btn-outline-primary btn-sm me-2">Download CSV</a>
                <button onclick="location.reload()" class="btn btn-secondary btn-sm">Refresh</button>
            </div>
        </div>

        <div class="row g-3 mb-4">
            <div class="col-md-3">
                <div class="card stat-card bg-white p-3">
                    <span class="text-muted small text-uppercase fw-semibold">Total Registered</span>
                    <h3 class="display-6 fw-bold my-1 text-primary">{total} <small class="text-muted fs-6">/ 150</small></h3>
                    <div class="progress mt-2" style="height: 6px;">
                        <div class="progress-bar bg-primary" style="width: {(total/150)*100}%"></div>
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
                    <small class="text-muted">Configured pools</small>
                </div>
            </div>
        </div>

        <!-- Subnet Pools Overview -->
        <div class="card stat-card bg-white mb-4">
            <div class="card-header bg-white py-3 border-bottom">
                <h5 class="mb-0 fw-semibold">Subnet Pools</h5>
            </div>
            <div class="card-body">
                <div class="row g-3">
                    {pool_cards_html}
                </div>
            </div>
        </div>

        <div class="card stat-card bg-white">
            <div class="card-header bg-white py-3 border-bottom d-flex justify-content-between align-items-center">
                <h5 class="mb-0 fw-semibold">Connected NUC Systems</h5>
                <span class="badge bg-light text-dark border">Live updates every 5s</span>
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

    <script>
    document.querySelectorAll('.reassign-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
            var mac = this.getAttribute('data-mac');
            var select = document.querySelector('.reassign-pool[data-mac="' + mac + '"]');
            var pool = select ? select.value : '';
            if (!pool) {{ alert('Select a target pool first.'); return; }}
            if (!confirm('Reassign ' + mac + ' to pool "' + pool + '"?')) return;
            fetch('/api/reassign', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{mac: mac, pool: pool}})
            }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                if (data.status === 'success') {{
                    alert('Reassigned to ' + data.assigned_ip + ' (pool: ' + data.pool + ')');
                    location.reload();
                }} else {{
                    alert('Error: ' + (data.error || 'Unknown error'));
                }}
            }})
            .catch(function(e) {{ alert('Request failed: ' + e); }});
        }});
    }});
    </script>
</body>
</html>
"""
        return html


def main():
    parser = argparse.ArgumentParser(description="NUC Mass Deployment & Key Registry Server")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--subnet", default="192.168.1", help="Base subnet for default pool (default: 192.168.1)")
    parser.add_argument("--start-ip", type=int, default=101, help="First IP host number for default pool (default: 101)")
    parser.add_argument("--max-ip", type=int, default=250, help="Max IP host number for default pool (default: 250)")
    parser.add_argument("--gateway", default="192.168.1.1", help="Default gateway (default: 192.168.1.1)")
    parser.add_argument("--netmask", default="255.255.255.0", help="Netmask (default: 255.255.255.0)")
    parser.add_argument("--cidr", default="24", help="CIDR prefix (default: 24)")
    parser.add_argument("--dns", default="1.1.1.1,8.8.8.8", help="DNS servers comma-separated")
    parser.add_argument("--prefix", default="nuc-", help="Hostname prefix (default: nuc-)")
    parser.add_argument("--output", default="inventory.csv", help="Output inventory CSV filename")
    parser.add_argument("--pool", default="ip_pool.csv", help="Custom MAC-to-IP pool CSV filename")
    parser.add_argument("--subnet-pool", action="append", dest="extra_pools", metavar="CONFIG",
                        help="Additional subnet pool. Format: "
                             "'name=LabA,subnet=10.10.35,start=1,max=250,gw=10.10.35.1,mask=255.255.255.0,cidr=24,dns=1.1.1.1,prefix=lab-'. "
                             "Can be specified multiple times for multiple pools.")
    args = parser.parse_args()

    # Parse extra subnet pools from CLI
    extra_pools = []
    if args.extra_pools:
        for pool_str in args.extra_pools:
            try:
                pool = SubnetPool.from_config_string(pool_str)
                extra_pools.append(pool)
                print(f"[*] Added subnet pool: '{pool.name}' -> {pool.subnet}.x "
                      f"({pool.start_ip}-{pool.max_ip}, GW: {pool.gateway})")
            except Exception as e:
                print(f"[!] Error parsing --subnet-pool '{pool_str}': {e}")
                sys.exit(1)

    global inventory_mgr
    inventory_mgr = InventoryManager(
        output_csv=args.output,
        pool_csv=args.pool,
        base_subnet=args.subnet,
        start_ip=args.start_ip,
        max_ip=args.max_ip,
        gateway=args.gateway,
        netmask=args.netmask,
        cidr=args.cidr,
        dns=args.dns,
        hostname_prefix=args.prefix,
        extra_pools=extra_pools,
    )

    # Change working directory to repo root so client files can be served
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(repo_root)

    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, DeploymentRequestHandler)

    print("=" * 70)
    print(" NUC MASS DEPLOYMENT & KEY HARVESTER SERVER RUNNING ")
    print("=" * 70)
    print(f" Web Dashboard & API : http://{args.host}:{args.port}/")
    print(f" Inventory File      : {os.path.abspath(args.output)}")
    print(f" Default Pool        : {args.subnet}.{args.start_ip}-{args.max_ip} (GW: {args.gateway}, /{args.cidr})")
    for pool in extra_pools:
        print(f" Pool '{pool.name}': {pool.subnet}.{pool.start_ip}-{pool.max_ip} (GW: {pool.gateway}, /{pool.cidr})")
    print(f" Hostname Prefix     : {args.prefix}XXX")
    print(f" Subnet Pools Total  : {1 + len(extra_pools)}")
    print("=" * 70)
    print("Waiting for NUCs to connect via Clonezilla / PXE ...\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()

if __name__ == "__main__":
    main()
