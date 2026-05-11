#!/usr/bin/env python3
"""
Module 4: Security Module — Tumba College SDN

- Monitors all flow statistics for attack signatures
- Detects DDoS when: single host sends > 100 Mbps to one destination
- Detects port scan when: > 50 connection attempts to different ports
  from same source within 10 seconds
- Detects unauthorized cross-zone access
- On detection: push DROP rule, log event, send alert, auto-unblock after 300s
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
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types
from ryu.ofproto import ofproto_v1_3


SECURITY_COOKIE = 0xCAFE5001
DDOS_COOKIE = 0xCAFE7001

# Zone subnet ranges
ZONE_SUBNETS = {
    'staff_lan':    ('10.10.0.', 4),
    'server_zone':  ('10.20.0.', 5),
    'it_lab':       ('10.30.0.', 6),
    'student_wifi': ('10.40.0.', 7),
}


class SecurityModule(app_manager.RyuApp):
    """Zero-Trust security enforcement with attack detection."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # DDoS detection parameters
    DDOS_MBPS_THRESHOLD = 100     # > 100 Mbps from single host
    DDOS_PPS_THRESHOLD = 500      # > 500 packets/sec
    DDOS_DROP_PRIORITY = 400
    DDOS_BLOCK_DURATION_S = 300   # 5 minutes auto-unblock

    # Port scan detection
    PORTSCAN_PORT_LIMIT = 50      # unique dst ports within window
    PORTSCAN_WINDOW_S = 10.0
    PORTSCAN_BLOCK_DURATION_S = 300
    PORTSCAN_DROP_PRIORITY = 390

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}

        # DDoS state
        self.ddos_active = False
        self.ddos_blocked_ips = {}     # {src_ip: block_ts}
        self.ddos_blocked_flows = 0
        self.ddos_attack_type = None
        self.ddos_detection_ts = 0.0

        # Port scan state
        self.portscan_state = {}       # {src_ip: {'ports': set, 'ts_deque': deque}}
        self.portscan_blocked = {}     # {src_ip: block_ts}

        # Security event log
        self.security_events = []
        self.security_flows_attempted = 0
        self.security_flows_blocked = 0

        # Control plane flood detection
        self.ctrl_pkt_in_times = defaultdict(lambda: deque(maxlen=500))
        self.ctrl_pkt_in_rate = {}
        self.CTRL_FLOOD_WINDOW_S = 0.5
        self.CTRL_FLOOD_THRESHOLD = 150

        self.db_path = os.environ.get('CAMPUS_TIMETABLE_DB', '/tmp/campus_timetable.db')
        self.events_file = os.environ.get('CAMPUS_SECURITY_EVENTS_FILE', '/tmp/campus_security_events.jsonl')

        self._cleanup_thread = hub.spawn(self._cleanup_loop)
        self.logger.info("SecurityModule initialized")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, [MAIN_DISPATCHER])
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Inspect packet-in events for security threats."""
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)

        # Track control-plane flood
        self._track_ctrl_flood(dp, dpid, in_port)

        # Parse packet
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth:
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            return

        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        self.security_flows_attempted += 1

        # Check port scan
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        if tcp_pkt:
            self._check_port_scan(dp, src_ip, dst_ip, tcp_pkt.dst_port, in_port)

        # Check unauthorized zone access
        src_zone = self._ip_to_zone(src_ip)
        dst_zone = self._ip_to_zone(dst_ip)
        if self._is_blocked_access(src_zone, dst_zone):
            self._block_flow(dp, src_ip, dst_ip, in_port,
                           reason='unauthorized_cross_zone',
                           attack_type='zero_trust_violation')

    def _track_ctrl_flood(self, dp, dpid, in_port):
        """Detect control-plane flooding via packet-in rate."""
        now = time.time()
        times = self.ctrl_pkt_in_times[dpid]
        times.append(now)
        cutoff = now - self.CTRL_FLOOD_WINDOW_S
        while times and times[0] < cutoff:
            times.popleft()
        rate = len(times) / self.CTRL_FLOOD_WINDOW_S
        self.ctrl_pkt_in_rate[dpid] = rate

        if rate >= self.CTRL_FLOOD_THRESHOLD:
            self._mitigate_ctrl_flood(dp, dpid, in_port, rate)

    def _mitigate_ctrl_flood(self, dp, dpid, in_port, rate):
        """Install DROP rule for control-plane flood."""
        parser = dp.ofproto_parser
        match = parser.OFPMatch(in_port=in_port)
        self._install_drop(dp, match, DDOS_COOKIE, self.DDOS_DROP_PRIORITY + 1,
                          hard_timeout=30)
        self.ddos_active = True
        self.ddos_attack_type = 'ctrl_plane'
        self.ddos_detection_ts = time.time()
        self._log_security_event(
            'ctrl_plane_flood', zone=self._dpid_to_zone(dpid),
            src_ip='', attack_type='ctrl_plane_flood',
            action_taken='port_drop_rule',
            response_ms=0,
            details=f'pkt_in_rate={rate:.0f}/s on dpid={dpid} port={in_port}',
        )

    def _check_port_scan(self, dp, src_ip, dst_ip, dst_port, in_port):
        """Detect port scanning behavior."""
        now = time.time()
        if src_ip not in self.portscan_state:
            self.portscan_state[src_ip] = {
                'ports': set(),
                'ts_deque': deque(),
                'alerted': False,
            }

        state = self.portscan_state[src_ip]
        state['ts_deque'].append((dst_port, now))
        state['ports'].add(dst_port)

        # Clean old entries
        cutoff = now - self.PORTSCAN_WINDOW_S
        while state['ts_deque'] and state['ts_deque'][0][1] < cutoff:
            old_port, _ = state['ts_deque'].popleft()
            # Recount ports in window
            state['ports'] = {p for p, t in state['ts_deque']}

        if len(state['ports']) >= self.PORTSCAN_PORT_LIMIT and not state['alerted']:
            state['alerted'] = True
            self._block_flow(dp, src_ip, dst_ip, in_port,
                           reason='port_scan_detected',
                           attack_type='port_scan',
                           block_duration=self.PORTSCAN_BLOCK_DURATION_S)
            self.portscan_blocked[src_ip] = now
            self.logger.warning(
                "PORT_SCAN_DETECTED src=%s unique_ports=%d",
                src_ip, len(state['ports'])
            )

    def check_ddos(self, dpid, port_no, pps, mbps):
        """Called by traffic monitor to check DDoS based on stats."""
        if pps >= self.DDOS_PPS_THRESHOLD or mbps >= self.DDOS_MBPS_THRESHOLD:
            src_ip = self._host_ip_for_port(dpid, port_no)
            if src_ip and src_ip not in self.ddos_blocked_ips:
                dp = self.datapaths.get(dpid)
                if dp:
                    self._block_ddos(dp, src_ip, pps, mbps)

    def _block_ddos(self, dp, src_ip, pps, mbps):
        """Block DDoS attacker."""
        parser = dp.ofproto_parser
        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=src_ip,
        )
        # Install on all switches
        for sw_dp in self.datapaths.values():
            self._install_drop(sw_dp, match, DDOS_COOKIE,
                             self.DDOS_DROP_PRIORITY,
                             hard_timeout=self.DDOS_BLOCK_DURATION_S)

        self.ddos_blocked_ips[src_ip] = time.time()
        self.ddos_blocked_flows += 1
        self.ddos_active = True
        self.ddos_attack_type = 'data_plane'
        self.ddos_detection_ts = time.time()

        self.logger.warning(
            "DDOS_BLOCKED src=%s pps=%.0f mbps=%.1f",
            src_ip, pps, mbps,
        )
        self._log_security_event(
            'ddos_detected', zone=self._ip_to_zone(src_ip),
            src_ip=src_ip, attack_type='ddos_flood',
            action_taken='drop_rule_all_switches',
            response_ms=0,
            details=f'pps={pps:.0f} mbps={mbps:.1f}',
        )

    def _block_flow(self, dp, src_ip, dst_ip, in_port, reason='blocked',
                    attack_type='unknown', block_duration=300):
        """Block a specific flow."""
        parser = dp.ofproto_parser
        match = parser.OFPMatch(
            in_port=in_port,
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=src_ip, ipv4_dst=dst_ip,
        )
        self._install_drop(dp, match, SECURITY_COOKIE,
                          self.PORTSCAN_DROP_PRIORITY,
                          hard_timeout=block_duration)
        self.security_flows_blocked += 1

        self._log_security_event(
            reason, zone=self._ip_to_zone(src_ip),
            src_ip=src_ip, dst_ip=dst_ip,
            attack_type=attack_type,
            action_taken='drop_rule',
            response_ms=0,
            details=f'dpid={dp.id} in_port={in_port}',
        )

    def _install_drop(self, dp, match, cookie, priority, hard_timeout=300):
        """Install a DROP flow rule."""
        parser = dp.ofproto_parser
        ofproto = dp.ofproto
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [])]
        mod = parser.OFPFlowMod(
            datapath=dp, priority=priority,
            match=match, instructions=inst,
            cookie=cookie, hard_timeout=hard_timeout,
        )
        dp.send_msg(mod)

    def _is_blocked_access(self, src_zone, dst_zone):
        """Check if cross-zone access is blocked by zero-trust policy."""
        blocked_pairs = [
            ('student_wifi', 'staff_lan'),  # Students cannot access staff
        ]
        return (src_zone, dst_zone) in blocked_pairs

    def _cleanup_loop(self):
        """Periodic cleanup of expired blocks."""
        while True:
            hub.sleep(10)
            now = time.time()

            # Clean expired DDoS blocks
            expired = [
                ip for ip, ts in list(self.ddos_blocked_ips.items())
                if now - ts >= self.DDOS_BLOCK_DURATION_S + 5
            ]
            for ip in expired:
                self.ddos_blocked_ips.pop(ip, None)
                self.logger.info("DDOS_BLOCK_EXPIRED src=%s", ip)
                self._log_security_event(
                    'ddos_block_expired', src_ip=ip,
                    action_taken='block_removed',
                )

            if expired:
                self.ddos_active = bool(self.ddos_blocked_ips)
                if not self.ddos_active:
                    self.ddos_attack_type = None

            # Clean expired port scan blocks
            ps_expired = [
                ip for ip, ts in list(self.portscan_blocked.items())
                if now - ts >= self.PORTSCAN_BLOCK_DURATION_S + 5
            ]
            for ip in ps_expired:
                self.portscan_blocked.pop(ip, None)
                self.portscan_state.pop(ip, None)
                self.logger.info("PORTSCAN_BLOCK_EXPIRED src=%s", ip)

    def _log_security_event(self, event_type, zone='', src_ip='', dst_ip='',
                           src_mac='', attack_type='', action_taken='',
                           response_ms=0, details=''):
        """Log security event to database and file."""
        event = {
            'ts': time.time(),
            'event_type': event_type,
            'zone': zone,
            'src_ip': src_ip,
            'dst_ip': dst_ip,
            'src_mac': src_mac,
            'attack_type': attack_type,
            'action_taken': action_taken,
            'response_ms': response_ms,
            'details': details,
        }
        self.security_events.append(event)
        self.security_events = self.security_events[-500:]

        try:
            with open(self.events_file, 'a') as f:
                f.write(json.dumps(event) + '\n')
        except Exception:
            pass

        # Also log to SQLite if available
        try:
            import sqlite3
            con = sqlite3.connect(self.db_path)
            con.execute(
                "INSERT INTO security_events "
                "(ts,event_type,zone,src_ip,src_mac,dst_ip,attack_type,action_taken,response_ms,details) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), event_type, zone, src_ip, src_mac,
                 dst_ip, attack_type, action_taken, response_ms, details),
            )
            con.commit()
            con.close()
        except Exception:
            pass

    @staticmethod
    def _ip_to_zone(ip):
        """Map IP address to zone name."""
        if not ip:
            return 'unknown'
        for zone, (prefix, _) in ZONE_SUBNETS.items():
            if ip.startswith(prefix):
                return zone
        return 'unknown'

    @staticmethod
    def _dpid_to_zone(dpid):
        for zone, (_, zone_dpid) in ZONE_SUBNETS.items():
            if dpid == zone_dpid:
                return zone
        return 'unknown'

    @staticmethod
    def _host_ip_for_port(dpid, port_no):
        """Map (dpid, port) to host IP."""
        zone_hosts = {
            4: {2: '10.10.0.1', 3: '10.10.0.2', 4: '10.10.0.3',
                5: '10.10.0.4', 6: '10.10.0.5', 7: '10.10.0.6'},
            5: {2: '10.20.0.1', 3: '10.20.0.2', 4: '10.20.0.3', 5: '10.20.0.4'},
            6: {2: '10.30.0.1', 3: '10.30.0.2', 4: '10.30.0.3', 5: '10.30.0.4'},
            7: {2: '10.40.0.1', 3: '10.40.0.2', 4: '10.40.0.3', 5: '10.40.0.4',
                6: '10.40.0.5', 7: '10.40.0.6', 8: '10.40.0.7', 9: '10.40.0.8',
                10: '10.40.0.9', 11: '10.40.0.10'},
        }
        return zone_hosts.get(dpid, {}).get(port_no)

    def get_security_summary(self):
        """Get security summary for dashboard."""
        return {
            'ts': time.time(),
            'ddos_active': self.ddos_active,
            'ddos_attack_type': self.ddos_attack_type,
            'ddos_blocked_ips': list(self.ddos_blocked_ips.keys()),
            'ddos_blocked_flows': self.ddos_blocked_flows,
            'portscan_blocked': list(self.portscan_blocked.keys()),
            'security_flows_attempted': self.security_flows_attempted,
            'security_flows_blocked': self.security_flows_blocked,
            'recent_events': self.security_events[-20:],
        }
