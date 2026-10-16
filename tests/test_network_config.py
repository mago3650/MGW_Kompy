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


# ------------------------------------------- per-device computer name and exact IP
def test_hostname_and_mac_validation():
    assert S.clean_hostname("  nuc-101 ") == "nuc-101"
    assert S.clean_hostname("A") == "A"
    assert S.clean_hostname("workshop-01") == "workshop-01"
    for bad in ["", "   ", "has space", "bad!", "nuc_101", "-lead", "trail-", "a" * 64, "dot.name"]:
        try:
            S.clean_hostname(bad)
        except ValueError:
            continue
        raise AssertionError(f"hostname {bad!r} should have been rejected")

    assert S.clean_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert S.clean_mac(" aa:bb:cc:dd:ee:ff ") == "aa:bb:cc:dd:ee:ff"
    for bad in ["", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aabbccddeeff", "aa:bb:cc:dd:ee"]:
        try:
            S.clean_mac(bad)
        except ValueError:
            continue
        raise AssertionError(f"MAC {bad!r} should have been rejected")

    assert S.format_pinned({"name", "ip"}) == "ip,name"
    assert S.parse_pinned("ip,name") == {"ip", "name"}
    assert S.parse_pinned("") == set() and S.parse_pinned(None) == set()


def test_pin_an_exact_ip_and_a_custom_computer_name():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"})
    mgr.register_device({"mac": "aa:bb:cc:00:00:02"})

    out, code = mgr.set_device_identity("AA:BB:CC:00:00:01", hostname="workshop-01",
                                        ip="192.168.1.150")
    assert code == 200, out
    assert (out["assigned_ip"], out["assigned_hostname"], out["pool"]) == \
        ("192.168.1.150", "workshop-01", "default"), out
    assert S.parse_pinned(out["pinned"]) == {"ip", "name"}
    assert out["device"]["status"] == "ASSIGNED"
    assert "192.168.1.150" in out["device"]["notes"]

    # the address is reserved now: the next unit must never receive it
    resp, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:03"})
    assert resp["assigned_ip"] != "192.168.1.150", resp

    # what the post-run hook reads back must be the pinned identity
    again, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:01"})
    assert again["assigned_hostname"] == "workshop-01"
    assert again["assigned_ip"] == "192.168.1.150"
    assert again["status"] == "already_registered"


def test_identity_input_is_validated():
    mgr = _manager(tempfile.mkdtemp())
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"})           # 192.168.1.101
    mgr.register_device({"mac": "aa:bb:cc:00:00:02"})           # 192.168.1.102
    mgr.mutate_pool({"name": "office", "network": "10.10.35.0/24", "start": 10, "max": 20})

    cases = [
        ({"mac": "aa:bb:cc:00:00:01", "hostname": "nuc-102"}, 409),          # name taken
        ({"mac": "aa:bb:cc:00:00:01", "hostname": "no spaces"}, 400),
        ({"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.102"}, 409),          # address taken
        ({"mac": "aa:bb:cc:00:00:01", "ip": "10.99.0.5"}, 400),              # outside every pool
        ({"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.0"}, 400),            # network address
        ({"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.255"}, 400),          # broadcast
        ({"mac": "aa:bb:cc:00:00:01", "ip": "not.an.ip"}, 400),
        ({"mac": "aa:bb:cc:00:00:01", "pool": "nope", "ip": "10.10.35.11"}, 404),
        ({"mac": "aa:bb:cc:00:00:01", "pool": "office", "ip": "192.168.1.150"}, 400),  # pool mismatch
        ({"mac": "aa:bb:cc:00:00:99", "hostname": "ghost"}, 404),            # not registered yet
        ({"mac": "", "hostname": "ghost"}, 400),
    ]
    for payload, want in cases:
        _, code = mgr.set_device_identity(payload.get("mac", ""),
                                          hostname=payload.get("hostname"),
                                          ip=payload.get("ip"),
                                          pool_name=payload.get("pool"))
        assert code == want, (payload, code, want)

    # nothing was changed by the rejected calls
    assert mgr.records["aa:bb:cc:00:00:01"]["assigned_ip"] == "192.168.1.101"
    assert mgr.records["aa:bb:cc:00:00:01"]["assigned_hostname"] == "nuc-101"

    # a valid cross-pool pin moves the unit into the owning pool
    out, code = mgr.set_device_identity("aa:bb:cc:00:00:01", ip="10.10.35.11")
    assert code == 200 and out["pool"] == "office", out
    assert out["assigned_hostname"] == "nuc-011"     # auto name follows the new offset
    assert S.parse_pinned(out["pinned"]) == {"ip"}


def test_auto_name_follows_and_can_be_given_back():
    mgr = _manager(tempfile.mkdtemp())
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"})       # nuc-101

    # pinning only the IP renames the unit after the new offset (the name was never pinned)
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", ip="192.168.1.140")
    assert out["assigned_hostname"] == "nuc-140", out
    assert out["pinned"] == "ip"

    # a typed name is pinned, so a later move keeps it
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", hostname="fixed-name")
    assert out["pinned"] == "ip,name"
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", ip="192.168.1.141")
    assert out["assigned_hostname"] == "fixed-name", out

    # "auto name" hands the name back to the pool prefix
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", auto_hostname=True)
    assert out["assigned_hostname"] == "nuc-141" and out["pinned"] == "ip", out

    # "next free IP" of the pool gives up the pin but keeps the name it had
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", auto_ip=True)
    assert out["pinned"] == "", out
    assert out["assigned_ip"] == "192.168.1.142", out       # 101..141 are used/skipped
    assert out["assigned_hostname"] == "nuc-142", out

    # a pinned name survives an "auto_ip" renumbering
    mgr.set_device_identity("aa:bb:cc:00:00:01", hostname="keeper")
    out, _ = mgr.set_device_identity("aa:bb:cc:00:00:01", auto_ip=True)
    assert out["assigned_hostname"] == "keeper" and out["pinned"] == "name", out


def test_pinned_identity_and_preassignments_survive_a_restart():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"})
    mgr.set_device_identity("aa:bb:cc:00:00:01", hostname="workshop-01", ip="192.168.1.150")
    mgr.set_predefined_entry({"mac": "aa:bb:cc:00:00:07", "ip": "192.168.1.120",
                              "hostname": "reserved-07"})

    # restart exactly like the server does: same files, fresh objects
    fresh = _manager(tmp)
    assert fresh.records["aa:bb:cc:00:00:01"]["assigned_hostname"] == "workshop-01"
    assert fresh.records["aa:bb:cc:00:00:01"]["assigned_ip"] == "192.168.1.150"
    assert S.parse_pinned(fresh.records["aa:bb:cc:00:00:01"]["pinned"]) == {"ip", "name"}
    assert fresh.predefined_pool["aa:bb:cc:00:00:07"]["hostname"] == "reserved-07"

    resp, _ = fresh.register_device({"mac": "aa:bb:cc:00:00:07"})
    assert (resp["assigned_ip"], resp["assigned_hostname"]) == ("192.168.1.120", "reserved-07"), resp


def test_preassigned_mac_gets_exactly_that_ip_and_name():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    out, code = mgr.set_predefined_entry({"mac": "AA-BB-CC-00-00:07", "ip": "192.168.1.140",
                                          "hostname": "reserved-07"})
    assert code == 200 and out["mac"] == "aa:bb:cc:00:00:07", out
    assert out["pool"] == "default" and out["persisted"] is True
    assert os.path.exists(mgr.pool_csv)

    # the reservation wins over sequential allocation, and the address is skipped
    resp, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:07"})
    assert (resp["assigned_ip"], resp["assigned_hostname"]) == ("192.168.1.140", "reserved-07")
    assert mgr.records["aa:bb:cc:00:00:07"]["pinned"] == "ip,name"   # shown as a pin in the table
    assert mgr.register_device({"mac": "aa:bb:cc:00:00:08"})[0]["assigned_ip"] == "192.168.1.101"

    # guards: bad input, duplicate address, already-registered MAC, unknown MAC
    for payload, want in [
        ({"mac": "nope", "ip": "192.168.1.141"}, 400),
        ({"mac": "aa:bb:cc:00:00:09", "ip": "192.168.1.999"}, 400),
        ({"mac": "aa:bb:cc:00:00:09", "ip": "192.168.1.140"}, 409),      # already reserved
        ({"mac": "aa:bb:cc:00:00:07", "ip": "192.168.1.141"}, 409),      # already registered
        ({"mac": "aa:bb:cc:00:00:09", "ip": "192.168.1.141", "hostname": "bad name"}, 400),
    ]:
        _, code = mgr.set_predefined_entry(payload)
        assert code == want, (payload, code, want)

    # an address outside the pool range is still allowed, but reported
    out, code = mgr.set_predefined_entry({"mac": "aa:bb:cc:00:00:0a", "ip": "192.168.1.20"})
    assert code == 200 and any("outside" in n for n in out["notes"]), out

    # deleting frees it again
    out, code = mgr.set_predefined_entry({"mac": "aa:bb:cc:00:00:0a"}, remove=True)
    assert code == 200 and "aa:bb:cc:00:00:0a" not in out["predefined"], out
    assert mgr.set_predefined_entry({"mac": "aa:bb:cc:00:00:0a"}, remove=True)[1] == 404


def test_dashboard_ships_the_editors_and_auto_refresh_toggle_off():
    mgr = _manager(tempfile.mkdtemp())
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"})
    previous, S.inventory_mgr = S.inventory_mgr, mgr
    try:
        handler = S.DeploymentRequestHandler.__new__(S.DeploymentRequestHandler)   # no socket needed
        rows = handler.render_device_rows(list(mgr.records.values()), handler.pools_payload())
        for needle in ('class="editor-row', 'edit-device-btn', 'data-f="hostname"', 'data-f="auto_ip"',
                       'data-act="save-identity"', 'data-act="auto-identity"', 'pool-next'):
            assert needle in rows, needle

        stats = handler.dashboard_stats(list(mgr.records.values()))
        assert stats["total"] == 1 and stats["target"] == 150 and stats["pools"] == 1, stats

        page = handler.render_dashboard()
        assert "auto-refresh-toggle" in page and "Auto-refresh: off" in page
        assert "refresh-now" in page and "predefined-rows" in page and "device-rows" in page
        assert 'id="stat-total"' in page
        # auto-refresh is off unless it was switched on before (?refresh=on / localStorage)
        assert "localStorage.getItem(AUTO_KEY) === 'on'" in S.DASHBOARD_JS
        assert "autoRefresh = readAutoRefreshPreference();" in S.DASHBOARD_JS
        assert "location.reload()" not in S.DASHBOARD_JS      # refreshes must not reload the page
    finally:
        S.inventory_mgr = previous


# ------------------------------------------------ bench network + seen address
def test_checkin_records_the_address_it_really_came_from():
    mgr = _manager(tempfile.mkdtemp())
    resp, code = mgr.register_device({"mac": "aa:bb:cc:00:00:01"}, seen_ip="192.168.169.45")
    assert code == 200, resp
    rec = mgr.records["aa:bb:cc:00:00:01"]
    assert rec["seen_ip"] == "192.168.169.45" and rec["seen_at"], rec
    assert rec["assigned_ip"] == "192.168.1.101"

    # a later check-in from the assigned address replaces the previous sighting
    again, _ = mgr.register_device({"mac": "aa:bb:cc:00:00:01"}, seen_ip="192.168.1.101")
    assert again["seen_ip"] == "192.168.1.101"
    assert mgr.ip_mismatches() == []

    # the post-run / boot-agent status call is a check-in too
    assert mgr.update_status("aa:bb:cc:00:00:01", "CONFIGURED", seen_ip="192.168.169.77")
    mismatches = mgr.ip_mismatches()
    assert len(mismatches) == 1, mismatches
    assert mismatches[0]["hostname"] == "nuc-101"
    assert mismatches[0]["seen_ip"] == "192.168.169.77"
    assert mismatches[0]["assigned_ip"] == "192.168.1.101"
    text = open(mgr.output_csv, encoding="utf-8").read()
    assert "seen_ip" in text.splitlines()[0]
    assert "192.168.169.77" in text


def test_seen_column_and_mismatch_banner_name_the_unit():
    mgr = _manager(tempfile.mkdtemp())
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"}, seen_ip="192.168.169.45")
    mgr.register_device({"mac": "aa:bb:cc:00:00:02"}, seen_ip="192.168.1.102")
    previous, S.inventory_mgr = S.inventory_mgr, mgr
    try:
        handler = S.DeploymentRequestHandler.__new__(S.DeploymentRequestHandler)
        rows = handler.render_device_rows(list(mgr.records.values()), handler.pools_payload())
        assert "&#10007;" in rows and "192.168.169.45" in rows
        assert "&#10003;" in rows
        page = handler.render_dashboard()
        assert "Seen as" in page
        assert "nuc-101" in page and "192.168.169.45" in page
        banners = handler.render_alert_banners({
            "warnings": [], "primary": None, "mismatches": mgr.ip_mismatches()})
        assert "nuc-101" in banners and "192.168.169.45" in banners
        assert "assigned address" in banners
    finally:
        S.inventory_mgr = previous


def test_boot_agent_persists_and_is_injected_into_the_client_script():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    assert mgr.settings["boot_agent"] is True
    page_mgr = mgr
    previous, S.inventory_mgr = S.inventory_mgr, page_mgr
    try:
        handler = S.DeploymentRequestHandler.__new__(S.DeploymentRequestHandler)
        page = handler.render_dashboard()
        assert "Keep NUCs in sync on every boot" in page
        assert 'id="set-boot-agent"' in page and "checked" in page
    finally:
        S.inventory_mgr = previous

    mgr.apply_config({"pools": [p.to_config_dict() for p in mgr.subnet_pools],
                      "settings": {"boot_agent": False}})
    assert mgr.settings["boot_agent"] is False
    stored = json.load(open(mgr.config_path, encoding="utf-8"))
    assert stored["settings"]["boot_agent"] is False

    fresh_pools = [S.SubnetPool.from_config_dict(p) for p in stored["pools"]]
    fresh = _manager(tmp, pools=fresh_pools, **stored["settings"])
    assert fresh.settings["boot_agent"] is False

    on = S.client_script_preamble("192.168.169.250", 8000, "default", True)
    off = S.client_script_preamble("192.168.169.250", 8000, "default", False)
    assert 'DEPLOY_BOOT_AGENT:=1' in on
    assert 'DEPLOY_BOOT_AGENT:=0' in off
    assert "export DEPLOY_SERVER DEPLOY_PORT DEPLOY_POOL DEPLOY_BOOT_AGENT" in on
    assert "DEPLOY_BOOT_AGENT=0" in mgr.hook_commands()["injected"]


def test_bench_warning_names_a_pool_on_the_wrong_network():
    networks = [{"iface": "eth0", "address": "192.168.169.250", "prefix": 24,
                 "network": "192.168.169.0/24", "gateway": "192.168.169.254"}]
    mgr = _manager(tempfile.mkdtemp(),
                   pools=[S.SubnetPool(name="default", network="192.168.100.0/24",
                                       start_ip=101, max_ip=250, gateway="192.168.100.1")],
                   server_ip="192.168.100.1")
    report = mgr.bench_status(networks=networks)
    assert report["ok"] is False, report
    assert report["warnings"], report
    line = S.format_startup_warning(report["warnings"][0])
    assert line.startswith(
        "[!] Pool 'default' uses 192.168.100.0/24, but this PC is only on 192.168.169.0/24"), line
    assert any("192.168.100.1" in w and "not an address of this PC" in w for w in report["warnings"])

    same = _manager(tempfile.mkdtemp(),
                    pools=[S.SubnetPool(name="default", network="192.168.169.0/24",
                                        gateway="192.168.169.254")],
                    server_ip="192.168.169.250")
    quiet = same.bench_status(networks=networks)
    assert quiet["ok"] is True and quiet["warnings"] == [], quiet
    # nothing detected -> stay quiet rather than invent a mismatch
    assert mgr.bench_status(networks=[])["ok"] is True


def test_adopt_rewrites_the_default_pool_onto_this_pc():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp,
                   pools=[S.SubnetPool(name="default", network="192.168.100.0/24",
                                       start_ip=101, max_ip=250, gateway="192.168.100.1"),
                          S.SubnetPool(name="office", network="10.10.35.0/24",
                                       start_ip=1, max_ip=20)],
                   server_ip="192.168.100.1")
    out = mgr.adopt_bench({"address": "192.168.169.250", "prefix": 24,
                           "gateway": "192.168.169.254", "iface": "eth0"})
    pool = mgr.get_pool_by_name("default")
    assert str(pool.network) == "192.168.169.0/24", pool.network
    assert pool.gateway == "192.168.169.254"
    assert mgr.settings["server_ip"] == "192.168.169.250"
    assert (pool.start_ip, pool.max_ip) == (101, 250)
    assert pool.ip_at(pool.current_ip_counter) == "192.168.169.101", pool.current_ip_counter
    assert out["next_ip"] == "192.168.169.101"
    # the other pool is not rewritten
    assert str(mgr.get_pool_by_name("office").network) == "10.10.35.0/24"
    stored = json.load(open(mgr.config_path, encoding="utf-8"))
    assert stored["pools"][0]["network"] == "192.168.169.0/24"
    assert stored["settings"]["server_ip"] == "192.168.169.250"
    assert "192.168.169.250" in stored["pools"][0]["reserve"]
    hooks = mgr.hook_commands()
    assert "http://192.168.169.250:8000/client/configure_network.sh" in hooks["postrun"]


def test_adopt_keeps_range_numbers_and_skips_gateway_and_this_pc():
    mgr = _manager(tempfile.mkdtemp(),
                   pools=[S.SubnetPool(name="default", network="10.0.0.0/16",
                                       start_ip=101, max_ip=250, gateway="10.0.0.1")])
    mgr.adopt_bench({"address": "192.168.169.250", "prefix": 24, "gateway": "192.168.169.254"})
    pool = mgr.default_pool
    assert (pool.start_ip, pool.max_ip) == (101, 250)
    taken = mgr.taken_offsets(pool)
    assert pool.offset_of("192.168.169.250") in taken
    assert pool.offset_of("192.168.169.254") in taken
    handed = []
    while True:
        try:
            ip, _host = pool.allocate_next(mgr.taken_offsets(pool))
        except ValueError:
            break
        handed.append(ip)
    assert handed and handed[0] == "192.168.169.101"
    assert "192.168.169.250" not in handed
    assert "192.168.169.254" not in handed

    # numbers that cannot fit the new network are clamped, not rejected
    wide = _manager(tempfile.mkdtemp(),
                    pools=[S.SubnetPool(name="default", network="10.30.0.0/16",
                                        start_ip=1000, max_ip=5000)])
    wide.adopt_bench({"address": "10.9.9.10", "prefix": 24, "gateway": "10.9.9.1"})
    assert str(wide.default_pool.network) == "10.9.9.0/24"
    assert wide.default_pool.start_ip <= wide.default_pool.host_limit
    assert wide.default_pool.max_ip <= wide.default_pool.host_limit
    assert wide.settings["server_ip"] == "10.9.9.10"


def test_data_lock_is_reentrant_for_adopt():
    # adopt_bench holds DATA_LOCK and then calls apply_config, which takes it again.
    # A plain Lock would deadlock here; fail on the type before we can hang.
    assert type(S.DATA_LOCK).__name__ == "RLock", type(S.DATA_LOCK)
    mgr = _manager(tempfile.mkdtemp(),
                   pools=[S.SubnetPool(name="default", network="192.168.100.0/24",
                                       start_ip=101, max_ip=250, gateway="192.168.100.1")],
                   server_ip="192.168.100.1")
    with S.DATA_LOCK:
        out = mgr.adopt_bench({"address": "192.168.169.250", "prefix": 24,
                               "gateway": "192.168.169.254"})
    assert out["server_ip"] == "192.168.169.250"
    assert str(mgr.default_pool.network) == "192.168.169.0/24"


def test_dashboard_payload_carries_bench_and_mismatch_banners():
    mgr = _manager(tempfile.mkdtemp(),
                   pools=[S.SubnetPool(name="default", network="192.168.100.0/24",
                                       gateway="192.168.100.1")],
                   server_ip="192.168.100.1")
    mgr.register_device({"mac": "aa:bb:cc:00:00:01"}, seen_ip="192.168.169.45")
    previous, S.inventory_mgr = S.inventory_mgr, mgr
    try:
        handler = S.DeploymentRequestHandler.__new__(S.DeploymentRequestHandler)
        networks = [{"iface": "eth0", "address": "192.168.169.250", "prefix": 24,
                     "network": "192.168.169.0/24", "gateway": "192.168.169.254"}]
        bench = mgr.bench_status(networks=networks)
        banners = handler.render_alert_banners(bench)
        assert "Use this PC's network" in banners
        assert 'data-address="192.168.169.250"' in banners
        assert 'data-prefix="24"' in banners
        assert "nuc-101" in banners and "192.168.169.45" in banners
        # the poll endpoint returns the same banners so a refresh can swap them in place
        payload = handler.dashboard_payload()
    finally:
        S.inventory_mgr = previous
    for key in ("banners", "bench", "mismatches", "rows", "stats", "pool_cards"):
        assert key in payload, key
    assert payload["mismatches"][0]["seen_ip"] == "192.168.169.45"
    assert payload["mismatches"][0]["hostname"] == "nuc-101"
    assert "nuc-101" in payload["banners"]
    assert "banners" in S.DASHBOARD_JS and "adopt-bench" in S.DASHBOARD_JS


def test_local_network_parsers_and_detection():
    addrs = S.parse_ip_addr(
        "2: eth0    inet 192.168.169.250/24 brd 192.168.169.255 scope global eth0\n"
        "1: lo    inet 127.0.0.1/8 scope host lo\n")
    assert addrs == [{"iface": "eth0", "address": "192.168.169.250", "prefix": 24}], addrs
    routes = S.parse_ip_route(
        "default via 192.168.169.254 dev eth0\n"
        "192.168.169.0/24 dev eth0 proto kernel scope link src 192.168.169.250\n")
    assert routes["eth0"] == "192.168.169.254"
    # FEA9A8C0 is the little-endian /proc/net/route form of 192.168.169.254
    proc = ("Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 00000000 FEA9A8C0 0003 0 0 0 00000000 0 0 0\n")
    assert S.parse_proc_net_route(proc)["eth0"] == "192.168.169.254"
    live = S.detect_local_networks()
    assert isinstance(live, list)
    for net in live:
        assert not str(net["address"]).startswith("127.")
        assert "/" in net["network"]
        assert net["iface"]
    # the helper used at startup must be the same sentence the dashboard shows
    sample = S.format_startup_warning(
        "Pool 'default' uses 192.168.100.0/24, but this PC is only on 192.168.169.0/24 (eth0 192.168.169.250)")
    assert sample.startswith("[!] Pool 'default' uses 192.168.100.0/24, but this PC is only on")


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
