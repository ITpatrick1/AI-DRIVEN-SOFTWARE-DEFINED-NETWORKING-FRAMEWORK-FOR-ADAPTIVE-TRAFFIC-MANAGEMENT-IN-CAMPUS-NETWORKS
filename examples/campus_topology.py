#!/usr/bin/env python3

"""Campus-like Mininet topology for Ryu/ONOS controller testing.

This script now provides a runtime control API so external tools (dashboard)
can execute real actions against the active Mininet network:
- POST /pingall
- POST /add_host
- GET  /device/<name>
- PUT  /device/<name>
- DELETE /device/<name>
- POST /start_stress
- POST /stop_stress
- GET  /health
- GET  /topology
- GET  /operations
"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# Ensure we import the local repository's Mininet package even when using sudo.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import error, info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


DEFAULT_TOPOLOGY_STATE_FILE = os.getenv(
    "CAMPUS_TOPOLOGY_STATE_FILE", "/tmp/campus_topology_state.json"
)
DEFAULT_RUNTIME_API_HOST = os.getenv("CAMPUS_RUNTIME_API_HOST", "127.0.0.1")
DEFAULT_RUNTIME_API_PORT = _env_int("CAMPUS_RUNTIME_API_PORT", 9091)


NODE_POSITIONS = {
    "s1": (350, 220),
    "s2": (200, 320),
    "s3": (200, 140),
    "s4": (500, 320),
    "s5": (500, 140),
    "h_server": (610, 220),
    "h_server_b": (90, 140),
    "h_it1": (80, 300),
    "h_it2": (80, 350),
    "h_net1": (80, 90),
    "h_net2": (80, 135),
    "h_staff1": (620, 300),
    "h_staff2": (620, 345),
    "h_wifi1": (620, 90),
    "h_wifi2": (620, 140),
}

SWITCH_LABELS = {
    "s1": "Core Switch",
    "s2": "IT Lab Switch",
    "s3": "Networking Lab Switch",
    "s4": "Staff LAN Switch",
    "s5": "Student Wi-Fi Switch",
}

HOST_LABELS = {
    "h_it1": "IT Lab PC 1",
    "h_it2": "IT Lab PC 2",
    "h_net1": "Networking Lab PC 1",
    "h_net2": "Networking Lab PC 2",
    "h_staff1": "Staff PC 1",
    "h_staff2": "Staff PC 2",
    "h_wifi1": "Student Wi-Fi Device 1",
    "h_wifi2": "Student Wi-Fi Device 2",
    "h_server": "Primary Service Server",
    "h_server_b": "Backup Service Server",
}

HOST_CATEGORIES = {
    "h_it1": "lab_device",
    "h_it2": "lab_device",
    "h_net1": "lab_device",
    "h_net2": "lab_device",
    "h_staff1": "user_device",
    "h_staff2": "user_device",
    "h_wifi1": "user_device",
    "h_wifi2": "user_device",
    "h_server": "service_node",
    "h_server_b": "service_node",
}

DEVICE_CATEGORY_ALIASES = {
    "user": "user_device",
    "user_device": "user_device",
    "iot": "iot",
    "iot_device": "iot",
    "service": "service_node",
    "service_node": "service_node",
    "lab": "lab_device",
    "lab_device": "lab_device",
}

DEVICE_CATEGORY_LABELS = {
    "user_device": "User device",
    "iot": "IoT device",
    "service_node": "Service node",
    "lab_device": "Lab device",
}


def _to_cidr(ip_text):
    ip_text = str(ip_text).strip()
    return ip_text if "/" in ip_text else f"{ip_text}/24"


def _bare_ip(ip_text):
    ip_text = str(ip_text or "").strip()
    return ip_text.split("/", 1)[0] if "/" in ip_text else ip_text


def _normalize_category(category_text):
    key = re.sub(r"[^a-z0-9]+", "_", str(category_text or "").strip().lower()).strip("_")
    return DEVICE_CATEGORY_ALIASES.get(key, "user_device")


def _category_label(category_key):
    return DEVICE_CATEGORY_LABELS.get(
        _normalize_category(category_key), DEVICE_CATEGORY_LABELS["user_device"]
    )


def _write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


class CampusRuntimeAPI:
    """Local runtime API bound to loopback for live Mininet control."""

    def __init__(self, net, hosts, state_file, bind_host, bind_port):
        self.net = net
        self.hosts = hosts
        self.state_file = state_file
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.lock = threading.RLock()
        self.server = None
        self.thread = None
        self.dynamic_labels = {}
        self.dynamic_meta = {}
        self.operation_events = []
        self.stress_processes = {}
        self.last_pingall_result = {}

    def _node_exists(self, name):
        try:
            self.net.get(name)
            return True
        except Exception:
            return False

    def _record_operation(self, op_type, status, **fields):
        event = {
            "ts": time.time(),
            "op": op_type,
            "status": status,
        }
        event.update(fields)
        self.operation_events.append(event)
        self.operation_events = self.operation_events[-200:]
        return event

    @staticmethod
    def _intf_port(intf):
        try:
            return int(intf.node.ports[intf])
        except Exception:
            pass
        name = getattr(intf, "name", "")
        if "-eth" in name:
            try:
                return int(name.rsplit("eth", 1)[1])
            except Exception:
                return None
        return None

    @staticmethod
    def _link_param(link, key):
        for params in (getattr(link, "params1", {}), getattr(link, "params2", {})):
            if isinstance(params, dict) and key in params:
                return params[key]
        params = getattr(link, "params", {})
        if isinstance(params, dict) and key in params:
            return params[key]
        return None

    @staticmethod
    def _link_bw_mbps(link):
        for attr in ("campus_bw_mbps", "bw_mbps", "bw"):
            val = getattr(link, attr, None)
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    pass
        for intf in (getattr(link, "intf1", None), getattr(link, "intf2", None)):
            params = getattr(intf, "params", {})
            if isinstance(params, dict) and "bw" in params:
                try:
                    return float(params["bw"])
                except Exception:
                    pass
        return 0.0

    def _collect_nodes(self):
        nodes = []
        dynamic_idx = 0

        for sw in sorted(self.net.switches, key=lambda x: x.name):
            x, y = NODE_POSITIONS.get(sw.name, (350, 460))
            nodes.append(
                {
                    "id": sw.name,
                    "label": SWITCH_LABELS.get(sw.name, sw.name),
                    "kind": "switch",
                    "x": x,
                    "y": y,
                }
            )

        for host in sorted(self.net.hosts, key=lambda x: x.name):
            pos = NODE_POSITIONS.get(host.name)
            kind = "host"
            if pos is None:
                kind = "dynamic"
                row = dynamic_idx // 6
                col = dynamic_idx % 6
                pos = (110 + col * 95, 500 + row * 44)
                dynamic_idx += 1
            ip = _bare_ip(host.params.get("ip", ""))
            meta = self.dynamic_meta.get(host.name, {})
            label = meta.get(
                "display_name",
                self.dynamic_labels.get(host.name, HOST_LABELS.get(host.name, host.name)),
            )
            category = meta.get("category", HOST_CATEGORIES.get(host.name, "user_device"))
            link_profile = self._link_profile_for_host(host.name)
            default_intf = ""
            try:
                intf = host.defaultIntf()
                default_intf = getattr(intf, "name", "") or ""
            except Exception:
                default_intf = ""
            try:
                mac = host.MAC() or ""
            except Exception:
                mac = ""
            nodes.append(
                {
                    "id": host.name,
                    "label": label,
                    "kind": kind,
                    "ip": ip,
                    "mac": mac,
                    "category": category,
                    "category_label": _category_label(category),
                    "default_intf": default_intf,
                    "attach_switch": link_profile.get("attach_switch", ""),
                    "bandwidth_mbps": link_profile.get("bandwidth_mbps", 0.0),
                    "delay": link_profile.get("delay", ""),
                    "host_interface": link_profile.get("host_interface", ""),
                    "switch_interface": link_profile.get("switch_interface", ""),
                    "switch_port": link_profile.get("switch_port"),
                    "removable": bool(host.name in self.dynamic_meta),
                    "management_origin": (
                        "dashboard_added"
                        if host.name in self.dynamic_meta
                        else "baseline_topology"
                    ),
                    "x": pos[0],
                    "y": pos[1],
                }
            )
        return nodes

    def _collect_links(self):
        links = []
        for link in self.net.links:
            i1 = link.intf1
            i2 = link.intf2
            bw = self._link_bw_mbps(link)
            delay = self._link_param(link, "delay")
            try:
                bw = float(bw) if bw is not None else 0.0
            except Exception:
                bw = 0.0
            links.append(
                {
                    "src": i1.node.name,
                    "dst": i2.node.name,
                    "src_intf": i1.name,
                    "dst_intf": i2.name,
                    "src_port": self._intf_port(i1),
                    "dst_port": self._intf_port(i2),
                    "bw_mbps": bw,
                    "delay": str(delay) if delay is not None else "",
                }
            )
        return links

    def _next_dynamic_ifname(self):
        """Return a short, unique Linux interface name for dynamic hosts."""
        existing = set()
        for host in self.net.hosts:
            for intf in host.intfList():
                if getattr(intf, "name", None):
                    existing.add(intf.name)
        idx = 1
        while True:
            cand = f"dh{idx}-eth0"
            if cand not in existing:
                return cand
            idx += 1

    def _link_profile_for_host(self, host_name):
        for link in self.net.links:
            i1 = getattr(link, "intf1", None)
            i2 = getattr(link, "intf2", None)
            if not i1 or not i2:
                continue
            if i1.node.name == host_name:
                host_intf, other_intf = i1, i2
            elif i2.node.name == host_name:
                host_intf, other_intf = i2, i1
            else:
                continue
            other_name = getattr(other_intf.node, "name", "")
            return {
                "attach_switch": other_name if other_name.startswith("s") else "",
                "bandwidth_mbps": round(float(self._link_bw_mbps(link) or 0.0), 3),
                "delay": str(self._link_param(link, "delay") or ""),
                "host_interface": getattr(host_intf, "name", "") or "",
                "switch_interface": getattr(other_intf, "name", "") or "",
                "switch_port": self._intf_port(other_intf),
            }
        return {
            "attach_switch": "",
            "bandwidth_mbps": 0.0,
            "delay": "",
            "host_interface": "",
            "switch_interface": "",
            "switch_port": None,
        }

    def _allocate_host_id(self, display_name):
        cleaned = re.sub(r"[^a-z0-9_]+", "_", str(display_name).strip().lower())
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        if not cleaned:
            cleaned = "device"
        if not re.match(r"^[a-z]", cleaned):
            cleaned = "d_" + cleaned
        cleaned = cleaned[:20]
        base = cleaned
        idx = 2
        existing = {h.name for h in self.net.hosts}
        while cleaned in existing:
            suffix = f"_{idx}"
            cleaned = (base[: max(1, 20 - len(suffix))] + suffix)
            idx += 1
        return cleaned

    def snapshot(self):
        with self.lock:
            payload = {
                "ts": time.time(),
                "nodes": self._collect_nodes(),
                "links": self._collect_links(),
                "last_pingall_result": self.last_pingall_result,
                "operation_events": self.operation_events[-80:],
                "runtime_api": {
                    "host": self.bind_host,
                    "port": self.bind_port,
                },
            }
            _write_json_atomic(self.state_file, payload)
            return payload

    @staticmethod
    def _parse_ping_output(raw):
        sent = 1
        received = 0
        avg_rtt = 0.0
        packet_loss = 100.0

        m = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received", raw)
        if m:
            sent = int(m.group(1))
            received = int(m.group(2))
            if sent > 0:
                packet_loss = max(0.0, min(100.0, 100.0 * (sent - received) / sent))
        elif "unreachable" in raw.lower():
            sent = 1
            received = 0
            packet_loss = 100.0

        m2 = re.search(
            r"rtt min/avg/max/mdev = "
            r"(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+) ms",
            raw,
        )
        if m2 and received > 0:
            avg_rtt = float(m2.group(2))

        return sent, received, packet_loss, avg_rtt

    def _run_pingall_once(self):
        hosts = sorted(self.net.hosts, key=lambda h: h.name)
        pair_results = []
        total_sent = 0
        total_received = 0
        rtts = []
        fail_pairs = []

        for src in hosts:
            for dst in hosts:
                if src == dst:
                    continue
                raw = src.cmd("LANG=C ping -c1 -W1 %s" % dst.IP())
                sent, received, loss, avg_rtt = self._parse_ping_output(raw)
                total_sent += sent
                total_received += received
                if received > 0:
                    rtts.append(avg_rtt)
                else:
                    fail_pairs.append({"src": src.name, "dst": dst.name})
                pair_results.append(
                    {
                        "src": src.name,
                        "dst": dst.name,
                        "sent": sent,
                        "received": received,
                        "loss_pct": round(loss, 3),
                        "avg_rtt_ms": round(avg_rtt, 3),
                    }
                )

        if total_sent <= 0:
            loss_pct = 0.0
        else:
            loss_pct = max(
                0.0, min(100.0, 100.0 * (total_sent - total_received) / total_sent)
            )
        avg_rtt = (sum(rtts) / len(rtts)) if rtts else 0.0

        slowest = sorted(
            [p for p in pair_results if p["received"] > 0],
            key=lambda x: x["avg_rtt_ms"],
            reverse=True,
        )[:8]

        return {
            "ok": True,
            "ts": time.time(),
            "packet_loss_pct": round(loss_pct, 3),
            "avg_rtt_ms": round(avg_rtt, 3),
            "pairs_total": len(pair_results),
            "pairs_failed": len(fail_pairs),
            "failed_pairs": fail_pairs[:16],
            "slowest_pairs": slowest,
            "pairs": pair_results[:120],
        }

    def _busy_hosts(self):
        busy = []
        for host in sorted(self.net.hosts, key=lambda h: h.name):
            if bool(getattr(host, "waiting", False)):
                busy.append(host.name)
        return busy

    def wait_for_idle_hosts(self, timeout_s=30.0, poll_s=0.25):
        start = time.time()
        last_busy = []
        while (time.time() - start) < timeout_s:
            busy = self._busy_hosts()
            if not busy:
                return True, round(time.time() - start, 3), []
            last_busy = busy
            time.sleep(poll_s)
        return False, round(time.time() - start, 3), last_busy

    def pingall(self):
        with self.lock:
            last_exc = None
            idle, idle_wait_s, busy = self.wait_for_idle_hosts(
                timeout_s=35.0, poll_s=0.25
            )
            if not idle:
                err = (
                    "pingall delayed because Mininet hosts stayed busy for %.2fs: %s"
                    % (idle_wait_s, ", ".join(busy))
                )
                self._record_operation(
                    "pingall",
                    "error",
                    error=err,
                    waited_for_idle_s=idle_wait_s,
                    busy_hosts=busy,
                )
                self.snapshot()
                raise RuntimeError(err)

            # Mininet shell commands can still assert if a host transitions back
            # to busy between polls. Retry longer instead of surfacing a 503.
            for _ in range(80):
                try:
                    result = self._run_pingall_once()
                    result["waited_for_idle_s"] = idle_wait_s
                    self.last_pingall_result = result
                    self._record_operation(
                        "pingall",
                        "ok",
                        packet_loss_pct=result["packet_loss_pct"],
                        avg_rtt_ms=result["avg_rtt_ms"],
                        failed_pairs=result["pairs_failed"],
                        waited_for_idle_s=idle_wait_s,
                    )
                    self.snapshot()
                    return result
                except AssertionError as exc:
                    last_exc = exc
                    time.sleep(0.25)
                except Exception as exc:
                    last_exc = exc
                    break
            busy = self._busy_hosts()
            err = f"pingall failed while Mininet was busy: {last_exc}"
            self._record_operation(
                "pingall",
                "error",
                error=err,
                waited_for_idle_s=idle_wait_s,
                busy_hosts=busy,
            )
            self.snapshot()
            raise RuntimeError(err)

    def _host_exists(self, name):
        try:
            self.net.get(name)
            return True
        except Exception:
            return False

    def get_device(self, name):
        with self.lock:
            if not self._node_exists(name):
                raise ValueError(f"device not found: {name}")
            host = self.net.get(name)
            if host not in self.net.hosts:
                raise ValueError(f"node is not an endpoint host: {name}")

            profile = self._link_profile_for_host(name)
            meta = self.dynamic_meta.get(name, {})
            display_name = meta.get(
                "display_name",
                self.dynamic_labels.get(name, HOST_LABELS.get(name, name)),
            )
            category = meta.get("category", HOST_CATEGORIES.get(name, "user_device"))
            removable = bool(name in self.dynamic_meta)
            interfaces = []
            for intf in host.intfList():
                intf_name = getattr(intf, "name", "") or ""
                if not intf_name:
                    continue
                try:
                    intf_ip = _bare_ip(host.IP(intf))
                except Exception:
                    intf_ip = ""
                try:
                    intf_mac = host.MAC(intf) or ""
                except Exception:
                    intf_mac = ""
                interfaces.append(
                    {
                        "name": intf_name,
                        "ip": intf_ip,
                        "mac": intf_mac,
                    }
                )

            try:
                default_route = host.cmd("ip route show default 2>/dev/null").strip()
            except Exception:
                default_route = ""

            stress_active = False
            proc = self.stress_processes.get(name)
            if proc is not None:
                try:
                    stress_active = proc.poll() is None
                except Exception:
                    stress_active = False

            ip_cidr = str(host.params.get("ip", ""))
            return {
                "ok": True,
                "device": {
                    "name": name,
                    "display_name": display_name,
                    "kind": "dynamic" if removable else "host",
                    "ip": _bare_ip(ip_cidr),
                    "ip_cidr": ip_cidr,
                    "mac": interfaces[0]["mac"] if interfaces else "",
                    "category": category,
                    "category_label": _category_label(category),
                    "attach_switch": profile.get("attach_switch", ""),
                    "bandwidth_mbps": profile.get("bandwidth_mbps", 0.0),
                    "delay": profile.get("delay", ""),
                    "default_intf": profile.get("host_interface", ""),
                    "host_interface": profile.get("host_interface", ""),
                    "switch_interface": profile.get("switch_interface", ""),
                    "switch_port": profile.get("switch_port"),
                    "default_route": default_route,
                    "interfaces": interfaces,
                    "removable": removable,
                    "management_origin": (
                        "dashboard_added" if removable else "baseline_topology"
                    ),
                    "stress_active": stress_active,
                    "created_ts": meta.get("created_ts"),
                },
            }

    def remove_host(self, name):
        with self.lock:
            if name not in self.dynamic_meta:
                raise ValueError(
                    "only dashboard-added endpoints can be removed from the live topology"
                )
            if not self._node_exists(name):
                raise ValueError(f"device not found: {name}")

            host = self.net.get(name)
            meta = dict(self.dynamic_meta.get(name, {}))
            profile = self._link_profile_for_host(name)
            display_name = meta.get("display_name", self.dynamic_labels.get(name, name))
            ip_addr = _bare_ip(str(host.params.get("ip", "")))

            proc = self.stress_processes.pop(name, None)
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass

            attach_switch = profile.get("attach_switch") or meta.get("attach_switch", "")
            if attach_switch and self._node_exists(attach_switch):
                try:
                    self.net.delLinkBetween(host, self.net.get(attach_switch), allLinks=True)
                except Exception:
                    pass
            else:
                for link in list(self.net.links):
                    try:
                        if link.intf1.node == host or link.intf2.node == host:
                            self.net.delLink(link)
                    except Exception:
                        continue

            self.net.delHost(host)
            self.hosts.pop(name, None)
            self.dynamic_labels.pop(name, None)
            self.dynamic_meta.pop(name, None)
            self._record_operation(
                "remove_host",
                "ok",
                host_id=name,
                display_name=display_name,
                ip=ip_addr,
                attach_switch=attach_switch,
            )

            topo = self.snapshot()
            return {
                "ok": True,
                "message": "endpoint removed from the live topology",
                "device": {
                    "name": name,
                    "display_name": display_name,
                    "ip": ip_addr,
                    "attach_switch": attach_switch,
                },
                "topology_nodes": len(topo.get("nodes", [])),
                "topology_links": len(topo.get("links", [])),
            }

    def update_host(
        self,
        name,
        display_name=None,
        ip=None,
        attach_switch=None,
        bandwidth_mbps=None,
        category=None,
    ):
        with self.lock:
            if name not in self.dynamic_meta:
                raise ValueError(
                    "only dashboard-added endpoints can be edited from the live topology"
                )
            if not self._node_exists(name):
                raise ValueError(f"device not found: {name}")

            host = self.net.get(name)
            meta = dict(self.dynamic_meta.get(name, {}))
            profile = self._link_profile_for_host(name)

            new_display_name = str(
                display_name
                or meta.get("display_name")
                or self.dynamic_labels.get(name, name)
            ).strip()
            if not new_display_name:
                raise ValueError("display_name is required")

            new_ip = _bare_ip(ip or host.params.get("ip", ""))
            if not new_ip:
                raise ValueError("ip is required")
            try:
                ip_obj = ipaddress.ip_address(new_ip)
            except ValueError as exc:
                raise ValueError("invalid IPv4 address") from exc
            campus_subnet = ipaddress.ip_network("10.0.0.0/24")
            if ip_obj not in campus_subnet or ip_obj in {
                campus_subnet.network_address,
                campus_subnet.broadcast_address,
            }:
                raise ValueError(
                    "device IP must be inside the campus subnet 10.0.0.0/24"
                )

            for other in self.net.hosts:
                if other.name == name:
                    continue
                if _bare_ip(str(other.params.get("ip", ""))) == new_ip:
                    raise ValueError(
                        "duplicate IP detected: %s is already assigned to %s"
                        % (new_ip, other.name)
                    )

            current_attach = (
                profile.get("attach_switch") or meta.get("attach_switch", "")
            )
            new_attach = str(attach_switch or current_attach).strip() or current_attach
            if not new_attach or not new_attach.startswith("s") or not self._node_exists(
                new_attach
            ):
                raise ValueError("attach_switch must reference an existing switch")

            current_bw = float(
                profile.get("bandwidth_mbps") or meta.get("bandwidth_mbps") or 50.0
            )
            new_bw = (
                current_bw if bandwidth_mbps is None else float(bandwidth_mbps or 0.0)
            )
            if new_bw <= 0:
                raise ValueError("bandwidth_mbps must be > 0")

            category_key = _normalize_category(category or meta.get("category"))
            ip_cidr = _to_cidr(new_ip)
            link_changed = (
                new_attach != current_attach or abs(new_bw - current_bw) > 1e-9
            )

            if link_changed:
                if current_attach and self._node_exists(current_attach):
                    try:
                        self.net.delLinkBetween(
                            host, self.net.get(current_attach), allLinks=True
                        )
                    except Exception:
                        pass
                else:
                    for link in list(self.net.links):
                        try:
                            if link.intf1.node == host or link.intf2.node == host:
                                self.net.delLink(link)
                        except Exception:
                            continue

                link_kwargs = {"bw": new_bw, "delay": "1ms", "use_tbf": True}
                if len(name) + len("-eth0") > 15:
                    link_kwargs["intfName2"] = self._next_dynamic_ifname()
                link = self.net.addLink(self.net.get(new_attach), host, **link_kwargs)
                link.campus_bw_mbps = new_bw
                link.campus_delay = "1ms"

            host.params["ip"] = ip_cidr
            host.configDefault(ip=ip_cidr)
            self.net.staticArp()

            self.dynamic_labels[name] = new_display_name
            meta.update(
                {
                    "display_name": new_display_name,
                    "category": category_key,
                    "attach_switch": new_attach,
                    "bandwidth_mbps": new_bw,
                    "updated_ts": time.time(),
                }
            )
            self.dynamic_meta[name] = meta

            updated_profile = self._link_profile_for_host(name)
            self._record_operation(
                "update_host",
                "ok",
                host_id=name,
                display_name=new_display_name,
                ip=new_ip,
                attach_switch=new_attach,
                bandwidth_mbps=new_bw,
                category=category_key,
            )

            topo = self.snapshot()
            return {
                "ok": True,
                "message": "endpoint configuration updated",
                "device": {
                    "name": name,
                    "display_name": new_display_name,
                    "ip": new_ip,
                    "ip_cidr": ip_cidr,
                    "attach_switch": updated_profile.get("attach_switch", new_attach),
                    "bandwidth_mbps": updated_profile.get("bandwidth_mbps", new_bw),
                    "delay": updated_profile.get("delay", ""),
                    "host_interface": updated_profile.get("host_interface", ""),
                    "switch_interface": updated_profile.get("switch_interface", ""),
                    "switch_port": updated_profile.get("switch_port"),
                    "category": category_key,
                    "category_label": _category_label(category_key),
                    "removable": True,
                    "management_origin": "dashboard_added",
                    "updated_ts": meta.get("updated_ts"),
                },
                "topology_nodes": len(topo.get("nodes", [])),
                "topology_links": len(topo.get("links", [])),
            }

    def start_stress(
        self,
        seconds=45,
        iperf_port=5201,
        clients=None,
        reverse_download=True,
    ):
        with self.lock:
            if clients is None:
                clients = ["h_wifi1", "h_wifi2"]
            clients = [c for c in clients if self._host_exists(c)]
            if not clients:
                raise ValueError("no valid client hosts available for stress test")

            seconds = max(10, int(seconds))
            iperf_port = int(iperf_port)

            server = self.net.get("h_server")
            if server.cmd("command -v iperf3 >/dev/null 2>&1; echo $?").strip() != "0":
                raise RuntimeError("iperf3 is not installed inside Mininet hosts")
            server.cmd("pkill -f 'iperf3 -s -p %d' || true" % iperf_port)
            server.cmd(
                "iperf3 -s -p %d >/tmp/iperf3_server_%d.log 2>&1 &"
                % (iperf_port, iperf_port)
            )

            # Clean old client jobs.
            for proc in self.stress_processes.values():
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.stress_processes = {}

            for name in clients:
                host = self.net.get(name)
                cmd = (
                    "iperf3 -c 10.0.0.100 -p %d -t %d -i 1 %s"
                    " >/tmp/%s_film_download.log 2>&1"
                ) % (
                    iperf_port,
                    seconds,
                    "-R" if reverse_download else "",
                    name,
                )
                self.stress_processes[name] = host.popen(cmd)

            event = self._record_operation(
                "start_stress",
                "ok",
                clients=clients,
                seconds=seconds,
                mode="download" if reverse_download else "upload",
                iperf_port=iperf_port,
            )
            self.snapshot()
            return {"ok": True, "message": "stress test started", "details": event}

    def stop_stress(self):
        with self.lock:
            stopped = []
            for host_name, proc in list(self.stress_processes.items()):
                try:
                    proc.terminate()
                except Exception:
                    pass
                stopped.append(host_name)
            self.stress_processes = {}

            if self._host_exists("h_server"):
                server = self.net.get("h_server")
                server.cmd("pkill -f 'iperf3 -s -p 5201' || true")

            event = self._record_operation(
                "stop_stress", "ok", stopped_clients=stopped
            )
            self.snapshot()
            return {"ok": True, "message": "stress test stopped", "details": event}

    def get_operations(self):
        with self.lock:
            running = []
            for host_name, proc in list(self.stress_processes.items()):
                alive = proc.poll() is None
                if alive:
                    running.append(host_name)
                else:
                    self.stress_processes.pop(host_name, None)
            return {
                "ok": True,
                "running_stress_clients": running,
                "last_pingall_result": self.last_pingall_result,
                "events": self.operation_events[-80:],
            }

    def add_host(self, name, ip, attach_switch, bandwidth_mbps, category=None):
        with self.lock:
            if not name:
                raise ValueError("name is required")
            if not ip:
                raise ValueError("ip is required")

            display_name = str(name).strip()
            host_id = self._allocate_host_id(display_name)

            switch = self.net.get(attach_switch)
            ip_cidr = _to_cidr(ip)
            bw = float(bandwidth_mbps)
            category_key = _normalize_category(category)
            if bw <= 0:
                raise ValueError("bandwidth_mbps must be > 0")

            host = self.net.addHost(host_id, ip=ip_cidr)
            # Linux interface names are max 15 chars. If host name is long,
            # explicitly set a short host interface name for link creation.
            link_kwargs = {"bw": bw, "delay": "1ms", "use_tbf": True}
            if len(host_id) + len("-eth0") > 15:
                link_kwargs["intfName2"] = self._next_dynamic_ifname()
            link = self.net.addLink(switch, host, **link_kwargs)
            link.campus_bw_mbps = bw
            link.campus_delay = "1ms"
            host.configDefault()
            self.net.staticArp()
            self.hosts[host_id] = host
            self.dynamic_labels[host_id] = display_name
            self.dynamic_meta[host_id] = {
                "display_name": display_name,
                "category": category_key,
                "attach_switch": attach_switch,
                "bandwidth_mbps": bw,
                "created_ts": time.time(),
            }
            self._record_operation(
                "add_host",
                "ok",
                host_id=host_id,
                display_name=display_name,
                ip=_bare_ip(ip_cidr),
                attach_switch=attach_switch,
                bandwidth_mbps=bw,
                category=category_key,
            )

            topo = self.snapshot()
            return {
                "ok": True,
                "device": {
                    "name": host_id,
                    "display_name": display_name,
                    "ip": _bare_ip(ip_cidr),
                    "attach_switch": attach_switch,
                    "bandwidth_mbps": bw,
                    "category": category_key,
                    "ts": time.time(),
                },
                "topology_nodes": len(topo.get("nodes", [])),
                "topology_links": len(topo.get("links", [])),
            }

    def start(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, payload, status=200):
                body = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _read_json(self):
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except Exception:
                    n = 0
                raw = self.rfile.read(n) if n > 0 else b"{}"
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    with parent.lock:
                        self._send_json(
                            {
                                "ok": True,
                                "switches": [s.name for s in parent.net.switches],
                                "hosts": [h.name for h in parent.net.hosts],
                            }
                        )
                    return
                if path == "/topology":
                    self._send_json(parent.snapshot())
                    return
                if path == "/operations":
                    self._send_json(parent.get_operations())
                    return
                m = re.fullmatch(r"/device/([^/]+)", path)
                if m:
                    try:
                        self._send_json(parent.get_device(unquote(m.group(1))))
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 404)
                    return
                self._send_json({"error": "not found", "path": path}, 404)

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/pingall":
                    try:
                        self._send_json(parent.pingall())
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 503)
                    return
                if path == "/start_stress":
                    body = self._read_json()
                    try:
                        self._send_json(
                            parent.start_stress(
                                seconds=body.get("seconds", 45),
                                iperf_port=body.get("iperf_port", 5201),
                                clients=body.get("clients"),
                                reverse_download=bool(
                                    body.get("reverse_download", True)
                                ),
                            )
                        )
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                if path == "/stop_stress":
                    try:
                        self._send_json(parent.stop_stress())
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                if path == "/add_host":
                    body = self._read_json()
                    try:
                        resp = parent.add_host(
                            name=str(body.get("name", "")).strip(),
                            ip=str(body.get("ip", "")).strip(),
                            attach_switch=str(body.get("attach_switch", "s1")).strip()
                            or "s1",
                            bandwidth_mbps=body.get("bandwidth_mbps", 50),
                            category=body.get("category"),
                        )
                        self._send_json(resp)
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                self._send_json({"error": "not found", "path": path}, 404)

            def do_DELETE(self):
                parsed = urlparse(self.path)
                path = parsed.path
                m = re.fullmatch(r"/device/([^/]+)", path)
                if m:
                    try:
                        self._send_json(parent.remove_host(unquote(m.group(1))))
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                self._send_json({"error": "not found", "path": path}, 404)

            def do_PUT(self):
                parsed = urlparse(self.path)
                path = parsed.path
                m = re.fullmatch(r"/device/([^/]+)", path)
                if m:
                    body = self._read_json()
                    try:
                        self._send_json(
                            parent.update_host(
                                name=unquote(m.group(1)),
                                display_name=body.get("display_name"),
                                ip=body.get("ip"),
                                attach_switch=body.get("attach_switch"),
                                bandwidth_mbps=body.get("bandwidth_mbps"),
                                category=body.get("category"),
                            )
                        )
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                self._send_json({"error": "not found", "path": path}, 404)

            def log_message(self, fmt, *args):
                return

        # Bind runtime API with graceful fallback if preferred port is busy.
        last_exc = None
        for offset in range(0, 20):
            cand_port = int(self.bind_port) + offset
            try:
                self.server = ThreadingHTTPServer((self.bind_host, cand_port), Handler)
                if offset > 0:
                    info(
                        "*** Runtime API port %s busy, using fallback %s\n"
                        % (self.bind_port, cand_port)
                    )
                self.bind_port = cand_port
                break
            except OSError as exc:
                last_exc = exc
                if getattr(exc, "errno", None) == 98:
                    continue
                raise
        if self.server is None:
            raise RuntimeError(
                "Failed to bind runtime API near %s:%s (%s)"
                % (self.bind_host, self.bind_port, last_exc)
            )

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        info(
            "*** Runtime API listening on http://%s:%s\n"
            % (self.bind_host, self.bind_port)
        )
        self.snapshot()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None


def create_campus_net():
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoStaticArp=True,
        waitConnected=10,
    )

    info("*** Adding remote controller (Ryu on 127.0.0.1:6653)\n")
    net.addController("c0", ip="127.0.0.1", port=6653)

    info("*** Adding switches\n")
    s1 = net.addSwitch("s1", protocols="OpenFlow13")  # core
    s2 = net.addSwitch("s2", protocols="OpenFlow13")  # IT lab
    s3 = net.addSwitch("s3", protocols="OpenFlow13")  # Networking lab
    s4 = net.addSwitch("s4", protocols="OpenFlow13")  # Staff
    s5 = net.addSwitch("s5", protocols="OpenFlow13")  # Student Wi-Fi

    info("*** Adding hosts (single L2 segment for baseline testing)\n")
    hosts = {
        "h_it1": net.addHost("h_it1", ip="10.0.0.11/24", mac="00:00:00:00:01:01"),
        "h_it2": net.addHost("h_it2", ip="10.0.0.12/24", mac="00:00:00:00:01:02"),
        "h_net1": net.addHost("h_net1", ip="10.0.0.21/24", mac="00:00:00:00:02:01"),
        "h_net2": net.addHost("h_net2", ip="10.0.0.22/24", mac="00:00:00:00:02:02"),
        "h_staff1": net.addHost(
            "h_staff1", ip="10.0.0.31/24", mac="00:00:00:00:03:01"
        ),
        "h_staff2": net.addHost(
            "h_staff2", ip="10.0.0.32/24", mac="00:00:00:00:03:02"
        ),
        "h_wifi1": net.addHost("h_wifi1", ip="10.0.0.41/24", mac="00:00:00:00:04:01"),
        "h_wifi2": net.addHost("h_wifi2", ip="10.0.0.42/24", mac="00:00:00:00:04:02"),
        "h_server": net.addHost(
            "h_server", ip="10.0.0.100/24", mac="00:00:00:00:00:64"
        ),
        "h_server_b": net.addHost(
            "h_server_b", ip="10.0.0.101/24", mac="00:00:00:00:00:65"
        ),
    }

    # Use TBF shaping to avoid noisy HTB quantum warnings on some kernels.
    def add_qos_link(node1, node2, bw, delay):
        link = net.addLink(node1, node2, bw=bw, delay=delay, use_tbf=True)
        link.campus_bw_mbps = float(bw)
        link.campus_delay = str(delay)
        return link

    info("*** Adding core/access links\n")
    add_qos_link(s1, s2, bw=1000, delay="1ms")
    add_qos_link(s1, s3, bw=1000, delay="1ms")
    add_qos_link(s1, s4, bw=1000, delay="1ms")
    add_qos_link(s1, s5, bw=1000, delay="2ms")

    info("*** Adding host links\n")
    add_qos_link(s2, hosts["h_it1"], bw=100, delay="1ms")
    add_qos_link(s2, hosts["h_it2"], bw=100, delay="1ms")
    add_qos_link(s3, hosts["h_net1"], bw=100, delay="1ms")
    add_qos_link(s3, hosts["h_net2"], bw=100, delay="1ms")
    add_qos_link(s4, hosts["h_staff1"], bw=100, delay="1ms")
    add_qos_link(s4, hosts["h_staff2"], bw=100, delay="1ms")
    add_qos_link(s5, hosts["h_wifi1"], bw=50, delay="5ms")
    add_qos_link(s5, hosts["h_wifi2"], bw=50, delay="5ms")
    add_qos_link(s1, hosts["h_server"], bw=1000, delay="1ms")
    add_qos_link(s3, hosts["h_server_b"], bw=1000, delay="1ms")
    return net, hosts


def _configure_priority_qos():
    """Configure OVS queues used by adaptive priority policy.

    Queue IDs:
    - queue 0: high-priority traffic (exam/auth)
    - queue 1: medium-priority traffic (normal browsing)
    - queue 2: low-priority traffic (entertainment/bulk downloads)
    """

    profiles = [
        # Access uplinks toward core.
        {"port": "s2-eth1", "max_bps": 1_000_000_000, "mid_bps": 300_000_000, "low_bps": 60_000_000},
        {"port": "s3-eth1", "max_bps": 1_000_000_000, "mid_bps": 300_000_000, "low_bps": 60_000_000},
        {"port": "s4-eth1", "max_bps": 1_000_000_000, "mid_bps": 300_000_000, "low_bps": 60_000_000},
        {"port": "s5-eth1", "max_bps": 1_000_000_000, "mid_bps": 120_000_000, "low_bps": 20_000_000},
        # s5 downlinks to student Wi-Fi hosts
        {"port": "s5-eth2", "max_bps": 50_000_000, "mid_bps": 22_000_000, "low_bps": 8_000_000},
        {"port": "s5-eth3", "max_bps": 50_000_000, "mid_bps": 22_000_000, "low_bps": 8_000_000},
    ]

    info("*** Applying adaptive QoS queue profiles on access/uplink links\n")
    for p in profiles:
        cmd = (
            "ovs-vsctl -- "
            "--id=@q0 create Queue other-config:max-rate={max_bps} "
            "-- --id=@q1 create Queue other-config:max-rate={mid_bps} "
            "-- --id=@q2 create Queue other-config:max-rate={low_bps} "
            "-- --id=@newqos create QoS type=linux-htb other-config:max-rate={max_bps} "
            "queues:0=@q0 queues:1=@q1 queues:2=@q2 "
            "-- set Port {port} qos=@newqos"
        ).format(**p)
        cp = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if cp.returncode != 0:
            info(
                "*** QoS warning on %s: %s\n"
                % (p["port"], (cp.stderr or cp.stdout or "unknown error").strip())
            )


def _check_controller_connected(net):
    disconnected = [sw.name for sw in net.switches if not sw.connected()]
    if disconnected:
        error(
            "*** Controller is not reachable on 127.0.0.1:6653.\n"
            "*** Disconnected switches: %s\n"
            "*** Start Ryu first: "
            "`source ~/sdn-env/bin/activate && "
            "ryu-manager examples/campus_controller.py`\n"
            % ", ".join(disconnected)
        )
        return False
    return True


def _run_quick_tests(net, hosts):
    info("*** Full connectivity test (pingall)\n")
    loss_pct = float(net.pingAll(timeout="1"))
    if loss_pct > 0.0:
        raise RuntimeError(
            "Topology connectivity check failed: pingall loss %.2f%%" % loss_pct
        )

    info("*** Zone-to-server reachability checks\n")
    zone_hosts = ["h_it1", "h_net1", "h_staff1", "h_wifi1"]
    for zh in zone_hosts:
        pair_loss = float(net.ping([hosts[zh], hosts["h_server"]], timeout="1"))
        if pair_loss > 0.0:
            raise RuntimeError(
                "Host %s cannot fully reach h_server (loss %.2f%%)"
                % (zh, pair_loss)
            )

    info("*** Quick throughput test (h_it1 <-> h_server)\n")
    try:
        net.iperf3([hosts["h_it1"], hosts["h_server"]], seconds=3)
    except Exception as exc:
        info("*** Iperf3 test warning: %s\n" % exc)
        try:
            net.iperf([hosts["h_it1"], hosts["h_server"]], seconds=3)
        except Exception as exc2:
            info("*** Iperf fallback warning: %s\n" % exc2)


def build_campus(
    start_cli=True,
    run_tests=True,
    hold_seconds=0,
    topology_state_file=DEFAULT_TOPOLOGY_STATE_FILE,
    runtime_host=DEFAULT_RUNTIME_API_HOST,
    runtime_port=DEFAULT_RUNTIME_API_PORT,
):
    net, hosts = create_campus_net()
    runtime_api = None

    info("*** Starting network\n")
    net.start()
    if not _check_controller_connected(net):
        net.stop()
        return
    _configure_priority_qos()

    runtime_api = CampusRuntimeAPI(
        net=net,
        hosts=hosts,
        state_file=topology_state_file,
        bind_host=runtime_host,
        bind_port=runtime_port,
    )
    # Publish topology state early so dashboard can render while startup tests run.
    runtime_api.snapshot()

    try:
        if run_tests:
            _run_quick_tests(net, hosts)

        idle, idle_wait_s, busy = runtime_api.wait_for_idle_hosts(
            timeout_s=20.0, poll_s=0.25
        )
        if idle_wait_s > 0.0:
            info("*** Runtime API startup wait for host idle: %.2fs\n" % idle_wait_s)
        if not idle:
            info(
                "*** Runtime API warning: hosts still busy at startup: %s\n"
                % ", ".join(busy)
            )

        runtime_api.start()

        if start_cli:
            info("*** Campus zones active: IT, Networking, Staff, Student Wi-Fi, Server\n")
            info("*** OpenFlow profile: 1.3 on switches s1..s5\n")
            info("*** Entering Mininet CLI\n")
            info("Try: pingall\n")
            info("Try: iperf h_it1 h_server\n")
            info("Try: sh ovs-ofctl -O OpenFlow13 dump-flows s1\n")
            CLI(net)
        elif hold_seconds > 0:
            info(
                "*** Holding topology for %ss (runtime API active)\n"
                % int(hold_seconds)
            )
            time.sleep(max(1, int(hold_seconds)))
        else:
            info("*** Non-interactive test mode complete (no CLI)\n")
    finally:
        info("*** Stopping network\n")
        if runtime_api is not None:
            runtime_api.stop()
        net.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-cli",
        action="store_true",
        help="Run smoke/throughput tests and exit without opening Mininet CLI",
    )
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=0,
        help="When --no-cli is used, keep topology alive for N seconds",
    )
    parser.add_argument(
        "--topology-state-file",
        default=DEFAULT_TOPOLOGY_STATE_FILE,
        help="Path for live topology JSON consumed by dashboard",
    )
    parser.add_argument(
        "--runtime-host",
        default=DEFAULT_RUNTIME_API_HOST,
        help="Runtime API bind host",
    )
    parser.add_argument(
        "--runtime-port",
        type=int,
        default=DEFAULT_RUNTIME_API_PORT,
        help="Runtime API bind port",
    )
    args = parser.parse_args()

    setLogLevel("info")
    build_campus(
        start_cli=not args.no_cli,
        hold_seconds=args.hold_seconds,
        topology_state_file=args.topology_state_file,
        runtime_host=args.runtime_host,
        runtime_port=args.runtime_port,
    )
