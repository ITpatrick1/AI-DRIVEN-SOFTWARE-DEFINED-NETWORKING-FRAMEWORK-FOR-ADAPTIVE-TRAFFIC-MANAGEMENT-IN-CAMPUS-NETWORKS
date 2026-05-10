#!/usr/bin/env python3

"""Campus-like Mininet topology for Ryu/ONOS controller testing.

This script now provides a runtime control API so external tools (dashboard)
can execute real actions against the active Mininet network:
- POST /pingall
- POST /add_host
- GET  /device/<name>
- GET  /device/<name>/workspace
- POST /device/<name>/action
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
import random
import re
import shlex
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
DEFAULT_SKIP_SMOKE_TESTS = os.getenv(
    "CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS", "0"
).strip().lower() in {"1", "true", "yes", "on"}


NODE_POSITIONS = {
    # ── Tier 0: Servers (top row) ──────────────────────────────────
    "h_server1":  (155,  38),   # SA Server 2  – hangs off dist_left
    "h_server2":  (600,  38),   # Server 1     – hangs off dist_right

    # ── Tier 1: Distribution layer ─────────────────────────────────
    "s2":  (155, 128),          # Dist Left  / Switch0
    "s1":  (378, 128),          # Core Switch (3560-24PS)  – centre
    "s3":  (600, 128),          # Dist Right / Switch1

    # ── Tier 2: Access switches ────────────────────────────────────
    # Left branch (s2):  5 switches spread x=20..290
    "s10": ( 20, 248),          # Incubation Centre
    "s4":  ( 88, 248),          # Students Lab 7
    "s5":  (155, 248),          # Students Lab 6
    "s6":  (222, 248),          # Students Mech Lab 1
    "s9":  (290, 248),          # Mechatronic Network

    # Core-direct (s1): 4 switches spread x=330..540
    "s7":  (330, 262),          # Students Mech Lab 2
    "s8":  (400, 262),          # Students Lab 2
    "s12": (470, 262),          # Students Lab 4
    "s14": (540, 262),          # Administration

    # Right branch (s3): 2 switches
    "s11": (610, 248),          # Students Lab 3
    "s13": (700, 248),          # Academic Network

    # ── Tier 3: Hosts (bottom row) ────────────────────────────────
    # Incubation (s10 @ 20,248)
    "h_incub_1":  ( 0, 375),
    "h_incub_2":  ( 20, 393),
    "h_incub_3":  ( 40, 375),

    # Lab 7 (s4 @ 88,248)
    "h_lab7_1":   ( 68, 375),
    "h_lab7_2":   ( 88, 393),
    "h_lab7_3":   (108, 375),

    # Lab 6 (s5 @ 155,248)
    "h_lab6_1":   (135, 375),
    "h_lab6_2":   (155, 393),
    "h_lab6_3":   (175, 375),

    # Mech Lab 1 (s6 @ 222,248)
    "h_mechl1_1": (202, 375),
    "h_mechl1_2": (222, 393),
    "h_mechl1_3": (242, 375),

    # Mechatronic (s9 @ 290,248)
    "h_mech_1":   (270, 375),
    "h_mech_2":   (290, 393),
    "h_mech_3":   (310, 375),

    # Mech Lab 2 (s7 @ 330,262)
    "h_mechl2_1": (310, 393),
    "h_mechl2_2": (330, 410),
    "h_mechl2_3": (350, 393),

    # Lab 2 (s8 @ 400,262)
    "h_lab2_1":   (380, 393),
    "h_lab2_2":   (400, 410),
    "h_lab2_3":   (420, 393),

    # Lab 4 (s12 @ 470,262)
    "h_lab4_1":   (450, 393),
    "h_lab4_2":   (470, 410),
    "h_lab4_3":   (490, 393),

    # Admin (s14 @ 540,262)
    "h_admin_1":  (520, 393),
    "h_admin_2":  (540, 410),
    "h_admin_3":  (560, 393),

    # Lab 3 (s11 @ 610,248)
    "h_lab3_1":   (590, 375),
    "h_lab3_2":   (610, 393),
    "h_lab3_3":   (630, 375),

    # Academic (s13 @ 700,248)
    "h_acad_1":   (680, 375),
    "h_acad_2":   (700, 393),
    "h_acad_3":   (720, 375),
}

SWITCH_LABELS = {
    "s1":  "Core Switch (3560-24PS)",
    "s2":  "Dist Left",
    "s3":  "Dist Right",
    "s4":  "Lab 7 Switch",
    "s5":  "Lab 6 Switch",
    "s6":  "Mech Lab 1 Switch",
    "s7":  "Mech Lab 2 Switch",
    "s8":  "Lab 2 Switch",
    "s9":  "Mechatronic Switch",
    "s10": "Incubation Switch",
    "s11": "Lab 3 Switch",
    "s12": "Lab 4 Switch",
    "s13": "Academic Switch",
    "s14": "Admin Switch",
}

HOST_LABELS = {
    "h_server1":  "SA Server 2",
    "h_server2":  "Server 1",
    "h_lab7_1":   "Lab 7 PC 1",
    "h_lab7_2":   "Lab 7 PC 2",
    "h_lab7_3":   "Lab 7 PC 3",
    "h_lab6_1":   "Lab 6 PC 1",
    "h_lab6_2":   "Lab 6 PC 2",
    "h_lab6_3":   "Lab 6 PC 3",
    "h_mechl1_1": "Mech Lab 1 PC 1",
    "h_mechl1_2": "Mech Lab 1 PC 2",
    "h_mechl1_3": "Mech Lab 1 PC 3",
    "h_mechl2_1": "Mech Lab 2 PC 1",
    "h_mechl2_2": "Mech Lab 2 PC 2",
    "h_mechl2_3": "Mech Lab 2 PC 3",
    "h_lab2_1":   "Lab 2 PC 1",
    "h_lab2_2":   "Lab 2 PC 2",
    "h_lab2_3":   "Lab 2 PC 3",
    "h_mech_1":   "Mechatronic PC 1",
    "h_mech_2":   "Mechatronic PC 2",
    "h_mech_3":   "Mechatronic PC 3",
    "h_incub_1":  "Incubation PC 1",
    "h_incub_2":  "Incubation PC 2",
    "h_incub_3":  "Incubation PC 3",
    "h_lab3_1":   "Lab 3 PC 1",
    "h_lab3_2":   "Lab 3 PC 2",
    "h_lab3_3":   "Lab 3 PC 3",
    "h_lab4_1":   "Lab 4 PC 1",
    "h_lab4_2":   "Lab 4 PC 2",
    "h_lab4_3":   "Lab 4 PC 3",
    "h_acad_1":   "Academic PC 1",
    "h_acad_2":   "Academic PC 2",
    "h_acad_3":   "Academic PC 3",
    "h_admin_1":  "Admin PC 1",
    "h_admin_2":  "Admin PC 2",
    "h_admin_3":  "Admin PC 3",
}

HOST_CATEGORIES = {
    "h_server1":  "service_node",
    "h_server2":  "service_node",
    "h_lab7_1":   "lab_device",
    "h_lab7_2":   "lab_device",
    "h_lab7_3":   "lab_device",
    "h_lab6_1":   "lab_device",
    "h_lab6_2":   "lab_device",
    "h_lab6_3":   "lab_device",
    "h_mechl1_1": "lab_device",
    "h_mechl1_2": "lab_device",
    "h_mechl1_3": "lab_device",
    "h_mechl2_1": "lab_device",
    "h_mechl2_2": "lab_device",
    "h_mechl2_3": "lab_device",
    "h_lab2_1":   "lab_device",
    "h_lab2_2":   "lab_device",
    "h_lab2_3":   "lab_device",
    "h_mech_1":   "lab_device",
    "h_mech_2":   "lab_device",
    "h_mech_3":   "lab_device",
    "h_incub_1":  "user_device",
    "h_incub_2":  "user_device",
    "h_incub_3":  "user_device",
    "h_lab3_1":   "lab_device",
    "h_lab3_2":   "lab_device",
    "h_lab3_3":   "lab_device",
    "h_lab4_1":   "lab_device",
    "h_lab4_2":   "lab_device",
    "h_lab4_3":   "lab_device",
    "h_acad_1":   "user_device",
    "h_acad_2":   "user_device",
    "h_acad_3":   "user_device",
    "h_admin_1":  "user_device",
    "h_admin_2":  "user_device",
    "h_admin_3":  "user_device",
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

DEVICE_ACTION_CATALOG = {
    "ping_target": {
        "label": "Ping target",
        "kind": "probe",
        "description": "Send live ICMP traffic from this endpoint to a selected target.",
    },
    "discover_network": {
        "label": "Discover network",
        "kind": "probe",
        "description": "Probe the campus subnet and list reachable peers from this endpoint.",
    },
    "scan_surface": {
        "label": "Scan attack surface",
        "kind": "session",
        "description": "Run a sustained scan so the controller can detect and react to suspicious probing.",
    },
    "film_download": {
        "label": "Film download",
        "kind": "session",
        "description": "Launch a real bulk-download session from the media server.",
    },
    "elearning_access": {
        "label": "E-learning portal",
        "kind": "session",
        "description": "Keep an academic portal session running so the controller treats it as protected traffic.",
    },
    "college_mis_access": {
        "label": "College MIS",
        "kind": "session",
        "description": "Keep the college MIS session running so administrative traffic stays protected.",
    },
    "social_media_access": {
        "label": "Social media browsing",
        "kind": "session",
        "description": "Generate normal browsing traffic such as staff social-media access.",
    },
    "google_meet": {
        "label": "Google Meet session",
        "kind": "session",
        "description": "Generate a real-time collaboration traffic flow with sustained UDP media.",
    },
    "stop_sessions": {
        "label": "Stop device sessions",
        "kind": "control",
        "description": "Terminate the active simulated application sessions on this endpoint.",
    },
}

SERVICE_PORTS = {
    "college_sync": _env_int("CAMPUS_COLLEGE_SYNC_HTTP_PORT", 8008),
    "social_media": _env_int("CAMPUS_SOCIAL_MEDIA_HTTP_PORT", 8088),
    "elearning": _env_int("CAMPUS_ELEARNING_HTTP_PORT", 8443),
    "college_mis": _env_int("CAMPUS_COLLEGE_MIS_HTTP_PORT", 9443),
    "film_download": _env_int("CAMPUS_FILM_IPERF_PORT", 5203),
    "google_meet": _env_int("CAMPUS_MEET_IPERF_PORT", 5204),
}

PORT_SCAN_PORTS = [
    22,
    80,
    443,
    SERVICE_PORTS["college_sync"],
    SERVICE_PORTS["social_media"],
    SERVICE_PORTS["elearning"],
    SERVICE_PORTS["college_mis"],
    5201,
    5203,
    5204,
]

ACTION_TRAFFIC_PROFILE = {
    "discover_network": {
        "traffic_class": "discovery",
        "priority": "observe",
        "controller_expectation": "Benign reachability probe; should remain visible without queue escalation.",
    },
    "scan_surface": {
        "traffic_class": "security_probe",
        "priority": "blocked when suspicious",
        "controller_expectation": "Repeated probing should trigger a security alert and be blocked by policy or scan defense.",
    },
    "film_download": {
        "traffic_class": "bulk_download",
        "priority": "low",
        "service_port": SERVICE_PORTS["film_download"],
        "controller_expectation": "Bulk traffic should stay in the low queue and be throttled first during congestion.",
    },
    "elearning_access": {
        "traffic_class": "academic_critical",
        "priority": "high",
        "service_port": SERVICE_PORTS["elearning"],
        "controller_expectation": "Academic services should stay in the highest-priority queue.",
    },
    "college_mis_access": {
        "traffic_class": "academic_critical",
        "priority": "high",
        "service_port": SERVICE_PORTS["college_mis"],
        "controller_expectation": "Administrative academic services should stay protected in the high queue.",
    },
    "social_media_access": {
        "traffic_class": "normal_browsing",
        "priority": "medium",
        "service_port": SERVICE_PORTS["social_media"],
        "controller_expectation": "Normal browsing should use the medium queue and yield to academic and real-time flows.",
    },
    "google_meet": {
        "traffic_class": "live_collaboration",
        "priority": "high",
        "service_port": SERVICE_PORTS["google_meet"],
        "controller_expectation": "Real-time collaboration should stay in a protected high-priority treatment path.",
    },
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
        self.attack_processes = {}
        self.last_pingall_result = {}
        self.device_histories = {}
        self.device_sessions = {}
        self.portal_root = os.getenv("CAMPUS_PORTAL_ROOT", "/tmp/campus_demo_portal")
        self.portal_seeded = False
        self.simulation_context_file = os.getenv(
            "CAMPUS_SIM_CONTEXT_FILE", "/tmp/campus_simulation_context.json"
        )
        self._write_simulation_context()

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

    def _write_simulation_context(self):
        active_sessions = []
        for host_name, sessions in sorted(self.device_sessions.items()):
            try:
                host_ip = _bare_ip(self.net.get(host_name).IP())
            except Exception:
                host_ip = ""
            for session in sessions:
                proc = session.get("proc")
                if proc is not None and proc.poll() is not None:
                    continue
                active_sessions.append(
                    {
                        "host_id": host_name,
                        "ip": host_ip,
                        "action": session.get("action"),
                        "traffic_class": session.get("traffic_class"),
                        "priority": session.get("priority"),
                        "started_ts": session.get("started_ts"),
                        "duration_s": session.get("duration_s"),
                    }
                )
        try:
            _write_json_atomic(
                self.simulation_context_file,
                {"ts": time.time(), "active_sessions": active_sessions},
            )
        except Exception:
            pass

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

    def _next_available_campus_ip(self):
        # Dynamic hosts are placed in 10.5.0.0/24 to avoid conflicts with
        # the fixed subnets used by the real college topology.
        campus_subnet = ipaddress.ip_network("10.5.0.0/24")
        used = {
            _bare_ip(str(host.params.get("ip", "")))
            for host in self.net.hosts
            if _bare_ip(str(host.params.get("ip", "")))
        }
        for candidate in campus_subnet.hosts():
            text = str(candidate)
            if text not in used:
                return text
        raise RuntimeError("no free IP addresses remain in 10.5.0.0/24")

    def _resolve_target(self, target_text):
        target_text = str(target_text or "").strip()
        if not target_text:
            raise ValueError("target is required")
        if self._host_exists(target_text):
            host = self.net.get(target_text)
            return {
                "name": host.name,
                "display_name": self.dynamic_labels.get(
                    host.name, HOST_LABELS.get(host.name, host.name)
                ),
                "ip": _bare_ip(str(host.params.get("ip", ""))),
            }
        try:
            ipaddress.ip_address(target_text)
        except ValueError as exc:
            raise ValueError("target must be a host ID or IPv4 address") from exc
        for host in self.net.hosts:
            if _bare_ip(str(host.params.get("ip", ""))) == target_text:
                return {
                    "name": host.name,
                    "display_name": self.dynamic_labels.get(
                        host.name, HOST_LABELS.get(host.name, host.name)
                    ),
                    "ip": target_text,
                }
        return {"name": "", "display_name": target_text, "ip": target_text}

    def _append_device_history(self, host_name, entry):
        rows = self.device_histories.setdefault(host_name, [])
        rows.append(entry)
        self.device_histories[host_name] = rows[-20:]

    def _read_tail(self, path, limit=1200):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return ""
        text = text.strip()
        if len(text) <= limit:
            return text
        return "..." + text[-limit:]

    def _catalog_actions(self):
        rows = []
        for key, cfg in DEVICE_ACTION_CATALOG.items():
            traffic = ACTION_TRAFFIC_PROFILE.get(key, {})
            rows.append(
                {
                    "key": key,
                    "label": cfg["label"],
                    "kind": cfg["kind"],
                    "description": cfg["description"],
                    "traffic_class": traffic.get("traffic_class"),
                    "priority": traffic.get("priority"),
                    "service_port": traffic.get("service_port"),
                }
            )
        return rows

    def _seed_portal_assets(self):
        if self.portal_seeded:
            return
        os.makedirs(os.path.join(self.portal_root, "elearning"), exist_ok=True)
        os.makedirs(os.path.join(self.portal_root, "mis"), exist_ok=True)
        os.makedirs(os.path.join(self.portal_root, "social"), exist_ok=True)
        os.makedirs(os.path.join(self.portal_root, "sync"), exist_ok=True)
        with open(os.path.join(self.portal_root, "index.html"), "w", encoding="utf-8") as f:
            f.write(
                "<html><body><h1>Campus Digital Services</h1>"
                "<p>Simulated college portal for SDN traffic workflows.</p></body></html>"
            )
        with open(
            os.path.join(self.portal_root, "elearning", "index.html"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                "<html><body><h1>E-Learning</h1>"
                "<p>Course content, assignments, and lecture material are available.</p></body></html>"
            )
        with open(
            os.path.join(self.portal_root, "mis", "status.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "service": "college-mis",
                    "status": "online",
                    "note": "Academic records and registration systems are reachable.",
                },
                f,
                indent=2,
                sort_keys=True,
            )
        with open(
            os.path.join(self.portal_root, "social", "feed.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "service": "social-media",
                    "status": "online",
                    "feed": [
                        "College announcement board",
                        "Staff social feed",
                        "Student club updates",
                    ],
                },
                f,
                indent=2,
                sort_keys=True,
            )
        with open(
            os.path.join(self.portal_root, "sync", "college.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "service": "college-system-sync",
                    "status": "synchronized",
                    "source": "simulated academic systems",
                },
                f,
                indent=2,
                sort_keys=True,
            )
        self.portal_seeded = True

    def _ensure_http_service(self, service_key):
        if not self._host_exists("h_server1"):
            raise RuntimeError("primary service server is unavailable")
        self._seed_portal_assets()
        if service_key not in SERVICE_PORTS:
            raise ValueError("unknown service key: %s" % service_key)
        port = SERVICE_PORTS[service_key]
        host = self.net.get("h_server1")
        running = host.cmd(
            "pgrep -f %s >/dev/null 2>&1; echo $?"
            % shlex.quote(f"python3 -m http.server {port} --directory {self.portal_root}")
        ).strip()
        if running == "0":
            return port
        host.cmd(
            "nohup python3 -m http.server %d --directory %s >/tmp/campus_portal_http_%d.log 2>&1 &"
            % (port, shlex.quote(self.portal_root), port)
        )
        time.sleep(0.2)
        return port

    def _start_http_session(self, host, url, duration_s, log_path):
        code = (
            "import sys,time,urllib.request\n"
            "url=sys.argv[1]\n"
            "duration=int(sys.argv[2])\n"
            "end=time.time()+duration\n"
            "idx=0\n"
            "while time.time()<end:\n"
            " start=time.time()\n"
            " try:\n"
            "  with urllib.request.urlopen(url, timeout=5) as resp:\n"
            "   data=resp.read(4096)\n"
            "   elapsed=(time.time()-start)*1000.0\n"
            "   print(f'[{idx}] status={getattr(resp, \"status\", 200)} bytes={len(data)} elapsed_ms={elapsed:.2f}', flush=True)\n"
            " except Exception as exc:\n"
            "  print(f'[{idx}] error={exc}', flush=True)\n"
            " time.sleep(0.8)\n"
            " idx += 1\n"
        )
        return host.popen(
            ["python3", "-c", code, url, str(int(duration_s))],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )

    def _start_port_scan_session(self, host, target_ip, duration_s, log_path):
        code = (
            "import socket,sys,time\n"
            "target=sys.argv[1]\n"
            "duration=int(sys.argv[2])\n"
            "end=time.time()+duration\n"
            "ports=list(range(1,41))\n"
            "round_no=0\n"
            "while time.time()<end:\n"
            " for port in ports:\n"
            "  sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "  sock.settimeout(0.08)\n"
            "  try:\n"
            "   sock.connect((target, port))\n"
            "   print(f'[round {round_no}] port {port} open', flush=True)\n"
            "  except Exception:\n"
            "   print(f'[round {round_no}] port {port} probed', flush=True)\n"
            "  finally:\n"
            "   sock.close()\n"
            "  time.sleep(0.2)\n"
            "  if time.time() >= end:\n"
            "   break\n"
            " round_no += 1\n"
            " time.sleep(0.5)\n"
        )
        return host.popen(
            ["python3", "-c", code, target_ip, str(int(duration_s))],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )

    def _launch_device_session(
        self,
        host_name,
        host,
        action,
        label,
        proc,
        log_path,
        duration_s,
        target,
        traffic_profile=None,
    ):
        now = time.time()
        traffic_profile = traffic_profile or ACTION_TRAFFIC_PROFILE.get(action, {})
        session = {
            "action": action,
            "label": label,
            "target": target,
            "duration_s": duration_s,
            "started_ts": now,
            "log_path": log_path,
            "proc": proc,
            "traffic_class": traffic_profile.get("traffic_class"),
            "priority": traffic_profile.get("priority"),
            "controller_expectation": traffic_profile.get("controller_expectation"),
            "service_port": traffic_profile.get("service_port"),
        }
        self.device_sessions.setdefault(host_name, []).append(session)
        self._write_simulation_context()
        return session

    def _ensure_iperf_server(self, port):
        if not self._host_exists("h_server1"):
            raise RuntimeError("primary service server is unavailable")
        host = self.net.get("h_server1")
        if host.cmd("command -v iperf3 >/dev/null 2>&1; echo $?").strip() != "0":
            raise RuntimeError("iperf3 is not installed inside Mininet hosts")
        running = host.cmd(
            "pgrep -f %s >/dev/null 2>&1; echo $?"
            % shlex.quote(f"iperf3 -s -p {int(port)}")
        ).strip()
        if running != "0":
            host.cmd(
                "nohup iperf3 -s -p %d >/tmp/campus_iperf_%d.log 2>&1 &"
                % (int(port), int(port))
            )
            time.sleep(0.2)
        return int(port)

    def _run_host_fetch(self, host, url, label):
        script = (
            "import json,time,urllib.request\n"
            f"url={json.dumps(url)}\n"
            "started=time.time()\n"
            "with urllib.request.urlopen(url, timeout=8) as resp:\n"
            " data=resp.read()\n"
            " headers=dict(resp.headers)\n"
            "status=getattr(resp,'status',200)\n"
            "elapsed=(time.time()-started)*1000.0\n"
            "preview=data[:180].decode('utf-8', errors='replace')\n"
            "print(json.dumps({'status':status,'bytes':len(data),'elapsed_ms':round(elapsed,2),'preview':preview,'label':"
            + json.dumps(label)
            + "}))\n"
        )
        raw = host.cmd("python3 - <<'PY'\n%s\nPY\n" % script)
        payload = None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
        if not isinstance(payload, dict):
            raise RuntimeError("service response could not be parsed")
        return payload

    def _discover_reachable_hosts(self, host_name):
        peers = []
        for peer in self.net.hosts:
            ip_addr = _bare_ip(str(peer.params.get("ip", "")))
            if peer.name == host_name or not ip_addr:
                continue
            peers.append((peer.name, ip_addr))
        if not peers:
            return []
        host = self.net.get(host_name)
        cmd = "for ip in %s; do ping -c 1 -W 1 $ip >/dev/null 2>&1 && echo $ip; done" % " ".join(
            shlex.quote(ip_addr) for _, ip_addr in peers
        )
        seen_ips = {
            line.strip()
            for line in host.cmd(cmd).splitlines()
            if line.strip()
        }
        discovered = []
        for peer_name, ip_addr in peers:
            if ip_addr not in seen_ips:
                continue
            discovered.append(
                {
                    "name": peer_name,
                    "display_name": self.dynamic_labels.get(
                        peer_name, HOST_LABELS.get(peer_name, peer_name)
                    ),
                    "ip": ip_addr,
                    "category": self.dynamic_meta.get(peer_name, {}).get(
                        "category", HOST_CATEGORIES.get(peer_name, "user_device")
                    ),
                }
            )
        return discovered

    def _scan_attack_surface(self, host_name, targets):
        if not targets:
            return []
        host = self.net.get(host_name)
        script = (
            "import json,socket\n"
            f"targets={json.dumps(targets)}\n"
            f"ports={json.dumps(PORT_SCAN_PORTS)}\n"
            "rows=[]\n"
            "for item in targets:\n"
            " open_ports=[]\n"
            " for port in ports:\n"
            "  sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "  sock.settimeout(0.35)\n"
            "  try:\n"
            "   sock.connect((item['ip'], int(port)))\n"
            "   open_ports.append(int(port))\n"
            "  except Exception:\n"
            "   pass\n"
            "  finally:\n"
            "   sock.close()\n"
            " rows.append({'name':item['name'],'ip':item['ip'],'open_ports':open_ports})\n"
            "print(json.dumps(rows))\n"
        )
        raw = host.cmd("python3 - <<'PY'\n%s\nPY\n" % script)
        for line in reversed(raw.splitlines()):
            try:
                parsed = json.loads(line.strip())
            except Exception:
                continue
            if isinstance(parsed, list):
                return parsed
        return []

    def _cleanup_device_sessions(self):
        for host_name, sessions in list(self.device_sessions.items()):
            active = []
            for session in sessions:
                proc = session.get("proc")
                if proc is None:
                    continue
                alive = proc.poll() is None
                if alive:
                    session["elapsed_s"] = round(
                        max(time.time() - float(session.get("started_ts", time.time())), 0.0),
                        2,
                    )
                    active.append(session)
                    continue
                if not session.get("completed_recorded"):
                    session["completed_recorded"] = True
                    detail = self._read_tail(session.get("log_path", ""))
                    entry = {
                        "ts": time.time(),
                        "action": session.get("action"),
                        "label": session.get("label"),
                        "status": "completed",
                        "target": session.get("target"),
                        "detail": detail or "Session completed.",
                        "traffic_class": session.get("traffic_class"),
                        "priority": session.get("priority"),
                        "service_port": session.get("service_port"),
                    }
                    self._append_device_history(host_name, entry)
                    self._record_operation(
                        "device_session_completed",
                        "ok",
                        host_id=host_name,
                        action=session.get("action"),
                        target=session.get("target"),
                        label=session.get("label"),
                    )
            if active:
                self.device_sessions[host_name] = active
            else:
                self.device_sessions.pop(host_name, None)
        self._write_simulation_context()

    def _build_workspace_payload(self, host_name):
        self._cleanup_device_sessions()
        device = self.get_device(host_name)["device"]
        sessions = []
        for session in self.device_sessions.get(host_name, []):
            sessions.append(
                {
                    "action": session.get("action"),
                    "label": session.get("label"),
                    "target": session.get("target"),
                    "started_ts": session.get("started_ts"),
                    "elapsed_s": round(
                        max(time.time() - float(session.get("started_ts", time.time())), 0.0),
                        2,
                    ),
                    "duration_s": session.get("duration_s"),
                    "log_excerpt": self._read_tail(session.get("log_path", "")),
                    "traffic_class": session.get("traffic_class"),
                    "priority": session.get("priority"),
                    "controller_expectation": session.get("controller_expectation"),
                    "service_port": session.get("service_port"),
                }
            )
        return {
            "ok": True,
            "workspace": {
                "device": device,
                "actions": self._catalog_actions(),
                "active_sessions": sessions,
                "recent_activity": list(reversed(self.device_histories.get(host_name, []))),
                "service_endpoints": {
                    "elearning_url": f"http://10.0.1.10:{SERVICE_PORTS['elearning']}/elearning/index.html",
                    "college_mis_url": f"http://10.0.1.10:{SERVICE_PORTS['college_mis']}/mis/status.json",
                    "social_media_url": f"http://10.0.1.10:{SERVICE_PORTS['social_media']}/social/feed.json",
                    "college_sync_url": f"http://10.0.1.10:{SERVICE_PORTS['college_sync']}/sync/college.json",
                },
            },
        }

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
                    "updated_ts": meta.get("updated_ts"),
                    "ip_assignment": meta.get("ip_assignment", "manual"),
                    "provided_mac": meta.get("provided_mac", ""),
                    "known_on_connect": [
                        "switch",
                        "switch_port",
                        "mac_address",
                    ],
                    "device_lab_actions": self._catalog_actions(),
                },
            }

    def get_device_workspace(self, name):
        with self.lock:
            if not self._node_exists(name):
                raise ValueError(f"device not found: {name}")
            host = self.net.get(name)
            if host not in self.net.hosts:
                raise ValueError(f"node is not an endpoint host: {name}")
            return self._build_workspace_payload(name)

    def run_device_action(self, name, action, target=None, duration=None):
        with self.lock:
            if not self._node_exists(name):
                raise ValueError(f"device not found: {name}")
            host = self.net.get(name)
            if host not in self.net.hosts:
                raise ValueError(f"node is not an endpoint host: {name}")

            action = str(action or "").strip()
            if action not in DEVICE_ACTION_CATALOG:
                raise ValueError(
                    "unsupported device action: %s" % (action or "unknown")
                )

            self._cleanup_device_sessions()
            display_name = self.dynamic_labels.get(name, HOST_LABELS.get(name, name))
            duration_s = max(15, min(900, int(duration or 120)))
            now = time.time()
            traffic_profile = ACTION_TRAFFIC_PROFILE.get(action, {})

            if action == "stop_sessions":
                stopped = []
                for session in self.device_sessions.pop(name, []):
                    proc = session.get("proc")
                    if proc is not None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    stopped.append(session.get("label") or session.get("action"))
                self._write_simulation_context()
                detail = (
                    "Stopped sessions: %s." % ", ".join(stopped)
                    if stopped
                    else "No active sessions were running on this endpoint."
                )
                entry = {
                    "ts": now,
                    "action": action,
                    "label": DEVICE_ACTION_CATALOG[action]["label"],
                    "status": "ok",
                    "target": "",
                    "detail": detail,
                }
                self._append_device_history(name, entry)
                self._record_operation(
                    "device_session_stopped",
                    "ok",
                    host_id=name,
                    label=display_name,
                    stopped=stopped,
                )
                return {
                    "ok": True,
                    "message": detail,
                    "result": entry,
                    "workspace": self._build_workspace_payload(name)["workspace"],
                }

            if action == "ping_target":
                resolved = self._resolve_target(target or "10.0.1.10")
                raw = host.cmd(
                    "ping -c 3 -W 1 %s 2>&1" % shlex.quote(resolved["ip"])
                )
                packet_loss = "unknown"
                match = re.search(r"(\d+)% packet loss", raw)
                if match:
                    packet_loss = match.group(1) + "%"
                detail = (
                    f"Pinged {resolved['display_name']} [{resolved['ip']}]. "
                    f"Observed packet loss: {packet_loss}.\n\n{raw.strip()}"
                ).strip()
                entry = {
                    "ts": now,
                    "action": action,
                    "label": DEVICE_ACTION_CATALOG[action]["label"],
                    "status": "ok",
                    "target": resolved["ip"],
                    "detail": detail,
                }
            elif action == "discover_network":
                discovered = self._discover_reachable_hosts(name)
                if discovered:
                    detail = "Reachable campus peers:\n" + "\n".join(
                        "- %s [%s] (%s)"
                        % (
                            row["display_name"],
                            row["ip"],
                            _category_label(row["category"]),
                        )
                        for row in discovered
                    )
                else:
                    detail = "No campus peers responded to the live discovery probe."
                entry = {
                    "ts": now,
                    "action": action,
                    "label": DEVICE_ACTION_CATALOG[action]["label"],
                    "status": "ok",
                    "target": "campus-subnet",
                    "detail": detail,
                }
            elif action == "scan_surface":
                resolved = self._resolve_target(target or "10.0.1.10")
                log_path = f"/tmp/{name}_{action}.log"
                proc = self._start_port_scan_session(
                    host, resolved["ip"], duration_s, log_path
                )
                self._launch_device_session(
                    name,
                    host,
                    action,
                    "Network scan",
                    proc,
                    log_path,
                    duration_s,
                    resolved["ip"],
                    traffic_profile=traffic_profile,
                )
                detail = (
                    "Continuous network scan started against %s [%s] for %s seconds. "
                    "This should create repeated port probes so the controller can flag suspicious access and install a block if policy thresholds are crossed."
                    % (resolved["display_name"], resolved["ip"], duration_s)
                )
                entry = {
                    "ts": now,
                    "action": action,
                    "label": "Network scan",
                    "status": "running",
                    "target": resolved["ip"],
                    "detail": detail,
                    "traffic_class": traffic_profile.get("traffic_class"),
                    "priority": traffic_profile.get("priority"),
                    "service_port": traffic_profile.get("service_port"),
                }
                self._record_operation(
                    "device_session_started",
                    "ok",
                    host_id=name,
                    label=display_name,
                    action=action,
                    target=resolved["ip"],
                    duration_s=duration_s,
                    traffic_class=traffic_profile.get("traffic_class"),
                    priority=traffic_profile.get("priority"),
                )
                self._append_device_history(name, entry)
                return {
                    "ok": True,
                    "message": detail,
                    "result": entry,
                    "workspace": self._build_workspace_payload(name)["workspace"],
                }
            elif action in {
                "elearning_access",
                "college_mis_access",
                "social_media_access",
                "film_download",
                "google_meet",
            }:
                target_ip = "10.0.1.10"
                port = traffic_profile.get("service_port")
                label = DEVICE_ACTION_CATALOG[action]["label"]
                log_path = f"/tmp/{name}_{action}.log"
                if action == "elearning_access":
                    port = self._ensure_http_service("elearning")
                    proc = self._start_http_session(
                        host,
                        f"http://10.0.1.10:{port}/elearning/index.html",
                        duration_s,
                        log_path,
                    )
                elif action == "college_mis_access":
                    port = self._ensure_http_service("college_mis")
                    proc = self._start_http_session(
                        host,
                        f"http://10.0.1.10:{port}/mis/status.json",
                        duration_s,
                        log_path,
                    )
                elif action == "social_media_access":
                    port = self._ensure_http_service("social_media")
                    proc = self._start_http_session(
                        host,
                        f"http://10.0.1.10:{port}/social/feed.json",
                        duration_s,
                        log_path,
                    )
                elif action == "film_download":
                    port = self._ensure_iperf_server(SERVICE_PORTS["film_download"])
                    proc = host.popen(
                        shlex.split(
                            "iperf3 -c 10.0.1.10 -p %d -t %d -i 1 -R"
                            % (port, duration_s)
                        ),
                        stdout=open(log_path, "w"),
                        stderr=subprocess.STDOUT,
                    )
                else:
                    port = self._ensure_iperf_server(SERVICE_PORTS["google_meet"])
                    proc = host.popen(
                        shlex.split(
                            "iperf3 -c 10.0.1.10 -p %d -u -b 2M -t %d -i 1"
                            % (port, duration_s)
                        ),
                        stdout=open(log_path, "w"),
                        stderr=subprocess.STDOUT,
                    )
                traffic_profile = dict(traffic_profile)
                traffic_profile["service_port"] = port
                self._launch_device_session(
                    name,
                    host,
                    action,
                    label,
                    proc,
                    log_path,
                    duration_s,
                    target_ip,
                    traffic_profile=traffic_profile,
                )
                detail = (
                    "%s started for %s seconds on service port %s. "
                    "%s"
                    % (
                        label,
                        duration_s,
                        port,
                        traffic_profile.get(
                            "controller_expectation",
                            "Watch the controller response while this session runs.",
                        ),
                    )
                )
                entry = {
                    "ts": now,
                    "action": action,
                    "label": label,
                    "status": "running",
                    "target": target_ip,
                    "detail": detail,
                    "traffic_class": traffic_profile.get("traffic_class"),
                    "priority": traffic_profile.get("priority"),
                    "service_port": port,
                }
                self._record_operation(
                    "device_session_started",
                    "ok",
                    host_id=name,
                    label=display_name,
                    action=action,
                    target=target_ip,
                    duration_s=duration_s,
                    traffic_class=traffic_profile.get("traffic_class"),
                    priority=traffic_profile.get("priority"),
                    service_port=port,
                )
                self._append_device_history(name, entry)
                return {
                    "ok": True,
                    "message": detail,
                    "result": entry,
                    "workspace": self._build_workspace_payload(name)["workspace"],
                }
            else:
                raise ValueError("unsupported device action path")

            if traffic_profile:
                entry["traffic_class"] = traffic_profile.get("traffic_class")
                entry["priority"] = traffic_profile.get("priority")
                entry["service_port"] = traffic_profile.get("service_port")
            self._append_device_history(name, entry)
            self._record_operation(
                "device_action",
                "ok",
                host_id=name,
                label=display_name,
                action=action,
                target=entry.get("target"),
                traffic_class=traffic_profile.get("traffic_class"),
                priority=traffic_profile.get("priority"),
            )
            return {
                "ok": True,
                "message": entry["detail"].splitlines()[0],
                "result": entry,
                "workspace": self._build_workspace_payload(name)["workspace"],
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
            campus_supernet = ipaddress.ip_network("10.0.0.0/8")
            if ip_obj not in campus_supernet or ip_obj in {
                campus_supernet.network_address,
                campus_supernet.broadcast_address,
            }:
                raise ValueError(
                    "device IP must be inside the campus supernet 10.0.0.0/8"
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
                    "ip_assignment": "manual",
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
                    "ip_assignment": meta.get("ip_assignment", "manual"),
                    "provided_mac": meta.get("provided_mac", ""),
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
                clients = ["h_lab7_1", "h_lab6_1"]
            clients = [c for c in clients if self._host_exists(c)]
            if not clients:
                raise ValueError("no valid client hosts available for stress test")

            seconds = max(10, int(seconds))
            iperf_port = int(iperf_port)

            server = self.net.get("h_server1")
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
                    "iperf3 -c 10.0.1.10 -p %d -t %d -i 1 %s"
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

            if self._host_exists("h_server1"):
                server = self.net.get("h_server1")
                server.cmd("pkill -f 'iperf3 -s -p 5201' || true")

            event = self._record_operation(
                "stop_stress", "ok", stopped_clients=stopped
            )
            self.snapshot()
            return {"ok": True, "message": "stress test stopped", "details": event}

    # ── Attack scripts (pure Python, no external tools) ───────────────────────

    # UDP flood → data-plane saturation. Detected via port PPS stats (~500 ms).
    _UDP_FLOOD_SCRIPT = (
        "import socket,time,sys\n"
        "tgt=sys.argv[1]; end=time.time()+int(sys.argv[2])\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        "s.setblocking(False)\n"
        "while time.time()<end:\n"
        "  for p in range(1,512):\n"
        "    try: s.sendto(b'X'*512,(tgt,p))\n"
        "    except: pass\n"
    )

    # ICMP flood → switch CPU + link saturation. Detected via port PPS stats (~500 ms).
    _ICMP_FLOOD_SCRIPT = (
        "import socket,struct,time,sys\n"
        "def ck(d):\n"
        " if len(d)%2: d+=b'\\x00'\n"
        " s=sum(struct.unpack('!%dH'%(len(d)//2),d))\n"
        " s=(s>>16)+(s&0xffff); s+=(s>>16); return ~s&0xffff\n"
        "tgt=sys.argv[1]; end=time.time()+int(sys.argv[2])\n"
        "sk=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)\n"
        "sk.setblocking(False); pl=b'X'*56; seq=0\n"
        "while time.time()<end:\n"
        " seq=(seq+1)%65536\n"
        " h=struct.pack('!BBHHH',8,0,0,1,seq)\n"
        " h=struct.pack('!BBHHH',8,0,ck(h+pl),1,seq)\n"
        " try: sk.sendto(h+pl,(tgt,0))\n"
        " except: pass\n"
    )

    # Controller flood → random unknown-MAC frames → table-miss → packet-in storm.
    # Detected in packet_in_handler (real-time, no polling) in <100 ms.
    _CTRL_FLOOD_SCRIPT = (
        "import socket,random,time,sys,subprocess\n"
        "dur=int(sys.argv[1]) if len(sys.argv)>1 else 30\n"
        "out=subprocess.check_output(['ip','-o','link','show']).decode()\n"
        "iface=None\n"
        "for ln in out.split('\\n'):\n"
        " p=ln.split()\n"
        " if len(p)>=2:\n"
        "  n=p[1].rstrip(':')\n"
        "  if n!='lo' and '@' not in n: iface=n; break\n"
        "if not iface: raise RuntimeError('no iface')\n"
        "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
        "s.bind((iface,0)); s.setblocking(False)\n"
        "end=time.time()+dur; hdr=b'\\x08\\x00'+b'X'*60\n"
        "while time.time()<end:\n"
        " dst=bytes(random.getrandbits(8) for _ in range(6))\n"
        " src=bytes(random.getrandbits(8) for _ in range(6))\n"
        " try: s.send(dst+src+hdr)\n"
        " except (BlockingIOError,OSError): pass\n"
    )

    _ATTACK_SCRIPTS = {
        "udp_flood":  (_UDP_FLOOD_SCRIPT,  ["python3", "{path}", "{target}", "{duration}"]),
        "icmp_flood": (_ICMP_FLOOD_SCRIPT, ["python3", "{path}", "{target}", "{duration}"]),
        "ctrl_flood": (_CTRL_FLOOD_SCRIPT, ["python3", "{path}", "{duration}"]),
    }

    def start_attack(self, attacker="h_lab7_1", target="10.0.1.10",
                     duration=30, attack_type="udp_flood"):
        """Launch a DDoS simulation from any host. Supports udp_flood, icmp_flood, ctrl_flood."""
        with self.lock:
            if not self._host_exists(attacker):
                raise ValueError("Attacker host %r not found in topology" % attacker)

            valid_types = tuple(self._ATTACK_SCRIPTS.keys())
            if attack_type not in valid_types:
                raise ValueError("attack_type must be one of: %s" % ", ".join(valid_types))

            duration = max(5, min(300, int(duration)))

            # Stop any running attack first.
            self._kill_attack_procs()

            host = self.net.get(attacker)
            script_body, cmd_tmpl = self._ATTACK_SCRIPTS[attack_type]
            script_path = "/tmp/%s_ddos_flood.py" % attacker
            host.cmd("cat > %s << 'PYEOF'\n%sPYEOF" % (script_path, script_body))

            cmd = [
                c.format(path=script_path, target=target, duration=str(duration))
                for c in cmd_tmpl
            ]
            proc = host.popen(
                cmd,
                stdout=open("/tmp/%s_ddos.log" % attacker, "w"),
                stderr=subprocess.STDOUT,
            )
            self.attack_processes[attacker] = proc

            event = self._record_operation(
                "start_attack", "ok",
                attacker=attacker, target=target,
                attack_type=attack_type, duration=duration,
            )
            self.snapshot()
            return {
                "ok": True,
                "message": "Attack started: %s from %s" % (attack_type, attacker),
                "details": event,
            }

    def _kill_attack_procs(self):
        for name in list(self.attack_processes.keys()):
            if self._host_exists(name):
                self.net.get(name).cmd("pkill -f ddos_flood.py 2>/dev/null; true")
            try:
                self.attack_processes[name].terminate()
            except Exception:
                pass
        self.attack_processes = {}

    def stop_attack(self):
        """Stop all running DDoS attack processes."""
        with self.lock:
            stopped = list(self.attack_processes.keys())
            self._kill_attack_procs()
            event = self._record_operation("stop_attack", "ok", stopped=stopped)
            self.snapshot()
            return {"ok": True, "message": "DDoS attack stopped", "details": event}

    def get_attack_status(self):
        """Return current DDoS attack state."""
        with self.lock:
            active_attackers = []
            for name, proc in list(self.attack_processes.items()):
                if proc.poll() is None:
                    active_attackers.append(name)
                else:
                    self.attack_processes.pop(name, None)
            return {
                "ok": True,
                "attack_active": bool(active_attackers),
                "attackers": active_attackers,
            }

    def get_operations(self):
        with self.lock:
            self._cleanup_device_sessions()
            running = []
            for host_name, proc in list(self.stress_processes.items()):
                alive = proc.poll() is None
                if alive:
                    running.append(host_name)
                else:
                    self.stress_processes.pop(host_name, None)
            device_sessions = []
            recent_device_activity = []
            for host_name, sessions in sorted(self.device_sessions.items()):
                for session in sessions:
                    device_sessions.append(
                        {
                            "host_id": host_name,
                            "label": self.dynamic_labels.get(
                                host_name, HOST_LABELS.get(host_name, host_name)
                            ),
                            "action": session.get("action"),
                            "title": session.get("label"),
                            "target": session.get("target"),
                            "duration_s": session.get("duration_s"),
                            "elapsed_s": round(
                                max(
                                    time.time()
                                    - float(session.get("started_ts", time.time())),
                                    0.0,
                                ),
                                2,
                            ),
                            "traffic_class": session.get("traffic_class"),
                            "priority": session.get("priority"),
                            "controller_expectation": session.get("controller_expectation"),
                            "service_port": session.get("service_port"),
                        }
                    )
            for host_name, rows in self.device_histories.items():
                for row in rows[-3:]:
                    item = dict(row)
                    item["host_id"] = host_name
                    recent_device_activity.append(item)
            recent_device_activity.sort(key=lambda row: float(row.get("ts", 0.0)))
            return {
                "ok": True,
                "running_stress_clients": running,
                "last_pingall_result": self.last_pingall_result,
                "events": self.operation_events[-80:],
                "device_sessions": device_sessions,
                "recent_device_activity": recent_device_activity[-20:],
            }

    def add_host(
        self,
        name,
        ip,
        attach_switch,
        bandwidth_mbps,
        category=None,
        mac=None,
        auto_assign_ip=False,
    ):
        with self.lock:
            if not name:
                raise ValueError("name is required")

            display_name = str(name).strip()
            host_id = self._allocate_host_id(display_name)

            switch = self.net.get(attach_switch)
            ip_text = str(ip or "").strip()
            ip_assignment = "manual"
            if not ip_text:
                if not auto_assign_ip:
                    raise ValueError("ip is required unless auto_assign_ip is enabled")
                ip_text = self._next_available_campus_ip()
                ip_assignment = "auto"
            ip_cidr = _to_cidr(ip_text)
            bw = float(bandwidth_mbps)
            category_key = _normalize_category(category)
            mac_text = str(mac or "").strip().lower()
            if bw <= 0:
                raise ValueError("bandwidth_mbps must be > 0")
            if mac_text and not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac_text):
                raise ValueError("mac must be in aa:bb:cc:dd:ee:ff format")

            host_kwargs = {"ip": ip_cidr}
            if mac_text:
                host_kwargs["mac"] = mac_text
            host = self.net.addHost(host_id, **host_kwargs)
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
                "ip_assignment": ip_assignment,
                "provided_mac": mac_text,
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
                ip_assignment=ip_assignment,
                provided_mac=mac_text,
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
                    "category_label": _category_label(category_key),
                    "delay": "1ms",
                    "removable": True,
                    "management_origin": "dashboard_added",
                    "ip_assignment": ip_assignment,
                    "provided_mac": mac_text,
                    "ts": time.time(),
                },
                "message": "endpoint added to the live topology",
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
                if path == "/attack_status":
                    self._send_json(parent.get_attack_status())
                    return
                m = re.fullmatch(r"/device/([^/]+)/workspace", path)
                if m:
                    try:
                        self._send_json(parent.get_device_workspace(unquote(m.group(1))))
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 404)
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
                if path == "/start_attack":
                    body = self._read_json()
                    try:
                        self._send_json(
                            parent.start_attack(
                                attacker=body.get("attacker", "h_lab7_1"),
                                target=body.get("target", "10.0.1.10"),
                                duration=body.get("duration", 30),
                                attack_type=body.get("attack_type", "udp_flood"),
                            )
                        )
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                if path == "/stop_attack":
                    try:
                        self._send_json(parent.stop_attack())
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
                            mac=body.get("mac"),
                            auto_assign_ip=bool(body.get("auto_assign_ip", False)),
                        )
                        self._send_json(resp)
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                m = re.fullmatch(r"/device/([^/]+)/action", path)
                if m:
                    body = self._read_json()
                    try:
                        self._send_json(
                            parent.run_device_action(
                                unquote(m.group(1)),
                                body.get("action"),
                                target=body.get("target"),
                                duration=body.get("duration"),
                            )
                        )
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


class BackgroundTrafficGenerator:
    """Continuously generates realistic background traffic between campus hosts.

    Runs iperf3 sessions between zones and the servers to produce real measured
    throughput that the Ryu controller can observe via port stats and write to
    campus_metrics.json, which in turn drives the live flow-dot animation on
    the dashboard topology map.
    """

    # (src, dst, target_mbps, session_secs, repeat_every_secs, label)
    _PATTERNS = [
        ("h_lab7_1",  "h_server1",  18, 12, 18, "Lab7-PC1 → SA-Server"),
        ("h_lab7_2",  "h_server1",  14, 12, 22, "Lab7-PC2 → SA-Server"),
        ("h_lab6_1",  "h_server1",  20, 12, 16, "Lab6-PC1 → SA-Server"),
        ("h_lab6_2",  "h_server2",  10, 12, 20, "Lab6-PC2 → Server1"),
        ("h_mechl1_1","h_server1",   9, 10, 28, "MechLab1-PC1 → SA-Server"),
        ("h_mechl2_1","h_server1",   8, 10, 35, "MechLab2-PC1 → SA-Server"),
        ("h_lab2_1",  "h_server1",  16, 10, 13, "Lab2-PC1 → SA-Server"),
        ("h_lab3_1",  "h_server2",  12, 10, 17, "Lab3-PC1 → Server1"),
        ("h_admin_1", "h_server1",   6, 10, 25, "Admin-PC1 → SA-Server"),
        ("h_acad_1",  "h_server1",  10, 10, 20, "Academic-PC1 → SA-Server"),
    ]

    _SERVER_PORT   = 5201
    _BACKUP_PORT   = 5202

    def __init__(self, net, hosts):
        self.net   = net
        self.hosts = hosts
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        info("*** Starting background traffic generator\n")
        self._ensure_servers()
        for pat in self._PATTERNS:
            jitter = random.uniform(0, pat[4] * 0.6)
            t = threading.Thread(
                target=self._traffic_loop,
                args=(pat, jitter),
                daemon=True,
                name=f"bg-{pat[0]}-{pat[1]}",
            )
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._ping_loop, daemon=True, name="bg-ping")
        t.start()
        self._threads.append(t)
        info("*** Background traffic generator running (%d patterns)\n" % len(self._PATTERNS))

    def stop(self):
        info("*** Stopping background traffic generator\n")
        self._stop.set()
        for host_name, port in (("h_server1", self._SERVER_PORT), ("h_server2", self._BACKUP_PORT)):
            h = self.hosts.get(host_name)
            if h:
                try:
                    h.cmd("pkill -f 'iperf3 -s' 2>/dev/null; true")
                except Exception:
                    pass

    def _ensure_servers(self):
        server = self.hosts.get("h_server1")
        if server:
            server.cmd("pkill -f 'iperf3 -s' 2>/dev/null; true")
            server.cmd(
                "iperf3 -s -p %d -D --logfile /tmp/iperf3_bg_main.log 2>/dev/null"
                % self._SERVER_PORT
            )
        backup = self.hosts.get("h_server2")
        if backup:
            backup.cmd("pkill -f 'iperf3 -s' 2>/dev/null; true")
            backup.cmd(
                "iperf3 -s -p %d -D --logfile /tmp/iperf3_bg_backup.log 2>/dev/null"
                % self._BACKUP_PORT
            )
        time.sleep(1.5)

    def _traffic_loop(self, pat, initial_delay):
        src_name, dst_name, mbps, dur, interval, label = pat
        if self._stop.wait(timeout=initial_delay):
            return
        port = self._BACKUP_PORT if dst_name == "h_server2" else self._SERVER_PORT
        while not self._stop.is_set():
            try:
                src = self.net.get(src_name)
                dst = self.net.get(dst_name)
                dst_ip = dst.IP()
                bw_arg = "%dM" % int(mbps * random.uniform(0.7, 1.2))
                src.cmd(
                    "iperf3 -c %s -p %d -b %s -t %d "
                    "--logfile /tmp/iperf3_bg_%s.log 2>/dev/null"
                    % (dst_ip, port, bw_arg, dur, src_name)
                )
            except Exception:
                pass
            rest = max(2.0, interval - dur + random.uniform(-3, 3))
            self._stop.wait(timeout=rest)

    def _ping_loop(self):
        pairs = [
            ("h_lab7_1",  "h_lab6_1"),
            ("h_admin_1", "h_server1"),
            ("h_lab2_1",  "h_server1"),
            ("h_acad_1",  "h_server2"),
        ]
        while not self._stop.is_set():
            for src_name, dst_name in pairs:
                try:
                    src = self.net.get(src_name)
                    dst = self.net.get(dst_name)
                    src.cmd("ping -c 4 -W 1 %s >/dev/null 2>&1 &" % dst.IP())
                except Exception:
                    pass
                if self._stop.wait(timeout=2.5):
                    return
            self._stop.wait(timeout=8)


def create_campus_net():
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoStaticArp=True,
        waitConnected=10,
    )

    info("*** Adding remote controller (Ryu on 127.0.0.1:6633)\n")
    net.addController("c0", ip="127.0.0.1", port=6633)

    info("*** Adding switches (real college topology)\n")
    s1  = net.addSwitch("s1",  protocols="OpenFlow13")  # core_switch (L3 SWITCH 3560-24PS)
    s2  = net.addSwitch("s2",  protocols="OpenFlow13")  # dist_left
    s3  = net.addSwitch("s3",  protocols="OpenFlow13")  # dist_right
    s4  = net.addSwitch("s4",  protocols="OpenFlow13")  # lab7_sw
    s5  = net.addSwitch("s5",  protocols="OpenFlow13")  # lab6_sw
    s6  = net.addSwitch("s6",  protocols="OpenFlow13")  # mechl1_sw
    s7  = net.addSwitch("s7",  protocols="OpenFlow13")  # mechl2_sw
    s8  = net.addSwitch("s8",  protocols="OpenFlow13")  # lab2_sw
    s9  = net.addSwitch("s9",  protocols="OpenFlow13")  # mechatronic_sw
    s10 = net.addSwitch("s10", protocols="OpenFlow13")  # incubation_sw
    s11 = net.addSwitch("s11", protocols="OpenFlow13")  # lab3_sw
    s12 = net.addSwitch("s12", protocols="OpenFlow13")  # lab4_sw
    s13 = net.addSwitch("s13", protocols="OpenFlow13")  # academic_sw
    s14 = net.addSwitch("s14", protocols="OpenFlow13")  # admin_sw

    info("*** Adding hosts\n")
    hosts = {
        # Servers
        "h_server1":  net.addHost("h_server1",  ip="10.0.1.10/16", mac="00:00:00:00:00:0a"),
        "h_server2":  net.addHost("h_server2",  ip="10.0.1.11/16", mac="00:00:00:00:00:0b"),
        # Lab 7 (s4)
        "h_lab7_1":   net.addHost("h_lab7_1",   ip="10.1.7.1/16",  mac="00:00:01:07:00:01"),
        "h_lab7_2":   net.addHost("h_lab7_2",   ip="10.1.7.2/16",  mac="00:00:01:07:00:02"),
        "h_lab7_3":   net.addHost("h_lab7_3",   ip="10.1.7.3/16",  mac="00:00:01:07:00:03"),
        # Lab 6 (s5)
        "h_lab6_1":   net.addHost("h_lab6_1",   ip="10.1.6.1/16",  mac="00:00:01:06:00:01"),
        "h_lab6_2":   net.addHost("h_lab6_2",   ip="10.1.6.2/16",  mac="00:00:01:06:00:02"),
        "h_lab6_3":   net.addHost("h_lab6_3",   ip="10.1.6.3/16",  mac="00:00:01:06:00:03"),
        # Mech Lab 1 (s6)
        "h_mechl1_1": net.addHost("h_mechl1_1", ip="10.1.10.1/16", mac="00:00:01:0a:00:01"),
        "h_mechl1_2": net.addHost("h_mechl1_2", ip="10.1.10.2/16", mac="00:00:01:0a:00:02"),
        "h_mechl1_3": net.addHost("h_mechl1_3", ip="10.1.10.3/16", mac="00:00:01:0a:00:03"),
        # Mech Lab 2 (s7)
        "h_mechl2_1": net.addHost("h_mechl2_1", ip="10.1.11.1/16", mac="00:00:01:0b:00:01"),
        "h_mechl2_2": net.addHost("h_mechl2_2", ip="10.1.11.2/16", mac="00:00:01:0b:00:02"),
        "h_mechl2_3": net.addHost("h_mechl2_3", ip="10.1.11.3/16", mac="00:00:01:0b:00:03"),
        # Lab 2 (s8)
        "h_lab2_1":   net.addHost("h_lab2_1",   ip="10.1.2.1/16",  mac="00:00:01:02:00:01"),
        "h_lab2_2":   net.addHost("h_lab2_2",   ip="10.1.2.2/16",  mac="00:00:01:02:00:02"),
        "h_lab2_3":   net.addHost("h_lab2_3",   ip="10.1.2.3/16",  mac="00:00:01:02:00:03"),
        # Mechatronic (s9)
        "h_mech_1":   net.addHost("h_mech_1",   ip="10.1.12.1/16", mac="00:00:01:0c:00:01"),
        "h_mech_2":   net.addHost("h_mech_2",   ip="10.1.12.2/16", mac="00:00:01:0c:00:02"),
        "h_mech_3":   net.addHost("h_mech_3",   ip="10.1.12.3/16", mac="00:00:01:0c:00:03"),
        # Incubation (s10)
        "h_incub_1":  net.addHost("h_incub_1",  ip="10.2.1.1/16",  mac="00:00:02:01:00:01"),
        "h_incub_2":  net.addHost("h_incub_2",  ip="10.2.1.2/16",  mac="00:00:02:01:00:02"),
        "h_incub_3":  net.addHost("h_incub_3",  ip="10.2.1.3/16",  mac="00:00:02:01:00:03"),
        # Lab 3 (s11)
        "h_lab3_1":   net.addHost("h_lab3_1",   ip="10.1.3.1/16",  mac="00:00:01:03:00:01"),
        "h_lab3_2":   net.addHost("h_lab3_2",   ip="10.1.3.2/16",  mac="00:00:01:03:00:02"),
        "h_lab3_3":   net.addHost("h_lab3_3",   ip="10.1.3.3/16",  mac="00:00:01:03:00:03"),
        # Lab 4 (s12)
        "h_lab4_1":   net.addHost("h_lab4_1",   ip="10.1.4.1/16",  mac="00:00:01:04:00:01"),
        "h_lab4_2":   net.addHost("h_lab4_2",   ip="10.1.4.2/16",  mac="00:00:01:04:00:02"),
        "h_lab4_3":   net.addHost("h_lab4_3",   ip="10.1.4.3/16",  mac="00:00:01:04:00:03"),
        # Academic (s13)
        "h_acad_1":   net.addHost("h_acad_1",   ip="10.3.1.1/16",  mac="00:00:03:01:00:01"),
        "h_acad_2":   net.addHost("h_acad_2",   ip="10.3.1.2/16",  mac="00:00:03:01:00:02"),
        "h_acad_3":   net.addHost("h_acad_3",   ip="10.3.1.3/16",  mac="00:00:03:01:00:03"),
        # Admin (s14)
        "h_admin_1":  net.addHost("h_admin_1",  ip="10.4.1.1/16",  mac="00:00:04:01:00:01"),
        "h_admin_2":  net.addHost("h_admin_2",  ip="10.4.1.2/16",  mac="00:00:04:01:00:02"),
        "h_admin_3":  net.addHost("h_admin_3",  ip="10.4.1.3/16",  mac="00:00:04:01:00:03"),
    }

    # Use TBF shaping to avoid noisy HTB quantum warnings on some kernels.
    def add_qos_link(node1, node2, bw, delay):
        link = net.addLink(node1, node2, bw=bw, delay=delay, use_tbf=True)
        link.campus_bw_mbps = float(bw)
        link.campus_delay = str(delay)
        return link

    info("*** Adding distribution/uplink links (1 Gbps)\n")
    add_qos_link(s1, s2, bw=1000, delay="1ms")   # core ↔ dist_left
    add_qos_link(s1, s3, bw=1000, delay="1ms")   # core ↔ dist_right

    info("*** Adding access links from dist_left (s2) to lab switches\n")
    add_qos_link(s2, s4,  bw=100, delay="1ms")   # dist_left ↔ lab7_sw
    add_qos_link(s2, s5,  bw=100, delay="1ms")   # dist_left ↔ lab6_sw
    add_qos_link(s2, s6,  bw=100, delay="1ms")   # dist_left ↔ mechl1_sw
    add_qos_link(s2, s9,  bw=100, delay="1ms")   # dist_left ↔ mechatronic_sw
    add_qos_link(s2, s10, bw=100, delay="1ms")   # dist_left ↔ incubation_sw

    info("*** Adding access links from dist_right (s3) to lab switches\n")
    add_qos_link(s3, s11, bw=100, delay="1ms")   # dist_right ↔ lab3_sw
    add_qos_link(s3, s13, bw=100, delay="1ms")   # dist_right ↔ academic_sw

    info("*** Adding access links directly off core (s1)\n")
    add_qos_link(s1, s7,  bw=100, delay="1ms")   # core ↔ mechl2_sw
    add_qos_link(s1, s8,  bw=100, delay="1ms")   # core ↔ lab2_sw
    add_qos_link(s1, s12, bw=100, delay="1ms")   # core ↔ lab4_sw
    add_qos_link(s1, s14, bw=100, delay="1ms")   # core ↔ admin_sw

    info("*** Adding server links\n")
    add_qos_link(s2, hosts["h_server1"], bw=100, delay="1ms")   # dist_left ↔ SA Server 2
    add_qos_link(s3, hosts["h_server2"], bw=100, delay="1ms")   # dist_right ↔ Server 1

    info("*** Adding host links (100 Mbps)\n")
    # Lab 7
    add_qos_link(s4, hosts["h_lab7_1"],   bw=100, delay="1ms")
    add_qos_link(s4, hosts["h_lab7_2"],   bw=100, delay="1ms")
    add_qos_link(s4, hosts["h_lab7_3"],   bw=100, delay="1ms")
    # Lab 6
    add_qos_link(s5, hosts["h_lab6_1"],   bw=100, delay="1ms")
    add_qos_link(s5, hosts["h_lab6_2"],   bw=100, delay="1ms")
    add_qos_link(s5, hosts["h_lab6_3"],   bw=100, delay="1ms")
    # Mech Lab 1
    add_qos_link(s6, hosts["h_mechl1_1"], bw=100, delay="1ms")
    add_qos_link(s6, hosts["h_mechl1_2"], bw=100, delay="1ms")
    add_qos_link(s6, hosts["h_mechl1_3"], bw=100, delay="1ms")
    # Mech Lab 2
    add_qos_link(s7, hosts["h_mechl2_1"], bw=100, delay="1ms")
    add_qos_link(s7, hosts["h_mechl2_2"], bw=100, delay="1ms")
    add_qos_link(s7, hosts["h_mechl2_3"], bw=100, delay="1ms")
    # Lab 2
    add_qos_link(s8, hosts["h_lab2_1"],   bw=100, delay="1ms")
    add_qos_link(s8, hosts["h_lab2_2"],   bw=100, delay="1ms")
    add_qos_link(s8, hosts["h_lab2_3"],   bw=100, delay="1ms")
    # Mechatronic
    add_qos_link(s9, hosts["h_mech_1"],   bw=100, delay="1ms")
    add_qos_link(s9, hosts["h_mech_2"],   bw=100, delay="1ms")
    add_qos_link(s9, hosts["h_mech_3"],   bw=100, delay="1ms")
    # Incubation
    add_qos_link(s10, hosts["h_incub_1"], bw=100, delay="1ms")
    add_qos_link(s10, hosts["h_incub_2"], bw=100, delay="1ms")
    add_qos_link(s10, hosts["h_incub_3"], bw=100, delay="1ms")
    # Lab 3
    add_qos_link(s11, hosts["h_lab3_1"],  bw=100, delay="1ms")
    add_qos_link(s11, hosts["h_lab3_2"],  bw=100, delay="1ms")
    add_qos_link(s11, hosts["h_lab3_3"],  bw=100, delay="1ms")
    # Lab 4
    add_qos_link(s12, hosts["h_lab4_1"],  bw=100, delay="1ms")
    add_qos_link(s12, hosts["h_lab4_2"],  bw=100, delay="1ms")
    add_qos_link(s12, hosts["h_lab4_3"],  bw=100, delay="1ms")
    # Academic
    add_qos_link(s13, hosts["h_acad_1"],  bw=100, delay="1ms")
    add_qos_link(s13, hosts["h_acad_2"],  bw=100, delay="1ms")
    add_qos_link(s13, hosts["h_acad_3"],  bw=100, delay="1ms")
    # Admin
    add_qos_link(s14, hosts["h_admin_1"], bw=100, delay="1ms")
    add_qos_link(s14, hosts["h_admin_2"], bw=100, delay="1ms")
    add_qos_link(s14, hosts["h_admin_3"], bw=100, delay="1ms")

    return net, hosts


def _configure_priority_qos():
    """Configure OVS queues used by adaptive priority policy.

    Queue IDs:
    - queue 0: high-priority traffic (exam/auth)
    - queue 1: medium-priority traffic (normal browsing)
    - queue 2: low-priority traffic (entertainment/bulk downloads)
    """

    profiles = [
        # Distribution uplinks toward core (1 Gbps).
        {"port": "s2-eth1", "max_bps": 1_000_000_000, "mid_bps": 400_000_000, "low_bps": 80_000_000},
        {"port": "s3-eth1", "max_bps": 1_000_000_000, "mid_bps": 400_000_000, "low_bps": 80_000_000},
        # Access-switch uplinks to distribution (100 Mbps).
        {"port": "s4-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s5-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s6-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s9-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s10-eth1", "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s11-eth1", "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s13-eth1", "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        # Core-attached access switch uplinks (100 Mbps).
        {"port": "s7-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s8-eth1",  "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s12-eth1", "max_bps": 100_000_000, "mid_bps": 60_000_000, "low_bps": 15_000_000},
        {"port": "s14-eth1", "max_bps": 100_000_000, "mid_bps": 80_000_000, "low_bps": 20_000_000},
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
            "*** Controller is not reachable on 127.0.0.1:6633.\n"
            "*** Disconnected switches: %s\n"
            "*** Start Ryu first: "
            "`source ~/sdn-env/bin/activate && "
            "ryu-manager examples/campus_controller.py ryu.app.ofctl_rest --wsapi-port 8081`\n"
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
    zone_hosts = ["h_lab7_1", "h_lab6_1", "h_admin_1", "h_acad_1"]
    for zh in zone_hosts:
        pair_loss = float(net.ping([hosts[zh], hosts["h_server1"]], timeout="1"))
        if pair_loss > 0.0:
            raise RuntimeError(
                "Host %s cannot fully reach h_server1 (loss %.2f%%)"
                % (zh, pair_loss)
            )

    info("*** Quick throughput test (h_lab7_1 <-> h_server1)\n")
    try:
        net.iperf3([hosts["h_lab7_1"], hosts["h_server1"]], seconds=3)
    except Exception as exc:
        info("*** Iperf3 test warning: %s\n" % exc)
        try:
            net.iperf([hosts["h_lab7_1"], hosts["h_server1"]], seconds=3)
        except Exception as exc2:
            info("*** Iperf fallback warning: %s\n" % exc2)


def build_campus(
    start_cli=True,
    run_tests=True,
    hold_seconds=0,
    topology_state_file=DEFAULT_TOPOLOGY_STATE_FILE,
    runtime_host=DEFAULT_RUNTIME_API_HOST,
    runtime_port=DEFAULT_RUNTIME_API_PORT,
    skip_smoke_tests=DEFAULT_SKIP_SMOKE_TESTS,
):
    net, hosts = create_campus_net()
    runtime_api = None
    bg_traffic = None

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
        if run_tests and not skip_smoke_tests:
            _run_quick_tests(net, hosts)
        elif skip_smoke_tests:
            info(
                "*** Skipping legacy full-mesh smoke tests because segmentation-aware mode is enabled\n"
            )

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

        # Start continuous background traffic so the topology shows real
        # measured throughput on every link instead of idle zeros.
        bg_traffic = BackgroundTrafficGenerator(net, hosts)
        bg_traffic.start()

        if start_cli:
            info("*** Campus zones active: Lab7, Lab6, MechLab1, MechLab2, Lab2, "
                 "Mechatronic, Incubation, Lab3, Lab4, Academic, Admin, Servers\n")
            info("*** OpenFlow profile: 1.3 on switches s1..s14\n")
            info("*** Background traffic active: iperf3 sessions running on all zones\n")
            info("*** Entering Mininet CLI (Ctrl-D or 'exit' to quit)\n")
            info("Try: pingall\n")
            info("Try: iperf h_lab7_1 h_server1\n")
            info("Try: sh ovs-ofctl -O OpenFlow13 dump-flows s1\n")
            CLI(net)
        elif hold_seconds > 0:
            info(
                "*** Holding topology for %ss (runtime API + background traffic active)\n"
                % int(hold_seconds)
            )
            time.sleep(max(1, int(hold_seconds)))
        else:
            info("*** Non-interactive test mode complete (no CLI)\n")
    finally:
        info("*** Stopping network\n")
        if bg_traffic is not None:
            bg_traffic.stop()
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
    parser.add_argument(
        "--skip-smoke-tests",
        action="store_true",
        default=DEFAULT_SKIP_SMOKE_TESTS,
        help="Skip legacy full-connectivity smoke tests at startup",
    )
    args = parser.parse_args()

    setLogLevel("info")
    build_campus(
        start_cli=not args.no_cli,
        hold_seconds=args.hold_seconds,
        topology_state_file=args.topology_state_file,
        runtime_host=args.runtime_host,
        runtime_port=args.runtime_port,
        skip_smoke_tests=bool(args.skip_smoke_tests),
    )
