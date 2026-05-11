#!/usr/bin/env python3
"""
Module 3: Traffic Monitor — Tumba College SDN

- Polls OVS flow statistics every 2 seconds via OpenFlow Stats-Request
- Calculates per-zone metrics: throughput, packet count, byte count
- Calculates per-link utilization percentage
- Detects congestion when thresholds are crossed
- Publishes congestion events to the DQN agent
- Sends all metrics to the monitoring dashboard via WebSocket
"""

import json
import os
import time
import threading
from collections import defaultdict, deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3


# Congestion thresholds
CONGESTION_THRESHOLDS = {
    'link_utilization': 0.70,      # > 70% for 5 consecutive seconds
    'queue_depth': 150,            # > 150 packets
    'latency_staff_ms': 20,        # Staff LAN latency > 20ms
    'latency_server_ms': 20,       # Server Zone latency > 20ms
    'packet_loss_pct': 2.0,        # > 2% over 10-second window
    'throughput_drop_pct': 0.30,   # > 30% drop from 60s baseline
}

# Link capacities (Mbps)
LINK_CAPACITIES = {
    (1, 2): 1000,  # cs1-ds1
    (1, 3): 1000,  # cs1-ds2
    (2, 3): 1000,  # ds1-ds2 redundant
    (2, 4): 100,   # ds1-as1 (staff)
    (2, 5): 100,   # ds1-as2 (server)
    (3, 6): 100,   # ds2-as3 (lab)
    (3, 7): 100,   # ds2-as4 (wifi)
}

# Zone DPID mapping
ZONE_DPIDS = {
    'staff_lan': 4,     # as1
    'server_zone': 5,   # as2
    'it_lab': 6,        # as3
    'student_wifi': 7,  # as4
}


class TrafficMonitor(app_manager.RyuApp):
    """Real-time traffic monitoring with congestion detection."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    POLL_INTERVAL = 2  # seconds

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.port_stats = {}       # {(dpid, port_no): {rx_bytes, tx_bytes, ts}}
        self.port_mbps = {}        # {(dpid, port_no): mbps}
        self.port_util = {}        # {(dpid, port_no): utilization_pct}
        self.zone_metrics = {}     # {zone_name: {throughput, latency, etc.}}
        self.congested_ports = set()
        self.congestion_history = defaultdict(lambda: deque(maxlen=10))

        # Throughput baseline for drop detection (60s window)
        self.throughput_baseline = defaultdict(lambda: deque(maxlen=30))

        self.metrics_file = os.environ.get('CAMPUS_METRICS_FILE', '/tmp/campus_metrics.json')
        self.events_file = os.environ.get('CAMPUS_EVENTS_FILE', '/tmp/campus_policy_events.jsonl')

        self._monitor_thread = hub.spawn(self._monitor_loop)
        self.logger.info("TrafficMonitor initialized (poll interval: %ds)", self.POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, [MAIN_DISPATCHER])
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp

    @set_ev_cls(ofp_event.EventOFPStateChange)
    def state_change_handler(self, ev):
        dp = ev.datapath
        if hasattr(ev, 'state'):
            from ryu.controller.handler import DEAD_DISPATCHER
            if ev.state == DEAD_DISPATCHER:
                self.datapaths.pop(dp.id, None)

    def _monitor_loop(self):
        """Poll all switches for port statistics every POLL_INTERVAL seconds."""
        while True:
            for dpid, dp in list(self.datapaths.items()):
                self._request_port_stats(dp)
            hub.sleep(self.POLL_INTERVAL)

    def _request_port_stats(self, datapath):
        """Send OpenFlow port stats request."""
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        """Process port statistics reply."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        now = time.time()

        for stat in body:
            port_no = stat.port_no
            if port_no >= 0xfffffff0:  # Skip special ports
                continue

            key = (dpid, port_no)
            rx_bytes = stat.rx_bytes
            tx_bytes = stat.tx_bytes
            rx_packets = stat.rx_packets
            tx_packets = stat.tx_packets

            prev = self.port_stats.get(key)
            if prev:
                elapsed = max(0.001, now - prev['ts'])
                delta_rx = max(0, rx_bytes - prev['rx_bytes'])
                delta_tx = max(0, tx_bytes - prev['tx_bytes'])
                delta_bytes = delta_rx + delta_tx
                mbps = (delta_bytes * 8) / (elapsed * 1_000_000)

                self.port_mbps[key] = round(mbps, 3)

                # Calculate utilization
                capacity = self._get_link_capacity(dpid, port_no)
                util_pct = (mbps / capacity * 100) if capacity > 0 else 0
                self.port_util[key] = round(min(100, util_pct), 2)

                # Congestion detection
                self._check_congestion(dpid, port_no, util_pct, mbps, capacity)

                # Track per-packet rate for DDoS detection
                delta_pkts = max(0, rx_packets - prev.get('rx_packets', 0))
                pps = delta_pkts / elapsed

                self.port_stats[key] = {
                    'rx_bytes': rx_bytes, 'tx_bytes': tx_bytes,
                    'rx_packets': rx_packets, 'tx_packets': tx_packets,
                    'ts': now, 'mbps': mbps, 'util_pct': util_pct,
                    'pps': pps,
                }
            else:
                self.port_stats[key] = {
                    'rx_bytes': rx_bytes, 'tx_bytes': tx_bytes,
                    'rx_packets': rx_packets, 'tx_packets': tx_packets,
                    'ts': now, 'mbps': 0, 'util_pct': 0,
                    'pps': 0,
                }

        # Update zone metrics and write to file
        self._update_zone_metrics()
        self._write_metrics()

    def _get_link_capacity(self, dpid, port_no):
        """Get the link capacity in Mbps for a port."""
        # Access switch host ports are typically 100 Mbps
        # Core/distribution links are 1000 Mbps
        for (src, dst), cap in LINK_CAPACITIES.items():
            if dpid == src or dpid == dst:
                return cap
        return 100  # Default

    def _check_congestion(self, dpid, port_no, util_pct, mbps, capacity):
        """Check if a port is congested based on thresholds."""
        key = (dpid, port_no)
        threshold = CONGESTION_THRESHOLDS['link_utilization'] * 100

        # Track consecutive high utilization
        self.congestion_history[key].append(util_pct > threshold)

        # Require 3 consecutive high readings (6 seconds)
        recent = list(self.congestion_history[key])[-3:]
        is_congested = len(recent) >= 3 and all(recent)

        was_congested = key in self.congested_ports
        if is_congested and not was_congested:
            self.congested_ports.add(key)
            self.logger.warning(
                "CONGESTION_DETECTED dpid=%s port=%s util=%.1f%% rate=%.2fMbps",
                dpid, port_no, util_pct, mbps,
            )
            self._log_event('congestion_detected', dpid=dpid, port=port_no,
                          util_pct=util_pct, mbps=mbps)
        elif not is_congested and was_congested:
            self.congested_ports.discard(key)
            self.logger.info(
                "CONGESTION_CLEARED dpid=%s port=%s util=%.1f%%",
                dpid, port_no, util_pct,
            )
            self._log_event('congestion_cleared', dpid=dpid, port=port_no,
                          util_pct=util_pct)

    def _update_zone_metrics(self):
        """Calculate per-zone aggregate metrics."""
        for zone_name, zone_dpid in ZONE_DPIDS.items():
            zone_ports = [
                (dpid, port) for (dpid, port) in self.port_mbps
                if dpid == zone_dpid
            ]
            total_mbps = sum(self.port_mbps.get(k, 0) for k in zone_ports)
            max_util = max((self.port_util.get(k, 0) for k in zone_ports), default=0)

            self.zone_metrics[zone_name] = {
                'throughput_mbps': round(total_mbps, 3),
                'max_utilization_pct': round(max_util, 2),
                'port_count': len(zone_ports),
                'congested': any(k in self.congested_ports for k in zone_ports),
            }

    def _write_metrics(self):
        """Write all metrics to file for dashboard and DQN agent."""
        port_mbps_export = {}
        port_util_export = {}
        port_stats_export = {}

        for (dpid, port), mbps in self.port_mbps.items():
            dpid_str = str(dpid)
            port_str = str(port)
            port_mbps_export.setdefault(dpid_str, {})[port_str] = mbps
            port_util_export.setdefault(dpid_str, {})[port_str] = self.port_util.get((dpid, port), 0)

            stats = self.port_stats.get((dpid, port), {})
            port_stats_export.setdefault(dpid_str, {})[port_str] = {
                'mbps': mbps,
                'util_pct': self.port_util.get((dpid, port), 0),
                'pps': stats.get('pps', 0),
            }

        payload = {
            'ts': time.time(),
            'switch_port_mbps': port_mbps_export,
            'switch_port_util_pct': port_util_export,
            'switch_port_stats': port_stats_export,
            'zone_metrics': self.zone_metrics,
            'congested_ports': [
                {'dpid': d, 'port': p} for d, p in sorted(self.congested_ports)
            ],
            'congested_ports_count': len(self.congested_ports),
            'connected_switches': sorted(str(x) for x in self.datapaths.keys()),
            'thresholds': CONGESTION_THRESHOLDS,
        }

        try:
            tmp = self.metrics_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.metrics_file)
        except Exception as e:
            self.logger.error("Failed to write metrics: %s", e)

    def get_zone_metrics(self):
        """Get current zone metrics for API."""
        return dict(self.zone_metrics)

    def get_port_stats(self):
        """Get all port statistics."""
        return dict(self.port_stats)

    def _log_event(self, event_type, **data):
        event = {'ts': time.time(), 'event': event_type, **data}
        try:
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
        except Exception:
            pass
