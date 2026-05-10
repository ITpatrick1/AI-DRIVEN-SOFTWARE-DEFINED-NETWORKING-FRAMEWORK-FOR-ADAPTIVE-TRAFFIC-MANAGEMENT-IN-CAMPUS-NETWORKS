#!/usr/bin/env python3

"""Campus SDN controller with adaptive congestion policy.

Baseline behavior:
- OpenFlow 1.3 learning-switch forwarding.

Adaptive behavior:
- Monitors the primary server uplink load on the core switch.
- When congestion crosses a threshold, activates an ICMP reroute policy:
  IT clients -> primary server IP are rewritten toward backup server IP.
- Deactivates policy when congestion falls below a lower threshold.
"""

import os
import time
import json
import re
from collections import deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib.packet import ipv4
from ryu.lib.packet import packet
from ryu.lib.packet import tcp
from ryu.lib.packet import udp
from ryu.ofproto import ofproto_v1_3


class CampusController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    POLICY_COOKIE = 0xCAFE0001
    POLICY_ENGINE_COOKIE = 0xCAFE1001
    VLAN_AUTOMATION_COOKIE = 0xCAFE3001
    SECURITY_COOKIE = 0xCAFE5001
    DDOS_COOKIE = 0xCAFE7001

    # DDoS detection parameters — data-plane (port-statistics based)
    DDOS_PPS_THRESHOLD = 500      # packets/sec on a host-facing port to trigger detection
    DDOS_DROP_PRIORITY = 400      # higher than SECURITY_COOKIE flows (360)
    DDOS_DROP_HARD_TIMEOUT_S = 30 # auto-expire the drop rule after this many seconds

    # DDoS detection parameters — control-plane (packet-in rate based, real-time)
    CTRL_FLOOD_WINDOW_S = 0.5      # sliding window for packet-in rate (seconds)
    CTRL_FLOOD_THRESHOLD = 150     # packet-in events/sec per switch to flag ctrl-plane flood
    CTRL_FLOOD_DROP_PRIORITY = 401 # highest priority — must beat data-plane rules
    CTRL_FLOOD_HARD_TIMEOUT_S = 30 # auto-expire ctrl-plane mitigation rule

    # Service IPs for adaptive reroute policy (real college servers).
    PRIMARY_SERVER_IP = "10.0.1.10"   # h_server1 — SA Server 2 (on s2)
    BACKUP_SERVER_IP  = "10.0.1.11"   # h_server2 — Server 1    (on s3)
    BACKUP_SERVER_MAC = "00:00:00:00:00:0b"

    # Zone IP lists — real college topology
    ZONE_STUDENT_LAB = (
        "10.1.2.1", "10.1.2.2", "10.1.2.3",    # lab2
        "10.1.3.1", "10.1.3.2", "10.1.3.3",    # lab3
        "10.1.4.1", "10.1.4.2", "10.1.4.3",    # lab4
        "10.1.6.1", "10.1.6.2", "10.1.6.3",    # lab6
        "10.1.7.1", "10.1.7.2", "10.1.7.3",    # lab7
        "10.1.10.1","10.1.10.2","10.1.10.3",   # mechl1
        "10.1.11.1","10.1.11.2","10.1.11.3",   # mechl2
        "10.1.12.1","10.1.12.2","10.1.12.3",   # mechatronic
    )
    ZONE_SERVER     = ("10.0.1.10", "10.0.1.11")
    ZONE_ADMIN      = ("10.4.1.1", "10.4.1.2", "10.4.1.3")
    ZONE_ACADEMIC   = ("10.3.1.1", "10.3.1.2", "10.3.1.3")
    ZONE_INCUBATION = ("10.2.1.1", "10.2.1.2", "10.2.1.3")

    # Aliases for legacy code paths
    IT_CLIENT_IPS = ZONE_STUDENT_LAB

    # DPID/port constants — new 14-switch topology.
    # s1=core, s2=dist_left, s3=dist_right
    # Link order in create_campus_net():
    #   s1-eth1: s1↔s2   s1-eth2: s1↔s3
    #   s1-eth3: s1↔s7   s1-eth4: s1↔s8   s1-eth5: s1↔s12  s1-eth6: s1↔s14
    #   s2-eth1: s2↔s1   s2-eth2: s2↔s4   s2-eth3: s2↔s5
    #   s2-eth4: s2↔s6   s2-eth5: s2↔s9   s2-eth6: s2↔s10
    #   s2-eth7: s2↔h_server1
    #   s3-eth1: s3↔s1   s3-eth2: s3↔s11  s3-eth3: s3↔s13
    #   s3-eth4: s3↔h_server2
    CORE_DPID        = 1
    DIST_LEFT_DPID   = 2    # s2
    DIST_RIGHT_DPID  = 3    # s3
    CORE_TO_LEFT_PORT  = 1  # s1 -> s2
    CORE_TO_RIGHT_PORT = 2  # s1 -> s3

    # Legacy aliases (used by policy engine / reroute logic)
    IT_DPID  = DIST_LEFT_DPID
    NET_DPID = DIST_RIGHT_DPID
    WIFI_DPID = 5   # s5 lab6 switch (closest equivalent of old wifi switch)
    CORE_TO_NET_PORT    = CORE_TO_RIGHT_PORT
    CORE_TO_WIFI_PORT   = 3    # s1 -> s7 (mechl2)
    CORE_TO_PRIMARY_PORT = 1   # s1 -> s2 (path to servers via dist_left)
    IT_TO_CORE_PORT     = 1    # s2 -> s1
    NET_TO_CORE_PORT    = 1    # s3 -> s1
    NET_TO_BACKUP_PORT  = 4    # s3 -> h_server2
    WIFI_TO_CORE_PORT   = 1    # s5 -> s2

    # Port maps: IP → switch-port (used for DDoS per-IP detection on student ports).
    # These are approximations; the learning-switch fills in the rest dynamically.
    IT_CLIENT_PORTS = {
        "10.1.7.1": 2, "10.1.7.2": 3, "10.1.7.3": 4,     # lab7 on s4
        "10.1.6.1": 2, "10.1.6.2": 3, "10.1.6.3": 4,     # lab6 on s5
        "10.1.10.1": 2,"10.1.10.2": 3,"10.1.10.3": 4,    # mechl1 on s6
        "10.1.12.1": 2,"10.1.12.2": 3,"10.1.12.3": 4,    # mechatronic on s9
        "10.2.1.1": 2, "10.2.1.2": 3, "10.2.1.3": 4,     # incubation on s10
    }
    NET_CLIENT_PORTS = {
        "10.1.3.1": 2, "10.1.3.2": 3, "10.1.3.3": 4,     # lab3 on s11
        "10.1.11.1": 2,"10.1.11.2": 3,"10.1.11.3": 4,    # mechl2 on s7
        "10.1.2.1": 2, "10.1.2.2": 3, "10.1.2.3": 4,     # lab2 on s8
    }
    STAFF_CLIENT_PORTS = {
        "10.4.1.1": 2, "10.4.1.2": 3, "10.4.1.3": 4,     # admin on s14
    }
    WIFI_CLIENT_PORTS = {
        "10.3.1.1": 2, "10.3.1.2": 3, "10.3.1.3": 4,     # academic on s13
        "10.1.4.1": 2, "10.1.4.2": 3, "10.1.4.3": 4,     # lab4 on s12
    }
    STAFF_CLIENT_IPS = tuple(ZONE_ADMIN)
    LAB_CLIENT_IPS   = ZONE_STUDENT_LAB
    SERVER_ZONE_IPS  = ZONE_SERVER

    # Allowed TCP ports for student → server access (hosted platform only)
    STUDENT_ALLOWED_SERVER_TCP_PORTS = (80, 443, 8080, 8443)

    ACADEMIC_TCP_PORTS = (8443, 9443)
    EXAM_TCP_PORT      = ACADEMIC_TCP_PORTS[0]
    AUTH_UDP_PORTS     = (67, 68, 1812, 1813)
    REALTIME_UDP_PORTS = (5204,)
    NORMAL_TCP_PORTS   = (80, 443, 8008, 8088)
    BULK_TCP_PORTS     = (5201, 5203, 6881)
    HIGH_PRIORITY_QUEUE_ID   = 0
    MEDIUM_PRIORITY_QUEUE_ID = 1
    LOW_PRIORITY_QUEUE_ID    = 2
    WIFI_CLIENT_IPS  = tuple(ZONE_ACADEMIC) + tuple(
        "10.1.4.%d" % i for i in range(1, 4)
    )
    THROTTLE_QUEUE_ID = LOW_PRIORITY_QUEUE_ID

    # Link capacities (Mbps), aligned with new 14-switch campus_topology.py
    # s1: ports 1-6 (uplinks to s2,s3 @1Gbps; downlinks to s7,s8,s12,s14 @100Mbps)
    # s2: ports 1-7 (uplink to s1 @1Gbps; downlinks to s4-s6,s9-s10 @100Mbps; h_server1 @100Mbps)
    # s3: ports 1-4 (uplink to s1 @1Gbps; downlinks to s11,s13 @100Mbps; h_server2 @100Mbps)
    PORT_CAPACITY_MBPS = {
        1:  {1: 1000.0, 2: 1000.0, 3: 100.0, 4: 100.0, 5: 100.0, 6: 100.0},
        2:  {1: 1000.0, 2: 100.0,  3: 100.0, 4: 100.0, 5: 100.0, 6: 100.0, 7: 100.0},
        3:  {1: 1000.0, 2: 100.0,  3: 100.0, 4: 100.0},
        4:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        5:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        6:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        7:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        8:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        9:  {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        10: {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        11: {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        12: {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        13: {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
        14: {1: 100.0,  2: 100.0,  3: 100.0, 4: 100.0},
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # {dpid: {mac: port}} learning-switch table
        self.mac_to_port = {}
        self.datapaths = {}
        self.port_samples = {}
        self.switch_port_mbps = {}
        self.switch_port_util_pct = {}
        self.switch_port_stats = {}
        self.congested_ports = set()
        self.throughput_congested = False
        self.reroute_active = False
        self.last_core_primary_mbps = 0.0
        self.last_core_wifi_mbps = 0.0
        self.packet_in_count = 0
        self.flow_mod_count = 0
        self.mac_learn_count = 0
        self.policy_engine_rules = {}
        self.vlan_automation_rules = {}
        self.last_ml_routing_choice = None
        self.last_ml_q_values = {}
        self.last_ml_state = {}
        self.last_ml_reward = None
        self.last_ml_epsilon = None
        self.last_ml_steps = 0
        self.last_ml_action_index = None
        self.last_ml_note = None
        self.last_ml_action_ts = 0.0
        self.dqn_integration_enabled = os.getenv(
            "CAMPUS_DQN_INTEGRATION_ENABLED", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.dqn_pending_decision = False
        self.dqn_last_trigger_ts = 0.0
        self.dqn_last_decision_ts = 0.0
        self.dqn_last_action_name = None
        self.dqn_last_trigger_reason = None

        self.congest_high_mbps = float(
            os.getenv("CAMPUS_CONGEST_HIGH_MBPS", "150")
        )
        self.congest_low_mbps = float(os.getenv("CAMPUS_CONGEST_LOW_MBPS", "80"))
        if self.congest_low_mbps >= self.congest_high_mbps:
            self.congest_low_mbps = self.congest_high_mbps * 0.7
        self.port_congest_high_pct = float(
            os.getenv("CAMPUS_PORT_CONGEST_HIGH_PCT", "80")
        )
        self.port_congest_low_pct = float(
            os.getenv("CAMPUS_PORT_CONGEST_LOW_PCT", "65")
        )
        if self.port_congest_low_pct >= self.port_congest_high_pct:
            self.port_congest_low_pct = self.port_congest_high_pct * 0.8

        self.metrics_file = os.getenv("CAMPUS_METRICS_FILE", "/tmp/campus_metrics.json")
        self.events_file = os.getenv(
            "CAMPUS_EVENTS_FILE", "/tmp/campus_policy_events.jsonl"
        )
        self.ml_action_file = os.getenv(
            "CAMPUS_ML_ACTION_FILE", "/tmp/campus_ml_action.json"
        )
        self.manual_settings_file = os.getenv(
            "CAMPUS_MANUAL_SETTINGS_FILE", "/tmp/campus_manual_settings.json"
        )
        self.security_policy_file = os.getenv(
            "CAMPUS_SECURITY_POLICY_FILE", "/tmp/campus_security_policy.json"
        )
        self.network_automation_file = os.getenv(
            "CAMPUS_NETWORK_AUTOMATION_FILE", "/tmp/campus_network_automation.json"
        )
        # Timetable synchronisation (reads state written by timetable_engine.py)
        self.timetable_state_file = os.getenv(
            "CAMPUS_TIMETABLE_STATE", "/tmp/campus_timetable_state.json"
        )
        self.timetable_override_file = os.getenv(
            "CAMPUS_TIMETABLE_OVERRIDE", "/tmp/campus_timetable_override.json"
        )
        self.timetable_db_file = os.getenv(
            "CAMPUS_TIMETABLE_DB", "/tmp/campus_timetable.db"
        )
        self._timetable_state_mtime = 0.0
        self.timetable_mode         = "normal"
        self.timetable_label        = "Normal Operations"
        self.timetable_icon         = "✅"
        self.timetable_active       = False   # True when an override is in effect
        self.timetable_exam_flag    = 0.0
        self.timetable_slot         = {}
        self.timetable_dqn_hint     = "normal_mode"

        # Port-scan detection state
        # {src_ip: deque of (dst_port, timestamp)} — track unique ports contacted
        self.portscan_state     = {}   # {src_ip: {"ports": set, "ts_deque": deque, "alerted": bool}}
        self.portscan_block_ips = {}   # {src_ip: blocked_until_ts}
        self.PORTSCAN_PORT_LIMIT   = 30    # unique dst ports within window triggers detection
        self.PORTSCAN_WINDOW_S     = 10.0  # seconds
        self.PORTSCAN_BLOCK_S      = 300   # block duration
        self.PORTSCAN_DROP_PRIORITY= 390   # below DDoS drop (400) but above security (360)
        self.PORTSCAN_HARD_TIMEOUT = 300

        self.stats_log_interval_s = float(os.getenv("CAMPUS_STATS_LOG_INTERVAL_S", "2"))
        self.dqn_decision_timeout_s = float(
            os.getenv("CAMPUS_DQN_DECISION_TIMEOUT_S", "6")
        )
        self.last_stats_log_ts = 0.0
        self._ml_action_mtime = 0.0
        self._network_automation_mtime = 0.0
        self._security_policy_mtime = 0.0
        self.manual_settings_active = False
        self.network_automation_state = {"switches": {}}
        self.security_policy = self._default_security_policy()
        self.security_block_count = 0   # kept for backward compat
        self.security_flows_attempted = 0
        self.security_flows_blocked = 0
        self.backup_path_packet_count = 0
        self.security_last_event = {}
        self.recent_security_block_src = {}
        self.SECURITY_BLOCK_SUPPRESS_DDOS_S = 300.0
        self.simulation_context_file = os.getenv(
            "CAMPUS_SIM_CONTEXT_FILE", "/tmp/campus_simulation_context.json"
        )
        self._simulation_context_mtime = 0.0
        self.simulation_probe_ips = set()

        # DDoS attack detection and mitigation state — data plane
        self.ddos_active = False
        self.ddos_attacker_ips = {}    # {src_ip: timestamp when first detected}
        self.ddos_blocked_flows = 0    # cumulative DROP rules installed
        self.ddos_port_pps = {}        # {(dpid, port_no): packets/sec}
        self.port_samples_pkts = {}    # {(dpid, port_no): (total_rx_pkts, timestamp)}

        # DDoS attack detection — control plane (real-time, not waiting for stats poll)
        self.ctrl_flood_active = False
        self.ctrl_flood_switches = {}  # {dpid: timestamp when first detected}
        self.ctrl_pkt_in_times = {}    # {dpid: deque of packet-in timestamps}
        self.ctrl_pkt_in_rate = {}     # {dpid: current pkt-in/sec}

        # Unified attack state (used by dashboard)
        self.ddos_attack_type = None   # "data_plane", "ctrl_plane", "icmp_flood", None
        self.ddos_detection_ts = 0.0   # when mitigation was installed (unix time)
        self.ddos_attack_start_ts = 0.0  # set by topology via file signal

        self.monitor_thread = hub.spawn(self._monitor)
        self.logger.info(
            "Adaptive thresholds: high=%.1fMbps low=%.1fMbps | "
            "port_congest high=%.1f%% low=%.1f%%",
            self.congest_high_mbps,
            self.congest_low_mbps,
            self.port_congest_high_pct,
            self.port_congest_low_pct,
        )
        self.logger.info(
            "DQN integration: enabled=%s decision_timeout=%.1fs",
            self.dqn_integration_enabled,
            self.dqn_decision_timeout_s,
        )
        self._write_metrics()

    def _append_event(self, event, **fields):
        payload = {"ts": time.time(), "event": event}
        payload.update(fields)
        try:
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception:
            self.logger.exception("Failed to append event log")

    def _write_metrics(self):
        port_mbps_export = {}
        for dpid, ports in self.switch_port_mbps.items():
            port_mbps_export[str(dpid)] = {
                str(port): round(float(mbps), 3) for port, mbps in ports.items()
            }
        port_util_export = {}
        for dpid, ports in self.switch_port_util_pct.items():
            port_util_export[str(dpid)] = {
                str(port): round(float(util), 3) for port, util in ports.items()
            }
        port_stats_export = {}
        for dpid, ports in self.switch_port_stats.items():
            port_stats_export[str(dpid)] = {
                str(port): {
                    "mbps": round(float(stats.get("mbps", 0.0)), 3),
                    "bps": round(float(stats.get("bps", 0.0)), 3),
                    "util_pct": round(float(stats.get("util_pct", 0.0)), 3),
                    "capacity_mbps": round(
                        float(stats.get("capacity_mbps", 0.0)), 3
                    ),
                    "congested": bool(stats.get("congested", False)),
                    "delta_bytes": int(stats.get("delta_bytes", 0)),
                    "elapsed_s": round(float(stats.get("elapsed_s", 0.0)), 6),
                }
                for port, stats in ports.items()
            }
        congested_ports = [
            {"dpid": int(dpid), "port": int(port)}
            for dpid, port in sorted(self.congested_ports)
        ]

        payload = {
            "ts": time.time(),
            "reroute_active": self.reroute_active,
            "student_throttle_active": self.reroute_active,
            "congest_high_mbps": self.congest_high_mbps,
            "congest_low_mbps": self.congest_low_mbps,
            "port_congest_high_pct": self.port_congest_high_pct,
            "port_congest_low_pct": self.port_congest_low_pct,
            "congested_ports": congested_ports,
            "congested_ports_count": len(congested_ports),
            "core_primary_mbps": self.last_core_primary_mbps,
            "core_wifi_mbps": self.last_core_wifi_mbps,
            "switch_port_mbps": port_mbps_export,
            "switch_port_util_pct": port_util_export,
            "switch_port_stats": port_stats_export,
            "priority_profiles": {
                "exam_traffic": {
                    "description": "Academic services such as e-learning, exams, and college MIS",
                    "queue": self.HIGH_PRIORITY_QUEUE_ID,
                    "match": "tcp dst %s to %s"
                    % (
                        ",".join(str(x) for x in self.ACADEMIC_TCP_PORTS),
                        self.PRIMARY_SERVER_IP,
                    ),
                },
                "authentication_traffic": {
                    "description": "DHCP/RADIUS authentication traffic",
                    "queue": self.HIGH_PRIORITY_QUEUE_ID,
                    "match": "udp dst %s to %s"
                    % (
                        ",".join(str(x) for x in self.AUTH_UDP_PORTS),
                        self.PRIMARY_SERVER_IP,
                    ),
                },
                "live_collaboration": {
                    "description": "Real-time collaboration sessions such as Google Meet",
                    "queue": self.HIGH_PRIORITY_QUEUE_ID,
                    "match": "udp dst %s to %s"
                    % (
                        ",".join(str(x) for x in self.REALTIME_UDP_PORTS),
                        self.PRIMARY_SERVER_IP,
                    ),
                },
                "normal_browsing": {
                    "description": "Normal web/application browsing and social media access",
                    "queue": self.MEDIUM_PRIORITY_QUEUE_ID,
                    "match": "tcp dst %s to %s"
                    % (
                        ",".join(str(x) for x in self.NORMAL_TCP_PORTS),
                        self.PRIMARY_SERVER_IP,
                    ),
                },
                "entertainment_bulk_download": {
                    "description": "Bulk/entertainment flows",
                    "queue": self.LOW_PRIORITY_QUEUE_ID,
                    "match": "tcp dst %s to %s"
                    % (
                        ",".join(str(x) for x in self.BULK_TCP_PORTS),
                        self.PRIMARY_SERVER_IP,
                    ),
                },
                "critical": {
                    "description": "IT/staff and control traffic",
                    "queue": self.HIGH_PRIORITY_QUEUE_ID,
                },
                "student_bulk": {
                    "description": "Student bulk/download traffic under congestion",
                    "queue": self.THROTTLE_QUEUE_ID,
                    "throttle_active": self.reroute_active,
                },
            },
            "policy_engine_cookie": self.POLICY_ENGINE_COOKIE,
            "policy_engine_rules": {
                str(dpid): int(cnt)
                for dpid, cnt in sorted(self.policy_engine_rules.items())
            },
            "vlan_automation_cookie": self.VLAN_AUTOMATION_COOKIE,
            "vlan_automation_rules": {
                str(dpid): int(cnt)
                for dpid, cnt in sorted(self.vlan_automation_rules.items())
            },
            "connected_switches": sorted(str(x) for x in self.datapaths.keys()),
            "controller_packet_ins": self.packet_in_count,
            "controller_flow_mods": self.flow_mod_count,
            "controller_mac_learns": self.mac_learn_count,
            "metrics_file": self.metrics_file,
            "events_file": self.events_file,
            "ml_action_file": self.ml_action_file,
            "manual_settings_file": self.manual_settings_file,
            "manual_settings_active": self.manual_settings_active,
            "security_policy_file": self.security_policy_file,
            "security_policy_enabled": bool(self.security_policy.get("enabled", False)),
            "security_policy": self.security_policy,
            "security_block_count": int(self.security_block_count),
            "security_flows_attempted": int(self.security_flows_attempted),
            "security_flows_blocked": int(self.security_flows_blocked),
            "security_efficacy": {
                "flows_attempted": int(self.security_flows_attempted),
                "flows_blocked": int(self.security_flows_blocked),
                "efficacy_pct": (
                    round(self.security_flows_blocked / self.security_flows_attempted * 100.0, 2)
                    if self.security_flows_attempted > 0 else None
                ),
            },
            "backup_path_packet_count": int(self.backup_path_packet_count),
            "security_last_event": self.security_last_event,
            "network_automation_file": self.network_automation_file,
            "network_automation": self.network_automation_state,
            "network_automation_summary": self._network_automation_summary(),
            "last_ml_routing_choice": self.last_ml_routing_choice,
            "last_ml_q_values": self.last_ml_q_values,
            "last_ml_state": self.last_ml_state,
            "last_ml_reward": self.last_ml_reward,
            "last_ml_epsilon": self.last_ml_epsilon,
            "last_ml_steps": self.last_ml_steps,
            "last_ml_action_index": self.last_ml_action_index,
            "last_ml_note": self.last_ml_note,
            "last_ml_action_ts": self.last_ml_action_ts,
            "dqn_integration_enabled": self.dqn_integration_enabled,
            "dqn_pending_decision": self.dqn_pending_decision,
            "dqn_last_trigger_ts": self.dqn_last_trigger_ts,
            "dqn_last_decision_ts": self.dqn_last_decision_ts,
            "dqn_last_action_name": self.dqn_last_action_name,
            "dqn_last_trigger_reason": self.dqn_last_trigger_reason,
            "dqn_decision_timeout_s": self.dqn_decision_timeout_s,
            "ddos_active": self.ddos_active or self.ctrl_flood_active,
            "ddos_attacker_ips": list(self.ddos_attacker_ips.keys()),
            "ddos_blocked_flows": int(self.ddos_blocked_flows),
            "ddos_pps_threshold": self.DDOS_PPS_THRESHOLD,
            "ddos_port_pps": {
                "%d:%d" % (dpid, port): round(float(pps), 1)
                for (dpid, port), pps in self.ddos_port_pps.items()
            },
            "ctrl_flood_active": self.ctrl_flood_active,
            "ctrl_flood_switches": list(self.ctrl_flood_switches.keys()),
            "ctrl_pkt_in_rate": {
                str(dpid): round(float(rate), 1)
                for dpid, rate in self.ctrl_pkt_in_rate.items()
            },
            "ctrl_flood_threshold": self.CTRL_FLOOD_THRESHOLD,
            "ddos_attack_type": self.ddos_attack_type,
            "ddos_detection_ts": self.ddos_detection_ts,
            "ddos_attack_start_ts": self.ddos_attack_start_ts,
            # Timetable synchronisation
            "timetable_mode":     self.timetable_mode,
            "timetable_label":    self.timetable_label,
            "timetable_icon":     self.timetable_icon,
            "timetable_active":   self.timetable_active,
            "timetable_exam_flag":self.timetable_exam_flag,
            "timetable_slot":     self.timetable_slot,
            "timetable_dqn_hint": self.timetable_dqn_hint,
            # Port-scan detection
            "portscan_blocked_ips": list(self.portscan_block_ips.keys()),
            "portscan_block_count": len(self.portscan_block_ips),
        }
        tmp = self.metrics_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, self.metrics_file)
        except Exception:
            self.logger.exception("Failed to write metrics file")

    def _port_capacity_mbps(self, dpid, port_no):
        return float(self.PORT_CAPACITY_MBPS.get(int(dpid), {}).get(int(port_no), 0.0))

    def _update_port_congestion(self, dpid, port_no, util_pct, mbps, capacity_mbps):
        key = (int(dpid), int(port_no))
        is_active = key in self.congested_ports

        if (not is_active) and util_pct >= self.port_congest_high_pct:
            self.congested_ports.add(key)
            self.logger.warning(
                "CONGESTION_DETECTED dpid=%s port=%s util=%.1f%% rate=%.2fMbps cap=%.0fMbps",
                dpid,
                port_no,
                util_pct,
                mbps,
                capacity_mbps,
            )
            self._append_event(
                "port_congestion_on",
                dpid=int(dpid),
                port=int(port_no),
                util_pct=util_pct,
                mbps=mbps,
                capacity_mbps=capacity_mbps,
                high_pct=self.port_congest_high_pct,
                low_pct=self.port_congest_low_pct,
            )
            return

        if is_active and util_pct <= self.port_congest_low_pct:
            self.congested_ports.discard(key)
            self.logger.info(
                "CONGESTION_CLEARED dpid=%s port=%s util=%.1f%% rate=%.2fMbps cap=%.0fMbps",
                dpid,
                port_no,
                util_pct,
                mbps,
                capacity_mbps,
            )
            self._append_event(
                "port_congestion_off",
                dpid=int(dpid),
                port=int(port_no),
                util_pct=util_pct,
                mbps=mbps,
                capacity_mbps=capacity_mbps,
                high_pct=self.port_congest_high_pct,
                low_pct=self.port_congest_low_pct,
            )

    def _is_congested(self, mbps=None):
        cur_mbps = self.last_core_primary_mbps if mbps is None else float(mbps)
        return (
            cur_mbps >= self.congest_high_mbps
            or len(self.congested_ports) > 0
            or self.reroute_active
        )

    def _trigger_dqn_decision(self, reason, mbps):
        if not self.dqn_integration_enabled:
            return
        if self.dqn_pending_decision:
            return
        self.dqn_pending_decision = True
        self.dqn_last_trigger_ts = time.time()
        self.dqn_last_trigger_reason = str(reason)
        self.logger.info(
            "DQN decision requested reason=%s core/server=%.2fMbps",
            reason,
            mbps,
        )
        self._append_event(
            "dqn_decision_requested",
            reason=str(reason),
            mbps=float(mbps),
            congest_high_mbps=self.congest_high_mbps,
            congest_low_mbps=self.congest_low_mbps,
            pending=True,
        )

    def _resolve_dqn_routing_choice(self, action, mbps):
        routing_choice = str(action.get("routing_choice", "")).strip().lower()
        dqn_meta = action.get("dqn", {})
        action_name = ""
        if isinstance(dqn_meta, dict):
            action_name = str(dqn_meta.get("action_name", "")).strip().lower()

        # Direct routing-choice mapping first.
        if routing_choice in {"backup_path", "prefer_backup_path", "reroute_backup"}:
            decision = True
            source = "routing_choice"
        elif routing_choice in {
            "primary_path",
            "prefer_primary_path",
            "keep_primary",
        }:
            decision = False
            source = "routing_choice"
        # Fallback to DQN action-name mapping.
        elif action_name in {"prefer_backup_path", "emergency_throttle"}:
            decision = True
            source = "action_name"
        elif action_name in {"keep_primary", "relax_thresholds"}:
            decision = False
            source = "action_name"
        elif action_name in {"tighten_thresholds"}:
            # Tightening under congestion should preserve protection.
            decision = True if self._is_congested(mbps) else self.reroute_active
            source = "action_name"
        else:
            decision = self.reroute_active
            source = "current_state"

        # Safety override: if congestion persists, do not disable reroute.
        safety_override = False
        if self._is_congested(mbps) and (decision is False):
            decision = True
            safety_override = True

        return bool(decision), action_name, routing_choice, source, safety_override

    def _apply_dqn_routing_decision(self, action, reason, mbps):
        (
            decision,
            action_name,
            routing_choice,
            source,
            safety_override,
        ) = self._resolve_dqn_routing_choice(action, mbps)

        self.dqn_last_action_name = action_name or None
        self.dqn_last_decision_ts = time.time()
        self.dqn_pending_decision = False

        if decision != self.reroute_active:
            self._set_reroute_policy(decision, mbps)
        self.logger.info(
            "DQN decision applied reason=%s action=%s routing=%s -> reroute=%s "
            "(source=%s safety_override=%s core/server=%.2fMbps)",
            reason,
            action_name or "-",
            routing_choice or "-",
            decision,
            source,
            safety_override,
            mbps,
        )
        self._append_event(
            "dqn_decision_applied",
            reason=str(reason),
            action_name=action_name,
            routing_choice=routing_choice,
            reroute_enabled=decision,
            source=source,
            safety_override=safety_override,
            mbps=float(mbps),
            congest_high_mbps=self.congest_high_mbps,
            congest_low_mbps=self.congest_low_mbps,
        )

    def _check_dqn_decision_timeout(self):
        if not self.dqn_pending_decision:
            return
        if self.dqn_last_trigger_ts <= 0:
            return
        age = time.time() - self.dqn_last_trigger_ts
        if age < self.dqn_decision_timeout_s:
            return
        self.dqn_pending_decision = False
        self.logger.warning(
            "DQN decision timeout (%.1fs) -> fallback reroute enable core/server=%.2fMbps",
            age,
            self.last_core_primary_mbps,
        )
        self._append_event(
            "dqn_decision_timeout",
            age_s=round(age, 3),
            timeout_s=self.dqn_decision_timeout_s,
            fallback="enable_reroute",
            mbps=float(self.last_core_primary_mbps),
        )
        if self._is_congested(self.last_core_primary_mbps):
            self._set_reroute_policy(True, self.last_core_primary_mbps)

    def _apply_ml_action_hook(self):
        manual_override = self._load_manual_settings_override()
        if not os.path.exists(self.ml_action_file):
            if manual_override:
                self._apply_threshold_settings(
                    manual_override,
                    event_name="manual_threshold_update",
                    log_prefix="Manual override",
                )
                self._write_metrics()
            return
        try:
            mtime = os.path.getmtime(self.ml_action_file)
            if mtime <= self._ml_action_mtime:
                if manual_override:
                    self._apply_threshold_settings(
                        manual_override,
                        event_name="manual_threshold_update",
                        log_prefix="Manual override",
                    )
                    self._write_metrics()
                return
            self._ml_action_mtime = mtime
            with open(self.ml_action_file, "r", encoding="utf-8") as f:
                action = json.load(f)
        except Exception:
            self.logger.exception("Failed reading ML action file")
            return

        dqn_meta = action.get("dqn", {})
        dqn_action_name = ""
        if isinstance(dqn_meta, dict):
            dqn_action_name = str(dqn_meta.get("action_name", "")).strip()
            state_view = dqn_meta.get("state", {})
            self.last_ml_state = state_view if isinstance(state_view, dict) else {}
            reward = dqn_meta.get("reward")
            epsilon = dqn_meta.get("epsilon")
            self.last_ml_reward = float(reward) if reward is not None else None
            self.last_ml_epsilon = float(epsilon) if epsilon is not None else None
            self.last_ml_steps = int(dqn_meta.get("steps", 0) or 0)
            action_index = dqn_meta.get("action_index")
            self.last_ml_action_index = (
                int(action_index) if action_index is not None else None
            )
        else:
            self.last_ml_state = {}
            self.last_ml_reward = None
            self.last_ml_epsilon = None
            self.last_ml_steps = 0
            self.last_ml_action_index = None
        if dqn_action_name:
            self.dqn_last_action_name = dqn_action_name

        routing_choice = action.get("routing_choice")
        if routing_choice is not None:
            self.last_ml_routing_choice = str(routing_choice)
            self.last_ml_note = str(action.get("note", "external_ml"))
            self.last_ml_action_ts = float(action.get("ts", time.time()) or time.time())
            q_values = action.get("q_values", {})
            if isinstance(q_values, dict):
                self.last_ml_q_values = q_values
            self.logger.info(
                "ML routing choice: %s",
                self.last_ml_routing_choice,
            )
            self._append_event(
                "ml_routing_choice",
                routing_choice=self.last_ml_routing_choice,
                q_values=self.last_ml_q_values,
                source=action.get("note", "external_ml"),
            )

        if not manual_override:
            self._apply_threshold_settings(
                action,
                event_name="ml_threshold_update",
                log_prefix="ML hook",
            )
        else:
            self.manual_settings_active = True

        if self.dqn_integration_enabled:
            # Stage 9 integration path: apply DQN decision during congestion windows.
            if self.dqn_pending_decision and isinstance(dqn_meta, dict):
                self._apply_dqn_routing_decision(
                    action=action,
                    reason=self.dqn_last_trigger_reason or "congestion_window",
                    mbps=self.last_core_primary_mbps,
                )
            elif self._is_congested(self.last_core_primary_mbps) and isinstance(
                dqn_meta, dict
            ):
                self._apply_dqn_routing_decision(
                    action=action,
                    reason="ongoing_congestion",
                    mbps=self.last_core_primary_mbps,
                )
        else:
            if "force_reroute" in action:
                force = bool(action["force_reroute"])
                if force != self.reroute_active:
                    self._set_reroute_policy(force, self.last_core_primary_mbps)
                self._append_event("ml_force_reroute", force=force)
            elif self.last_ml_routing_choice is not None:
                inferred = None
                choice = self.last_ml_routing_choice.lower()
                if choice in {"backup_path", "prefer_backup_path", "reroute_backup"}:
                    inferred = True
                elif choice in {"primary_path", "prefer_primary_path", "keep_primary"}:
                    inferred = False
                if inferred is not None and inferred != self.reroute_active:
                    self._set_reroute_policy(inferred, self.last_core_primary_mbps)
                    self._append_event("ml_force_reroute", force=inferred, inferred=True)
        if manual_override:
            self._apply_threshold_settings(
                manual_override,
                event_name="manual_threshold_update",
                log_prefix="Manual override",
            )
        self._write_metrics()

    def _load_manual_settings_override(self):
        if not os.path.exists(self.manual_settings_file):
            self.manual_settings_active = False
            return None
        try:
            with open(self.manual_settings_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            self.logger.exception("Failed reading manual settings file")
            self.manual_settings_active = False
            return None

        if not isinstance(payload, dict):
            self.manual_settings_active = False
            return None

        settings = {}
        for key in (
            "congest_high_mbps",
            "congest_low_mbps",
            "port_congest_high_pct",
            "port_congest_low_pct",
        ):
            if key in payload:
                settings[key] = payload[key]
        self.manual_settings_active = bool(settings)
        return settings or None

    def _apply_threshold_settings(self, payload, event_name, log_prefix):
        updated = False
        if "congest_high_mbps" in payload:
            value = float(payload["congest_high_mbps"])
            if value != self.congest_high_mbps:
                self.congest_high_mbps = value
                updated = True
        if "congest_low_mbps" in payload:
            value = float(payload["congest_low_mbps"])
            if value != self.congest_low_mbps:
                self.congest_low_mbps = value
                updated = True
        if "port_congest_high_pct" in payload:
            value = float(payload["port_congest_high_pct"])
            if value != self.port_congest_high_pct:
                self.port_congest_high_pct = value
                updated = True
        if "port_congest_low_pct" in payload:
            value = float(payload["port_congest_low_pct"])
            if value != self.port_congest_low_pct:
                self.port_congest_low_pct = value
                updated = True
        if self.congest_low_mbps >= self.congest_high_mbps:
            corrected = self.congest_high_mbps * 0.7
            if corrected != self.congest_low_mbps:
                self.congest_low_mbps = corrected
                updated = True
        if self.port_congest_low_pct >= self.port_congest_high_pct:
            corrected = self.port_congest_high_pct * 0.8
            if corrected != self.port_congest_low_pct:
                self.port_congest_low_pct = corrected
                updated = True
        if updated:
            self.logger.info(
                "%s updated thresholds: high=%.1fMbps low=%.1fMbps "
                "| port_congest high=%.1f%% low=%.1f%%",
                log_prefix,
                self.congest_high_mbps,
                self.congest_low_mbps,
                self.port_congest_high_pct,
                self.port_congest_low_pct,
            )
            self._append_event(
                event_name,
                high=self.congest_high_mbps,
                low=self.congest_low_mbps,
                port_high=self.port_congest_high_pct,
                port_low=self.port_congest_low_pct,
            )
        return updated

    def _default_security_policy(self):
        return {
            "enabled": True,
            "source": "controller_default",
            # Admin IPs (= staff) are protected from student access
            "protected_staff_ips": list(self.ZONE_ADMIN),
            # Servers that students may only access on allowed ports
            "protected_server_ips": list(self.ZONE_SERVER),
            # Zones whose traffic is subject to server-access port restrictions
            "restricted_source_zones": ["student_lab", "incubation"],
            # Explicit DROP pairs: student → admin, student → academic, student → incubation
            "block_zone_pairs": [
                {"src_zone": "student_lab", "dst_zone": "admin_zone",    "reason": "student_isolation"},
                {"src_zone": "student_lab", "dst_zone": "academic_zone", "reason": "student_isolation"},
                {"src_zone": "student_lab", "dst_zone": "incubation",    "reason": "student_isolation"},
            ],
            "allow_icmp_to_servers": True,
            # Students may reach servers only on hosted-platform ports
            "allowed_server_tcp_ports": list(self.STUDENT_ALLOWED_SERVER_TCP_PORTS) + [
                8008, 8088, 9443, 5201, 5202, 5203, 5204
            ],
            "allowed_server_udp_ports": list(self.AUTH_UDP_PORTS) + list(self.REALTIME_UDP_PORTS),
            "drop_priority": 360,
            "drop_idle_timeout_s": 120,
            "drop_hard_timeout_s": 300,
        }

    @staticmethod
    def _normalize_ip_list(values):
        out = []
        seen = set()
        for value in values if isinstance(values, list) else []:
            ip_text = str(value or "").strip()
            if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip_text):
                continue
            if ip_text in seen:
                continue
            seen.add(ip_text)
            out.append(ip_text)
        return out

    def _normalize_security_policy(self, payload):
        policy = self._default_security_policy()
        if not isinstance(payload, dict):
            return policy

        policy["enabled"] = bool(payload.get("enabled", False))
        policy["source"] = str(payload.get("source", "external")).strip() or "external"
        policy["protected_staff_ips"] = self._normalize_ip_list(
            payload.get("protected_staff_ips", list(self.STAFF_CLIENT_IPS))
        ) or list(self.STAFF_CLIENT_IPS)
        policy["protected_server_ips"] = self._normalize_ip_list(
            payload.get("protected_server_ips", list(self.SERVER_ZONE_IPS))
        ) or list(self.SERVER_ZONE_IPS)

        valid_zones = {
            "student_lab",
            "admin_zone",
            "academic_zone",
            "incubation",
            "server_zone",
            "backup_service",
            # Legacy aliases accepted from external policy files
            "student_wifi",
            "staff_lan",
            "it_lab",
            "network_lab",
        }
        restricted = []
        for zone in payload.get(
            "restricted_source_zones", policy["restricted_source_zones"]
        ):
            zone_text = str(zone or "").strip().lower()
            if zone_text in valid_zones and zone_text not in restricted:
                restricted.append(zone_text)
        policy["restricted_source_zones"] = restricted or list(
            self._default_security_policy()["restricted_source_zones"]
        )

        block_pairs = []
        seen_pairs = set()
        for item in payload.get("block_zone_pairs", []):
            if not isinstance(item, dict):
                continue
            src_zone = str(item.get("src_zone", "")).strip().lower()
            dst_zone = str(item.get("dst_zone", "")).strip().lower()
            if src_zone not in valid_zones or dst_zone not in valid_zones:
                continue
            pair = (src_zone, dst_zone)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            block_pairs.append(
                {
                    "src_zone": src_zone,
                    "dst_zone": dst_zone,
                    "reason": str(item.get("reason", "")).strip(),
                }
            )
        policy["block_zone_pairs"] = block_pairs

        default_policy = self._default_security_policy()
        for key, default_values in (
            ("allowed_server_tcp_ports", policy["allowed_server_tcp_ports"]),
            ("allowed_server_udp_ports", policy["allowed_server_udp_ports"]),
        ):
            ports = []
            seen = set()
            for raw in payload.get(key, default_values):
                try:
                    port = int(raw)
                except Exception:
                    continue
                if port <= 0 or port > 65535 or port in seen:
                    continue
                ports.append(port)
                seen.add(port)
            merged = list(default_policy.get(key, []))
            for port in ports:
                if port not in merged:
                    merged.append(port)
            policy[key] = merged or list(default_values)

        policy["allow_icmp_to_servers"] = bool(
            payload.get("allow_icmp_to_servers", True)
        )
        try:
            policy["drop_priority"] = max(
                300, min(390, int(payload.get("drop_priority", 360)))
            )
        except Exception:
            policy["drop_priority"] = 360
        try:
            policy["drop_idle_timeout_s"] = max(
                10, min(900, int(payload.get("drop_idle_timeout_s", 120)))
            )
        except Exception:
            policy["drop_idle_timeout_s"] = 120
        try:
            policy["drop_hard_timeout_s"] = max(
                int(policy["drop_idle_timeout_s"]),
                min(1800, int(payload.get("drop_hard_timeout_s", 300))),
            )
        except Exception:
            policy["drop_hard_timeout_s"] = 300
        return policy

    def _load_security_policy_hook(self):
        if not os.path.exists(self.security_policy_file):
            if self.security_policy.get("enabled", False):
                self.security_policy = self._default_security_policy()
                self._security_policy_mtime = 0.0
                self.logger.info("Security policy cleared")
                self._append_event("security_policy_cleared")
                self._write_metrics()
            return

        try:
            mtime = os.path.getmtime(self.security_policy_file)
            if mtime <= self._security_policy_mtime:
                return
            self._security_policy_mtime = mtime
            with open(self.security_policy_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            self.logger.exception("Failed reading security policy file")
            return

        normalized = self._normalize_security_policy(payload)
        self.security_policy = normalized
        self.logger.info(
            "Security policy updated: enabled=%s block_pairs=%s restricted_zones=%s",
            normalized.get("enabled", False),
            len(normalized.get("block_zone_pairs", [])),
            ",".join(normalized.get("restricted_source_zones", [])) or "-",
        )
        self._append_event(
            "security_policy_updated",
            enabled=bool(normalized.get("enabled", False)),
            block_pairs=len(normalized.get("block_zone_pairs", [])),
            restricted_zones=normalized.get("restricted_source_zones", []),
        )
        self._write_metrics()

    def _load_simulation_context_hook(self):
        if not os.path.exists(self.simulation_context_file):
            self._simulation_context_mtime = 0.0
            self.simulation_probe_ips = set()
            return
        try:
            mtime = os.path.getmtime(self.simulation_context_file)
            if mtime <= self._simulation_context_mtime:
                return
            self._simulation_context_mtime = mtime
            with open(self.simulation_context_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            self.logger.exception("Failed reading simulation context file")
            return
        sessions = payload.get("active_sessions", []) if isinstance(payload, dict) else []
        probe_ips = set()
        for session in sessions if isinstance(sessions, list) else []:
            if not isinstance(session, dict):
                continue
            if str(session.get("traffic_class", "")).strip() != "security_probe":
                continue
            ip_text = str(session.get("ip", "")).strip()
            if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip_text):
                probe_ips.add(ip_text)
        self.simulation_probe_ips = probe_ips

    def _ip_zone(self, ip_addr):
        if ip_addr in self.ZONE_STUDENT_LAB:
            return "student_lab"
        if ip_addr in self.ZONE_ADMIN:
            return "admin_zone"
        if ip_addr in self.ZONE_ACADEMIC:
            return "academic_zone"
        if ip_addr in self.ZONE_INCUBATION:
            return "incubation"
        if ip_addr == self.PRIMARY_SERVER_IP:
            return "server_zone"
        if ip_addr == self.BACKUP_SERVER_IP:
            return "backup_service"
        # Legacy aliases for backward compatibility with old policy files
        if ip_addr in self.IT_CLIENT_PORTS:
            return "student_lab"
        if ip_addr in self.NET_CLIENT_PORTS:
            return "student_lab"
        if ip_addr in self.STAFF_CLIENT_PORTS:
            return "admin_zone"
        if ip_addr in self.WIFI_CLIENT_PORTS:
            return "academic_zone"
        return "unknown"

    def _track_ctrl_pkt_in(self, datapath, dpid, in_port):
        """Real-time control-plane flood detection via per-switch packet-in rate.

        Called on every packet_in event — detects and mitigates in the same call,
        achieving sub-100 ms response (no stats-polling round-trip needed).
        """
        now = time.time()
        times = self.ctrl_pkt_in_times.setdefault(dpid, deque())
        times.append(now)
        cutoff = now - self.CTRL_FLOOD_WINDOW_S
        while times and times[0] < cutoff:
            times.popleft()
        rate = len(times) / self.CTRL_FLOOD_WINDOW_S
        self.ctrl_pkt_in_rate[dpid] = rate

        if rate >= self.CTRL_FLOOD_THRESHOLD and dpid not in self.ctrl_flood_switches:
            self._mitigate_ctrl_flood(datapath, dpid, in_port, rate)

    def _mitigate_ctrl_flood(self, datapath, dpid, in_port, rate):
        """Mitigate control-plane flood: drop all traffic on the flooding ingress port."""
        if dpid in self.ctrl_flood_switches:
            return
        self.ctrl_flood_switches[dpid] = time.time()
        self.ctrl_flood_active = True
        self.ddos_active = True
        self.ddos_attack_type = "ctrl_plane"
        self.ddos_detection_ts = time.time()
        self.ddos_blocked_flows += 1

        parser = datapath.ofproto_parser
        # Drop all traffic arriving on the flooding port (targeted, not a catch-all).
        match = parser.OFPMatch(in_port=in_port)
        self.add_flow(
            datapath,
            self.CTRL_FLOOD_DROP_PRIORITY,
            match,
            [],  # empty actions = DROP
            cookie=self.DDOS_COOKIE,
            hard_timeout=self.CTRL_FLOOD_HARD_TIMEOUT_S,
        )
        self.logger.warning(
            "CTRL_PLANE_FLOOD dpid=%s in_port=%s pkt_in_rate=%.0f/s "
            "-> port DROP rule installed (priority=%d hard_timeout=%ds)",
            dpid, in_port, rate, self.CTRL_FLOOD_DROP_PRIORITY,
            self.CTRL_FLOOD_HARD_TIMEOUT_S,
        )
        self._append_event(
            "ctrl_flood_detected",
            dpid=int(dpid),
            in_port=int(in_port),
            pkt_in_rate=round(float(rate), 1),
            threshold=self.CTRL_FLOOD_THRESHOLD,
            mitigation="port_drop_rule",
        )
        self._write_metrics()

    def _cleanup_ctrl_flood(self):
        """Remove expired ctrl-flood entries after hard-timeout."""
        now = time.time()
        expiry = self.CTRL_FLOOD_HARD_TIMEOUT_S + 2.0
        expired = [
            dpid for dpid, ts in list(self.ctrl_flood_switches.items())
            if now - ts >= expiry
        ]
        for dpid in expired:
            self.ctrl_flood_switches.pop(dpid, None)
            self.ctrl_pkt_in_times.pop(dpid, None)
            self.logger.info("CTRL_FLOOD_EXPIRED dpid=%s", dpid)
            self._append_event("ctrl_flood_expired", dpid=int(dpid))
        if expired:
            self.ctrl_flood_active = bool(self.ctrl_flood_switches)
            if not self.ctrl_flood_active and not self.ddos_active:
                self.ddos_attack_type = None
            self._write_metrics()

    def _host_ip_for_port(self, dpid, port_no):
        """Return the host IP connected to a specific switch port, if known.

        Covers the access-layer switches in the real college topology.
        DPID→switch mapping: 4=lab7, 5=lab6, 6=mechl1, 7=mechl2, 8=lab2,
        9=mechatronic, 10=incubation, 11=lab3, 12=lab4, 13=academic, 14=admin.
        """
        port_maps = [
            (4,  self.IT_CLIENT_PORTS),       # lab7
            (5,  self.IT_CLIENT_PORTS),        # lab6
            (6,  self.IT_CLIENT_PORTS),        # mechl1
            (9,  self.IT_CLIENT_PORTS),        # mechatronic
            (10, self.IT_CLIENT_PORTS),        # incubation
            (7,  self.NET_CLIENT_PORTS),       # mechl2
            (8,  self.NET_CLIENT_PORTS),       # lab2
            (11, self.NET_CLIENT_PORTS),       # lab3
            (14, self.STAFF_CLIENT_PORTS),     # admin
            (13, self.WIFI_CLIENT_PORTS),      # academic
            (12, self.WIFI_CLIENT_PORTS),      # lab4
        ]
        for sw_dpid, port_map in port_maps:
            if dpid == sw_dpid:
                for ip, p in port_map.items():
                    if p == port_no:
                        return ip
        return None

    def _check_port_ddos(self, dpid, port_no, pps, datapath):
        """Detect DDoS flood based on ingress packet rate on a host-facing port."""
        self.ddos_port_pps[(dpid, port_no)] = pps
        if pps < self.DDOS_PPS_THRESHOLD:
            return
        src_ip = self._host_ip_for_port(dpid, port_no)
        if not src_ip or src_ip in self.ddos_attacker_ips:
            return
        if src_ip in self.simulation_probe_ips:
            return
        blocked_ts = float(self.recent_security_block_src.get(src_ip, 0.0) or 0.0)
        if blocked_ts:
            if time.time() - blocked_ts < self.SECURITY_BLOCK_SUPPRESS_DDOS_S:
                return
            self.recent_security_block_src.pop(src_ip, None)
        # Install DROP rule on all connected switches so the flood is stopped everywhere.
        for dp in list(self.datapaths.values()):
            self._install_ddos_drop(dp, src_ip, pps)

    def _install_ddos_drop(self, datapath, src_ip, detected_pps=0.0):
        """Install a high-priority DROP flow rule for the attacking source IP."""
        if src_ip in self.ddos_attacker_ips:
            return
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=src_ip,
        )
        self.add_flow(
            datapath,
            self.DDOS_DROP_PRIORITY,
            match,
            [],  # empty action list = DROP
            cookie=self.DDOS_COOKIE,
            hard_timeout=self.DDOS_DROP_HARD_TIMEOUT_S,
        )
        self.ddos_attacker_ips[src_ip] = time.time()
        self.ddos_blocked_flows += 1
        self.ddos_active = True
        self.ddos_detection_ts = time.time()
        if self.ddos_attack_type is None:
            self.ddos_attack_type = "data_plane"
        self.logger.warning(
            "DDOS_DETECTED src_ip=%s port_pps=%.0f threshold=%d "
            "-> DROP rule installed (priority=%d hard_timeout=%ds)",
            src_ip, detected_pps, self.DDOS_PPS_THRESHOLD,
            self.DDOS_DROP_PRIORITY, self.DDOS_DROP_HARD_TIMEOUT_S,
        )
        self._append_event(
            "ddos_detected",
            src_ip=src_ip,
            dpid=int(datapath.id),
            detected_pps=round(float(detected_pps), 1),
            threshold_pps=self.DDOS_PPS_THRESHOLD,
        )
        self._write_metrics()

    def _cleanup_ddos_state(self):
        """Remove attacker IPs whose DROP rule hard-timeout has expired."""
        now = time.time()
        expiry = self.DDOS_DROP_HARD_TIMEOUT_S + 2.0
        expired = [
            ip for ip, ts in list(self.ddos_attacker_ips.items())
            if now - ts >= expiry
        ]
        for ip in expired:
            self.ddos_attacker_ips.pop(ip, None)
            self.logger.info("DDOS_MITIGATION_EXPIRED src_ip=%s", ip)
            self._append_event("ddos_mitigation_expired", src_ip=ip)
        if expired:
            self.ddos_active = bool(self.ddos_attacker_ips)
            if not self.ddos_active and not self.ctrl_flood_active:
                self.ddos_attack_type = None
            self._write_metrics()

    def _drop_security_flow(self, datapath, msg, match, event):
        ofproto = datapath.ofproto
        buffer_id = msg.buffer_id
        if buffer_id == ofproto.OFP_NO_BUFFER:
            buffer_id = None
        self.add_flow(
            datapath,
            int(self.security_policy.get("drop_priority", 360)),
            match,
            [],
            buffer_id=buffer_id,
            cookie=self.SECURITY_COOKIE,
            idle_timeout=int(self.security_policy.get("drop_idle_timeout_s", 120)),
            hard_timeout=int(self.security_policy.get("drop_hard_timeout_s", 300)),
        )
        self.security_block_count += 1
        self.security_flows_blocked += 1
        self.security_last_event = dict(event)
        src_ip = str(event.get("src_ip", "") or "").strip()
        if src_ip:
            self.recent_security_block_src[src_ip] = time.time()
        self.logger.warning(
            "SECURITY_BLOCK reason=%s src=%s dst=%s src_zone=%s dst_zone=%s proto=%s port=%s",
            event.get("reason"),
            event.get("src_ip"),
            event.get("dst_ip"),
            event.get("src_zone"),
            event.get("dst_zone"),
            event.get("proto"),
            event.get("dst_port"),
        )
        self._append_event("security_violation_blocked", **event)
        self._write_metrics()
        return True

    def _enforce_security_policy(self, datapath, msg, in_port, pkt, ip_pkt):
        if not ip_pkt or not bool(self.security_policy.get("enabled", False)):
            return False

        self.security_flows_attempted += 1
        src_ip = str(ip_pkt.src)
        dst_ip = str(ip_pkt.dst)
        src_zone = self._ip_zone(src_ip)
        dst_zone = self._ip_zone(dst_ip)
        parser = datapath.ofproto_parser

        for pair in self.security_policy.get("block_zone_pairs", []):
            if src_zone != pair.get("src_zone") or dst_zone != pair.get("dst_zone"):
                continue
            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=src_ip,
                ipv4_dst=dst_ip,
            )
            return self._drop_security_flow(
                datapath,
                msg,
                match,
                {
                    "dpid": int(datapath.id),
                    "in_port": int(in_port),
                    "src_ip": src_ip,
                    "dst_ip": dst_ip,
                    "src_zone": src_zone,
                    "dst_zone": dst_zone,
                    "proto": "ip",
                    "dst_port": None,
                    "reason": pair.get("reason") or "blocked_zone_pair",
                },
            )

        protected_servers = set(self.security_policy.get("protected_server_ips", []))
        restricted_zones = set(self.security_policy.get("restricted_source_zones", []))
        if src_zone not in restricted_zones or dst_ip not in protected_servers:
            return False

        if ip_pkt.proto == 1 and bool(
            self.security_policy.get("allow_icmp_to_servers", True)
        ):
            return False

        allowed_tcp = set(self.security_policy.get("allowed_server_tcp_ports", []))
        allowed_udp = set(self.security_policy.get("allowed_server_udp_ports", []))
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        if tcp_pkt is not None and int(tcp_pkt.dst_port) in allowed_tcp:
            return False
        if udp_pkt is not None and int(udp_pkt.dst_port) in allowed_udp:
            return False

        match_kwargs = {
            "in_port": in_port,
            "eth_type": ether_types.ETH_TYPE_IP,
            "ipv4_src": src_ip,
            "ipv4_dst": dst_ip,
        }
        proto_name = "ip"
        dst_port = None
        if tcp_pkt is not None:
            match_kwargs["ip_proto"] = 6
            match_kwargs["tcp_dst"] = int(tcp_pkt.dst_port)
            proto_name = "tcp"
            dst_port = int(tcp_pkt.dst_port)
        elif udp_pkt is not None:
            match_kwargs["ip_proto"] = 17
            match_kwargs["udp_dst"] = int(udp_pkt.dst_port)
            proto_name = "udp"
            dst_port = int(udp_pkt.dst_port)
        elif ip_pkt.proto is not None:
            match_kwargs["ip_proto"] = int(ip_pkt.proto)
            proto_name = str(int(ip_pkt.proto))

        return self._drop_security_flow(
            datapath,
            msg,
            parser.OFPMatch(**match_kwargs),
            {
                "dpid": int(datapath.id),
                "in_port": int(in_port),
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "src_zone": src_zone,
                "dst_zone": dst_zone,
                "proto": proto_name,
                "dst_port": dst_port,
                "reason": "unapproved_server_access",
            },
        )

    @staticmethod
    def _switch_name(dpid):
        return "s%s" % int(dpid)

    @staticmethod
    def _is_valid_mac(text):
        return bool(
            re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", str(text or "").strip().lower())
        )

    def _network_automation_summary(self):
        switches = self.network_automation_state.get("switches", {})
        vlan_count = 0
        member_count = 0
        interconnect_count = 0
        for cfg in switches.values():
            vlans = cfg.get("vlans", {})
            vlan_count += len(vlans)
            for vlan_cfg in vlans.values():
                member_count += len(vlan_cfg.get("members", []))
            interconnect_count += len(cfg.get("allow_between", []))
        return {
            "managed_switches": len(switches),
            "vlans": vlan_count,
            "members": member_count,
            "interconnects": interconnect_count,
        }

    def _normalize_network_automation(self, payload):
        result = {"switches": {}}
        if not isinstance(payload, dict):
            return result
        switches = payload.get("switches", {})
        if not isinstance(switches, dict):
            return result

        for switch_name, switch_cfg in switches.items():
            sw = str(switch_name or "").strip().lower()
            if not re.fullmatch(r"s\d+", sw):
                continue
            if not isinstance(switch_cfg, dict):
                continue

            vlans_out = {}
            raw_vlans = switch_cfg.get("vlans", {})
            if isinstance(raw_vlans, dict):
                for vlan_key, vlan_cfg in raw_vlans.items():
                    try:
                        vlan_id = int(vlan_key)
                    except Exception:
                        continue
                    if vlan_id <= 0 or vlan_id > 4094:
                        continue
                    members = []
                    seen_ports = set()
                    raw_members = (
                        vlan_cfg.get("members", [])
                        if isinstance(vlan_cfg, dict)
                        else vlan_cfg
                    )
                    if not isinstance(raw_members, list):
                        raw_members = []
                    for member in raw_members:
                        if not isinstance(member, dict):
                            continue
                        try:
                            port = int(member.get("port"))
                        except Exception:
                            continue
                        mac = str(member.get("mac", "")).strip().lower()
                        if port <= 0 or port in seen_ports or not self._is_valid_mac(mac):
                            continue
                        members.append(
                            {
                                "device": str(member.get("device", "")).strip(),
                                "label": str(member.get("label", "")).strip(),
                                "port": port,
                                "mac": mac,
                            }
                        )
                        seen_ports.add(port)
                    if members:
                        vlans_out[str(vlan_id)] = {"members": members}

            allow_between = []
            seen_pairs = set()
            raw_allow = switch_cfg.get("allow_between", [])
            if isinstance(raw_allow, list):
                for item in raw_allow:
                    if isinstance(item, dict):
                        a = item.get("src_vlan")
                        b = item.get("dst_vlan")
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        a, b = item[0], item[1]
                    else:
                        continue
                    try:
                        vlan_a = int(a)
                        vlan_b = int(b)
                    except Exception:
                        continue
                    if (
                        vlan_a <= 0
                        or vlan_a > 4094
                        or vlan_b <= 0
                        or vlan_b > 4094
                        or vlan_a == vlan_b
                    ):
                        continue
                    pair = (min(vlan_a, vlan_b), max(vlan_a, vlan_b))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    allow_between.append(
                        {"src_vlan": pair[0], "dst_vlan": pair[1]}
                    )

            if vlans_out or allow_between:
                result["switches"][sw] = {
                    "vlans": vlans_out,
                    "allow_between": allow_between,
                }
        return result

    def _apply_network_automation_hook(self):
        if not os.path.exists(self.network_automation_file):
            if self.network_automation_state.get("switches"):
                self.network_automation_state = {"switches": {}}
                self.logger.info("Network automation cleared")
                self._append_event("network_automation_cleared")
                for datapath in list(self.datapaths.values()):
                    self._install_vlan_automation_flows(datapath)
                self._write_metrics()
            return

        try:
            mtime = os.path.getmtime(self.network_automation_file)
            if mtime <= self._network_automation_mtime:
                return
            self._network_automation_mtime = mtime
            with open(self.network_automation_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            self.logger.exception("Failed reading network automation file")
            return

        normalized = self._normalize_network_automation(payload)
        if normalized == self.network_automation_state:
            return
        self.network_automation_state = normalized
        summary = self._network_automation_summary()
        self.logger.info(
            "Network automation updated: switches=%s vlans=%s members=%s interconnects=%s",
            summary["managed_switches"],
            summary["vlans"],
            summary["members"],
            summary["interconnects"],
        )
        self._append_event(
            "network_automation_updated",
            switches=summary["managed_switches"],
            vlans=summary["vlans"],
            members=summary["members"],
            interconnects=summary["interconnects"],
        )
        for datapath in list(self.datapaths.values()):
            self._install_vlan_automation_flows(datapath)
        self._write_metrics()

    def add_flow(
        self,
        datapath,
        priority,
        match,
        actions,
        buffer_id=None,
        cookie=0,
        idle_timeout=0,
        hard_timeout=0,
    ):
        self.flow_mod_count += 1
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id is not None:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                cookie=cookie,
                priority=priority,
                match=match,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                instructions=inst,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                cookie=cookie,
                priority=priority,
                match=match,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                instructions=inst,
            )
        datapath.send_msg(mod)

    def _policy_map_for_switch(self, dpid):
        # Distribution switches: install QoS policy rules that steer traffic to
        # the primary server into the correct priority queue on the core uplink.
        if dpid == self.DIST_LEFT_DPID:   # s2 — uplink to s1 is port 1
            return {"uplink": self.IT_TO_CORE_PORT, "clients": self.IT_CLIENT_PORTS}
        if dpid == self.DIST_RIGHT_DPID:  # s3 — uplink to s1 is port 1
            return {"uplink": self.NET_TO_CORE_PORT, "clients": self.NET_CLIENT_PORTS}
        # Access switches attached to dist_left (s2): uplink to s2 is port 1
        if dpid in (4, 5, 6, 9, 10):
            return {"uplink": 1, "clients": self.IT_CLIENT_PORTS}
        # Access switches attached to dist_right (s3) or directly to core: uplink port 1
        if dpid in (7, 8, 11, 12):
            return {"uplink": 1, "clients": self.NET_CLIENT_PORTS}
        if dpid == 13:  # academic — uplink port 1 to s3
            return {"uplink": 1, "clients": self.WIFI_CLIENT_PORTS}
        if dpid == 14:  # admin — uplink port 1 to s1 (highest priority)
            return {"uplink": 1, "clients": self.STAFF_CLIENT_PORTS}
        return None

    def _install_policy_engine_flows(self, datapath):
        mapping = self._policy_map_for_switch(datapath.id)
        if not mapping:
            return 0

        parser = datapath.ofproto_parser
        uplink = int(mapping["uplink"])
        client_ports = dict(mapping["clients"])
        rule_count = 0

        def push(priority, match, actions):
            nonlocal rule_count
            self.add_flow(
                datapath,
                priority,
                match,
                actions,
                cookie=self.POLICY_ENGINE_COOKIE,
            )
            rule_count += 1

        def qout(queue_id, out_port):
            return [parser.OFPActionSetQueue(queue_id), parser.OFPActionOutput(out_port)]

        # Client/source -> primary server policies (policy-based routing + priority).
        for p in self.ACADEMIC_TCP_PORTS:
            push(
                340,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=6,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                    tcp_dst=p,
                ),
                qout(self.HIGH_PRIORITY_QUEUE_ID, uplink),
            )
        for p in self.AUTH_UDP_PORTS:
            push(
                330,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=17,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                    udp_dst=p,
                ),
                qout(self.HIGH_PRIORITY_QUEUE_ID, uplink),
            )
        for p in self.REALTIME_UDP_PORTS:
            push(
                335,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=17,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                    udp_dst=p,
                ),
                qout(self.HIGH_PRIORITY_QUEUE_ID, uplink),
            )
        for p in self.BULK_TCP_PORTS:
            push(
                320,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=6,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                    tcp_dst=p,
                ),
                qout(self.LOW_PRIORITY_QUEUE_ID, uplink),
            )
        for p in self.NORMAL_TCP_PORTS:
            push(
                310,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=6,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                    tcp_dst=p,
                ),
                qout(self.MEDIUM_PRIORITY_QUEUE_ID, uplink),
            )
        push(
            250,
            parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=self.PRIMARY_SERVER_IP,
            ),
            qout(self.MEDIUM_PRIORITY_QUEUE_ID, uplink),
        )

        # Primary server -> known local clients.
        for client_ip, client_port in client_ports.items():
            for p in self.ACADEMIC_TCP_PORTS:
                push(
                    340,
                    parser.OFPMatch(
                        in_port=uplink,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=6,
                        ipv4_src=self.PRIMARY_SERVER_IP,
                        ipv4_dst=client_ip,
                        tcp_src=p,
                    ),
                    qout(self.HIGH_PRIORITY_QUEUE_ID, client_port),
                )
            for p in self.AUTH_UDP_PORTS:
                push(
                    330,
                    parser.OFPMatch(
                        in_port=uplink,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=17,
                        ipv4_src=self.PRIMARY_SERVER_IP,
                        ipv4_dst=client_ip,
                        udp_src=p,
                    ),
                    qout(self.HIGH_PRIORITY_QUEUE_ID, client_port),
                )
            for p in self.REALTIME_UDP_PORTS:
                push(
                    335,
                    parser.OFPMatch(
                        in_port=uplink,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=17,
                        ipv4_src=self.PRIMARY_SERVER_IP,
                        ipv4_dst=client_ip,
                        udp_src=p,
                    ),
                    qout(self.HIGH_PRIORITY_QUEUE_ID, client_port),
                )
            for p in self.BULK_TCP_PORTS:
                push(
                    320,
                    parser.OFPMatch(
                        in_port=uplink,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=6,
                        ipv4_src=self.PRIMARY_SERVER_IP,
                        ipv4_dst=client_ip,
                        tcp_src=p,
                    ),
                    qout(self.LOW_PRIORITY_QUEUE_ID, client_port),
                )
            for p in self.NORMAL_TCP_PORTS:
                push(
                    310,
                    parser.OFPMatch(
                        in_port=uplink,
                        eth_type=ether_types.ETH_TYPE_IP,
                        ip_proto=6,
                        ipv4_src=self.PRIMARY_SERVER_IP,
                        ipv4_dst=client_ip,
                        tcp_src=p,
                    ),
                    qout(self.MEDIUM_PRIORITY_QUEUE_ID, client_port),
                )
            push(
                250,
                parser.OFPMatch(
                    in_port=uplink,
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=self.PRIMARY_SERVER_IP,
                    ipv4_dst=client_ip,
                ),
                qout(self.MEDIUM_PRIORITY_QUEUE_ID, client_port),
            )

        self.policy_engine_rules[datapath.id] = rule_count
        self.logger.info(
            "Policy Engine installed %s rules on switch dpid=%s",
            rule_count,
            datapath.id,
        )
        self._append_event(
            "policy_engine_programmed",
            dpid=int(datapath.id),
            rule_count=rule_count,
            uplink_port=uplink,
            exam_port=self.EXAM_TCP_PORT,
            academic_ports=list(self.ACADEMIC_TCP_PORTS),
            auth_ports=list(self.AUTH_UDP_PORTS),
            collaboration_ports=list(self.REALTIME_UDP_PORTS),
            normal_ports=list(self.NORMAL_TCP_PORTS),
            bulk_ports=list(self.BULK_TCP_PORTS),
        )
        return rule_count

    def delete_cookie_flows(self, datapath, cookie):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=0,
            command=ofproto.OFPFC_DELETE,
            cookie=cookie,
            cookie_mask=0xFFFFFFFFFFFFFFFF,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            priority=0,
            match=parser.OFPMatch(),
        )
        datapath.send_msg(mod)

    def _install_vlan_automation_flows(self, datapath):
        self.delete_cookie_flows(datapath, self.VLAN_AUTOMATION_COOKIE)
        switch_cfg = self.network_automation_state.get("switches", {}).get(
            self._switch_name(datapath.id),
            {},
        )
        if not isinstance(switch_cfg, dict):
            self.vlan_automation_rules.pop(datapath.id, None)
            return 0

        parser = datapath.ofproto_parser
        known_ports = set(
            int(port_no)
            for port_no in self.PORT_CAPACITY_MBPS.get(int(datapath.id), {}).keys()
            if int(port_no) > 0
        )
        raw_vlans = switch_cfg.get("vlans", {})
        if not isinstance(raw_vlans, dict):
            self.vlan_automation_rules.pop(datapath.id, None)
            return 0

        vlan_members = {}
        managed_ports = set()
        for vlan_id_text, vlan_cfg in raw_vlans.items():
            try:
                vlan_id = int(vlan_id_text)
            except Exception:
                continue
            members = []
            for member in vlan_cfg.get("members", []):
                try:
                    port_no = int(member.get("port"))
                except Exception:
                    continue
                if known_ports and port_no not in known_ports:
                    continue
                mac = str(member.get("mac", "")).strip().lower()
                if not self._is_valid_mac(mac):
                    continue
                members.append(
                    {
                        "device": str(member.get("device", "")).strip(),
                        "label": str(member.get("label", "")).strip(),
                        "port": port_no,
                        "mac": mac,
                    }
                )
                managed_ports.add(port_no)
            if members:
                vlan_members[vlan_id] = members

        if not vlan_members:
            self.vlan_automation_rules.pop(datapath.id, None)
            return 0

        adjacency = {vlan_id: {vlan_id} for vlan_id in vlan_members}
        for item in switch_cfg.get("allow_between", []):
            try:
                vlan_a = int(item.get("src_vlan"))
                vlan_b = int(item.get("dst_vlan"))
            except Exception:
                continue
            if vlan_a not in vlan_members or vlan_b not in vlan_members:
                continue
            adjacency.setdefault(vlan_a, {vlan_a}).add(vlan_b)
            adjacency.setdefault(vlan_b, {vlan_b}).add(vlan_a)

        if not known_ports:
            known_ports = set(managed_ports)

        rule_count = 0

        def push(priority, match, actions):
            nonlocal rule_count
            self.add_flow(
                datapath,
                priority,
                match,
                actions,
                cookie=self.VLAN_AUTOMATION_COOKIE,
            )
            rule_count += 1

        # Allow intra-VLAN traffic and explicit inter-VLAN communication policies.
        for vlan_id, members in sorted(vlan_members.items()):
            for src in members:
                src_port = int(src["port"])
                allowed_targets = []
                seen_ports = set()
                for peer_vlan in sorted(adjacency.get(vlan_id, {vlan_id})):
                    for dst in vlan_members.get(peer_vlan, []):
                        dst_port = int(dst["port"])
                        if dst_port == src_port or dst_port in seen_ports:
                            continue
                        allowed_targets.append(dst)
                        seen_ports.add(dst_port)

                if allowed_targets:
                    push(
                        390,
                        parser.OFPMatch(
                            in_port=src_port,
                            eth_type=ether_types.ETH_TYPE_ARP,
                            eth_dst="ff:ff:ff:ff:ff:ff",
                        ),
                        [
                            parser.OFPActionOutput(int(dst["port"]))
                            for dst in allowed_targets
                        ],
                    )

                for dst in allowed_targets:
                    push(
                        395,
                        parser.OFPMatch(
                            in_port=src_port,
                            eth_dst=str(dst["mac"]).lower(),
                        ),
                        [parser.OFPActionOutput(int(dst["port"]))],
                    )

                # Managed access ports are closed by default unless an automation rule allows the traffic.
                push(380, parser.OFPMatch(in_port=src_port), [])

        # Block traffic from unmanaged/disallowed ports toward managed endpoints.
        for vlan_id, members in sorted(vlan_members.items()):
            allowed_sources = set()
            for peer_vlan in adjacency.get(vlan_id, {vlan_id}):
                allowed_sources.update(
                    int(member["port"]) for member in vlan_members.get(peer_vlan, [])
                )
            for member in members:
                dst_port = int(member["port"])
                for src_port in sorted(known_ports):
                    if src_port == dst_port or src_port in allowed_sources:
                        continue
                    push(
                        385,
                        parser.OFPMatch(
                            in_port=src_port,
                            eth_dst=str(member["mac"]).lower(),
                        ),
                        [],
                    )

        for src_port in sorted(known_ports):
            if src_port in managed_ports:
                continue
            push(
                384,
                parser.OFPMatch(
                    in_port=src_port,
                    eth_type=ether_types.ETH_TYPE_ARP,
                    eth_dst="ff:ff:ff:ff:ff:ff",
                ),
                [],
            )

        self.vlan_automation_rules[datapath.id] = rule_count
        self.logger.info(
            "VLAN automation installed %s rules on switch dpid=%s",
            rule_count,
            datapath.id,
        )
        self._append_event(
            "vlan_automation_programmed",
            dpid=int(datapath.id),
            rule_count=rule_count,
            vlan_count=len(vlan_members),
            managed_ports=sorted(managed_ports),
        )
        return rule_count

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
        self._write_metrics()

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss: send unknown packets to controller.
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
        ]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Installed table-miss flow on switch dpid=%s", datapath.id)

        # Static helper path for backup server MAC.
        if datapath.id == self.CORE_DPID:
            m = parser.OFPMatch(eth_dst=self.BACKUP_SERVER_MAC)
            a = [parser.OFPActionOutput(self.CORE_TO_NET_PORT)]
            self.add_flow(datapath, 50, m, a)
        elif datapath.id == self.NET_DPID:
            m = parser.OFPMatch(eth_dst=self.BACKUP_SERVER_MAC)
            a = [parser.OFPActionOutput(self.NET_TO_BACKUP_PORT)]
            self.add_flow(datapath, 50, m, a)

        # Policy Engine: context-aware priority/queue handling by traffic class.
        self._install_policy_engine_flows(datapath)
        self._install_vlan_automation_flows(datapath)

        self.logger.info("Switch connected: dpid=%s", datapath.id)
        self._append_event("switch_connected", dpid=datapath.id)
        self._write_metrics()

    # ── Timetable synchronisation hook ──────────────────────────────────────────
    _TIMETABLE_EFFECTS = {
        "exam":        {"high": 20.0, "low": 10.0, "exam": 1.0, "hint": "exam_mode"},
        "lecture":     {"high": 35.0, "low": 18.0, "exam": 0.0, "hint": "boost_lab_zone"},
        "lab":         {"high": 50.0, "low": 25.0, "exam": 0.0, "hint": "boost_lab_zone"},
        "admin":       {"high": 30.0, "low": 15.0, "exam": 0.0, "hint": "boost_staff_lan"},
        "break":       {"high": 40.0, "low": 20.0, "exam": 0.0, "hint": "normal_mode"},
        "after_hours": {"high": 60.0, "low": 30.0, "exam": 0.0, "hint": "normal_mode"},
        "peak_hour":   {"high": 25.0, "low": 12.0, "exam": 0.0, "hint": "peak_hour_mode"},
        "congestion":  {"high": 15.0, "low":  8.0, "exam": 0.0, "hint": "throttle_wifi_70pct"},
        "ddos":        {"high": 20.0, "low": 10.0, "exam": 0.0, "hint": "security_isolation_wifi"},
        "combined":    {"high": 18.0, "low":  9.0, "exam": 1.0, "hint": "emergency_staff_protection"},
        "normal":      {"high": 40.0, "low": 20.0, "exam": 0.0, "hint": "normal_mode"},
    }

    def _apply_timetable_hook(self):
        """Read /tmp/campus_timetable_state.json and adjust congestion thresholds."""
        if not os.path.exists(self.timetable_state_file):
            return
        try:
            mtime = os.path.getmtime(self.timetable_state_file)
            if mtime <= self._timetable_state_mtime:
                return
            self._timetable_state_mtime = mtime
            with open(self.timetable_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return

        mode     = str(state.get("mode", "normal"))
        label    = str(state.get("label", "Normal Operations"))
        icon     = str(state.get("icon", ""))
        is_ovr   = bool(state.get("is_override", False))
        slot     = state.get("slot") or {}
        dqn_hint = str(state.get("dqn_hint", "normal_mode"))

        effects = self._TIMETABLE_EFFECTS.get(mode, self._TIMETABLE_EFFECTS["normal"])
        old_mode = self.timetable_mode

        self.timetable_mode      = mode
        self.timetable_label     = label
        self.timetable_icon      = icon
        self.timetable_active    = is_ovr
        self.timetable_exam_flag = effects["exam"]
        self.timetable_slot      = slot
        self.timetable_dqn_hint  = dqn_hint

        if mode != old_mode:
            # Apply threshold changes
            self.congest_high_mbps = effects["high"]
            self.congest_low_mbps  = effects["low"]
            self.logger.info(
                "Timetable: %s → %s | thresholds high=%.1f low=%.1f | hint=%s",
                old_mode, mode, effects["high"], effects["low"], dqn_hint,
            )
            self._append_event(
                "timetable_mode_change",
                old_mode=old_mode,
                new_mode=mode,
                label=label,
                congest_high=effects["high"],
                congest_low=effects["low"],
                exam_flag=effects["exam"],
                is_override=is_ovr,
            )
            self._write_metrics()

    # ── Port-scan detection ─────────────────────────────────────────────────────
    def _check_port_scan(self, src_ip, dst_port, datapath):
        """Track unique dst_port contacts per src_ip; fire detection if > LIMIT in window."""
        if not src_ip:
            return
        now = time.time()

        # Check if already blocked (suppress duplicate drops)
        if src_ip in self.portscan_block_ips:
            if now < self.portscan_block_ips[src_ip]:
                return
            del self.portscan_block_ips[src_ip]

        if src_ip not in self.portscan_state:
            self.portscan_state[src_ip] = {
                "ports":    set(),
                "ts_deque": deque(),
                "alerted":  False,
                "first_ts": now,
            }
        entry = self.portscan_state[src_ip]

        # Prune old timestamps outside window
        while entry["ts_deque"] and now - entry["ts_deque"][0] > self.PORTSCAN_WINDOW_S:
            entry["ts_deque"].popleft()

        entry["ts_deque"].append(now)
        entry["ports"].add(dst_port)

        unique_in_window = len(set(
            port for port, ts in zip(
                sorted(entry["ports"]),
                list(entry["ts_deque"])
            )
        ))
        # Simpler: count ports contacted since first_ts within window
        unique_in_window = len(entry["ports"])

        if unique_in_window >= self.PORTSCAN_PORT_LIMIT and not entry["alerted"]:
            entry["alerted"] = True
            self.portscan_block_ips[src_ip] = now + self.PORTSCAN_BLOCK_S
            self._install_portscan_drop(datapath, src_ip)
            self.logger.warning(
                "Port scan detected: src=%s unique_ports=%d — DROP for %ds",
                src_ip, unique_in_window, self.PORTSCAN_BLOCK_S,
            )
            self._append_event(
                "portscan_detected",
                src_ip=src_ip,
                unique_ports=unique_in_window,
                block_duration=self.PORTSCAN_BLOCK_S,
            )
            self._write_metrics()

    def _install_portscan_drop(self, datapath, src_ip):
        """Install DROP rule for port-scan source IP."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch(
            eth_type=0x0800,  # IPv4
            ipv4_src=src_ip,
        )
        self.add_flow(
            datapath,
            self.PORTSCAN_DROP_PRIORITY,
            match,
            [],  # empty actions = drop
            cookie=self.DDOS_COOKIE,
            hard_timeout=self.PORTSCAN_HARD_TIMEOUT,
        )
        # Install on ALL switches
        for dp in list(self.datapaths.values()):
            if dp.id != datapath.id:
                m2 = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip)
                self.add_flow(
                    dp, self.PORTSCAN_DROP_PRIORITY, m2, [],
                    cookie=self.DDOS_COOKIE,
                    hard_timeout=self.PORTSCAN_HARD_TIMEOUT,
                )

    def _cleanup_portscan_state(self):
        """Remove expired port-scan tracking entries."""
        now = time.time()
        expired_ips = [
            ip for ip, until in list(self.portscan_block_ips.items())
            if now > until
        ]
        for ip in expired_ips:
            self.portscan_block_ips.pop(ip, None)
            self.portscan_state.pop(ip, None)
            self._append_event("portscan_block_expired", src_ip=ip)

    def _monitor(self):
        while True:
            self._apply_ml_action_hook()
            self._apply_timetable_hook()
            self._load_security_policy_hook()
            self._load_simulation_context_hook()
            self._apply_network_automation_hook()
            self._check_dqn_decision_timeout()
            self._cleanup_ddos_state()
            self._cleanup_ctrl_flood()
            self._cleanup_portscan_state()
            for datapath in list(self.datapaths.values()):
                parser = datapath.ofproto_parser
                ofproto = datapath.ofproto
                req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
                datapath.send_msg(req)
            hub.sleep(0.5)  # 0.5 s polling for fast data-plane detection

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        datapath = ev.msg.datapath
        dpid = datapath.id
        now = time.time()
        log_parts = []
        self._load_simulation_context_hook()

        for stat in ev.msg.body:
            if stat.port_no == datapath.ofproto.OFPP_LOCAL:
                continue
            key = (dpid, stat.port_no)
            total_bytes = stat.rx_bytes + stat.tx_bytes
            prev = self.port_samples.get(key)
            self.port_samples[key] = (total_bytes, now)

            # Track ingress packet rate for DDoS detection.
            total_rx_pkts = stat.rx_packets
            prev_pkts = self.port_samples_pkts.get(key)
            self.port_samples_pkts[key] = (total_rx_pkts, now)
            if prev_pkts:
                prev_p, prev_pts = prev_pkts
                elapsed_p = max(now - prev_pts, 1e-6)
                delta_pkts = max(total_rx_pkts - prev_p, 0)
                pps = delta_pkts / elapsed_p
                self._check_port_ddos(dpid, stat.port_no, pps, datapath)

            if not prev:
                continue

            prev_bytes, prev_ts = prev
            elapsed = max(now - prev_ts, 1e-6)
            delta = max(total_bytes - prev_bytes, 0)
            bps = (delta * 8.0) / elapsed
            mbps = bps / 1_000_000.0
            cap = self._port_capacity_mbps(dpid, stat.port_no)
            if cap > 0.0:
                util_pct = max(0.0, min(100.0, (mbps / cap) * 100.0))
            else:
                util_pct = 0.0
            self.switch_port_mbps.setdefault(dpid, {})[stat.port_no] = mbps
            self.switch_port_util_pct.setdefault(dpid, {})[stat.port_no] = util_pct
            self.switch_port_stats.setdefault(dpid, {})[stat.port_no] = {
                "mbps": mbps,
                "bps": bps,
                "util_pct": util_pct,
                "capacity_mbps": cap,
                "congested": (int(dpid), int(stat.port_no)) in self.congested_ports,
                "delta_bytes": delta,
                "elapsed_s": elapsed,
            }
            self._update_port_congestion(dpid, stat.port_no, util_pct, mbps, cap)
            self.switch_port_stats[dpid][stat.port_no]["congested"] = (
                (int(dpid), int(stat.port_no)) in self.congested_ports
            )
            log_parts.append(
                "p%s=%.2fMbps util=%.1f%% cap=%.0f"
                % (stat.port_no, mbps, util_pct, cap)
            )

            if dpid == self.CORE_DPID and stat.port_no == self.CORE_TO_PRIMARY_PORT:
                self.last_core_primary_mbps = mbps
                self._evaluate_congestion(mbps)
            if dpid == self.CORE_DPID and stat.port_no == self.CORE_TO_WIFI_PORT:
                self.last_core_wifi_mbps = mbps

        if log_parts and (now - self.last_stats_log_ts) >= self.stats_log_interval_s:
            self.last_stats_log_ts = now
            self.logger.info("PORT_STATS dpid=%s %s", dpid, " | ".join(log_parts))
        self._write_metrics()

    def _evaluate_congestion(self, mbps):
        if (not self.throughput_congested) and mbps >= self.congest_high_mbps:
            self.throughput_congested = True
            self._append_event(
                "throughput_congestion_on",
                mbps=float(mbps),
                high=self.congest_high_mbps,
                low=self.congest_low_mbps,
            )
        elif self.throughput_congested and mbps <= self.congest_low_mbps:
            self.throughput_congested = False
            self._append_event(
                "throughput_congestion_off",
                mbps=float(mbps),
                high=self.congest_high_mbps,
                low=self.congest_low_mbps,
            )

        if not self.reroute_active and mbps >= self.congest_high_mbps:
            if self.dqn_integration_enabled:
                self._trigger_dqn_decision("congestion_high", mbps)
                # Try consuming latest DQN output immediately if present.
                self._apply_ml_action_hook()
                if not self.dqn_pending_decision and not self.reroute_active:
                    self._set_reroute_policy(True, mbps)
            else:
                self._set_reroute_policy(True, mbps)
        elif self.reroute_active and mbps <= self.congest_low_mbps:
            if self.dqn_integration_enabled:
                self._trigger_dqn_decision("congestion_low", mbps)
                self._apply_ml_action_hook()
                if (not self.dqn_pending_decision) and self.reroute_active:
                    self._set_reroute_policy(False, mbps)
            else:
                self._set_reroute_policy(False, mbps)

    def _set_reroute_policy(self, enabled, mbps):
        if self.reroute_active == enabled:
            return
        self.reroute_active = enabled
        state = "ACTIVATED" if enabled else "DEACTIVATED"
        self.logger.info(
            "Adaptive policy %s (ICMP reroute + student QoS throttle) "
            "(core/server %.2f Mbps)",
            state,
            mbps,
        )
        self._append_event(
            "policy_" + state.lower(),
            mbps=mbps,
            high=self.congest_high_mbps,
            low=self.congest_low_mbps,
            student_throttle=enabled,
        )

        for datapath in self.datapaths.values():
            if enabled:
                self._install_policy_flows(datapath)
            else:
                self.delete_cookie_flows(datapath, self.POLICY_COOKIE)
        self._write_metrics()

    def _install_policy_flows(self, datapath):
        parser = datapath.ofproto_parser

        if datapath.id == self.IT_DPID:
            for src_ip in self.IT_CLIENT_IPS:
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=1,  # ICMP
                    ipv4_src=src_ip,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                )
                actions = [
                    parser.OFPActionSetField(ipv4_dst=self.BACKUP_SERVER_IP),
                    parser.OFPActionSetField(eth_dst=self.BACKUP_SERVER_MAC),
                    parser.OFPActionOutput(self.IT_TO_CORE_PORT),
                ]
                self.add_flow(
                    datapath,
                    200,
                    match,
                    actions,
                    cookie=self.POLICY_COOKIE,
                    idle_timeout=10,
                )
        elif datapath.id == self.NET_DPID:
            for dst_ip in self.IT_CLIENT_IPS:
                match = parser.OFPMatch(
                    in_port=self.NET_TO_BACKUP_PORT,
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=1,  # ICMP
                    ipv4_src=self.BACKUP_SERVER_IP,
                    ipv4_dst=dst_ip,
                )
                actions = [
                    parser.OFPActionSetField(ipv4_src=self.PRIMARY_SERVER_IP),
                    parser.OFPActionOutput(self.NET_TO_CORE_PORT),
                ]
                self.add_flow(
                    datapath,
                    200,
                    match,
                    actions,
                    cookie=self.POLICY_COOKIE,
                    idle_timeout=10,
                )
        elif datapath.id == self.WIFI_DPID:
            for ip_addr, out_port in self.WIFI_CLIENT_PORTS.items():
                # Throttle "film download" path: primary server -> student Wi-Fi host.
                match_dl = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=self.PRIMARY_SERVER_IP,
                    ipv4_dst=ip_addr,
                )
                actions_dl = [
                    parser.OFPActionSetQueue(self.THROTTLE_QUEUE_ID),
                    parser.OFPActionOutput(out_port),
                ]
                self.add_flow(
                    datapath,
                    180,
                    match_dl,
                    actions_dl,
                    cookie=self.POLICY_COOKIE,
                    idle_timeout=12,
                )

                # Throttle bulk uploads from student Wi-Fi back to core.
                match_ul = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=ip_addr,
                    ipv4_dst=self.PRIMARY_SERVER_IP,
                )
                actions_ul = [
                    parser.OFPActionSetQueue(self.THROTTLE_QUEUE_ID),
                    parser.OFPActionOutput(self.WIFI_TO_CORE_PORT),
                ]
                self.add_flow(
                    datapath,
                    180,
                    match_ul,
                    actions_ul,
                    cookie=self.POLICY_COOKIE,
                    idle_timeout=12,
                )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        self.packet_in_count += 1
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # Ignore LLDP packets so topology discovery traffic is not learned.
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})
        if src not in self.mac_to_port[dpid]:
            self.mac_learn_count += 1
        self.mac_to_port[dpid][src] = in_port

        # Control-plane flood detection: runs on every packet-in, no polling delay.
        # Detects random-MAC floods (controller attacks) within milliseconds.
        self._track_ctrl_pkt_in(datapath, dpid, in_port)

        # Port-scan detection: track unique dst_ports per src_ip in a sliding window.
        if ip_pkt is not None:
            _tcp = pkt.get_protocol(tcp.tcp)
            _udp = pkt.get_protocol(udp.udp)
            _dst_p = None
            if _tcp is not None:
                _dst_p = int(_tcp.dst_port)
            elif _udp is not None:
                _dst_p = int(_udp.dst_port)
            if _dst_p is not None:
                self._check_port_scan(ip_pkt.src, _dst_p, datapath)

        if self._enforce_security_policy(datapath, msg, in_port, pkt, ip_pkt):
            return

        # Fast-path for adaptive policy until stats-triggered flows are installed.
        if (
            self.reroute_active
            and dpid == self.IT_DPID
            and ip_pkt
            and ip_pkt.proto == 1
            and ip_pkt.src in self.IT_CLIENT_IPS
            and ip_pkt.dst == self.PRIMARY_SERVER_IP
        ):
            self.backup_path_packet_count += 1
            actions = [
                parser.OFPActionSetField(ipv4_dst=self.BACKUP_SERVER_IP),
                parser.OFPActionSetField(eth_dst=self.BACKUP_SERVER_MAC),
                parser.OFPActionOutput(self.IT_TO_CORE_PORT),
            ]
            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=1,
                ipv4_src=ip_pkt.src,
                ipv4_dst=self.PRIMARY_SERVER_IP,
            )
            self.add_flow(
                datapath,
                210,
                match,
                actions,
                cookie=self.POLICY_COOKIE,
                idle_timeout=10,
            )
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None,
            )
            datapath.send_msg(out)
            return

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD
            self.logger.info(
                "PACKET_IN table-miss dpid=%s src=%s dst=%s in_port=%s -> FLOOD",
                dpid,
                src,
                dst,
                in_port,
            )

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.logger.info(
                "FLOW_MOD install dpid=%s match(in_port=%s, eth_dst=%s) out_port=%s",
                dpid,
                in_port,
                dst,
                out_port,
            )
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            self.add_flow(datapath, 1, match, actions)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)
