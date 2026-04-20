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
from ryu.ofproto import ofproto_v1_3


class CampusController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    POLICY_COOKIE = 0xCAFE0001
    POLICY_ENGINE_COOKIE = 0xCAFE1001
    VLAN_AUTOMATION_COOKIE = 0xCAFE3001

    # Service IPs for adaptive reroute policy.
    PRIMARY_SERVER_IP = "10.0.0.100"
    BACKUP_SERVER_IP = "10.0.0.101"
    BACKUP_SERVER_MAC = "00:00:00:00:00:65"
    IT_CLIENT_IPS = ("10.0.0.11", "10.0.0.12")

    # DPID/port constants for campus_topology.py link order.
    CORE_DPID = 1
    IT_DPID = 2
    NET_DPID = 3
    WIFI_DPID = 5
    CORE_TO_NET_PORT = 2        # s1 -> s3
    CORE_TO_WIFI_PORT = 4       # s1 -> s5
    CORE_TO_PRIMARY_PORT = 5    # s1 -> h_server
    IT_TO_CORE_PORT = 1         # s2 -> s1
    NET_TO_CORE_PORT = 1        # s3 -> s1
    NET_TO_BACKUP_PORT = 4      # s3 -> h_server_b
    WIFI_TO_CORE_PORT = 1       # s5 -> s1
    IT_CLIENT_PORTS = {
        "10.0.0.11": 2,  # h_it1
        "10.0.0.12": 3,  # h_it2
    }
    NET_CLIENT_PORTS = {
        "10.0.0.21": 2,  # h_net1
        "10.0.0.22": 3,  # h_net2
    }
    STAFF_CLIENT_PORTS = {
        "10.0.0.31": 2,  # h_staff1
        "10.0.0.32": 3,  # h_staff2
    }
    WIFI_CLIENT_PORTS = {
        "10.0.0.41": 2,  # h_wifi1
        "10.0.0.42": 3,  # h_wifi2
    }
    EXAM_TCP_PORT = 8443
    AUTH_UDP_PORTS = (67, 68, 1812, 1813)
    NORMAL_TCP_PORTS = (80, 443)
    BULK_TCP_PORTS = (5201, 8080, 6881)
    HIGH_PRIORITY_QUEUE_ID = 0
    MEDIUM_PRIORITY_QUEUE_ID = 1
    LOW_PRIORITY_QUEUE_ID = 2
    WIFI_CLIENT_IPS = tuple(WIFI_CLIENT_PORTS.keys())
    THROTTLE_QUEUE_ID = LOW_PRIORITY_QUEUE_ID
    # Link capacities (Mbps), aligned with campus_topology.py
    PORT_CAPACITY_MBPS = {
        1: {1: 1000.0, 2: 1000.0, 3: 1000.0, 4: 1000.0, 5: 1000.0},
        2: {1: 1000.0, 2: 100.0, 3: 100.0},
        3: {1: 1000.0, 2: 100.0, 3: 100.0, 4: 1000.0},
        4: {1: 1000.0, 2: 100.0, 3: 100.0},
        5: {1: 1000.0, 2: 50.0, 3: 50.0},
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
            os.getenv("CAMPUS_CONGEST_HIGH_MBPS", "120")
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
        self.network_automation_file = os.getenv(
            "CAMPUS_NETWORK_AUTOMATION_FILE", "/tmp/campus_network_automation.json"
        )
        self.stats_log_interval_s = float(os.getenv("CAMPUS_STATS_LOG_INTERVAL_S", "2"))
        self.dqn_decision_timeout_s = float(
            os.getenv("CAMPUS_DQN_DECISION_TIMEOUT_S", "6")
        )
        self.last_stats_log_ts = 0.0
        self._ml_action_mtime = 0.0
        self._network_automation_mtime = 0.0
        self.manual_settings_active = False
        self.network_automation_state = {"switches": {}}

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
                    "description": "Exam platform/app traffic",
                    "queue": self.HIGH_PRIORITY_QUEUE_ID,
                    "match": "tcp dst %s to %s"
                    % (self.EXAM_TCP_PORT, self.PRIMARY_SERVER_IP),
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
                "normal_browsing": {
                    "description": "Normal web/application browsing",
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
        if dpid == self.IT_DPID:
            return {"uplink": self.IT_TO_CORE_PORT, "clients": self.IT_CLIENT_PORTS}
        if dpid == self.NET_DPID:
            return {"uplink": self.NET_TO_CORE_PORT, "clients": self.NET_CLIENT_PORTS}
        if dpid == 4:
            return {"uplink": 1, "clients": self.STAFF_CLIENT_PORTS}
        if dpid == self.WIFI_DPID:
            return {"uplink": self.WIFI_TO_CORE_PORT, "clients": self.WIFI_CLIENT_PORTS}
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
        push(
            340,
            parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=6,
                ipv4_dst=self.PRIMARY_SERVER_IP,
                tcp_dst=self.EXAM_TCP_PORT,
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
            push(
                340,
                parser.OFPMatch(
                    in_port=uplink,
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=6,
                    ipv4_src=self.PRIMARY_SERVER_IP,
                    ipv4_dst=client_ip,
                    tcp_src=self.EXAM_TCP_PORT,
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
            auth_ports=list(self.AUTH_UDP_PORTS),
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

    def _monitor(self):
        while True:
            self._apply_ml_action_hook()
            self._apply_network_automation_hook()
            self._check_dqn_decision_timeout()
            for datapath in list(self.datapaths.values()):
                parser = datapath.ofproto_parser
                ofproto = datapath.ofproto
                req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
                datapath.send_msg(req)
            hub.sleep(2)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        datapath = ev.msg.datapath
        dpid = datapath.id
        now = time.time()
        log_parts = []

        for stat in ev.msg.body:
            if stat.port_no == datapath.ofproto.OFPP_LOCAL:
                continue
            key = (dpid, stat.port_no)
            total_bytes = stat.rx_bytes + stat.tx_bytes
            prev = self.port_samples.get(key)
            self.port_samples[key] = (total_bytes, now)
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

        # Fast-path for adaptive policy until stats-triggered flows are installed.
        if (
            self.reroute_active
            and dpid == self.IT_DPID
            and ip_pkt
            and ip_pkt.proto == 1
            and ip_pkt.src in self.IT_CLIENT_IPS
            and ip_pkt.dst == self.PRIMARY_SERVER_IP
        ):
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
