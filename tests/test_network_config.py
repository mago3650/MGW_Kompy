#!/usr/bin/env python3
"""
Unit checks for the web-editable network configuration.

Runnable either way (no third-party dependencies):
    python3 tests/test_network_config.py
    pytest tests/test_network_config.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import inventory_server as S  # noqa: E402


# --------------------------------------------------------------------- subnet parsing
def test_parse_network_accepts_common_forms():
    cases = {
        ("192.168.1", None): "192.168.1.0/24",       # legacy 3-octet form
        ("192.168.100.1", None): "192.168.100.0/24",  # a host address still means its /24
        ("10.30.1.5/16", None): "10.30.0.0/16",       # host bits are dropped, not rejected
        ("10.30/16", None): "10.30.0.0/16",
        ("10.30.1", "24"): "10.30.1.0/24",            # legacy + explicit --cidr
        ("10.0.0.0/8", None): "10.0.0.0/8",
    }
    for (value, cidr), want in cases.items():
        assert str(S.SubnetPool.parse_network(value, cidr)) == want, value


def test_parse_network_rejects_junk():
    for value in ["", "abc", "999.1.1.0/24", "10.1.1.256/24", "192.168.1.0/33",
                  "::1/64", "10.0.0.0/31", "10.0.0.0/32"]:
        try:
            S.SubnetPool.parse_network(value, None)
        except ValueError:
            continue
        raise AssertionError(f"{value!r} should have been rejected")


def test_netmask_and_cidr_are_derived_not_typed():
    pool = S.SubnetPool(name="p", network="10.30.0.0/16")
    assert pool.netmask == "255.255.0.0" and pool.cidr == "16" and pool.subnet == "10.30"
    pool = S.SubnetPool(name="q", network="192.168.1")
    assert pool.netmask == "255.255.255.0" and pool.subnet == "192.168.1"


# ------------------------------------------------------------------------- allocation
def test_defaults_and_offset_math():
    pool = S.SubnetPool(name="default", network="192.168.1.0/24")
    assert (pool.start_ip, pool.max_ip) == (101, 250)          # classic bench defaults
    assert pool.ip_at(101) == "192.168.1.101"
    assert pool.hostname_for(101) == "nuc-101"
    small = S.SubnetPool(name="tiny", network="10.1.1.0/30")
    assert (small.start_ip, small.max_ip) == (1, 2)             # never starts past the range
    assert small.ip_at(2) == "10.1.1.2"


def test_allocation_skips_used_addresses():
    pool = S.SubnetPool(name="p", network="192.168.1.0/24", start_ip=101, max_ip=103)
    ip, host = pool.allocate_next({101})
    assert (ip, host) == ("192.168.1.102", "nuc-102")
    ip, _ = pool.allocate_next({101, 102, 103 - 1})
    assert ip == "192.168.1.103"
    try:
        pool.allocate_next({101, 102, 103})
    except ValueError as e:
        assert "exhausted" in str(e)
    else:
        raise AssertionError("exhausted pool must raise")


def test_setting_next_ip_widens_the_range_instead_of_dying():
    pool = S.SubnetPool(name="p", network="10.20.30.0/24", start_ip=101, max_ip=250)
    pool.set_next("10.20.30.10", strict=True)                   # below start
    assert pool.start_ip == 10 and pool.current_ip_counter == 10
    pool.set_next("10.20.30.250", strict=True)
    pool.set_next("10.20.30.254", strict=True)                   # above max
    assert pool.max_ip == 254 and "range end widened" in pool.adjustments[0]
    for bad in ["172.16.0.1", "banana", "99999", "10.20.31.5"]:
        try:
            pool.set_next(bad, strict=True)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_gateway_rederives_or_warns_when_the_subnet_changes():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    # subnet change without touching the gateway field -> kept, but reported
    out = mgr.mutate_pool({"original_name": "default", "network": "10.9.9.0/24"})
    assert out["pool"]["gateway"] == "192.168.1.1"
    assert any("outside" in n for n in out["notes"]), out["notes"]
    # blanking the field re-derives it from the new subnet
    out = mgr.mutate_pool({"original_name": "default", "gateway": "  "})
    assert out["pool"]["gateway"] == "10.9.9.1", out["pool"]
    assert not out["notes"], out["notes"]


def test_gateway_dns_and_hostname_are_validated():
    pool = S.SubnetPool(name="p", network="10.5.0.0/16", gateway="auto")
    assert pool.gateway == "10.5.0.1"
    for kwargs in [{"gateway": "10.5.999.1"}, {"dns": "8.8.8.8,nope"},
                   {"hostname_prefix": "bad prefix!"}, {"name": "has,comma"}]:
        try:
            S.SubnetPool(name=kwargs.pop("name", "p"), network="10.5.0.0/16", **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"{kwargs} should have been rejected")


# --------------------------------------------------------------------- config storage
def _manager(tmpdir, pools=None, **settings):
    cfg = {
        "server_ip": "192.168.1.1", "port": 8000, "hostname_prefix": "nuc-",
        "target_count": 150, "default_pool": "default",
    }
    cfg.update(settings)
    return S.InventoryManager(
        output_csv=os.path.join(tmpdir, "inventory.csv"),
        pool_csv=os.path.join(tmpdir, "missing-ip-pool.csv"),
        config_path=os.path.join(tmpdir, "network_config.json"),
        settings=cfg,
        pools=pools or [S.SubnetPool(name="default", network="192.168.1.0/24")],
    )


def test_config_round_trip(tmpdir=None):
    tmp = tmpdir or tempfile.mkdtemp()
    tmp = str(tmp)
    mgr = _manager(tmp)
    mgr.register_device({"mac": "AA:BB:CC:00:00:01", "serial": "SN1"})
    mgr.mutate_pool({"original_name": "default", "name": "bench", "network": "10.9.9.0/24",
                     "start": 10, "max": 20, "next_ip": "10.9.9.15", "prefix": "bn-"})

    stored = json.load(open(mgr.config_path, encoding="utf-8"))
    assert stored["pools"][0]["name"] == "bench"
    assert stored["pools"][0]["network"] == "10.9.9.0/24"
    assert stored["settings"]["default_pool"] == "bench"

    # reopen exactly like a server restart does
    fresh_pools = [S.SubnetPool.from_config_dict(p) for p in stored["pools"]]
    mgr2 = _manager(tmp, pools=fresh_pools, settings=stored["settings"])
    mgr2.load_predefined_pool()
    mgr2.load_existing_inventory()
    mgr2.sync_counters()
    assert mgr2.get_pool_by_name("bench") is not None
    resp, code = mgr2.register_device({"mac": "aa:bb:cc:00:00:02"})
    assert code == 200 and resp["assigned_ip"] == "10.9.9.15", resp
    assert resp["assigned_hostname"] == "bn-015"
    # the device registered before the restart keeps its original lease
    again, _ = mgr2.register_device({"mac": "aa:bb:cc:00:00:01"})
    assert again["status"] == "already_registered" and again["assigned_ip"] == "192.168.1.101"


def test_pool_lifecycle_and_guards(tmpdir=None):
    tmp = str(tmpdir or tempfile.mkdtemp())
    mgr = _manager(tmp)
    resp, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:09"})
    assert resp["assigned_ip"] == "192.168.1.101"

    out = mgr.mutate_pool({"name": "office", "network": "172.20.0.0/24", "start": 1, "max": 5})
    assert any(p["name"] == "office" for p in out["pools"])
    try:
        mgr.mutate_pool({"name": "office", "network": "172.21.0.0/24"})
        raise AssertionError("duplicate create must fail")
    except ValueError as e:
        assert "already exists" in str(e)

    # rename keeps ownership of the devices assigned from that pool
    mgr.mutate_pool({"original_name": "default", "name": "bench"})
    assert mgr.records["aa:bb:cc:00:00:09"]["pool"] == "bench"
    try:
        mgr.mutate_pool({"original_name": "office", "name": "bench"})
        raise AssertionError("rename onto an existing name must fail")
    except ValueError as e:
        assert "collide" in str(e)

    # delete guards
    try:
        mgr.mutate_pool({"original_name": "bench"}, mode="delete")
        raise AssertionError("deleting an in-use pool must require confirmation")
    except ValueError as e:
        assert "force" in str(e)
    mgr.mutate_pool({"original_name": "office", "force": True}, mode="delete")
    try:
        mgr.mutate_pool({"original_name": "bench", "force": True}, mode="delete")
        raise AssertionError("the last pool must not be deletable")
    except ValueError as e:
        assert "last remaining" in str(e)

    # a device reassigns to a pool, and to an explicit address inside it
    mgr.mutate_pool({"name": "depot", "network": "10.40.0.0/16", "start": 1, "max": 100})
    out, code = mgr.reassign_device("AA:BB:CC:00:00:09", "depot")
    assert code == 200 and out["assigned_ip"] == "10.40.0.1", out
    out, code = mgr.reassign_device("aa:bb:cc:00:00:09", "depot", "10.40.0.50")
    assert code == 200 and out["assigned_hostname"] == "nuc-050", out
    _, code = mgr.reassign_device("aa:bb:cc:00:00:09", "depot", "10.41.0.50")
    assert code == 400, "an address outside the pool must be refused"


def test_register_response_has_everything_the_client_needs():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.mutate_pool({"name": "lab", "network": "10.50.0.0/16", "start": 1, "max": 500,
                     "gateway": "10.50.0.254", "dns": "1.1.1.1,8.8.8.8", "prefix": "lab-"})
    resp, code = mgr.register_device({"mac": "aa:bb:cc:00:00:77", "pool": "lab"})
    assert code == 200
    for key, want in {"assigned_ip": "10.50.0.1", "assigned_hostname": "lab-001",
                      "netmask": "255.255.0.0", "cidr": "16", "gateway": "10.50.0.254",
                      "dns": "1.1.1.1,8.8.8.8", "pool": "lab"}.items():
        assert resp[key] == want, (key, resp[key], want)
    # configure_network.sh greps these out of the cached JSON, so they must survive a restart too
    again, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:77"})
    for key in ("netmask", "cidr", "gateway", "dns"):
        assert again[key] == resp[key], key


def test_hook_commands_follow_the_dashboard_settings():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.apply_config({"settings": {"server_ip": "10.1.2.3", "port": 9000,
                                  "default_pool": "wip"},
                      "pools": [{"name": "wip", "network": "10.1.2.0/24", "start": 1, "max": 9}]})
    hooks = mgr.hook_commands()
    assert "http://10.1.2.3:9000/client/extract_and_register.sh" in hooks["prerun"]
    assert "wip" in hooks["prerun"]
    assert "http://10.1.2.3:9000/client/configure_network.sh" in hooks["postrun"]


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL  {name}: {exc}")
                sys.exit(1)
            print(f"ok    {name}")
            passed += 1
    print(f"\n{passed} tests passed")
